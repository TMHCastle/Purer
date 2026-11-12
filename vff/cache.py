# -*- coding: utf-8 -*-
"""缩略图缓存（磁盘目录 + 7 天回收）。"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from .config import CACHE_TTL_DAYS, cache_root


def cache_dir_for(path: Path, step: float, tw: int, th: int,
                  mode: str = "step", threshold: float = 0.0) -> Path:
    """同一视频 + 同设置 -> 同一个缓存目录（秒开的基础）。

    ``mode``/``threshold`` 也进 key：按镜头抽的缓存不能和固定间隔的混在一起。
    """
    try:
        st = path.stat()
        stamp = f"{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        stamp = "0|0"
    if mode == "scene":
        step_key = f"scene{float(threshold):.3f}"
    else:
        step_key = f"{float(step):g}"
    key = f"v3|{path.resolve()}|{stamp}|{step_key}|{tw}x{th}"
    digest = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]
    return cache_root() / digest


def clean_cache(days: int = CACHE_TTL_DAYS) -> int:
    """清掉很久没用的缓存目录（临时目录，可安全删除）。返回删除个数。"""
    root = cache_root()
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def touch_cache_dir(path: Path) -> None:
    """更新目录 mtime，避免常用缓存被当成过期清掉。"""
    try:
        path.touch(exist_ok=True)
    except OSError:
        pass
