# -*- coding: utf-8 -*-
"""批量导出的「素材名」规则（纯函数，方便单测）。

用户填了名字就用用户的名字；没填就自动生成系列名：

    Cut - 1_原视频名.mp4
    Cut - 2_原视频名.mp4
    ...

重复（同一批里填了同一个名字 / 目标文件夹里已经有同名文件）会自动加
`` (2)``、`` (3)``，绝不静默覆盖别人的文件。
"""

from __future__ import annotations

from pathlib import Path

from .config import VIDEO_EXTS
from .util import safe_filename

SERIES_PREFIX = "Cut - "


def series_label(index: int) -> str:
    """0 -> ``Cut - 1``（显示给用户看的系列名）。"""
    return f"{SERIES_PREFIX}{int(index) + 1}"


def default_filename(index: int, stem: str, suffix: str) -> str:
    """没填名字时的默认文件名：系列名 + 原视频名。"""
    return f"{series_label(index)}_{safe_filename(stem)}{suffix}"


def strip_video_suffix(text: str) -> str:
    """用户自己打了扩展名时去掉它（输出容器必须跟源视频一致）。"""
    name = str(text or "").strip()
    lower = name.lower()
    for ext in sorted(VIDEO_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return name[: -len(ext)].rstrip(" .")
    return name


def clean_user_name(text: str, suffix: str) -> str:
    """把手填的名字变成合法文件名；空 / 全是非法字符 -> 空串（走默认名）。"""
    name = strip_video_suffix(text)
    if not name:
        return ""
    cleaned = safe_filename(name, fallback="")
    if not cleaned or cleaned in (".", ".."):
        return ""
    return f"{cleaned}{suffix}"


def plan_filenames(texts, stem: str, suffix: str, taken=()) -> list[str]:
    """``texts`` 里是用户填的（可能为空），返回可直接落盘的文件名列表。"""
    suffix = suffix or ""
    used = {str(t).lower() for t in taken}
    out: list[str] = []
    for index, raw in enumerate(texts):
        name = clean_user_name(raw, suffix) or default_filename(index, stem, suffix)
        base = (name[: -len(suffix)] if suffix and name.lower().endswith(suffix.lower())
                else name)
        candidate = name
        n = 2
        while candidate.lower() in used:
            candidate = f"{base} ({n}){suffix}"
            n += 1
        used.add(candidate.lower())
        out.append(candidate)
    return out


def existing_names(folder: Path) -> set[str]:
    """目标文件夹里已有的文件名（小写），用于避免覆盖。"""
    try:
        return {p.name.lower() for p in Path(folder).iterdir() if p.is_file()}
    except OSError:
        return set()
