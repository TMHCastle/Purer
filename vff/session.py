# -*- coding: utf-8 -*-
"""会话快照：标签页 / 当前帧 / 入出点 / 片段 / 导出记录（纯逻辑，不依赖 Qt）。

同一份文件服务三件事：

1. **更新重启**（``--resume``）：换完 exe 回来，界面原样接上；
2. **崩溃 / 断电**：定时落盘，下次启动接着干；
3. **日常重开**：关掉再打开，标签还在。

写盘规矩（跟 ``settings.py`` 一致）：原子写（临时文件 + replace），
读失败一律当「没有会话」——**不能因为 session.json 写坏就打不开程序**；
字段级收敛：多余字段丢弃、坏值退回默认。

两个刻意的设计：

* **不存缩略图路径** —— 那些由 ``cache_digest()`` 按「视频 + 设置」重算，
  存了反而会在缓存被清后指向不存在的文件；
* **``segments`` / ``exports`` 与缓存里的 ``segments.json`` 有重叠** ——
  session 负责「缓存丢了也能恢复」，缓存负责「跨会话保存」，互为兜底。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from .config import (DEFAULT_DISPLAY_SIDE, DEFAULT_STEP, DISPLAY_MAX, DISPLAY_MIN,
                     data_dir)

SESSION_VERSION = 1
SESSION_NAME = "session.json"
MAX_TABS = 50
MAX_SEGMENTS = 200
MAX_EXPORTS = 100
MAX_NAME = 200


# --------------------------------------------------------------------------- #
# 位置
# --------------------------------------------------------------------------- #
def session_file() -> Path:
    """默认 ``%LOCALAPPDATA%\\Purer\\session.json``（``PURER_SESSION_FILE`` 可覆盖）。"""
    override = os.environ.get("PURER_SESSION_FILE")
    if override:
        return Path(override)
    return data_dir() / SESSION_NAME


# --------------------------------------------------------------------------- #
# 字段收敛
# --------------------------------------------------------------------------- #
def _int_or_none(value, low: int = 0, high: int = 10 ** 9):
    """整数且落在 ``[low, high]`` 内才要；越界就当没写（不要瞎猜一个值）。"""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < low or number > high:
        return None
    return number


def _float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def sanitize_segment(raw) -> dict | None:
    """``{t_in, t_end, name}``；时间不是有限数或 ``t_end <= t_in`` 就丢掉。"""
    if not isinstance(raw, dict):
        return None
    t_in = _float(raw.get("t_in"), math.nan)
    t_end = _float(raw.get("t_end"), math.nan)
    if not (math.isfinite(t_in) and math.isfinite(t_end)):
        return None
    if t_in < 0 or t_end <= t_in:
        return None
    return {"t_in": round(t_in, 4), "t_end": round(t_end, 4),
            "name": str(raw.get("name") or "")[:MAX_NAME]}


def sanitize_export(raw) -> dict | None:
    """``{path, at, size, kind}``；没有 path 的条目没用。"""
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None
    return {"path": path,
            "at": _float(raw.get("at"), 0.0),
            "size": max(0, _int_or_none(raw.get("size"), 0) or 0),
            "kind": str(raw.get("kind") or "")[:40]}


def sanitize_tab(raw) -> dict | None:
    """一个标签页的完整状态；没有视频路径就当没这条。"""
    if not isinstance(raw, dict):
        return None
    path = str(raw.get("path") or "").strip()
    if not path:
        return None

    mode = str(raw.get("mode") or "step").strip().lower()
    if mode not in ("step", "scene"):
        mode = "step"
    step = _float(raw.get("step"), float(DEFAULT_STEP))
    step = max(1.0, min(3000.0, step))
    threshold = None
    if mode == "scene":
        threshold = max(0.01, min(0.99, _float(raw.get("threshold"), 0.30)))

    side = _int_or_none(raw.get("display_side"), DISPLAY_MIN, DISPLAY_MAX)
    if side is None:
        side = DEFAULT_DISPLAY_SIDE
    sidebar = _int_or_none(raw.get("sidebar_width"), 320, 520)
    scroll_row = _int_or_none(raw.get("scroll_row"), 0) or 0

    segments = []
    for item in (raw.get("segments") or [])[:MAX_SEGMENTS]:
        clean = sanitize_segment(item)
        if clean is not None:
            segments.append(clean)

    exports = []
    for item in (raw.get("exports") or [])[:MAX_EXPORTS]:
        clean = sanitize_export(item)
        if clean is not None:
            exports.append(clean)

    return {
        "path": path,
        "step": step,
        "mode": mode,
        "threshold": threshold,
        "display_side": side,
        "sidebar_width": sidebar,
        "cursor_frame": _int_or_none(raw.get("cursor_frame")),
        "in_frame": _int_or_none(raw.get("in_frame")),
        "out_frame": _int_or_none(raw.get("out_frame")),
        "scroll_row": scroll_row,
        "segments": segments,
        "exports": exports,
    }


def sanitize(raw) -> dict:
    """整份会话收敛成合法结构（永远返回可用的 dict）。"""
    raw = raw if isinstance(raw, dict) else {}
    tabs = []
    for item in (raw.get("tabs") or [])[:MAX_TABS]:
        clean = sanitize_tab(item)
        if clean is not None:
            tabs.append(clean)

    window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    geometry = str(window.get("geometry") or "")
    if len(geometry) > 65536:                    # 明显的脏数据
        geometry = ""

    current = _int_or_none(raw.get("current"), 0, max(0, len(tabs) - 1)) or 0
    return {
        "version": SESSION_VERSION,
        "saved_at": _float(raw.get("saved_at"), 0.0),
        "reason": str(raw.get("reason") or "")[:32],
        "window": {"geometry": geometry,
                   "maximized": bool(window.get("maximized") is True
                                     or str(window.get("maximized")).lower()
                                     in ("1", "true", "yes"))},
        "tabs": tabs,
        "current": current,
    }


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def empty() -> dict:
    return sanitize({})


def load(path: Path | None = None) -> dict:
    """读会话；文件不在 / 坏了 / 不是对象 → 空会话（绝不抛异常）。"""
    target = Path(path) if path is not None else session_file()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    return sanitize(data)


def save(payload: dict, path: Path | None = None) -> bool:
    """原子写；失败返回 False（调用方决定要不要提示，但绝不能因此不启动）。"""
    target = Path(path) if path is not None else session_file()
    data = sanitize(payload)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".session-", suffix=".json",
                                   dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, target)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return False
    return True


def clear(path: Path | None = None) -> bool:
    """删掉会话文件（用户「不恢复」时用）。"""
    target = Path(path) if path is not None else session_file()
    try:
        target.unlink()
    except OSError:
        return False
    return True
