# -*- coding: utf-8 -*-
"""查 GitHub 上的最新发布版本（只读、全异步、查不到就当没事发生）。

三条硬规矩，都是为了不给界面添麻烦：

1. **不开线程、不用 Qt 网络栈**：传输交给系统自带的 ``curl.exe``，用 ``QProcess``
   跑 —— 跟本项目跑 ffmpeg 完全一个路子。另外两条更"现代"的路实测都有坑：
   ``QNetworkAccessManager`` 的第一次请求会在界面线程里同步做 TLS / 代理解析
   （PySide6 调 C++ 不放 GIL），打包后的 exe 偶发整窗卡死；``threading`` +
   ``subprocess`` 同样偶发冻住（GIL 被后台线程长期占着）。``QProcess`` 全程异步，
   界面线程一次都不等。
2. **每一步都有超时**：单个请求 5 秒，一整轮 18 秒；超时就 kill 掉换下一条路。
3. **先系统代理再直连**：Clash 这类只设了「系统代理」的环境，直连一定不通。

结果用信号回界面（同一线程，天然安全）；绿 / 橙 / 灰三色映射见 ``version.state_for``。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import NamedTuple

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .config import APP_NAME, APP_VERSION, GITHUB_REPO
from .log import debug
from .version import state_for

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
REDIRECT_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"

TIMEOUT_S = 5              # 单个请求的超时
DEADLINE_S = 18            # 一整轮检查的总预算
_UA = f"{APP_NAME}/{APP_VERSION}"
_TAG_IN_URL = re.compile(r"/tag/([^/?#]+)")


class CheckResult(NamedTuple):
    """一次检查的结果（ok=False 时只有 error 有意义）。"""

    ok: bool
    tag: str = ""
    url: str = ""
    error: str = ""
    source: str = ""            # "curl+代理" / "curl+直连"
    asset_url: str = ""         # 新版 exe 的下载地址
    asset_size: int = 0         # 字节数（GitHub 给的就是这个）
    digest: str = ""            # "sha256:…"（GitHub 的资产校验值）

    def state(self, current: str = APP_VERSION) -> str:
        return state_for(self.ok, self.tag, current)

    def can_install(self) -> bool:
        """能不能自己下载安装（要 exe 地址 + 大小；有 digest 更放心）。"""
        return bool(self.asset_url and self.asset_size > 0)


# --------------------------------------------------------------------------- #
# 纯工具：系统代理 / curl 路径 / 输出解析
# --------------------------------------------------------------------------- #
def pick_proxy(text: str, scheme: str = "https") -> str:
    """WinINET 的 ``ProxyServer`` 值 → ``host:port``。

    两种写法：``127.0.0.1:7897`` 或 ``http=h:p;https=h:p;ftp=h:p``；
    没有对应协议时退回 ``http=`` / ``socks=`` 那一段。
    """
    if not text:
        return ""
    if "=" not in text:
        return text.strip()
    parts = {}
    for chunk in text.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            parts[key.strip().lower()] = value.strip()
    return parts.get(scheme) or parts.get("http") or parts.get("socks") or ""


def system_proxy() -> str:
    """从注册表读「系统代理」（只读，失败就当没有）。"""
    if os.name != "nt":
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    try:
        import winreg

        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not enabled:
            return ""
        return pick_proxy(str(server))
    except (OSError, ImportError, ValueError):
        return ""


def curl_path() -> str:
    found = shutil.which("curl")
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    fallback = Path(system_root) / "System32" / "curl.exe"
    return str(fallback) if fallback.is_file() else ""


def curl_args(url: str, proxy: str, timeout: int, want_url: bool = False) -> list[str]:
    """curl 参数：``-sS`` 静默但保留错误、``-L`` 跟跳转、``-m`` 硬超时。"""
    args = ["-sS", "-L", "-m", str(timeout),
            "-H", "Accept: application/vnd.github+json",
            "-H", f"User-Agent: {_UA}"]
    if proxy:
        args += ["-x", proxy if "://" in proxy else f"http://{proxy}"]
    if want_url:
        args += ["-o", os.devnull, "-w", "%{url_effective}"]
    else:
        args += ["-o", "-"]
    args.append(url)
    return args


def asset_from_json(data) -> dict:
    """从 release JSON 里挑出「给 Windows 用的 exe」资产。

    优先 ``Purer.exe`` 同名，其次第一个 ``.exe``；没有就返回空 dict。
    """
    best = {}
    for item in (data.get("assets") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if not name.endswith(".exe"):
            continue
        if name == "purer.exe":
            best = item
            break
        if not best:
            best = item
    if not best:
        return {}
    return {
        "url": str(best.get("browser_download_url") or ""),
        "size": int(best.get("size") or 0),
        "digest": str(best.get("digest") or ""),
    }


def tag_from_json(body: str):
    """release JSON → ``(tag, html_url, 失败原因)``（不含资产信息）。"""
    result = result_from_json(body)
    if not result.ok:
        return "", "", result.error
    return result.tag, result.url, ""


def result_from_json(body: str) -> CheckResult:
    """release JSON → 完整 ``CheckResult``（含新版 exe 的下载地址 / 大小 / 校验值）。"""
    try:
        data = json.loads(body)
    except ValueError as exc:
        return CheckResult(False, "", "", f"JSON 解析失败：{exc}")
    if not isinstance(data, dict):
        return CheckResult(False, "", "", "接口返回的不是对象")
    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        return CheckResult(False, "", "", "接口返回里没有 tag_name")
    url = str(data.get("html_url") or "").strip() or RELEASES_PAGE
    asset = asset_from_json(data)
    return CheckResult(True, tag, url, "", "",
                       asset.get("url", ""), asset.get("size", 0),
                       asset.get("digest", ""))


def tag_from_url(final: str) -> str:
    """``https://github.com/…/releases/tag/v1.1.4`` → ``v1.1.4``。"""
    found = _TAG_IN_URL.search(final or "")
    return found.group(1) if found else ""


# --------------------------------------------------------------------------- #
# 异步检查器
# --------------------------------------------------------------------------- #
class ReleaseChecker(QObject):
    """``check()`` 立刻返回；结果从 ``finished`` 信号回来，全程不阻塞界面。"""

    finished = Signal(object)          # CheckResult

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._step: dict | None = None
        self._buf = b""
        self._err = b""
        self._steps: list[dict] = []
        self._errors: list[str] = []
        self._deadline = 0.0
        self._timed_out = False
        self._exe = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        # 兜底：万一某一步没能回到我们的槽（写错代码/被系统掐住），
        # 整轮结束就强制给个结果，别让药丸永远停在「检查更新中…」
        self._guard = QTimer(self)
        self._guard.setSingleShot(True)
        self._guard.timeout.connect(self._on_guard)

    # ------------------------------------------------------------------ 对外
    def busy(self) -> bool:
        return self._proc is not None

    def check(self):
        debug("check(): 进入")
        if self.busy():
            debug("check(): 上一次还在跑，忽略这次点击")
            return
        self._exe = curl_path()
        debug(f"check(): curl={self._exe!r}")
        if not self._exe:
            self._finish(CheckResult(False, "", "",
                                     "系统里没有 curl.exe（Win10 1809 以上才自带）"))
            return
        proxy = system_proxy()
        debug(f"check(): 系统代理={proxy!r}")
        routes = [proxy] if proxy else []
        routes.append("")                     # 直连兜底
        self._steps = []
        for route in routes:
            label = "curl+代理" if route else "curl+直连"
            self._steps.append({"kind": "api", "url": API_URL,
                                "proxy": route, "label": label})
            self._steps.append({"kind": "redirect", "url": REDIRECT_URL,
                                "proxy": route, "label": label})
        self._errors = []
        self._deadline = time.monotonic() + DEADLINE_S
        self._guard.start((DEADLINE_S + 3) * 1000)
        debug(f"开始检查（curl={self._exe}，系统代理={proxy or '无'}）")
        self._next()

    def _on_guard(self):
        if not self.busy() and not self._steps:
            return
        self._errors.append("整轮超时")
        self._finish_fail()

    def cancel(self):
        """窗口关掉时调用：别把 curl 子进程留在后台。"""
        self._steps = []
        self._timer.stop()
        self._guard.stop()
        self._kill()

    # ------------------------------------------------------------------ 步骤
    def _next(self):
        if time.monotonic() > self._deadline:
            self._errors.append("超出总时长")
            self._finish_fail()
            return
        if not self._steps:
            self._finish_fail()
            return
        step = self._steps.pop(0)
        debug(f"{step['label']} {step['kind']} …")
        self._start(step)

    def _start(self, step: dict):
        want_url = step["kind"] == "redirect"
        proc = QProcess(self)
        proc.setProgram(self._exe)
        proc.setArguments(curl_args(step["url"], step["proxy"], TIMEOUT_S, want_url))
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(lambda: self._read(proc, False))
        proc.readyReadStandardError.connect(lambda: self._read(proc, True))
        proc.finished.connect(lambda code, status, p=proc, s=step:
                              self._on_finished(s, p, code))
        proc.errorOccurred.connect(lambda err, p=proc, s=step:
                                   self._on_error(s, p, err))
        self._proc = proc
        self._step = step
        self._buf = b""
        self._err = b""
        self._timed_out = False
        self._timer.start(TIMEOUT_S * 1000)
        debug(f"{step['label']} {step['kind']}: 启动 curl")
        proc.start()

    def _read(self, proc: QProcess, stderr: bool):
        if proc is not self._proc:
            return
        try:
            data = bytes(proc.readAllStandardError() if stderr
                         else proc.readAllStandardOutput())
        except RuntimeError:              # 对象已被回收
            return
        if stderr:
            self._err += data
        else:
            self._buf += data

    def _on_timeout(self):
        if self._proc is None:
            return
        self._timed_out = True
        step = self._step or {}
        self._kill()
        self._errors.append(f"{step.get('label', '?')}: 超时（{TIMEOUT_S}s）")
        self._step_failed(step)

    def _on_error(self, step: dict, proc: QProcess, error):
        if proc is not self._proc:
            return
        if error != QProcess.FailedToStart:
            return                 # 其他错误交给 finished 统一处理
        self._kill()
        self._errors.append(f"{step['label']}: curl 起不来")
        self._step_failed(step)

    def _on_finished(self, step: dict, proc: QProcess, code: int):
        if proc is not self._proc:
            return
        try:
            # 收尾：把管道里剩下的读干净（readAll 之后 proc 才有完整输出）
            self._buf += bytes(proc.readAllStandardOutput())
            self._err += bytes(proc.readAllStandardError())
        except RuntimeError:      # 窗口正在关，C++ 对象已经被回收
            self._proc = None
            self._timer.stop()
            return
        self._proc = None
        self._timer.stop()
        proc.deleteLater()
        if self._timed_out:
            return                 # 超时那条已经在 _on_timeout 处理
        if code != 0:
            detail = self._err.decode("utf-8", "replace").strip().splitlines()
            tail = detail[-1][:60] if detail else ""
            self._errors.append(f"{step['label']}: curl 退出码 {code} {tail}".strip())
            self._step_failed(step)
            return
        text = self._buf.decode("utf-8", "replace").strip()
        if step["kind"] == "redirect":
            tag = tag_from_url(text)
            if not tag:
                self._errors.append(f"{step['label']}: 跳转地址里没有 tag")
                self._next()
                return
            self._finish(CheckResult(True, tag, text, "", step["label"]))
            return
        result = result_from_json(text)
        if not result.ok:
            # 接口连得上但内容不对（被网关改写之类）→ 换跳转那条路
            self._errors.append(f"{step['label']}: {result.error}")
            self._next()
            return
        self._finish(result._replace(source=step["label"]))

    def _step_failed(self, step: dict):
        """这条路线确认不通：同一代理上的下一条也不用试了。"""
        if step.get("kind") == "api":
            self._steps = [s for s in self._steps if s["proxy"] != step["proxy"]]
        self._next()

    # ------------------------------------------------------------------ 收尾
    def _kill(self):
        proc, self._proc = self._proc, None
        self._timer.stop()
        self._step = None
        if proc is not None:
            try:
                # 先摘信号：kill/wait 期间 Qt 可能把对象回收掉，回调再进来就会炸
                proc.finished.disconnect()
                proc.errorOccurred.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                proc.kill()
                proc.waitForFinished(1500)
            except (RuntimeError, AttributeError):
                pass
            try:
                proc.deleteLater()
            except RuntimeError:
                pass

    def _finish_fail(self):
        reason = "；".join(self._errors[-3:]) or "网络不可用"
        self._finish(CheckResult(False, "", "", reason))

    def _finish(self, result: CheckResult):
        self._timer.stop()
        self._guard.stop()
        self._steps = []
        self._kill()
        debug(f"结束 ok={result.ok} tag={result.tag} src={result.source} "
              f"err={result.error!r}")
        self.finished.emit(result)


__all__ = ["CheckResult", "ReleaseChecker", "system_proxy", "pick_proxy",
           "curl_path", "curl_args", "tag_from_json", "tag_from_url",
           "API_URL", "REDIRECT_URL", "RELEASES_PAGE", "TIMEOUT_S", "DEADLINE_S"]
