# -*- coding: utf-8 -*-
"""底部「总进度条」：整段视频缩成一条，光标 / 多段区间 / 片段 / 标记都能直接操作。

这里不存「视频状态」—— ``TrackPage._sync_overview()`` 每次都把同一份
``cursor_frame`` / ``in_frame`` / ``out_frame`` / ``ranges`` / ``segments`` /
``markers`` 推过来；用户在这里点/拖只发信号，回去走的仍是墙上时间轴那套回调
（``_set_cursor`` / ``_on_range_drag`` / ``_end_range_drag``），所以两边永远一致。

操作：
  * 点 / 拖轨道 = 移动光标（精确到帧）；
  * 拖两端的旗子 = 改当前段的入点 / 出点；
  * 点片段条 = 选中并跳到该片段；
  * 点绿色标记 = 跳到 M 标记；
  * **Ctrl+滚轮** = 进度条整体横向缩放；**Alt+滚轮** = 片段层上下翻；
  * **普通滚轮** = 放大后左右平移（没放大且片段叠了很多层时也能翻层）；
  * 双击 = 回到「整段」；右键 = 设入出点 / 清除 / 加删标记 / 缩放。

片段标记**按重叠分层**（同一位置叠了很多段时按高度错开），
放大后还会自动跟着光标走。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from ..util import fmt_tc
from .range_palette import assign_lanes, color_for
from .style import ACCENT, DIM, PANEL2, WARN
from .wheel_util import wheel_direction

DEFAULT_BAR_H = 78      # 默认高度（约 3 层片段 + 轨道 + 时间行）
MIN_BAR_H = 52          # 再压就只剩轨道了
MARGIN = 16             # 左右留白（贴边不好点）
TRACK_H = 6             # 轨道粗细
HANDLE_HIT = 9          # 小旗命中宽度（左右各 9px）
MIN_WIDTH = 260

# 片段条：只改条本身的高度，缝隙永远是 4px（PR 里加高轨道不会加宽间距）
BAND_H_MIN = 6
BAND_H_MAX = 64
BAND_H_DEFAULT = 20
BAND_GAP = 2            # 固定缝隙
STACK_TOP = 4           # 片段条「页面」的顶部留白（从顶部往下排，像 PR 的轨道列表）
SCROLL_X = 4            # 左侧竖滚动条的 x
SCROLL_W = 8            # 左侧竖滚动条的宽度
DRAG_LOCK_PX = 50       # 水平拖动死区：小于这个距离不动
RULER_MIN_PX = 4.0      # 刻度之间至少留几个像素
FRAME_MAX_PX = 20.0     # 拉到最宽时一帧占的像素（1px 刻度 + 19px 间隔）
RULER_STEPS = (1, 2, 5, 10, 15, 20, 25, 30, 50, 60, 100, 150, 300, 600, 900,
               1800, 2700, 3600, 7200, 18000, 36000, 90000)
SNAP_PX = 8             # 横向吸附半径（像素）
GAP_GRAVITY = 4.0       # 竖直「轨道缝隙」吸附的引力半径（px）

SEG_COLOR = QColor(WARN)
SEG_COLOR.setAlpha(205)
MARKER_COLOR = QColor("#45C995")
RANGE_COLOR = QColor(ACCENT)
RANGE_FILL = QColor(ACCENT)
RANGE_FILL.setAlpha(110)
PROGRESS_COLOR = QColor("#3B4A6B")
RULER_MINOR = QColor("#333B4C")
RULER_MAJOR = QColor("#525D73")
CURSOR_COLOR = QColor("#F5C96B")
FLAG_HOT = QColor("#B7C8FF")


class OverviewBar(QWidget):
    """一条覆盖整个视频的进度条（只发信号，不自己改数据）。"""

    seekRequested = Signal(int)          # 点/拖：光标帧
    inMoved = Signal(int)                # 拖入点
    outMoved = Signal(int)               # 拖出点
    editFinished = Signal(str)           # 松手："in" / "out"
    setInRequested = Signal(int)         # 右键：在这一帧设入点
    setOutRequested = Signal(int)        # 右键：在这一帧设出点
    clearRangeRequested = Signal()       # 右键：清除区间
    addSegmentRequested = Signal()       # 右键：用当前选区新建片段
    segmentActivated = Signal(int)
    segmentRemoveRequested = Signal(int)   # 右键删队列里的片段（Delete 删不掉）       # 点片段标记：选中该片段
    rangeActivated = Signal(int)         # 双击区间条：切当前段
    focusRequested = Signal()            # 点了底栏 → 把键盘焦点还给图墙
    bandsMoved = Signal(list)            # 拖动结束：[(kind, item, lo, hi, lane), …]
    markerActivated = Signal(int)        # 点绿色标记：跳到该帧
    addMarkerRequested = Signal(int)     # 右键：在这一帧加标记
    removeMarkerRequested = Signal(int)  # 右键：删掉这个标记
    deleteActiveRequested = Signal()     # Delete / 退格：删掉选中的片段或区间

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(MIN_BAR_H)
        self.setMouseTracking(True)
        # 不抢焦点：页面里的 M / I / O / 空格 等快捷键要留给墙上
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("点/拖：移动光标 · 拖小旗：改入出点 · "
                        "滚轮：左右滚动 · Ctrl+滚轮：翻层 · "
                        "Alt+滚轮：伸缩/单条高度 · Shift+滚轮：全体高度")

        self.fps = 25.0
        self.total = 0
        self.cursor_frame: int | None = None
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self._segments: list[tuple[float, float, str]] = []
        self._segment_items: list[dict] = []
        self._range_items: list[dict] = []
        self._segment_lanes: list[int] = []
        self._ranges: list[tuple[int, int, str, int, bool]] = []
        # 片段层里真正要画的东西：区间（彩色）+ 片段（琥珀），一起排层
        self._bands: list[dict] = []
        self.selection: set[tuple] = set()   # 选中的条（key = kind/lo/hi）
        self._rubber: QRectF | None = None   # 空白处框选
        self._rubber_origin = None
        self._scroll_drag = False            # 正在拖左侧滚动条
        self._scroll_grab = 0.0              # 抓住滑块的位置偏移
        self._scroll_hover = False
        self._press_band = None              # 按下时命中的条
        self._press_pos = None
        self._dragging = False
        self._drag_dx = 0.0
        self._drag_lane = 0
        self._drag_origins: list[dict] = []
        self._drag_alt = False
        self._drag_mouse_y = None
        self._drag_snap = None
        self.markers: list[int] = []
        self._drag: str | None = None
        self._scrubbing = False
        self._seek_drag = False          # 在轨道/刻度尺上按住拖 = 拖播放头
        self._hover: str | None = None
        # 时间轴伸缩：1.0 = 整段铺满（默认），越大窗口越窄
        self.zoom = 1.0
        self._view_start = 0.0          # 可见窗口左边缘（帧，浮点）
        self.band_scroll = 0            # 片段层上下翻到第几层
        self.band_h = BAND_H_DEFAULT     # 默认条高（Shift+滚轮改全体）
        self._band_hs: dict[tuple, int] = {}   # 单独调过高的条：key → 高度

    # ------------------------------------------------------------------ 数据
    def set_media(self, total_frames: int, fps: float = 25.0):
        total = max(0, int(total_frames or 0))
        fps = max(1e-6, float(fps or 25.0))
        if (total, fps) != (self.total, self.fps):
            self.total, self.fps = total, fps
            self.zoom, self._view_start = 1.0, 0.0
            self._rebuild_bands()
            self.update()

    def set_state(self, cursor_frame=None, in_frame=None, out_frame=None):
        before = (self.cursor_frame, self.in_frame, self.out_frame)
        self.cursor_frame = None if cursor_frame is None else int(cursor_frame)
        self.in_frame = None if in_frame is None else int(in_frame)
        self.out_frame = None if out_frame is None else int(out_frame)
        changed = before != (self.cursor_frame, self.in_frame, self.out_frame)
        moved = (self.cursor_frame is not None
                 and self.cursor_frame != before[0])
        if self.zoom > 1.0 and moved:
            # 放大后光标跑出窗口就跟着走（播放 / 跳帧时时间轴不会「停住」）；
            # 只是在别处缩放时不动，免得把刚放大的位置拽回光标处。
            visible = self._visible()
            start, end = self._view_start, self._view_start + visible
            if not (start <= self.cursor_frame <= end):
                self._view_start = self.cursor_frame - visible / 2.0
                self._clamp_view()
                changed = True
        if changed:
            self.update()

    def set_segments(self, segments):
        """segments: ``[{t_in 秒, t_end 秒, name, lane?}, …]``（和侧栏片段表同一份）。"""
        cleaned = []
        items = []
        for item in segments or ():
            if isinstance(item, dict):
                try:
                    start = float(item["t_in"])
                    end = float(item["t_end"])
                except (KeyError, TypeError, ValueError):
                    continue
                name = str(item.get("name") or "")
                keep = item
            else:
                try:
                    start, end = float(item[0]), float(item[1])
                except (TypeError, ValueError, IndexError):
                    continue
                name = str(item[2]) if len(item) > 2 and item[2] else ""
                keep = {"t_in": start, "t_end": end, "name": name}
            if end < start:
                start, end = end, start
            cleaned.append((max(0.0, start), max(0.0, end), name))
            keep["t_in"], keep["t_end"] = max(0.0, start), max(0.0, end)
            items.append(keep)
        if cleaned != self._segments:
            self._segments = cleaned
            self._segment_items = items
            self._rebuild_bands()
            self.update()

    def set_markers(self, markers):
        """M 键标记：单点帧号（和入点/出点无关）。"""
        cleaned = sorted({int(m) for m in (markers or ()) if int(m) >= 0})
        if cleaned != self.markers:
            self.markers = cleaned
            self.update()

    def set_ranges(self, spans, active: int = -1):
        """多段区间：``[{in 帧, out 帧, color, lane?}, …]``（也兼容旧的元组）。"""
        cleaned = []
        items = []
        for index, entry in enumerate(spans or ()):
            if isinstance(entry, dict):
                lo, hi = entry.get("in"), entry.get("out")
                if lo is None or hi is None:
                    continue
                lo, hi = int(min(lo, hi)), int(max(lo, hi))
                color = color_for(int(entry.get("color") or 0))
                lane = entry.get("lane")
                keep = entry
            else:
                try:
                    lo, hi, color, lane, _active = entry
                except (TypeError, ValueError):
                    continue
                lo, hi = int(lo), int(hi)
                if hi < lo:
                    continue
                keep = {"in": lo, "out": hi, "lane": lane}
            is_active = (index == active) if isinstance(active, int) and active >= 0 \
                else bool(entry.get("active")) if isinstance(entry, dict) else False
            cleaned.append((lo, hi, str(color), int(lane or 0), bool(is_active)))
            keep["in"], keep["out"] = lo, hi
            items.append(keep)
        self._ranges = cleaned
        self._range_items = items
        self._rebuild_bands()
        self.update()

    def _rebuild_bands(self):
        """把区间（彩色）和片段（琥珀）合成一张列表，一起排层。

        条目的 ``lane``（用户手动拖过）优先；没指定过的自动找第一条空的层。
        """
        bands = []
        for index, item in enumerate(self._range_items):
            bands.append({"lo": int(item["in"]), "hi": int(item["out"]),
                          "color": color_for(int(item.get("color") or 0)),
                          "active": bool(item.get("active")), "label": "",
                          "kind": "range", "index": index, "item": item})
        for index, item in enumerate(self._segment_items):
            bands.append({"lo": int(round(float(item["t_in"]) * self.fps)),
                          "hi": int(round(float(item["t_end"]) * self.fps)),
                          "color": SEG_COLOR.name(), "active": False,
                          "label": str(item.get("name") or ""),
                          "kind": "segment", "index": index, "item": item})
        placed: dict[int, list] = {}
        order = sorted(bands, key=lambda b: (b["lo"], b["hi"]))
        for band in order:
            lane = band["item"].get("lane")
            if lane is None:
                continue
            band["lane"] = int(lane)
            placed.setdefault(int(lane), []).append((band["lo"], band["hi"]))
        for band in order:
            if "lane" in band:
                continue
            lane = 0
            while True:
                spans = placed.get(lane, ())
                if all(band["hi"] <= lo or band["lo"] >= hi for lo, hi in spans):
                    break
                lane += 1
            band["lane"] = lane
            placed.setdefault(lane, []).append((band["lo"], band["hi"]))
        queued = {(int(b["lo"]), int(b["hi"]))
                  for b in bands if b["kind"] == "segment"}
        for band in bands:
            band["h"] = self._band_hs.get(self._band_key(band))
            if band["kind"] == "range":
                band["queued"] = (int(band["lo"]), int(band["hi"])) in queued
        self._bands = bands
        self._segment_lanes = [b["lane"] for b in bands if b["kind"] == "segment"]
        live = {self._band_key(b) for b in bands}
        self.selection &= live
        self._clamp_band_scroll()

    # ------------------------------------------------------------------ 几何
    def _track_left(self) -> float:
        return float(MARGIN)

    def _track_right(self) -> float:
        return float(max(MARGIN + 1, self.width() - MARGIN))

    def _usable(self) -> float:
        return max(1.0, self._track_right() - self._track_left())

    def _span(self) -> float:
        return float(max(1, self.total - 1))

    def _visible(self) -> float:
        """当前窗口覆盖多少帧（zoom=1 时就是整段）。"""
        return max(1.0, self._span() / max(1.0, float(self.zoom)))

    def _max_zoom(self) -> float:
        """最大放大：拉到最宽时**一帧正好占 FRAME_MAX_PX 像素**（跟着视频长度变）。"""
        usable = max(1.0, self._usable())
        return max(1.0, self._span() * FRAME_MAX_PX / usable)

    def _clamp_view(self):
        limit = max(0.0, self._span() - self._visible())
        self._view_start = max(0.0, min(limit, float(self._view_start)))
        if self.zoom <= 1.0:
            self.zoom, self._view_start = 1.0, 0.0

    def _x_for_frame(self, frame: float) -> float:
        frac = (float(frame) - self._view_start) / self._visible()
        return self._track_left() + frac * self._usable()

    def _frame_at_x(self, x: float) -> int:
        frac = (float(x) - self._track_left()) / self._usable()
        frac = max(0.0, min(1.0, frac))
        frame = self._view_start + frac * self._visible()
        return int(round(max(0.0, min(self._span(), frame))))

    def frame_at(self, x: float) -> int:
        """公开给测试/外部用：屏幕 x → 帧号。"""
        return self._frame_at_x(x)

    def visible_range(self) -> tuple[float, float]:
        """当前窗口的 [起帧, 止帧]（浮点，给绘制/提示用）。"""
        return self._view_start, self._view_start + self._visible()

    # ------------------------------------------------------------------ 缩放
    def set_zoom(self, zoom: float, anchor_frame: float | None = None):
        """把时间轴伸缩到 ``zoom`` 倍；``anchor_frame`` 尽量留在原来的横位置。"""
        if self._span() <= 1:
            self.zoom, self._view_start = 1.0, 0.0
            self.update()
            return
        old_visible = self._visible()
        anchor = (self._span() / 2.0 if anchor_frame is None
                  else float(anchor_frame))
        frac = 0.5
        if anchor_frame is not None:
            frac = (anchor - self._view_start) / old_visible
            frac = max(0.0, min(1.0, frac))
        self.zoom = max(1.0, min(self._max_zoom(), float(zoom)))
        self._view_start = anchor - frac * self._visible()
        self._clamp_view()
        self.update()

    def reset_zoom(self):
        self.zoom, self._view_start = 1.0, 0.0
        self.update()

    def _pan_to(self, start: float):
        self._view_start = float(start)
        self._clamp_view()
        self.update()

    def _zoom_notches(self, notches: int, x: float):
        """Ctrl+滚轮：以鼠标所在帧为锚点伸缩（每格 ×1.25 / ÷1.25）。"""
        if not notches:
            return
        frac = (float(x) - self._track_left()) / self._usable()
        frac = max(0.0, min(1.0, frac))
        anchor = self._view_start + frac * self._visible()
        factor = 1.25 ** int(notches)
        if self.zoom * factor <= 1.0001:
            self.reset_zoom()
            return
        self.set_zoom(self.zoom * factor, anchor)

    # ------------------------------------------------------------------ 片段层
    def band_height(self) -> int:
        """默认条高（没有单独调过的那条用它）。"""
        return max(BAND_H_MIN, min(BAND_H_MAX, int(self.band_h)))

    def band_gap(self) -> int:
        """相邻两条之间的缝隙：固定 2px（滚轮只改条高，不改缝）。"""
        return BAND_GAP

    def band_step(self) -> int:
        """一条 + 一条缝的总高（排版/拖动吸附都用它）。"""
        return self.band_height() + self.band_gap()

    def track_y(self) -> int:
        """轨道中线（跟着控件高度走：下面还有一行时间）。"""
        return max(20, self.height() - 18)

    @staticmethod
    def _band_key(band) -> tuple:
        return (str(band.get("kind")), int(band["lo"]), int(band["hi"]))

    def _band_h(self, band) -> int:
        """这一条自己的高度（单轨调过就用它，否则用默认）。"""
        value = band.get("h")
        if value is None:
            return self.band_height()
        return max(BAND_H_MIN, min(BAND_H_MAX, int(value)))

    def _layout(self) -> dict:
        """{lane: (top, height)}：从页面顶部往下累加，每条可以不一样高。"""
        heights: dict[int, int] = {}
        for band in self._bands:
            lane = int(band.get("lane", 0))
            heights[lane] = max(heights.get(lane, 0), self._band_h(band))
        out: dict[int, tuple[int, int]] = {}
        y = STACK_TOP
        for lane in sorted(heights):
            out[lane] = (y, heights[lane])
            y += heights[lane] + self.band_gap()
        return out

    def _scroll_offset(self) -> int:
        layout = self._layout()
        first = layout.get(int(self.band_scroll))
        return 0 if first is None else first[0] - STACK_TOP

    def lane_count(self) -> int:
        return max((int(b.get("lane", 0)) + 1 for b in self._bands), default=0)

    def stack_bottom(self) -> int:
        """片段条区域的下沿（时间轴小旗上方）。"""
        return max(STACK_TOP, self.track_y() - 18)

    def visible_lanes(self) -> int:
        layout = self._layout()
        limit = self.track_y() - 14
        count = 0
        for lane in sorted(layout):
            if layout[lane][0] < limit:
                count += 1
            else:
                break
        return max(1, count)

    def _max_band_scroll(self) -> int:
        """最多能滚到第几层：最后一条的底边正好贴住区域下沿。"""
        layout = self._layout()
        if not layout:
            return 0
        last = max(layout)
        need = (layout[last][0] + layout[last][1]) - (self.track_y() - 14)
        if need <= 0:
            return 0
        best = 0
        for lane in sorted(layout):
            if layout[lane][0] - STACK_TOP >= need:
                return lane
            best = lane
        return best

    def _clamp_band_scroll(self):
        limit = max(0, self._max_band_scroll())
        self.band_scroll = max(0, min(limit, int(self.band_scroll)))

    def scroll_bands(self, notches: int) -> bool:
        """普通滚轮 / Ctrl+滚轮：上下翻片段层（向上 = 往第 1 层方向）。"""
        before = self.band_scroll
        self.band_scroll = int(self.band_scroll) - int(notches)
        self._clamp_band_scroll()
        if self.band_scroll != before:
            self.update()
            return True
        return False

    def scale_bands(self, notches: int) -> bool:
        """Shift+滚轮：所有条一起加高 / 压低（缝隙 2px 不变）。"""
        before = self.band_height()
        self.band_h = int(self.band_h) + int(notches) * 2
        self.band_h = max(BAND_H_MIN, min(BAND_H_MAX, self.band_h))
        self._clamp_band_scroll()
        if self.band_height() != before:
            self.update()
            return True
        return False

    def scale_band(self, band, notches: int) -> bool:
        """Alt+滚轮（鼠标在某一条上）：只调这一条的高度。"""
        current = self._band_h(band)
        target = max(BAND_H_MIN, min(BAND_H_MAX, current + int(notches) * 2))
        if target == current:
            return False
        self._band_hs[self._band_key(band)] = target
        band["h"] = target
        self._clamp_band_scroll()
        self.update()
        return True

    def _lane_visible(self, lane: int) -> bool:
        entry = self._layout().get(int(lane))
        if entry is None:
            return False
        top = entry[0] - self._scroll_offset()
        bottom = top + entry[1]
        limit = self.track_y() - 14
        return bottom > STACK_TOP - 1 and top < limit

    def _band_rect(self, band):
        """这一条在控件里的矩形（x1/x2 已按可见窗口算好，y 随滚动）。"""
        entry = self._layout().get(int(band.get("lane", 0)))
        if entry is None:
            return None
        top = entry[0] - self._scroll_offset()
        height = self._band_h(band)
        x1 = self._x_for_frame(band["lo"])
        x2 = self._x_for_frame(band["hi"])
        if x2 < x1:
            x1, x2 = x2, x1
        return QRectF(x1, top, max(3.0, x2 - x1), height)

    # ------------------------------------------------------------------ 滚动条
    def _scroll_area(self):
        top = STACK_TOP
        bottom = self.track_y() - 16
        if bottom - top < 24:
            return None
        return top, bottom

    def _scroll_geometry(self):
        """(x, track_top, track_h, thumb_y, thumb_h)；只有装不下时才画。"""
        area = self._scroll_area()
        if area is None:
            return None
        lanes = self.lane_count()
        visible = self.visible_lanes()
        if lanes <= visible:
            return None
        top, bottom = area
        height = bottom - top
        layout = self._layout()
        last = max(layout) if layout else 0
        total_h = ((layout[last][0] + layout[last][1]) - STACK_TOP) if layout else height
        thumb_h = max(24, int(round(height * height / float(max(height, total_h)))))
        max_scroll = max(1, self._max_band_scroll())
        frac = max(0.0, min(1.0, int(self.band_scroll) / float(max_scroll)))
        thumb_y = top + int(round((height - thumb_h) * frac))
        return SCROLL_X, top, height, thumb_y, thumb_h

    def _scroll_hit(self, pos) -> bool:
        geo = self._scroll_geometry()
        if geo is None:
            return False
        x, top, height, _ty, _th = geo
        return (x - 2 <= pos.x() <= x + SCROLL_W + 2
                and top - 2 <= pos.y() <= top + height + 2)

    def _scroll_to(self, y: float, grab: float = 0.0):
        geo = self._scroll_geometry()
        if geo is None:
            return
        _x, top, height, _ty, thumb_h = geo
        max_scroll = max(1, self._max_band_scroll())
        span = max(1.0, float(height - thumb_h))
        frac = (float(y) - top - grab) / span
        self.band_scroll = int(round(max(0.0, min(1.0, frac)) * max_scroll))
        self._clamp_band_scroll()
        self.update()

    # ------------------------------------------------------------------ 帧刻度尺
    def ruler_step(self) -> int:
        """刻度间隔（帧）：放得越大刻度越密，够宽时就是「每帧一根」。"""
        ppf = self._usable() / max(1e-9, self._visible())
        for step in RULER_STEPS:
            if step * ppf >= RULER_MIN_PX:
                return int(step)
        return int(RULER_STEPS[-1])

    def _paint_ruler(self, painter: QPainter, left: float, right: float,
                     y: float):
        """底轨上方的帧刻度尺（类似 PR 的时间尺）。"""
        if self.total <= 1:
            return
        step = self.ruler_step()
        top = float(y) - TRACK_H / 2.0
        frame = int(self._view_start // step) * step
        end = self._view_start + self._visible()
        while frame <= end + step:
            x = self._x_for_frame(frame)
            if left - 1 <= x <= right + 1:
                n = frame // step
                if n % 10 == 0:
                    height, color = 7.0, RULER_MAJOR
                elif n % 5 == 0:
                    height, color = 4.5, RULER_MAJOR
                else:
                    height, color = 2.5, RULER_MINOR
                painter.setPen(QPen(color, 1))
                painter.drawLine(QPointF(x, top), QPointF(x, top - height))
            frame += step
        painter.setPen(Qt.NoPen)

    def _paint_scrollbar(self, painter):
        geo = self._scroll_geometry()
        if geo is None:
            return
        x, top, height, thumb_y, thumb_h = geo
        hot = self._scroll_hover or self._scroll_drag
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(27, 31, 39, 210))
        painter.drawRoundedRect(QRectF(x, top, SCROLL_W, height),
                                SCROLL_W / 2.0, SCROLL_W / 2.0)
        # 两端的小圆点（PR 同款的小端点）
        painter.setBrush(QColor("#4A5570" if hot else "#3A4150"))
        painter.drawEllipse(QPointF(x + SCROLL_W / 2.0, top + 4), 2.0, 2.0)
        painter.drawEllipse(QPointF(x + SCROLL_W / 2.0, top + height - 4), 2.0, 2.0)
        thumb = QColor(ACCENT if hot else "#4A5570")
        painter.setBrush(thumb)
        painter.drawRoundedRect(
            QRectF(x + 1, thumb_y, SCROLL_W - 2, thumb_h),
            (SCROLL_W - 2) / 2.0, (SCROLL_W - 2) / 2.0)

    def in_stack_area(self, y: int) -> bool:
        """y 落在片段条那块区域（轨道上方）？"""
        return int(y) < self.track_y() - 14

    def _lane_at(self, y: int) -> int | None:
        for lane, (top, height) in self._layout().items():
            ytop = top - self._scroll_offset()
            if ytop - 1 <= y <= ytop + height + 1:
                return lane
        return None

    # ------------------------------------------------------------------ 选择
    def is_selected(self, band) -> bool:
        return self._band_key(band) in self.selection

    def toggle_band(self, band):
        key = self._band_key(band)
        if key in self.selection:
            self.selection.discard(key)
        else:
            self.selection.add(key)
        self.update()

    def select_all(self) -> int:
        """选中片段层里的所有条（Ctrl+A）。"""
        self.selection = {self._band_key(b) for b in self._bands}
        self.update()
        return len(self.selection)

    def clear_selection(self):
        if self.selection:
            self.selection.clear()
            self.update()

    def selected_bands(self) -> list[dict]:
        """选中的条（区间 + 片段，原样返回 band 字典）。"""
        return [band for band in self._bands
                if self._band_key(band) in self.selection]

    def selected_ranges(self) -> list[tuple[int, int]]:
        """选中的「区间」条（用来加入导出队列）。"""
        out = []
        for band in self._bands:
            if band["kind"] != "range":
                continue
            if self._band_key(band) in self.selection:
                out.append((int(band["lo"]), int(band["hi"])))
        return out

    def _bands_in_rect(self, rect: QRectF) -> set:
        keys = set()
        for band in self._bands:
            if not self._lane_visible(int(band.get("lane", 0))):
                continue
            r = self._band_rect(band)
            if r is None:
                continue
            if rect.intersects(r):
                keys.add(self._band_key(band))
        return keys

    # ------------------------------------------------------------------ 拖动
    def _drag_anchor_lane(self) -> int:
        if self._press_band is None:
            return 0
        return int(self._press_band.get("lane", 0))

    def _begin_drag(self, band, pos):
        self._press_band = band
        self._press_pos = pos
        self._dragging = False
        self._drag_dx = 0.0
        self._drag_delta = 0
        self._drag_lane = int(band.get("lane", 0))
        self._drag_alt = False
        self._drag_snap = None
        origins = []
        for item in self._bands:
            if self.is_selected(item):
                origins.append({"kind": item["kind"], "item": item["item"],
                                "lo": int(item["lo"]), "hi": int(item["hi"]),
                                "lane": int(item["lane"])})
        if not origins:
            origins.append({"kind": band["kind"], "item": band["item"],
                            "lo": int(band["lo"]), "hi": int(band["hi"]),
                            "lane": int(band["lane"])})
        self._drag_origins = origins

    def _moving_items(self) -> set:
        return {id(o["item"]) for o in self._drag_origins}

    def _snap_frames(self) -> list[int]:
        moving = self._moving_items()
        frames = set()
        for band in self._bands:
            if id(band["item"]) in moving:
                continue
            frames.add(int(band["lo"]))
            frames.add(int(band["hi"]))
        if self.cursor_frame is not None:
            frames.add(int(self.cursor_frame))
        frames.update(int(m) for m in self.markers)
        return sorted(frames)

    def _horizontal_delta(self) -> int:
        if abs(self._drag_dx) <= DRAG_LOCK_PX:
            return 0
        start = self._frame_at_x(self._press_pos.x())
        cur = self._frame_at_x(self._press_pos.x() + self._drag_dx)
        delta = int(cur) - int(start)
        if not self._drag_alt:
            delta = self._snap_delta(delta)
        return delta

    def _snap_delta(self, delta: int) -> int:
        anchors = self._snap_frames()
        if not anchors:
            return delta
        frames_per_px = self._visible() / max(1.0, self._usable())
        tol = SNAP_PX * frames_per_px
        best = None
        for o in self._drag_origins:
            for edge in (o["lo"] + delta, o["hi"] + delta):
                for anchor in anchors:
                    diff = anchor - edge
                    if abs(diff) <= tol and (best is None or abs(diff) < abs(best)):
                        best = diff
        return delta + int(round(best or 0))

    def _drag_spans(self, delta: int) -> list[tuple[int, int]]:
        return [(o["lo"] + delta, o["hi"] + delta) for o in self._drag_origins]

    def _lane_occupancy(self) -> dict[int, list[tuple[int, int]]]:
        """除「正在拖的条」以外，每层被占的水平区间。"""
        moving = self._moving_items()
        out: dict[int, list[tuple[int, int]]] = {}
        for band in self._bands:
            if id(band["item"]) in moving:
                continue
            out.setdefault(int(band["lane"]), []).append(
                (int(band["lo"]), int(band["hi"])))
        return out

    @staticmethod
    def _spans_free(spans, occupied) -> bool:
        """目标区间在这一层是否**没有被占**（首尾紧贴 ``hi == lo`` 算空着）。"""
        for lo, hi in spans:
            for span_lo, span_hi in occupied or ():
                if lo < span_hi and hi > span_lo:     # 真·重叠（共用一帧不算）
                    return False
        return True

    def _vertical_candidates(self, delta: int) -> list[dict]:
        """竖直吸附点只有两种：

        * **轨道本身**（``insert=False``）—— 该层目标位置**空着**才可用；
        * **轨道之间的缝隙**（``insert=True``）—— 新建一层，落点重叠的条整体下移。

        目标层这一横段被别的片段占住时，「轨道本身」不可用，
        缝隙吸附点自然「扩展」成上半段 / 下半段两个选择。
        """
        layout = self._layout()
        if not layout:
            return []
        offset = self._scroll_offset()
        gap = float(self.band_gap())
        occupied = self._lane_occupancy()
        spans = self._drag_spans(delta)
        out: list[dict] = []
        for lane in sorted(layout):
            top, height = layout[lane]
            wtop = float(top - offset)
            if self._spans_free(spans, occupied.get(int(lane))):
                out.append({"lane": int(lane), "insert": False,
                            "top": wtop, "height": float(height),
                            "y": wtop + height / 2.0, "gravity": 0.0})
            nxt = lane + 1
            if nxt in layout:
                n_top, n_h = layout[nxt]
                gtop = wtop + height / 2.0
                gheight = height / 2.0 + gap + n_h / 2.0
                gy = wtop + height + gap / 2.0
            else:
                gtop = wtop + height + gap + 1.0
                gheight = float(height)
                gy = gtop + gheight / 2.0
            out.append({"lane": int(nxt), "insert": True,
                        "top": gtop, "height": gheight,
                        "y": gy, "gravity": GAP_GRAVITY})
        return out

    def _vertical_snap(self, y: float, delta: int) -> dict | None:
        best = None
        best_score = None
        for cand in self._vertical_candidates(delta):
            score = abs(float(y) - cand["y"]) + cand["gravity"]
            if best_score is None or score < best_score:
                best, best_score = cand, score
        return best

    def _update_drag(self, pos, alt: bool):
        try:
            alt = alt or bool(QApplication.keyboardModifiers() & Qt.AltModifier)
        except (AttributeError, RuntimeError):
            pass
        # Alt 只关掉「横向」吸附；竖直永远只吸轨道 / 轨道缝隙
        self._drag_alt = bool(alt)
        self._drag_mouse_y = pos.y()
        self._drag_dx = pos.x() - self._press_pos.x()
        delta = self._horizontal_delta()
        snap = self._vertical_snap(pos.y(), delta)
        if snap is None:
            snap = {"lane": self._drag_anchor_lane(), "insert": False,
                    "top": float(STACK_TOP), "height": float(self.band_height()),
                    "y": float(STACK_TOP), "gravity": 0.0}
        self._drag_snap = snap
        self._drag_delta = delta
        self._drag_lane = int(snap["lane"])

    def ghost_rects(self) -> list[tuple[int, int, float, float]]:
        """落点虚影：``[(lo, hi, 顶边, 高), …]``（跟着竖直吸附点，不跟鼠标像素）。"""
        snap = self._drag_snap
        if not self._dragging or not self._drag_origins or snap is None:
            return []
        step = max(1, self.band_step())
        out = []
        for lo, hi, lane, _kind in self.drag_preview():
            top = float(snap["top"]) + (int(lane) - int(self._drag_lane)) * step
            out.append((lo, hi, top, float(snap["height"])))
        return out

    def drag_preview(self) -> list:
        """拖动中的虚影落点：``[(lo, hi, lane, kind), …]``"""
        if not self._dragging or not self._drag_origins:
            return []
        shift = self._drag_lane - self._drag_anchor_lane()
        return [(o["lo"] + self._drag_delta, o["hi"] + self._drag_delta,
                 o["lane"] + shift, o["kind"]) for o in self._drag_origins]


    def _handle_at(self, pos) -> str | None:
        """鼠标在入/出点小旗上就返回 ``"in"`` / ``"out"``。"""
        # 只在小旗那一带（轨道上下）认，别把片段层的点击抢走
        if pos.y() < self.track_y() - 14 or pos.y() > self.height() - 10:
            return None
        best = None
        for kind, frame in (("in", self.in_frame), ("out", self.out_frame)):
            if frame is None:
                continue
            x = self._x_for_frame(frame)
            dist = abs(pos.x() - x)
            if dist <= HANDLE_HIT and (best is None or dist < best[0]):
                best = (dist, kind)
        return best[1] if best else None

    def _band_at(self, pos):
        """点在哪个条上（区间条 / 片段条都算），返回那条 dict。"""
        for band in self._bands:
            if not self._lane_visible(int(band.get("lane", 0))):
                continue
            rect = self._band_rect(band)
            if rect is None:
                continue
            if rect.adjusted(-3, 0, 3, 0).contains(pos):
                return band
        return None

    def _segment_at(self, pos) -> int | None:
        band = self._band_at(pos)
        if band is not None and band.get("kind") == "segment":
            return int(band["index"])
        return None

    def _marker_at(self, pos, strict_y: bool = True) -> int | None:
        """鼠标落在绿色标记附近就返回帧号（strict_y=False 时只看横向）。"""
        if strict_y and not (self.track_y() - 10 <= pos.y() <= self.track_y() - 1):
            return None
        for frame in self.markers:
            x = self._x_for_frame(frame)
            if abs(pos.x() - x) <= 5:
                return int(frame)
        return None

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, _event):             # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        left, right = self._track_left(), self._track_right()
        y = self.track_y()
        painter.setPen(Qt.NoPen)

        # 片段层：区间（彩色）+ 片段（琥珀）都按重叠分层；每条可以单独调高
        limit = self.track_y() - 14
        for band in self._bands:
            if not self._lane_visible(int(band.get("lane", 0))):
                continue
            rect = self._band_rect(band)
            if rect is None:
                continue
            x1 = max(left, rect.left())
            x2 = min(right, rect.right())
            if x2 < left or x1 > right:
                continue
            if rect.bottom() < 0 or rect.top() > limit:
                continue
            height = rect.height()
            color = QColor(band["color"])
            if band["kind"] == "range" and not band["active"]:
                color.setAlpha(205)
            body = QRectF(x1, rect.top(), max(3.0, x2 - x1), height)
            painter.setBrush(color)
            painter.drawRect(body)          # 方角
            if band["active"] or self.is_selected(band):
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor("#FFFFFF"), 2 if self.is_selected(band) else 1))
                painter.drawRect(body.adjusted(0.5, 0.5, -0.5, -0.5))
                painter.setPen(Qt.NoPen)
            if height >= 16 and (x2 - x1) >= 46:
                font = painter.font()
                font.setPointSizeF(7.5)
                painter.setFont(font)
                painter.setPen(QColor("#20180A"))
                text = f"#{band['index'] + 1}"
                if band["kind"] == "segment":
                    text = "🔒 " + text
                elif band.get("queued"):
                    text = "✓ " + text
                if band.get("label"):
                    text += f" {band['label']}"
                painter.drawText(
                    QRectF(x1 + 5, rect.top(), x2 - x1 - 10, height),
                    Qt.AlignVCenter | Qt.AlignLeft, text)
                painter.setPen(Qt.NoPen)

        # 拖动中的虚影：目标位置半透明 + 虚线边
        snap = self._drag_snap
        if self._dragging and snap is not None:
            # 目标轨道：压暗一点点，一眼看出会落在哪一层
            lane_top = float(snap["top"])
            lane_h = float(snap["height"])
            painter.setBrush(QColor(255, 255, 255, 16))
            painter.setPen(QPen(QColor(255, 255, 255, 44), 1, Qt.DashLine))
            painter.drawRect(QRectF(left, lane_top,
                                    max(0.0, right - left), lane_h))
            accent = QColor(ACCENT)
            accent.setAlpha(140)
            painter.setPen(Qt.NoPen)
            painter.setBrush(accent)
            painter.drawRect(QRectF(left, lane_top, 3.0, lane_h))
            for lo, hi, top, height in self.ghost_rects():
                x1 = max(left, self._x_for_frame(lo))
                x2 = min(right, self._x_for_frame(hi))
                if x2 - x1 < 1:
                    continue
                painter.setBrush(QColor(255, 255, 255, 64))
                painter.setPen(QPen(QColor(255, 255, 255, 180), 1, Qt.DashLine))
                painter.drawRect(QRectF(x1, top, x2 - x1, height))
                painter.setPen(Qt.NoPen)


        self._paint_scrollbar(painter)

        # 帧刻度尺（PR 风格：越放大刻度越密）
        self._paint_ruler(painter, left, right, y)

        # 底轨
        painter.setBrush(QColor(PANEL2))
        painter.drawRoundedRect(
            QRectF(left, y - TRACK_H / 2, right - left, TRACK_H), 3, 3)

        # 已走的部分（光标左侧，裁到可见窗口）
        if self.cursor_frame is not None and self.total > 0:
            cx = min(right, self._x_for_frame(self.cursor_frame))
            if cx > left:
                painter.setBrush(PROGRESS_COLOR)
                painter.drawRoundedRect(
                    QRectF(left, y - TRACK_H / 2, max(2.0, cx - left),
                           TRACK_H), 3, 3)

        # 轨道上只标「当前段」一条细线：其余区间都在上面的片段层里画成条
        for lo, hi, color, _lane, active in self._ranges:
            if not active:
                continue
            x1 = max(left, self._x_for_frame(lo))
            x2 = min(right, self._x_for_frame(hi))
            if x2 > x1:
                painter.setPen(QPen(QColor(color), 4))
                painter.drawLine(QPointF(x1, y), QPointF(x2, y))

        # M 键标记：轨道上方绿色短竖线
        for frame in self.markers:
            x = self._x_for_frame(frame)
            if not (left - 1 <= x <= right + 1):
                continue
            painter.setPen(QPen(MARKER_COLOR, 2))
            painter.drawLine(QPointF(x, y - 7), QPointF(x, y - 3))

        # 两端小旗（不在窗口里就不画，免得跑到留白上）
        if self.in_frame is not None:
            x = self._x_for_frame(self.in_frame)
            if left - 6 <= x <= right + 6:
                self._paint_flag(painter, x, "in",
                                 self._hover == "in" or self._drag == "in")
        if self.out_frame is not None:
            x = self._x_for_frame(self.out_frame)
            if left - 6 <= x <= right + 6:
                self._paint_flag(painter, x, "out",
                                 self._hover == "out" or self._drag == "out")

        # 放大后：窗口两端的小箭头，提示「两边还有内容」
        if self.zoom > 1.0:
            painter.setPen(QPen(QColor(DIM), 1.5))
            start, end = self.visible_range()
            if start > 0.5:
                painter.drawPolyline(QPolygonF([
                    QPointF(left - 11, y - 4), QPointF(left - 7, y),
                    QPointF(left - 11, y + 4)]))
            if end < self._span() - 0.5:
                painter.drawPolyline(QPolygonF([
                    QPointF(right + 11, y - 4), QPointF(right + 7, y),
                    QPointF(right + 11, y + 4)]))

        # 光标：竖线 + 小圆点（只在窗口里才画）
        if self.cursor_frame is not None and self.total > 0:
            cx = self._x_for_frame(self.cursor_frame)
            if left - 1 <= cx <= right + 1:
                painter.setPen(QPen(CURSOR_COLOR, 1.5))
                painter.drawLine(QPointF(cx, y - 12), QPointF(cx, y + 7))
                painter.setPen(Qt.NoPen)
                painter.setBrush(CURSOR_COLOR)
                painter.drawEllipse(QPointF(cx, y), 4.0, 4.0)

        # 底部时间：左「总进度 / 片段层」，右「当前 / 总长」
        font = painter.font()
        font.setPointSizeF(8.0)
        painter.setFont(font)
        painter.setPen(QPen(QColor(DIM), 1))
        label = "总进度"
        if self.zoom > 1.0:
            label = f"总进度 ×{self.zoom:.1f}"
        lanes = self.lane_count()
        if lanes or self._ranges:
            label += f" · 条高 {self.band_height()}px"
        visible = self.visible_lanes()
        if lanes > visible:
            first = self.band_scroll + 1
            last = min(lanes, self.band_scroll + visible)
            label += f" · 片段 {lanes} 层（看第 {first}-{last} 层，滚轮翻）"
        if self.width() >= 520:
            painter.drawText(QRectF(left, self.height() - 12, 240, 12),
                             Qt.AlignLeft | Qt.AlignVCenter, label)
        if self.total > 0:
            current = (self.cursor_frame or 0) / self.fps
            total_s = max(0, self.total - 1) / self.fps
            text = f"{fmt_tc(current)} / 总长 {fmt_tc(total_s)}"
        else:
            text = "没有视频"
        painter.drawText(QRectF(left, self.height() - 12, right - left, 12),
                         Qt.AlignRight | Qt.AlignVCenter, text)
        if self._rubber is not None:
            painter.setPen(QPen(QColor(ACCENT), 1))
            painter.setBrush(QColor(255, 255, 255, 24))
            painter.drawRect(self._rubber)
        painter.end()

    def _paint_flag(self, painter: QPainter, x: float, kind: str, hot: bool):
        track = self.track_y()
        color = FLAG_HOT if hot else RANGE_COLOR
        painter.setPen(QPen(color, 2 if hot else 1))
        painter.drawLine(QPointF(x, track + 3), QPointF(x, track - 12))
        span = 9 if hot else 8
        top = track - 12
        if kind == "in":
            points = [QPointF(x, top), QPointF(x + span, top + 5),
                      QPointF(x, top + 10)]
        else:
            points = [QPointF(x, top), QPointF(x - span, top + 5),
                      QPointF(x, top + 10)]
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))

    # ------------------------------------------------------------------ 鼠标
    @staticmethod
    def _notches(event) -> int:
        """滚轮增量 → 「几格」：普通鼠标 120/格，高精度滚轮 / 横向滚动 40/格。"""
        angle = event.angleDelta()
        amount = angle.y() or angle.x()
        unit = 120
        if not amount:
            pixels = event.pixelDelta()
            amount = pixels.y() or pixels.x()
            unit = 40
        if not amount:
            return 0
        count = int(round(abs(amount) / unit))
        if count < 1:
            return 0
        return count if amount > 0 else -count

    def wheelEvent(self, event):              # noqa: N802
        """Ctrl+滚轮 = 横向伸缩；Alt+滚轮 = 片段层「带高 + 缝隙」一起放大/缩小。"""
        notches = self._notches(event)
        if not notches:
            super().wheelEvent(event)
            return
        mods = event.modifiers()
        pos = event.position().toPoint()
        if mods & Qt.ControlModifier:
            # 和 PR 一样：Ctrl+滚轮 = 上下移动（翻片段层）
            self.scroll_bands(notches)
            event.accept()
            return
        if mods & Qt.AltModifier:
            band = self._band_at(pos)
            if band is not None:
                # 鼠标在具体某一条上：只调这一条的高度（单轨垂直伸缩）
                self.scale_band(band, notches)
            else:
                # 空白处：时间轴水平伸缩
                self._zoom_notches(notches, pos.x())
            event.accept()
            return
        if mods & Qt.ShiftModifier:
            # 和 PR 一样：Shift+滚轮 = 垂直伸缩（所有条一起变高/变矮）
            self.scale_bands(notches)
            event.accept()
            return
        # 普通滚轮：时间轴左右滚动（和 PR 一致；放大后才有得滚）
        if self.zoom > 1.0:
            self._pan_to(self._view_start - notches * self._visible() * 0.1)
        event.accept()

    def keyPressEvent(self, event):           # noqa: N802
        """选中片段/区间后按 Delete 或退格就能删。"""
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.deleteActiveRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):   # noqa: N802
        """双击某条 = 跳到那一段；双击空白 = 回到整段。"""
        if event.button() != Qt.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        band = self._band_at(event.position().toPoint())
        if band is not None:
            if band.get("kind") == "range":
                self.rangeActivated.emit(int(band["index"]))
            else:
                self.segmentActivated.emit(int(band["index"]))
            event.accept()
            return
        self.reset_zoom()
        self.band_scroll = 0
        self.update()
        event.accept()

    def mousePressEvent(self, event):         # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self.focusRequested.emit()          # 键盘焦点还给图墙（M/I/O/回车…）
        pos = event.position().toPoint()
        if self._scroll_hit(pos):
            geo = self._scroll_geometry()
            _x, top, _h, thumb_y, thumb_h = geo
            if thumb_y <= pos.y() <= thumb_y + thumb_h:
                self._scroll_grab = pos.y() - thumb_y      # 抓住滑块拖
            else:
                self._scroll_grab = thumb_h / 2.0          # 点轨道：跳到那儿
                self._scroll_to(pos.y(), self._scroll_grab)
            self._scroll_drag = True
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        kind = self._handle_at(pos)
        if kind:
            self._drag = kind
            self._hover = kind
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        band = self._band_at(pos)
        if band is not None:
            if event.modifiers() & Qt.ShiftModifier:
                # 只有 Shift+左键 才能自由多选（再点一次取消）
                self.toggle_band(band)
            else:
                # 按在「已选中的条」上 → 保留整组选择，多条一起拖；
                # 按在没选中的条上 → 清掉之前的，只选这一条
                if self._band_key(band) not in self.selection:
                    self.selection = {self._band_key(band)}
                self._begin_drag(band, pos)
            self.update()
            event.accept()
            return
        marker = self._marker_at(pos)
        if marker is not None:
            self.markerActivated.emit(marker)
            event.accept()
            return
        # 轨道 / 刻度尺那一条（含下面一点）= 按住拖播放头（PR 手感）
        if pos.y() >= self.track_y() - 14:
            self._seek_drag = True
            self.setCursor(Qt.SizeHorCursor)
            self.seekRequested.emit(self._frame_at_x(pos.x()))
            self.update()
            event.accept()
            return
        # 片段层空白处：先不跳，松手时如果是「单击」再跳；拖动 = 框选
        self._rubber_origin = pos
        self._rubber = QRectF(pos, pos)
        self._scrubbing = True
        event.accept()

    def mouseMoveEvent(self, event):          # noqa: N802
        pos = event.position().toPoint()
        if self._press_band is not None:
            if not self._dragging:
                if (pos - self._press_pos).manhattanLength() < 4:
                    return
                self._dragging = True
                self.setCursor(Qt.ClosedHandCursor)
            self._update_drag(pos, bool(event.modifiers() & Qt.AltModifier))
            self.update()
            event.accept()
            return
        if self._scroll_drag:
            self._scroll_to(pos.y(), self._scroll_grab)
            event.accept()
            return
        hover = self._scroll_hit(pos)
        if hover != self._scroll_hover:
            self._scroll_hover = hover
            self.setCursor(Qt.OpenHandCursor if hover else Qt.ArrowCursor)
            self.update()
        if self._drag:
            frame = self._frame_at_x(pos.x())
            if self._drag == "in":
                self.inMoved.emit(frame)
            else:
                self.outMoved.emit(frame)
            event.accept()
            return
        if self._seek_drag:
            self.seekRequested.emit(self._frame_at_x(pos.x()))
            event.accept()
            return
        if self._scrubbing and self._rubber is not None:
            origin = self._rubber_origin
            self._rubber = QRectF(origin, pos).normalized()
            self.update()
            event.accept()
            return
        kind = self._handle_at(pos)
        if kind != self._hover:
            self._hover = kind
            self.setCursor(Qt.SizeHorCursor if kind else Qt.ArrowCursor)
            self.update()
        if kind is None:
            marker = self._marker_at(pos)
            if marker is not None:
                self.setCursor(Qt.PointingHandCursor)
            elif self._band_at(pos) is not None:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        self._update_tooltip(pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):       # noqa: N802
        if self._press_band is not None:
            dragging = self._dragging
            if dragging:
                # 松手这一刻再按当前 Alt 状态算一次（放开 Alt 立刻吸附）
                self._update_drag(event.position().toPoint(),
                                  bool(event.modifiers() & Qt.AltModifier))
            anchor_lane = self._drag_anchor_lane()
            if not dragging:
                # 只是点了一下（没拖）：收敛成单选
                self.selection = {self._band_key(self._press_band)}
            self._press_band = None
            self._dragging = False
            self._drag_mouse_y = None
            self.setCursor(Qt.ArrowCursor)
            if dragging and (self._drag_delta != 0
                             or self._drag_lane != anchor_lane):
                shift = self._drag_lane - anchor_lane
                moved = []
                for o in self._drag_origins:
                    lo = int(o["lo"] + self._drag_delta)
                    hi = int(o["hi"] + self._drag_delta)
                    if hi <= lo:
                        continue
                    moved.append((o, lo, hi, int(o["lane"] + shift)))
                moved_items = {id(o["item"]) for o in self._drag_origins}
                insert = bool(self._drag_snap and self._drag_snap.get("insert"))
                # 插入新层：同层及以下、和落点重叠的条整体往下让一层
                pushed: dict[int, dict] = {}
                for band in (self._bands if insert else ()):
                    if id(band["item"]) in moved_items:
                        continue
                    lane = int(band["lane"])
                    for _o, lo, hi, target in moved:
                        if lane >= target and not (band["hi"] < lo
                                                   or band["lo"] > hi):
                            pushed[id(band["item"])] = band
                            break
                moves = [(o["kind"], o["item"], lo, hi, lane)
                         for o, lo, hi, lane in moved]
                for band in pushed.values():
                    moves.append((band["kind"], band["item"],
                                  int(band["lo"]), int(band["hi"]),
                                  int(band["lane"]) + 1))
                self._drag_origins = []
                self._drag_delta = 0
                self._drag_snap = None
                self.update()
                if moves:
                    self.bandsMoved.emit(moves)
                event.accept()
                return
            self._drag_origins = []
            self._drag_delta = 0
            self._drag_snap = None
            self.update()
            event.accept()
            return
        if self._scroll_drag:
            self._scroll_drag = False
            self.setCursor(Qt.OpenHandCursor if self._scroll_hover
                           else Qt.ArrowCursor)
            self.update()
            event.accept()
            return
        if self._drag:
            kind, self._drag = self._drag, None
            self._hover = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            self.editFinished.emit(kind)
            event.accept()
            return
        if self._seek_drag:
            self._seek_drag = False
            self._rubber = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            event.accept()
            return
        if self._scrubbing:
            self._scrubbing = False
            rect, self._rubber = self._rubber, None
            self._rubber_origin = None
            if rect is not None and (rect.width() > 3 or rect.height() > 3):
                # 框选：替换成框里的条
                self.selection = self._bands_in_rect(rect)
            else:
                # 单击空白：清掉选择；**只有点时间轴那条才移动光标**，
                # 点片段层里的空白不动光标
                pos = event.position().toPoint()
                self.clear_selection()
                if not self.in_stack_area(pos.y()):
                    self.seekRequested.emit(self._frame_at_x(pos.x()))
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):              # noqa: N802
        if self._scroll_hover:
            self._scroll_hover = False
            self.update()
        if self._drag is None and self._scrubbing is False:
            self._hover = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
        super().leaveEvent(event)

    def _update_tooltip(self, pos):
        if self.total <= 0:
            self.setToolTip("没有视频")
            return
        frame = self._frame_at_x(pos.x())
        lines = [f"第 {frame} 帧 · {fmt_tc(frame / self.fps)}"]
        if self.zoom > 1.0:
            start, end = self.visible_range()
            lines.append(f"时间轴 ×{self.zoom:.1f}：{fmt_tc(start / self.fps)} → "
                         f"{fmt_tc(end / self.fps)}")
            lines.append("Ctrl+滚轮伸缩 · 普通滚轮平移 · 双击回到整段")
        if self.lane_count() > self.visible_lanes():
            lines.append(f"片段 {self.lane_count()} 层，当前从第 "
                         f"{self.band_scroll + 1} 层起")
            lines.append("Alt+滚轮调层高 · 普通滚轮翻层 · 拖上边框改整块高度")
        if self.in_frame is not None and self.out_frame is not None:
            lo, hi = sorted((self.in_frame, self.out_frame))
            if lo <= frame <= hi:
                lines.append(f"在当前段里（{hi - lo + 1} 帧）")
        if self.in_frame is not None:
            lines.append(f"入点 #{self.in_frame} {fmt_tc(self.in_frame / self.fps)}")
        if self.out_frame is not None:
            lines.append(f"出点 #{self.out_frame} {fmt_tc(self.out_frame / self.fps)}")
        for lo, hi, _color, _lane, active in self._ranges:
            if min(lo, hi) <= frame <= max(lo, hi):
                tag = "（当前段）" if active else ""
                lines.append(f"区间 {fmt_tc(lo / self.fps)} → "
                             f"{fmt_tc(hi / self.fps)}{tag}")
                break
        index = self._segment_at(pos)
        if index is not None and 0 <= index < len(self._segments):
            start, end, name = self._segments[index]
            label = f"片段「{name}」" if name else f"片段 #{index + 1}"
            lines.append(f"{label}：{fmt_tc(start)} → {fmt_tc(end)}")
        for frame in self.markers:
            if abs(pos.x() - self._x_for_frame(frame)) <= 5:
                lines.append(f"标记：第 {frame} 帧 · {fmt_tc(frame / self.fps)}")
                break
        self.setToolTip("\n".join(lines))

    # ------------------------------------------------------------------ 右键
    def contextMenuEvent(self, event):        # noqa: N802
        if self.total <= 0:
            return
        pos = event.pos()
        frame = self._frame_at_x(pos.x())
        menu = QMenu(self)
        act_in = menu.addAction(f"在这里设入点（第 {frame} 帧）")
        act_out = menu.addAction(f"在这里设出点（第 {frame} 帧）")
        menu.addSeparator()
        act_clear = menu.addAction("清除入点/出点")
        act_clear.setEnabled(self.in_frame is not None or self.out_frame is not None)
        act_seg = menu.addAction("用当前选区新建片段")
        act_seg.setEnabled(self.in_frame is not None and self.out_frame is not None
                           and self.out_frame > self.in_frame)
        menu.addSeparator()
        marker = self._marker_at(pos, strict_y=False)
        act_mark = menu.addAction(f"在这里添加标记（第 {frame} 帧）")
        act_unmark = None
        if marker is not None:
            act_unmark = menu.addAction(f"删除标记（第 {marker} 帧）")
        index = self._segment_at(pos)
        act_jump = None
        act_segdel = None
        if index is not None:
            menu.addSeparator()
            act_jump = menu.addAction(f"选中片段 #{index + 1}")
            act_segdel = menu.addAction(
                f"从导出队列删除片段 #{index + 1}（Delete 删不掉）")
        menu.addSeparator()
        act_seek = menu.addAction("光标跳到这里")
        menu.addSeparator()
        act_zin = menu.addAction("时间轴放大（也可以 Ctrl+滚轮向上）")
        act_zout = menu.addAction("时间轴缩小（也可以 Ctrl+滚轮向下）")
        act_zfit = menu.addAction("显示整段（双击时间轴）")
        act_zfit.setEnabled(self.zoom > 1.0 or self.band_scroll > 0)
        act_band_big = menu.addAction("片段条加高（Alt+滚轮向上）")
        act_band_small = menu.addAction("片段条压低（Alt+滚轮向下）")
        act_band_big.setEnabled(self.band_height() < BAND_H_MAX)
        act_band_small.setEnabled(self.band_height() > BAND_H_MIN)
        act_lane_up = menu.addAction("片段层往上看（普通滚轮向上）")
        act_lane_down = menu.addAction("片段层往下看（普通滚轮向下）")
        act_lane_up.setEnabled(
            self.band_scroll < self.lane_count() - self.visible_lanes())
        act_lane_down.setEnabled(self.band_scroll > 0)
        chosen = menu.exec(event.globalPos())
        if chosen is act_in:
            self.setInRequested.emit(frame)
        elif chosen is act_out:
            self.setOutRequested.emit(frame)
        elif chosen is act_clear:
            self.clearRangeRequested.emit()
        elif chosen is act_seg:
            self.addSegmentRequested.emit()
        elif chosen is act_mark:
            self.addMarkerRequested.emit(frame)
        elif act_unmark is not None and chosen is act_unmark:
            self.removeMarkerRequested.emit(marker)
        elif act_jump is not None and chosen is act_jump:
            self.segmentActivated.emit(index)
        elif act_segdel is not None and chosen is act_segdel:
            self.segmentRemoveRequested.emit(int(index))
        elif chosen is act_seek:
            self.seekRequested.emit(frame)
        elif chosen is act_zin:
            self._zoom_notches(1, pos.x())
        elif chosen is act_zout:
            self._zoom_notches(-1, pos.x())
        elif chosen is act_zfit:
            self.reset_zoom()
            self.band_scroll = 0
            self.update()
        elif chosen is act_band_big:
            self.scale_bands(1)
        elif chosen is act_band_small:
            self.scale_bands(-1)
        elif chosen is act_lane_up:
            self.scroll_bands(1)
        elif chosen is act_lane_down:
            self.scroll_bands(-1)

    # ------------------------------------------------------------------ 其它
    def resizeEvent(self, event):             # noqa: N802
        """高度变了（拖上边框 / 窗口缩放）要重新收敛片段层。"""
        super().resizeEvent(event)
        self._clamp_band_scroll()
        self.update()

    def sizeHint(self) -> QSize:              # noqa: N802
        return QSize(800, DEFAULT_BAR_H)

    def minimumSizeHint(self) -> QSize:       # noqa: N802
        return QSize(MIN_WIDTH, MIN_BAR_H)
