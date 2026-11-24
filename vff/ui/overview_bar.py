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
from PySide6.QtWidgets import QMenu, QWidget

from ..util import fmt_tc
from .range_palette import assign_lanes
from .style import ACCENT, DIM, PANEL2, WARN
from .wheel_util import wheel_direction

BAR_H = 62              # 控件总高（片段层 + 轨道 + 下面一行时间）
MARGIN = 16             # 左右留白（贴边不好点）
TRACK_Y = 44            # 轨道中线
TRACK_H = 6             # 轨道粗细
HANDLE_HIT = 9          # 小旗命中宽度（左右各 9px）
MIN_WIDTH = 260

# 片段层：从 STACK_BOTTOM 往上叠，一层 6px、间隔 1px
LANE_H = 6
LANE_GAP = 1
STACK_BOTTOM = 26       # 第 0 层带的底边
VISIBLE_LANES = 4       # 一屏能看到的层数（多的靠 Alt+滚轮翻）

SEG_COLOR = QColor(WARN)
SEG_COLOR.setAlpha(205)
MARKER_COLOR = QColor("#45C995")
RANGE_COLOR = QColor(ACCENT)
RANGE_FILL = QColor(ACCENT)
RANGE_FILL.setAlpha(110)
PROGRESS_COLOR = QColor("#3B4A6B")
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
    segmentActivated = Signal(int)       # 点片段标记：选中该片段
    markerActivated = Signal(int)        # 点绿色标记：跳到该帧
    addMarkerRequested = Signal(int)     # 右键：在这一帧加标记
    removeMarkerRequested = Signal(int)  # 右键：删掉这个标记

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(BAR_H)
        self.setMaximumHeight(BAR_H)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("点/拖：移动光标 · 拖小旗：改入出点 · "
                        "Ctrl+滚轮：伸缩 · Alt+滚轮：翻片段层 · 右键：更多")

        self.fps = 25.0
        self.total = 0
        self.cursor_frame: int | None = None
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self._segments: list[tuple[float, float, str]] = []
        self._segment_lanes: list[int] = []
        self._ranges: list[tuple[int, int, str, int, bool]] = []
        self.markers: list[int] = []
        self._drag: str | None = None
        self._scrubbing = False
        self._hover: str | None = None
        # 时间轴伸缩：1.0 = 整段铺满（默认），越大窗口越窄
        self.zoom = 1.0
        self._view_start = 0.0          # 可见窗口左边缘（帧，浮点）
        self.band_scroll = 0            # 片段层上下翻到第几层

    # ------------------------------------------------------------------ 数据
    def set_media(self, total_frames: int, fps: float = 25.0):
        total = max(0, int(total_frames or 0))
        fps = max(1e-6, float(fps or 25.0))
        if (total, fps) != (self.total, self.fps):
            self.total, self.fps = total, fps
            self.zoom, self._view_start = 1.0, 0.0
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
        """segments: ``[(t_in 秒, t_end 秒, 名称), …]``（和侧栏片段表同一份）。"""
        cleaned: list[tuple[float, float, str]] = []
        for item in segments or ():
            try:
                start, end = float(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            name = str(item[2]) if len(item) > 2 and item[2] else ""
            cleaned.append((max(0.0, min(start, end)), max(start, end), name))
        if cleaned != self._segments:
            self._segments = cleaned
            self._segment_lanes = assign_lanes(
                [(int(round(a * self.fps)), int(round(b * self.fps)))
                 for a, b, _n in cleaned])
            self._clamp_band_scroll()
            self.update()

    def set_markers(self, markers):
        """M 键标记：单点帧号（和入点/出点无关）。"""
        cleaned = sorted({int(m) for m in (markers or ()) if int(m) >= 0})
        if cleaned != self.markers:
            self.markers = cleaned
            self.update()

    def set_ranges(self, spans):
        """多段区间：``[(起帧, 止帧, 颜色, 层, 是否当前段), …]``。"""
        cleaned = [(int(lo), int(hi), str(color), int(lane), bool(active))
                   for lo, hi, color, lane, active in (spans or ())
                   if int(hi) >= int(lo)]
        if cleaned != self._ranges:
            self._ranges = cleaned
            self.update()

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
        # 最窄也留 4 帧，再窄就没意义了
        return max(1.0, min(64.0, self._span() / 4.0))

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
    def lane_count(self) -> int:
        return max((lane + 1 for lane in self._segment_lanes), default=0)

    def _clamp_band_scroll(self):
        limit = max(0, self.lane_count() - VISIBLE_LANES)
        self.band_scroll = max(0, min(limit, int(self.band_scroll)))

    def scroll_bands(self, notches: int) -> bool:
        """Alt+滚轮：上下翻片段层。返回是否真的动了。"""
        before = self.band_scroll
        self.band_scroll = int(self.band_scroll) + int(notches)
        self._clamp_band_scroll()
        if self.band_scroll != before:
            self.update()
            return True
        return False

    def _lane_y(self, lane: int) -> int:
        """第 lane 层带的**底边** y（随 band_scroll 上下移动）。"""
        row = int(lane) - int(self.band_scroll)
        return STACK_BOTTOM - row * (LANE_H + LANE_GAP)

    def _lane_visible(self, lane: int) -> bool:
        row = int(lane) - int(self.band_scroll)
        return 0 <= row < VISIBLE_LANES

    # ------------------------------------------------------------------ 命中
    def _handle_at(self, pos) -> str | None:
        """鼠标在入/出点小旗上就返回 ``"in"`` / ``"out"``。"""
        if pos.y() > self.height() - 10:
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

    def _segment_at(self, pos) -> int | None:
        """点在片段条上就返回片段序号（按层判断，叠起来也能点中）。"""
        if not self._segments:
            return None
        for index, (start, end, _name) in enumerate(self._segments):
            lane = self._segment_lanes[index] if index < len(self._segment_lanes) else 0
            if not self._lane_visible(lane):
                continue
            bottom = self._lane_y(lane)
            if not (bottom - LANE_H - 1 <= pos.y() <= bottom + 1):
                continue
            x1 = self._x_for_frame(start * self.fps)
            x2 = self._x_for_frame(end * self.fps)
            if x2 < x1:
                x1, x2 = x2, x1
            if x1 - 3 <= pos.x() <= x2 + 3:
                return index
        return None

    def _marker_at(self, pos, strict_y: bool = True) -> int | None:
        """鼠标落在绿色标记附近就返回帧号（strict_y=False 时只看横向）。"""
        if strict_y and not (TRACK_Y - 10 <= pos.y() <= TRACK_Y - 1):
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
        y = TRACK_Y
        painter.setPen(Qt.NoPen)

        # 片段层：按重叠分层，叠得再多也有高度差；Alt+滚轮翻层
        for index, (start, end, _name) in enumerate(self._segments):
            lane = self._segment_lanes[index] if index < len(self._segment_lanes) else 0
            if not self._lane_visible(lane):
                continue
            x1 = self._x_for_frame(start * self.fps)
            x2 = self._x_for_frame(end * self.fps)
            if x2 < x1:
                x1, x2 = x2, x1
            if x2 < left or x1 > right:
                continue
            x1, x2 = max(left, x1), min(right, x2)
            bottom = self._lane_y(lane)
            painter.setBrush(SEG_COLOR)
            painter.drawRoundedRect(
                QRectF(x1, bottom - LANE_H, max(3.0, x2 - x1), LANE_H), 1.6, 1.6)

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

        # 多段区间：每段一条彩色带（重叠的往上错开 2px）
        for lo, hi, color, lane, active in self._ranges:
            x1 = max(left, self._x_for_frame(lo))
            x2 = min(right, self._x_for_frame(hi))
            if x2 < x1:
                continue
            band = QColor(color)
            if not active:
                band.setAlpha(170)
            painter.setPen(QPen(band, 5 if active else 3))
            painter.drawLine(QPointF(x1, y - min(2, int(lane)) * 2),
                             QPointF(x2, y - min(2, int(lane)) * 2))

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
        if lanes > VISIBLE_LANES:
            label += f" · 片段 {lanes} 层（Alt+滚轮）"
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
        painter.end()

    @staticmethod
    def _paint_flag(painter: QPainter, x: float, kind: str, hot: bool):
        color = FLAG_HOT if hot else RANGE_COLOR
        painter.setPen(QPen(color, 2 if hot else 1))
        painter.drawLine(QPointF(x, TRACK_Y + 3), QPointF(x, TRACK_Y - 12))
        span = 9 if hot else 8
        top = TRACK_Y - 12
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
        """Ctrl+滚轮 = 进度条横向伸缩；Alt+滚轮 = 片段层上下翻。"""
        notches = self._notches(event)
        if not notches:
            super().wheelEvent(event)
            return
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            self._zoom_notches(notches, event.position().x())
            event.accept()
            return
        if mods & Qt.AltModifier:
            self.scroll_bands(notches)
            event.accept()
            return
        if self.zoom > 1.0:
            # 放大后：滚轮左右平移（向上 = 往左）
            self._pan_to(self._view_start - notches * self._visible() * 0.1)
            event.accept()
            return
        if self.lane_count() > VISIBLE_LANES and self.scroll_bands(notches):
            event.accept()
            return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event):   # noqa: N802
        """双击总进度条 = 缩放到整段（片段层也回到最下面）。"""
        if event.button() == Qt.LeftButton:
            self.reset_zoom()
            self.band_scroll = 0
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mousePressEvent(self, event):         # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        kind = self._handle_at(pos)
        if kind:
            self._drag = kind
            self._hover = kind
            self.setCursor(Qt.ClosedHandCursor)
            self.update()
            event.accept()
            return
        index = self._segment_at(pos)
        if index is not None:
            self.segmentActivated.emit(index)
            event.accept()
            return
        marker = self._marker_at(pos)
        if marker is not None:
            self.markerActivated.emit(marker)
            event.accept()
            return
        self._scrubbing = True
        self.seekRequested.emit(self._frame_at_x(pos.x()))
        event.accept()

    def mouseMoveEvent(self, event):          # noqa: N802
        pos = event.position().toPoint()
        if self._drag:
            frame = self._frame_at_x(pos.x())
            if self._drag == "in":
                self.inMoved.emit(frame)
            else:
                self.outMoved.emit(frame)
            event.accept()
            return
        if self._scrubbing:
            self.seekRequested.emit(self._frame_at_x(pos.x()))
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
            elif self._segment_at(pos) is not None:
                self.setCursor(Qt.PointingHandCursor)
            else:
                self.setCursor(Qt.ArrowCursor)
        self._update_tooltip(pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):       # noqa: N802
        if self._drag:
            kind, self._drag = self._drag, None
            self._hover = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            self.editFinished.emit(kind)
            event.accept()
            return
        if self._scrubbing:
            self._scrubbing = False
            self.seekRequested.emit(self._frame_at_x(
                event.position().toPoint().x()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):              # noqa: N802
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
        if self.lane_count() > VISIBLE_LANES:
            lines.append(f"片段 {self.lane_count()} 层，当前从第 "
                         f"{self.band_scroll + 1} 层起（Alt+滚轮翻层）")
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
        if index is not None:
            menu.addSeparator()
            act_jump = menu.addAction(f"选中片段 #{index + 1}")
        menu.addSeparator()
        act_seek = menu.addAction("光标跳到这里")
        menu.addSeparator()
        act_zin = menu.addAction("时间轴放大（也可以 Ctrl+滚轮向上）")
        act_zout = menu.addAction("时间轴缩小（也可以 Ctrl+滚轮向下）")
        act_zfit = menu.addAction("显示整段（双击时间轴）")
        act_zfit.setEnabled(self.zoom > 1.0 or self.band_scroll > 0)
        act_lane_up = menu.addAction("片段层往上看（Alt+滚轮向上）")
        act_lane_down = menu.addAction("片段层往下看（Alt+滚轮向下）")
        act_lane_up.setEnabled(self.band_scroll < self.lane_count() - VISIBLE_LANES)
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
        elif chosen is act_lane_up:
            self.scroll_bands(1)
        elif chosen is act_lane_down:
            self.scroll_bands(-1)

    # ------------------------------------------------------------------ 其它
    def sizeHint(self) -> QSize:              # noqa: N802
        return QSize(800, BAR_H)

    def minimumSizeHint(self) -> QSize:       # noqa: N802
        return QSize(MIN_WIDTH, BAR_H)
