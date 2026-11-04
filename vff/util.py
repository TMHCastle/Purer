# -*- coding: utf-8 -*-
"""通用小工具（纯函数，不依赖 Qt / ffmpeg，方便单测）。"""

from __future__ import annotations

import re

_ILLEGAL = re.compile(r'[:\\/*?"<>|\x00-\x1f]')


def fmt_tc(t: float | None) -> str:
    """秒 -> HH:MM:SS.mmm"""
    if t is None:
        return "--:--:--"
    t = max(0.0, float(t))
    hh = int(t // 3600)
    mm = int((t % 3600) // 60)
    ss = t % 60
    return f"{hh:02d}:{mm:02d}:{ss:06.3f}"


def fmt_tc_compact(t: float) -> str:
    """秒 -> HH-MM-SS.mmm（可直接做文件名）"""
    return fmt_tc(t).replace(":", "-")


def human_size(n: float | int | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def safe_filename(name: str) -> str:
    """去掉 Windows 文件名非法字符（保留中文），并把连续替换符合并。"""
    cleaned = _ILLEGAL.sub("-", name)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip(" .-")
    return cleaned or "output"


def parse_time_arg(text: str) -> float:
    """命令行时间：`12.5` 或 `00:01:23.456`（也接受 `1:23`）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("时间参数为空")
    if ":" not in text:
        value = float(text)
        if value < 0:
            raise ValueError("时间不能为负")
        return value
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError(f"时间格式不对：{text}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError("时间不能为负")
    return seconds
