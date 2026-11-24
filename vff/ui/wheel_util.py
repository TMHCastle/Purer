# -*- coding: utf-8 -*-
"""滚轮方向判定（Alt+缩放 / 普通滚动共用）。

Windows 上有三种情况都会出现，必须都认：

* 普通鼠标：``angleDelta().y()`` = ±120；
* 高精度滚轮 / 触控板：只有 ``pixelDelta()``，``angleDelta()`` 是 0；
* 某些驱动把 **Alt+滚轮** 转成**横向滚动**：``angleDelta().x()`` 有值、``y()`` 是 0
  （这时向上滚＝向右滚，取正号）。
"""

from __future__ import annotations


def wheel_direction(event) -> int:
    """+1 = 向上 / 放大，-1 = 向下 / 缩小，0 = 没有可用增量。"""
    angle = event.angleDelta()
    if angle.y():
        return 1 if angle.y() > 0 else -1
    if angle.x():
        # 驱动把 Alt+滚轮当横向滚：右＝原来的向上
        return 1 if angle.x() > 0 else -1
    pixels = event.pixelDelta()
    if pixels.y():
        return 1 if pixels.y() > 0 else -1
    if pixels.x():
        return 1 if pixels.x() > 0 else -1
    return 0
