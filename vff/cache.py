# -*- coding: utf-8 -*-
"""缩略图缓存（临时渲染目录 + 7 天回收）。

这里的缓存目录是「本次渲染的落脚点」（默认在 **Purer 同目录的 ``cache_tmp``**）：
同一视频 + 同设置会命中同一个目录，所以重开同一个视频能秒开。
想把图墙**跨重启**留下来，则由 :mod:`vff.cache_store` 把这一份复制进
「持久缓存库」（设置-缓存 里管理）。
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

from .config import CACHE_TTL_DAYS, cache_root

# 缓存 key 的格式版本：改抽帧口径时才动它（动了 = 旧缓存全部作废）
KEY_VERSION = "v3"


def cache_key(path: Path, step: float, tw: int, th: int,
              mode: str = "step", threshold: float = 0.0) -> str:
    """同一视频 + 同设置 -> 同一个 key（文件名/大小/mtime 变了就换 key）。

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
    return (f"{KEY_VERSION}|{path.resolve()}|{stamp}|{step_key}|"
            f"{int(tw)}x{int(th)}")


def cache_digest(path: Path, step: float, tw: int, th: int,
                 mode: str = "step", threshold: float = 0.0) -> str:
    """key 的短摘要 —— 同时用作缓存目录名和持久库里的条目 id。"""
    key = cache_key(path, step, tw, th, mode=mode, threshold=threshold)
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:16]


def cache_dir_for(path: Path, step: float, tw: int, th: int,
                  mode: str = "step", threshold: float = 0.0) -> Path:
    """同一视频 + 同设置 -> 同一个缓存目录（秒开的基础）。"""
    return cache_root() / cache_digest(path, step, tw, th,
                                       mode=mode, threshold=threshold)


def dir_bytes(path: Path) -> int:
    """目录内文件字节数（只算文件，不算目录本身；读不了就算 0）。"""
    total = 0
    try:
        for item in Path(path).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def wall_bytes(path: Path) -> int:
    """只算「会存进持久库」的那部分（图墙 f_*.jpg + .done + scene.json）。"""
    total = 0
    folder = Path(path)
    try:
        for item in folder.glob("f_*.jpg"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        for name in (".done", "scene.json"):
            item = folder / name
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def temp_cache_bytes() -> int:
    """临时渲染缓存总共占多少（不含 update 下载目录 / 日志文件）。"""
    root = cache_root()
    total = 0
    try:
        for folder in root.iterdir():
            try:
                if folder.is_dir() and folder.name != "update" \
                        and len(folder.name) == 16:
                    total += dir_bytes(folder)
            except OSError:
                continue
    except OSError:
        return total
    return total


def clean_cache(days: int = CACHE_TTL_DAYS) -> int:
    """清掉很久没用的缓存目录（临时目录，可安全删除）。返回删除个数。"""
    root = cache_root()
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for d in root.iterdir():
        try:
            # 别把更新下载目录 / 日志目录当成缓存删掉（它们在同一个根下）
            if not d.is_dir() or d.name in ("update",):
                continue
            if d.stat().st_mtime < cutoff:
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
