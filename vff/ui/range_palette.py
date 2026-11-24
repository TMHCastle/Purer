# -*- coding: utf-8 -*-
"""多段区间 / 片段条的颜色与「叠层」分配。

同一段视频里可以画很多个入点/出点，它们**允许重叠**：

* 颜色按创建顺序循环取，重叠时也能一眼分清是第几段；
* 位置上重叠的条子要**分层**（lane），否则底部的总进度条会糊成一条。
  `assign_lanes()` 做标准的区间分配：每一段放进第一条不与它重叠的层。
"""

from __future__ import annotations

# 6 个高对比色，深色界面下都能看清（顺序也尽量避开相邻色太像）
RANGE_COLORS = (
    "#668CFF",      # 蓝
    "#45C995",      # 绿
    "#E5B65C",      # 琥珀
    "#B78CFF",      # 紫
    "#FF8FB1",      # 粉
    "#4FD8E8",      # 青
)


def color_for(index: int) -> str:
    return RANGE_COLORS[int(index) % len(RANGE_COLORS)]


def assign_lanes(spans) -> list[int]:
    """给 ``[(lo, hi), …]`` 分配层号（0 起）。

    * 返回的列表与输入一一对应；
    * 只有**严格重叠**才换层，紧挨着（hi < lo）可以同层；
    * 不要求输入有序，内部按 lo 排序后分配。
    """
    items = list(spans)
    if not items:
        return []
    order = sorted(range(len(items)), key=lambda i: (items[i][0], items[i][1]))
    lane_ends: list[float] = []
    result = [0] * len(items)
    for i in order:
        lo, hi = items[i]
        placed = None
        for lane, end in enumerate(lane_ends):
            if lo > end:
                placed = lane
                break
        if placed is None:
            placed = len(lane_ends)
            lane_ends.append(hi)
        else:
            lane_ends[placed] = hi
        result[i] = placed
    return result
