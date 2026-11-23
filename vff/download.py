# -*- coding: utf-8 -*-
"""下载新版 exe / 热补丁包（QProcess + curl，跟检查更新同一套通道）。

规矩跟 ``update.py`` 一致：不开线程、不阻塞界面；单请求有硬超时，
另有一道整轮守卫；先系统代理再直连；下完 **先校验再交出去**（size + sha256）。

国内直连 GitHub 大文件经常是"连得上但只有 10–50 KB/s"，光靠超时永远等不到回退。
所以这里按**多个源**依次尝试（GitHub 原址 → 国内镜像），每个源先跑 2.5 秒看速度，
低于 40 KB/s 就掐掉换下一个；90% 已经下完的不换（免得白扔）。镜像只提供字节，
真伪由 ``sha256`` 兜底（校验值来自 GitHub API）。
"""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .log import debug
from .selfupdate import verify_file
from .update import curl_path, system_proxy

PER_TRY_S = 900          # 单个连接最多 15 分钟（45 MB 的 exe 走 10 KB/s 也够）
GUARD_S = 16 * 60        # 整轮兜底
POLL_MS = 400
SPEED_PROBE_S = 2.5      # 每个源先观察这么久
SPEED_MIN_KBPS = 40      # 低于这个速度就换源
MIN_DONE_RATIO = 0.85    # 已经下到 85% 就别换了（换源要重下）
STALL_S = 25             # 完全没进展这么久算卡死


# --------------------------------------------------------------------------- #
# 纯工具（好单测）
# --------------------------------------------------------------------------- #
def build_attempts(urls, proxy: str = "") -> list[dict]:
    """把「源列表」摊成「尝试列表」。

    GitHub 原址：先系统代理再直连（跟原来一致）；
    镜像：只走直连（它本来就在国内，绕代理反而慢/失败）。
    """
    cleaned: list[str] = []
    for url in urls:
        text = str(url or "").strip()
        if text and text not in cleaned:
            cleaned.append(text)
    out: list[dict] = []
    total = len(cleaned)
    for index, url in enumerate(cleaned):
        first = (index == 0)
        routes = ([proxy] if (first and proxy) else []) + [""]
        for route in routes:
            label = f"源 {index + 1}/{total}"
            label += " · 代理" if route else " · 直连"
            out.append({"url": url, "proxy": route, "label": label})
    return out


def speed_kbps(got: int, elapsed: float) -> float:
    if elapsed <= 0:
        return 0.0
    return max(0.0, float(got)) / 1024.0 / float(elapsed)


def too_slow(got: int, elapsed: float, total: int) -> bool:
    """这个源该不该掐掉？（还在观察期内、已经快下完，都不掐）"""
    if got <= 0 or elapsed < SPEED_PROBE_S:
        return False
    if total > 0 and got >= total * MIN_DONE_RATIO:
        return False
    return speed_kbps(got, elapsed) < SPEED_MIN_KBPS


class UpdateDownloader(QObject):
    """``start()`` 立刻返回；``progress`` 报百分比，``finished`` 报结果。"""

    progress = Signal(int, int, int)       # pct, 已下载, 总数
    status = Signal(str)                   # 「源 2/3 · 直连」「太慢，换源…」
    finished = Signal(bool, str, str)      # ok, 文件路径, 错误

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._dest: Path | None = None
        self._url = ""
        self._total = 0
        self._digest = ""
        self._attempts: list[dict] = []
        self._errors: list[str] = []
        self._err = b""
        self._timed_out = False
        self._done = False
        self._label = "curl"
        self._t0 = 0.0
        self._last_got = 0
        self._last_change = 0.0
        self._speed_ok = False
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._guard = QTimer(self)
        self._guard.setSingleShot(True)
        self._guard.timeout.connect(self._on_guard)

    # ------------------------------------------------------------------ 对外
    def busy(self) -> bool:
        return self._proc is not None

    def start(self, url: str, size: int, digest: str, dest,
              mirrors=()) -> bool:
        if self.busy():
            return False
        self._url = str(url or "")
        self._total = int(size or 0)
        self._digest = str(digest or "")
        self._dest = Path(dest)
        self._errors = []
        self._done = False
        self._timed_out = False
        if not self._url:
            self._fail("没有下载地址")
            return False
        if not curl_path():
            self._fail("系统里没有 curl.exe")
            return False
        # 上次已经下好了？（大小+sha256 都对得上就不用再下一遍）
        if self._dest.is_file():
            ok, _why = verify_file(self._dest, self._total, self._digest)
            if ok:
                debug("已有下载好的安装包，直接复用")
                self._finish(True, "")
                return True
            try:
                self._dest.unlink()
            except OSError:
                pass
        proxy = system_proxy()
        self._attempts = build_attempts([self._url, *(mirrors or ())], proxy)
        debug(f"开始下载 {self._url}（{self._total} 字节，"
              f"{len(self._attempts)} 次尝试，代理={proxy or '无'}）")
        self._guard.start(GUARD_S * 1000)
        self._next()
        return True

    def cancel(self):
        self._attempts = []
        self._poll.stop()
        self._guard.stop()
        self._kill()
        self._done = True

    # ------------------------------------------------------------------ 步骤
    def _next(self):
        if self._done:
            return
        if not self._attempts:
            self._fail("；".join(self._errors[-3:]) or "下载失败")
            return
        attempt = self._attempts.pop(0)
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            if self._dest.is_file():
                self._dest.unlink()
        except OSError:
            pass
        args = ["-fL", "--retry", "2", "--retry-delay", "2",
                "-m", str(PER_TRY_S), "-o", str(self._dest), attempt["url"]]
        if attempt["proxy"]:
            route = attempt["proxy"]
            args[0:0] = ["-x", route if "://" in route else f"http://{route}"]
        proc = QProcess(self)
        proc.setProgram(curl_path())
        proc.setArguments(args)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardError.connect(lambda: self._read_err(proc))
        proc.finished.connect(lambda code, status, p=proc: self._on_finished(p, code))
        proc.errorOccurred.connect(lambda err, p=proc: self._on_error(p, err))
        self._proc = proc
        self._err = b""
        self._timed_out = False
        self._label = attempt["label"]
        self._t0 = time.monotonic()
        self._last_got = 0
        self._last_change = self._t0
        self._speed_ok = False
        debug(f"{self._label} 开始下载 {attempt['url']}")
        self.status.emit(self._label)
        proc.start()
        self._poll.start()
        self._emit_progress()

    def _read_err(self, proc: QProcess):
        if proc is not self._proc:
            return
        try:
            self._err += bytes(proc.readAllStandardError())
        except RuntimeError:
            pass

    def _tick(self):
        got = self._emit_progress()
        if self._done or self._proc is None:
            return
        now = time.monotonic()
        elapsed = now - self._t0
        if got != self._last_got:
            self._last_got = got
            self._last_change = now
        elif elapsed > STALL_S and now - self._last_change > STALL_S:
            self._switch(f"卡住 {STALL_S} 秒没有任何数据")
            return
        if self._speed_ok:
            return
        if too_slow(got, elapsed, self._total):
            self._switch(f"太慢（{speed_kbps(got, elapsed):.0f} KB/s）")
        elif got > 0 and elapsed >= SPEED_PROBE_S:
            self._speed_ok = True          # 够快，这个源就一直用
            debug(f"{self._label} 速度 {speed_kbps(got, elapsed):.0f} KB/s，继续")

    def _switch(self, reason: str):
        """这个源不行：记一笔、掐掉、换下一个。"""
        label = self._label
        self._kill()
        self._errors.append(f"{label}: {reason}")
        debug(f"{label} 放弃：{reason}")
        self.status.emit(f"{label} {reason}，换下一个源…")
        self._next()

    def _emit_progress(self) -> int:
        if self._dest is None:
            return 0
        got = 0
        try:
            got = self._dest.stat().st_size
        except OSError:
            got = 0
        total = self._total
        pct = int(min(99.0, got * 100.0 / total)) if total > 0 else -1
        self.progress.emit(pct, got, total)
        return got

    def _on_error(self, proc: QProcess, error):
        if proc is not self._proc or error != QProcess.FailedToStart:
            return
        self._kill()
        self._errors.append(f"{self._label}: curl 起不来")
        self._next()

    def _on_finished(self, proc: QProcess, code: int):
        if proc is not self._proc:
            return
        self._proc = None
        self._poll.stop()
        self._read_err(proc)
        try:
            proc.deleteLater()
        except RuntimeError:
            pass
        if self._timed_out:
            return
        if code != 0:
            tail = self._err.decode("utf-8", "replace").strip().splitlines()
            self._errors.append(f"{self._label}: 退出码 {code} "
                                f"{(tail[-1][:60] if tail else '')}".strip())
            self._next()
            return
        ok, why = verify_file(self._dest, self._total, self._digest)
        if not ok:
            self._errors.append(f"{self._label}: {why}")
            debug(f"校验失败：{why}")
            try:
                self._dest.unlink()
            except OSError:
                pass
            self._next()
            return
        self._emit_progress()
        self._finish(True, "")

    def _on_guard(self):
        if self._done:
            return
        self._timed_out = True
        self._kill()
        self._fail("下载超时")

    # ------------------------------------------------------------------ 收尾
    def _kill(self):
        proc, self._proc = self._proc, None
        self._poll.stop()
        if proc is not None:
            try:
                proc.finished.disconnect()
                proc.errorOccurred.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                proc.kill()
                proc.waitForFinished(2000)
            except (RuntimeError, AttributeError):
                pass
            try:
                proc.deleteLater()
            except RuntimeError:
                pass

    def _fail(self, reason: str):
        self._poll.stop()
        self._guard.stop()
        self._kill()
        self._finish(False, reason)

    def _finish(self, ok: bool, reason: str = ""):
        if self._done:
            return
        self._done = True
        self._guard.stop()
        self._poll.stop()
        path = str(self._dest) if self._dest else ""
        debug(f"下载结束 ok={ok} path={path} err={reason!r}")
        self.finished.emit(ok, path, reason)
