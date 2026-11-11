# -*- coding: utf-8 -*-
"""诊断日志：窗口版 exe 没有控制台，出问题只能写文件。

三个东西，全部只在需要时打开：

* ``install_crash_log()`` —— 未捕获异常落到 ``%TEMP%\\Purer_cache\\error.log``
  （``PURER_UPDATE_DEBUG`` 不用开，默认就生效）；
* ``debug()`` —— 调试痕迹写 ``update.log``，需要 ``PURER_UPDATE_DEBUG=1``；
* ``start_debug_watchdog()`` —— 每 12 秒把所有线程的栈写进 ``debug_stacks.txt``，
  用来判断「界面没反应」到底是卡在哪一步（同样需要 ``PURER_UPDATE_DEBUG=1``）。
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from .cache import cache_root
from .config import APP_VERSION

_DEBUG_FLAG = "PURER_UPDATE_DEBUG"


def log_path(name: str = "error.log") -> Path:
    return cache_root() / name


def append(name: str, text: str) -> Path:
    """往缓存目录里的日志追加一段（失败就算了，绝不因为写日志崩）。"""
    path = log_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip("\n") + "\n")
    except OSError:
        pass
    return path


def debug_enabled() -> bool:
    return os.environ.get(_DEBUG_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def debug(message: str, name: str = "update.log"):
    if debug_enabled():
        append(name, f"{time.strftime('%H:%M:%S')}  {message}")


def start_debug_watchdog(interval_s: int = 12):
    """每隔几秒把所有线程的栈写进 debug_stacks.txt（只看不干预）。"""
    if not debug_enabled():
        return None
    import faulthandler

    path = log_path("debug_stacks.txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a", encoding="utf-8")
    except OSError:
        return None
    handle.write(f"\n=== watchdog start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"v{APP_VERSION} ===\n")
    handle.flush()
    faulthandler.dump_traceback_later(interval_s, repeat=True, file=handle)
    return handle


def install_crash_log() -> Path:
    """把未捕获异常（含 Qt 槽函数里的）落到 ``%TEMP%\\Purer_cache\\error.log``。"""
    path = log_path("error.log")

    def hook(exc_type, exc, tb):
        append("error.log",
               f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')}  v{APP_VERSION}"
               f"  ({sys.platform})\n"
               + "".join(traceback.format_exception(exc_type, exc, tb)))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    return path
