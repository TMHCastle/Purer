# -*- coding: utf-8 -*-
"""缩略图内存缓存与图片可用性检查（Qt 侧）。"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QImageReader, QPixmap


class ThumbCache:
    """按字节上限的 LRU，避免上千张缩略图吃满内存。"""

    def __init__(self, limit_bytes: int = 64 * 1024 * 1024):
        self.limit = limit_bytes
        self.data: OrderedDict[str, QPixmap] = OrderedDict()
        self.used = 0

    def get(self, key):
        pm = self.data.get(key)
        if pm is not None:
            self.data.move_to_end(key)
        return pm

    def put(self, key, pm):
        old = self.data.pop(key, None)
        if old is not None:
            self.used -= old.width() * old.height() * 4
        size = pm.width() * pm.height() * 4
        self.data[key] = pm
        self.used += size
        while self.used > self.limit and len(self.data) > 1:
            _, old = self.data.popitem(last=False)
            self.used -= old.width() * old.height() * 4

    def clear(self):
        self.data.clear()
        self.used = 0


THUMB_CACHE = ThumbCache()
# 原图（从 jpg 解码后的原始尺寸）与缩放结果分开缓存：
# 改显示尺寸时只重新缩放，不重新解码。
SOURCE_CACHE = ThumbCache(48 * 1024 * 1024)
SCALED_CACHE = ThumbCache(64 * 1024 * 1024)


def jpeg_ready(p: Path) -> bool:
    """文件是否已经写完并可以解码（抽帧过程中判断"这张读得没"）。"""
    try:
        if p.stat().st_size < 512:
            return False
    except OSError:
        return False
    reader = QImageReader(str(p))
    return bool(reader.canRead()) and reader.size().width() > 0


def thumb_pixmap(file_path: str, tw: int, th: int):
    """按显示尺寸取缩略图（解码只做一次，缩放结果按尺寸另存）。"""
    key = (file_path, tw, th)
    pm = SCALED_CACHE.get(key)
    if pm is not None:
        return pm
    src = SOURCE_CACHE.get(file_path)
    if src is None:
        src = QPixmap(file_path)
        if src.isNull():
            return None
        SOURCE_CACHE.put(file_path, src)
    if src.width() == tw and src.height() == th:
        pm = src
    else:
        pm = src.scaled(tw, th, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    SCALED_CACHE.put(key, pm)
    return pm
