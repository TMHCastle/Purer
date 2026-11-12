# -*- coding: utf-8 -*-
"""帧号 ↔ 缩略图序号 的映射（图墙时间轴的"坐标系"）。

整面墙的时间轴、拖动入出点、光标三角都建立在「第几个格子 = 哪一帧」上。
两种抽帧方式给出两种坐标系，接口完全一样，`wall_view` / `track_page`
只认这个接口，不再自己拿 step 做除法：

* ``StepTimeline``  —— 每 N 帧一张：等距，用公式算；
* ``SceneTimeline`` —— 每个镜头一张：帧号不规则，二分查找 + 格内线性插值。

「格内线性插值」是关键：按镜头之后格子宽窄不一，但格子内部仍然是连续帧号，
所以拖蓝旗 / 点帧 / ←→ 依然**精确到帧**，只是每格代表的帧数不同。
"""

from __future__ import annotations

import math
from bisect import bisect_right


class Timeline:
    """公共部分：帧范围、格子跨度、平均格宽。"""

    kind = "step"

    def __init__(self, total: int = 0):
        self.total = max(0, int(total))

    # ------------------------------------------------------------ 子类实现
    def count(self) -> int:
        return 0

    def frame_at(self, index: int) -> float:
        raise NotImplementedError

    def index_of(self, frame: float) -> tuple[int, float]:
        """帧号 → (格子序号, 格内比例 0~1)。"""
        raise NotImplementedError

    # ------------------------------------------------------------ 公共
    def span(self, index: int) -> tuple[float, float]:
        """第 index 个格子覆盖的帧范围（含两端）。"""
        count = self.count()
        if count <= 0:
            return (0.0, 0.0)
        index = max(0, min(count - 1, int(index)))
        start = self.frame_at(index)
        if index + 1 < count:
            end = self.frame_at(index + 1) - 1
        else:
            end = max(start, self.total - 1)
        return (start, max(start, end))

    def guess_step(self) -> float:
        """PageUp/PageDown 用：平均一格多少帧。"""
        return 1.0

    def label(self) -> str:
        return ""

    def short(self) -> str:
        return self.label()


class StepTimeline(Timeline):
    """每 N 帧一张（等距）。"""

    kind = "step"

    def __init__(self, step: float, total: int = 0):
        super().__init__(total)
        self.step = max(1e-6, float(step))

    def count(self) -> int:
        if self.total <= 0:
            return 0
        return max(1, int(math.ceil(self.total / self.step)))

    def frame_at(self, index: int) -> float:
        return max(0.0, float(index) * self.step)

    def index_of(self, frame: float) -> tuple[int, float]:
        pos = max(0.0, float(frame)) / self.step
        index = int(pos)
        return index, pos - index

    def guess_step(self) -> float:
        return self.step

    def label(self) -> str:
        return f"每 {self.step:g} 帧"


class SceneTimeline(Timeline):
    """每个镜头一张（帧号来自场景检测，不规则）。"""

    kind = "scene"

    def __init__(self, frames, total: int = 0):
        super().__init__(total)
        self.frames = sorted({int(f) for f in frames if int(f) >= 0})

    def count(self) -> int:
        return len(self.frames)

    def frame_at(self, index: int) -> float:
        if not self.frames:
            return 0.0
        index = max(0, min(len(self.frames) - 1, int(index)))
        return float(self.frames[index])

    def index_of(self, frame: float) -> tuple[int, float]:
        frames = self.frames
        if not frames:
            return 0, 0.0
        frame = max(0.0, float(frame))
        index = bisect_right(frames, frame) - 1
        if index < 0:
            index = 0
        if index >= len(frames) - 1:
            index = len(frames) - 1
            end = max(frames[-1] + 1, self.total)
            gap = max(1.0, end - frames[-1])
        else:
            gap = max(1.0, frames[index + 1] - frames[index])
        frac = (frame - frames[index]) / gap
        return index, min(1.0, max(0.0, frac))

    def guess_step(self) -> float:
        if not self.frames:
            return 1.0
        return max(1.0, (self.frames[-1] + 1) / len(self.frames))

    def label(self) -> str:
        return f"按镜头 · {len(self.frames)} 个"
