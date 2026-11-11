# -*- coding: utf-8 -*-
"""下载新版 exe（QProcess + curl，跟检查更新同一套通道）。

规矩跟 ``update.py`` 一致：不开线程、不阻塞界面；单请求有硬超时，
另有一道整轮守卫；先系统代理再直连；下完 **先校验再交出去**（size + sha256）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .log import debug
from .selfupdate import verify_file
from .update import curl_path, system_proxy

PER_TRY_S = 900          # 单个连接最多 15 分钟（45 MB 的 exe 走 10 KB/s 也够）
GUARD_S = 16 * 60        # 整轮兜底
POLL_MS = 400


class UpdateDownloader(QObject):
    """``start()`` 立刻返回；``progress`` 报百分比，``finished`` 报结果。"""

    progress = Signal(int, int, int)       # pct, 已下载, 总数
    finished = Signal(bool, str, str)      # ok, 文件路径, 错误

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._dest: Path | None = None
        self._url = ""
        self._total = 0
        self._digest = ""
        self._routes: list[str] = []
        self._errors: list[str] = []
        self._err = b""
        self._timed_out = False
        self._done = False
        self._label = "curl"
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._emit_progress)
        self._guard = QTimer(self)
        self._guard.setSingleShot(True)
        self._guard.timeout.connect(self._on_guard)

    # ------------------------------------------------------------------ 对外
    def busy(self) -> bool:
        return self._proc is not None

    def start(self, url: str, size: int, digest: str, dest) -> bool:
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
        self._routes = ([proxy] if proxy else []) + [""]
        debug(f"开始下载 {self._url}（{self._total} 字节，代理={proxy or '无'}）")
        self._guard.start(GUARD_S * 1000)
        self._next()
        return True

    def cancel(self):
        self._routes = []
        self._poll.stop()
        self._guard.stop()
        self._kill()
        self._done = True

    # ------------------------------------------------------------------ 步骤
    def _next(self):
        if self._done:
            return
        if not self._routes:
            self._fail("；".join(self._errors[-3:]) or "下载失败")
            return
        route = self._routes.pop(0)
        try:
            self._dest.parent.mkdir(parents=True, exist_ok=True)
            if self._dest.is_file():
                self._dest.unlink()
        except OSError:
            pass
        args = ["-fL", "--retry", "2", "--retry-delay", "2",
                "-m", str(PER_TRY_S), "-o", str(self._dest), self._url]
        if route:
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
        self._label = "curl+代理" if route else "curl+直连"
        debug(f"{self._label} 开始下载")
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

    def _emit_progress(self):
        if self._dest is None:
            return
        got = 0
        try:
            got = self._dest.stat().st_size
        except OSError:
            got = 0
        total = self._total
        pct = int(min(99.0, got * 100.0 / total)) if total > 0 else -1
        self.progress.emit(pct, got, total)

    def _on_error(self, proc: QProcess, error):
        if proc is not self._proc or error != QProcess.FailedToStart:
            return
        self._kill()
        self._errors.append("curl 起不来")
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
