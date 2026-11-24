# -*- coding: utf-8 -*-
"""图墙视图：每行下方那条窄带就是时间轴（刻度朝上）。

除了显示，这里还负责**入点/出点手柄的拖动**：
  * 蓝条严格按入点/出点的精确 x 画，不再取整到格子；
  * 两端各有一个小旗子（入点朝右、出点朝左），按住就能拖；
  * 横向拖 = 同一行里微调帧号；**纵向拖 = 跳到别的行**（自动吸附到离鼠标
    最近的那条时间轴，不用精确对准），所以一次能跳很远；
  * 拖动时只发信号，由 TrackPage 决定怎么更新（预览与光标都跟着被拖的那一帧）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QListView

from ..timeline import StepTimeline, Timeline
from .wheel_util import wheel_direction
from .widgets import LANE_H

LINE_COLOR = QColor("#2E3442")
TICK_COLOR = QColor("#454D5E")
MAJOR_COLOR = QColor("#5E6879")
RANGE_COLOR = QColor("#668CFF")
CURSOR_COLOR = QColor("#E5B65C")
MARKER_COLOR = QColor("#45C995")
LANE_BG = QColor("#141922")        # 工具带底色
LANE_TOP = QColor("#3C4456")       # 上界标记（工具带上沿）
FRAME_TICK = QColor("#39414F")     # 每帧一根细竖线
FRAME_TICK_H = 4                   # 细竖线高度

HANDLE_HALF_W = 8      # 手柄命中宽度（左右各 8px）
HANDLE_TOL_Y = 9       # 垂直命中容差


class WallView(QListView):
    zoomRequested = Signal(int)          # Alt+滚轮：+1 放大 / -1 缩小
    rangeMoved = Signal(str, int)        # 拖动入/出点："in"/"out", 帧号
    rangeEdited = Signal(str)            # 拖动结束
    rangeStarted = Signal()              # 开始拖一段新区间（框选刚过阈值）
    rangeDragged = Signal(int, int)      # 随意框选进行中：(按下点, 当前点)
    rangeSelected = Signal(int, int)     # 随意框选松手：按下点 → 松手点
    markerActivated = Signal(int)        # 点了绿色标记：跳到该帧
    seekRequested = Signal(int)          # 点行内时间轴：只移动当前帧
    deleteRequested = Signal()           # Delete / 退格：删掉选中的片段或区间

    def __init__(self, parent=None):
        super().__init__(parent)
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self.cursor_frame: int | None = None
        self.range_markers: list[int] = []   # M 键加的标记（和入点/出点无关）
        # 多段区间：[(起帧, 止帧, 颜色, 层, 是否当前段), …]
        self.ranges: list[tuple[int, int, str, int, bool]] = []
        self.timeline: Timeline = StepTimeline(24)
        self._drag: str | None = None
        self._drag_bucket: list[int] | None = None
        self._drag_rects: list = []
        self._hover: str | None = None
        # 随意框选：按「鼠标按下点 / 当前点」换算帧，而不是格子的左右边界
        self._press_pos: QPoint | None = None
        self._press_frame: int | None = None
        self._press_seek: int | None = None
        self._press_lane = False
        self._lane_scrub = False        # 在窄时间轴上拖播放头
        self._lane_bucket: list[int] | None = None
        self._lane_rects: list = []
        self._last_frame: int | None = None
        self._boxing = False
        self._started = False
        # Alt+滚轮缩放时记住「鼠标下那一帧 + 屏幕位置」，缩放后把它钉在原地
        self._zoom_anchor: tuple[int | None, QPoint] | None = None

    # ---------------- 外部设置 ----------------
    def set_timeline(self, timeline: Timeline):
        """换坐标系（每 N 帧 / 按镜头），几何与绘制全部跟着走。"""
        self.timeline = timeline
        self.viewport().update()

    @property
    def step(self) -> float:
        """兼容旧代码：等距时是真间隔，按镜头时是平均间隔。"""
        return self.timeline.guess_step()

    def set_markers(self, in_frame=None, out_frame=None, cursor_frame=None):
        self.in_frame = in_frame
        self.out_frame = out_frame
        self.cursor_frame = cursor_frame
        self.viewport().update()

    def set_range_marks(self, markers):
        """M 键标记（单点，不是入点/出点）。"""
        cleaned = sorted({int(m) for m in (markers or ()) if int(m) >= 0})
        if cleaned != self.range_markers:
            self.range_markers = cleaned
            self.viewport().update()

    def set_ranges(self, spans):
        """多段区间：``[(起帧, 止帧, 颜色, 层, 是否当前段), …]``。"""
        cleaned = [(int(lo), int(hi), str(color), int(lane), bool(active))
                   for lo, hi, color, lane, active in (spans or ())
                   if int(hi) >= int(lo)]
        if cleaned != self.ranges:
            self.ranges = cleaned
            self.viewport().update()

    # ---------------- 键盘 ----------------
    def keyPressEvent(self, event):
        # 退格在 QListView 里被「键盘搜索」吃掉，快捷键也可能抢不过它，
        # 所以直接在这里处理：两个键都是「删掉当前选中的片段/区间」。
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.deleteRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------------- 滚轮 ----------------
    def wheelEvent(self, event):
        # Alt+滚轮只改显示尺寸；普通滚轮照常滚动（和 Premiere 的缩放改键一致）
        if event.modifiers() & Qt.AltModifier:
            direction = wheel_direction(event)
            if direction:
                pos = event.position().toPoint()
                index = self.indexAt(pos)
                row = index.row() if index.isValid() else None
                self._zoom_anchor = (self._frame_at_pos(pos), pos, row)
                self.zoomRequested.emit(direction)
                event.accept()
                return
        super().wheelEvent(event)

    def take_zoom_anchor(self):
        """取走 Alt+滚轮记下的「鼠标下那一帧 + 屏幕位置 + 那一格」（只生效一次）。"""
        anchor = self._zoom_anchor
        self._zoom_anchor = None
        return anchor

    # ---------------- 可见范围 ----------------
    def _row_rect(self, row: int):
        index = self.model().index(row, 0) if self.model() else None
        if index is None or not index.isValid():
            return None
        return self.visualRect(index)

    def _first_visible(self) -> int:
        """当前最上面那一行的第一格。

        **不能用二分**：Batched 布局下远处的 item 还没排过版，``visualRect``
        返回 ``(-1,-1,0,0)``，二分会被骗到列表末尾 —— 大图墙（几千格）上
        框选 / 拖手柄 / Alt+滚轮找锚点全都会算到最后一帧去。
        """
        count = self.model().rowCount() if self.model() else 0
        if count == 0:
            return 0
        width = max(1, self.viewport().width())
        for y in (1, 4, 9, 16):
            for x in (2, width // 2, max(2, width - 4)):
                index = self.indexAt(QPoint(x, y))
                if index.isValid():
                    return max(0, min(count - 1, index.row()))
        # 退化：网格是均匀的，按滚动位置直接估一行再往前退一格
        grid = self.gridSize()
        gh = max(1, grid.height())
        gw = max(1, grid.width())
        per_row = max(1, width // gw)
        line = max(0, self.verticalScrollBar().value() // gh)
        return max(0, min(count - 1, (line - 1) * per_row))

    def _visible_rows(self):
        """返回 [(top, [item_row, ...]), ...]（只覆盖当前可见区域）。"""
        rows = []
        height = self.viewport().height()
        count = self.model().rowCount() if self.model() else 0
        row = self._first_visible()
        current_top = None
        bucket: list[int] = []
        while row < count:
            rect = self._row_rect(row)
            if rect is None:
                break
            if rect.width() <= 0 or rect.height() <= 0:
                # 还没排版到（Batched 布局）：可见区间之前跳过，之后直接停
                if bucket:
                    break
                row += 1
                continue
            if rect.top() > height + rect.height():
                break
            if current_top is None or abs(rect.top() - current_top) > 2:
                if bucket:
                    rows.append((current_top, bucket))
                current_top = rect.top()
                bucket = [row]
            else:
                bucket.append(row)
            row += 1
        if bucket:
            rows.append((current_top, bucket))
        return rows

    # ---------------- 几何：帧 <-> x ----------------
    def _segment(self, bucket, rects, n):
        """第 n 格的 ``(帧下界, 帧上界, 左边界, 右边界)``（上下界都是含的）。"""
        timeline = self.timeline
        index = bucket[n]
        lo = float(timeline.frame_at(index))
        hi = float(timeline.span(index)[1])
        rect = rects[n]
        return lo, hi, float(rect.left()), float(rect.right())

    # ---------------- 帧 <-> x（整行一条线性映射，唯一口径） ----------------
    def _row_span(self, bucket, rects):
        """这一行覆盖的 ``(起帧, 止帧, 左边界 x, 右边界 x)``。"""
        timeline = self.timeline
        lo = float(timeline.frame_at(bucket[0]))
        hi = float(timeline.span(bucket[-1])[1])
        x0 = float(rects[0].left())
        x1 = float(rects[-1].right())
        return lo, hi, x0, x1

    def _x_for_frame(self, bucket, rects, frame: int) -> float | None:
        """帧号 → 这一行上的精确 x（不在这一行的范围就返回 None）。"""
        if not rects or self.timeline.count() <= 0:
            return None
        lo, hi, x0, x1 = self._row_span(bucket, rects)
        if x1 <= x0:
            return x0
        tol = 0.5
        if not (lo - tol <= float(frame) <= hi + tol):
            return None
        t = (float(frame) - lo) / max(1e-9, hi - lo)
        return x0 + max(0.0, min(1.0, t)) * (x1 - x0)

    def _frame_at_x(self, bucket, rects, x: float) -> int | None:
        """这一行上的 x → 最近的帧号（点哪儿就是哪一帧）。"""
        if not rects or self.timeline.count() <= 0:
            return None
        lo, hi, x0, x1 = self._row_span(bucket, rects)
        if x1 <= x0:
            return int(round(lo))
        t = max(0.0, min(1.0, (x - x0) / (x1 - x0)))
        return int(round(lo + t * (hi - lo)))


    def _lane_band(self, rects):
        """返回 ``(带顶, 带底, 基线 y)``；带高固定 ``LANE_H``。"""
        bottom = max(r.bottom() for r in rects)
        top = bottom - LANE_H
        return top, bottom, top + LANE_H - 5

    def _frame_lines(self, bucket, rects, left, right):
        """每帧一根细竖线要画的 x 坐标（和点击/小旗同一口径，等距不漏帧）。"""
        if not rects or self.timeline.count() <= 0:
            return []
        lo, hi, x0, x1 = self._row_span(bucket, rects)
        span = hi - lo
        width = x1 - x0
        if span <= 0 or width <= 0:
            return []
        per_px = width / span
        stride = 1
        if per_px < 1.5:
            stride = int(math.ceil(1.5 / max(per_px, 1e-6)))
        out = []
        frame = int(math.ceil(lo))
        if stride > 1:
            frame = int(math.ceil(frame / stride) * stride)
        while frame <= hi:
            t = (frame - lo) / span
            x = x0 + t * width
            if left - 1 <= x <= right + 1:
                out.append(x)
            frame += stride
        return out


    def pending_seek(self):
        """按下还没松开时返回待定帧（用来压住 currentChanged 的重复 seek）。"""
        return self._press_seek

    def _row_at_y(self, y: int):
        """y 落在哪一行的工具带里（整条带都算，不再是 ±几像素的细线）。"""
        for _top, bucket in self._visible_rows():
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if not rects:
                continue
            top, bottom, _base = self._lane_band(rects)
            if top - 1 <= y <= bottom + 2:
                return bucket, rects
        return None

    def _nearest_row(self, y: int):
        """拖动用：离 y 最近的那一行（拖出工具带也不会断）。"""
        best = None
        for _top, bucket in self._visible_rows():
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if not rects:
                continue
            top, bottom, _base = self._lane_band(rects)
            if top <= y <= bottom:
                dist = 0
            else:
                dist = min(abs(y - top), abs(y - bottom))
            if best is None or dist < best[0]:
                best = (dist, bucket, rects)
        if best is None:
            return None
        return best[1], best[2]


    def _frame_in_row(self, bucket, rects, x: float) -> int | None:
        """把 x 换算成帧号，并限制在**这一行**的帧范围内。

        不夹的话，鼠标拖到行尾外侧会算出下一行的帧号，旗子就画到别的行去了。
        """
        frame = self._frame_at_x(bucket, rects, x)
        if frame is None:
            return None
        lo = self.timeline.frame_at(bucket[0])
        hi = self.timeline.span(bucket[-1])[1]
        return int(max(lo, min(hi, frame)))

    def _row_at_pos(self, pos: QPoint):
        """任意鼠标位置 → 所在行（不在行内就取纵向最近的一行）。"""
        best = None
        for _top, bucket in self._visible_rows():
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if not rects:
                continue
            top = min(r.top() for r in rects)
            bottom = max(r.bottom() for r in rects)
            if top <= pos.y() <= bottom:
                return bucket, rects
            dist = min(abs(pos.y() - top), abs(pos.y() - bottom))
            if best is None or dist < best[0]:
                best = (dist, bucket, rects)
        if best is None:
            return None
        return best[1], best[2]

    def _frame_at_pos(self, pos: QPoint) -> int | None:
        """鼠标位置 → 精确帧号（框选两端都用它）。"""
        hit = self._row_at_pos(pos)
        if hit is None:
            return None
        return self._frame_in_row(hit[0], hit[1], pos.x())

    def _frame_in_item(self, index, x: float) -> int | None:
        """点在某张缩略图上：按**整行线性映射**算最近帧（和工具带完全一致）。"""
        if index is None or not index.isValid():
            return None
        row = int(index.row())
        for _top, bucket in self._visible_rows():
            if row not in bucket:
                continue
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if len(rects) == len(bucket) and rects:
                return self._frame_at_x(bucket, rects, x)
            break
        rect = self.visualRect(index)
        if rect.width() <= 0:
            return None
        lo = float(self.timeline.frame_at(row))
        hi = float(self.timeline.span(row)[1])
        frac = max(0.0, min(1.0, (float(x) - rect.left()) / max(1.0, float(rect.width()))))
        return int(round(lo + frac * (hi - lo)))

    def box_selecting(self) -> bool:
        """正在随意框选？（TrackPage 用它忽略原生多选信号，免得覆盖精确帧）"""
        return self._boxing

    def marker_at(self, pos: QPoint) -> int | None:
        """鼠标落在绿色标记附近就返回那个帧号。"""
        if not self.range_markers:
            return None
        hit = self._row_at_y(pos.y())
        if hit is None:
            return None
        bucket, rects = hit
        for frame in self.range_markers:
            x = self._x_for_frame(bucket, rects, frame)
            if x is not None and abs(pos.x() - x) <= 5:
                return int(frame)
        return None

    def _handle_at(self, pos: QPoint):
        """命中入/出点手柄就返回 "in"/"out"。"""
        if self.in_frame is None and self.out_frame is None:
            return None
        hit = self._row_at_y(pos.y())
        if hit is None:
            return None
        bucket, rects = hit
        for kind, frame in (("in", self.in_frame), ("out", self.out_frame)):
            if frame is None:
                continue
            x = self._x_for_frame(bucket, rects, frame)
            if x is not None and abs(pos.x() - x) <= HANDLE_HALF_W:
                return kind
        return None

    def _set_hover_cursor(self, pos: QPoint):
        self.viewport().setCursor(
            Qt.SizeHorCursor if self._handle_at(pos) else Qt.ArrowCursor)

    # ---------------- 鼠标 ----------------
    def mousePressEvent(self, event):
        self._press_pos = None
        self._press_frame = None
        self._press_seek = None
        self._last_frame = None
        self._boxing = False
        self._started = False
        self._press_lane = False
        self._lane_scrub = False
        self._lane_bucket = None
        self._lane_rects = []
        if event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            kind = self._handle_at(pos)
            if kind:
                hit = self._row_at_y(pos.y())
                if hit is not None:
                    self._drag = kind
                    self._drag_bucket, self._drag_rects = hit
                    self._hover = kind
                    self.viewport().setCursor(Qt.ClosedHandCursor)
                    self.viewport().update()
                    event.accept()
                    return
            marker = self.marker_at(pos)
            if marker is not None:
                self.markerActivated.emit(marker)
                event.accept()
                return
            self._press_pos = pos
            self._press_frame = self._frame_at_pos(pos)
            self._press_seek = self._press_frame
            lane = self._row_at_y(pos.y())
            self._press_lane = lane is not None
            if lane is not None:
                # 压在工具带上：可以按住拖播放头
                self._lane_bucket, self._lane_rects = lane
            index = self.indexAt(pos)
            if index.isValid() and not self._press_lane:
                fine = self._frame_in_item(index, pos.x())
                if fine is not None:
                    self._press_seek = fine
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._drag:
            # 拖入点/出点：跨行也不会断（取最近的一行）
            hit = self._nearest_row(pos.y())
            if hit is None:
                event.accept()
                return
            self._drag_bucket, self._drag_rects = hit
            frame = self._frame_in_row(self._drag_bucket, self._drag_rects, pos.x())
            if frame is not None:
                self.rangeMoved.emit(self._drag, frame)
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if self._press_pos is not None and self._press_lane:
            # 工具带上拖动 = 直接拖当前帧（不再画框选区间）
            if not self._lane_scrub:
                if (pos - self._press_pos).manhattanLength() < 2:
                    return
                self._lane_scrub = True
                self.viewport().setCursor(Qt.ClosedHandCursor)
            hit = self._nearest_row(pos.y())
            if hit is not None:
                self._lane_bucket, self._lane_rects = hit
            if self._lane_bucket:
                frame = self._frame_in_row(self._lane_bucket,
                                           self._lane_rects, pos.x())
                if frame is not None:
                    self._last_frame = frame
                    self.seekRequested.emit(int(frame))
            event.accept()
            return
        if self._press_pos is not None and self._press_frame is not None:
            if (not self._boxing
                    and (pos - self._press_pos).manhattanLength() >= 4):
                self._boxing = True
                if not self._started:
                    self._started = True
                    self.rangeStarted.emit()      # 让 TrackPage 新开一段
            if self._boxing:
                frame = self._frame_at_pos(pos)
                if frame is not None:
                    self._last_frame = frame
                    self.rangeDragged.emit(self._press_frame, frame)
        kind = self._handle_at(pos)
        if kind != self._hover:
            self._hover = kind
            self.viewport().setCursor(Qt.SizeHorCursor if kind else Qt.ArrowCursor)
            self.viewport().update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag:
            kind, self._drag = self._drag, None
            self._drag_bucket = None
            self._drag_rects = []
            self._hover = None
            self.viewport().setCursor(Qt.ArrowCursor)
            self.viewport().update()
            self.rangeEdited.emit(kind)
            event.accept()
            return
        if self._lane_scrub:
            self._lane_scrub = False
            super().mouseReleaseEvent(event)      # 让 QListView 正常收尾
            self._press_pos = None
            self._press_frame = None
            self._press_seek = None
            self._last_frame = None
            self._press_lane = False
            self._lane_bucket = None
            self._lane_rects = []
            self.viewport().setCursor(Qt.ArrowCursor)
            self.viewport().update()
            event.accept()
            return
        had, start, end = self._boxing, self._press_frame, self._last_frame
        seek = self._press_seek
        # 交给 QListView 收尾（_press_seek 还没清，能压住重复 seek）
        super().mouseReleaseEvent(event)
        self._boxing = False
        self._press_pos = None
        self._press_frame = None
        self._press_seek = None
        self._last_frame = None
        self._press_lane = False
        self._lane_bucket = None
        self._lane_rects = []
        if had and start is not None and end is not None:
            self.rangeSelected.emit(start, end)
            event.accept()
            return
        if seek is not None and not (
                event.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier)):
            self.seekRequested.emit(int(seek))
            event.accept()
            return

    def leaveEvent(self, event):
        if not self._drag and self._hover is not None:
            self._hover = None
            self.viewport().setCursor(Qt.ArrowCursor)
            self.viewport().update()
        super().leaveEvent(event)

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.model() or self.model().rowCount() == 0:
            return
        painter = QPainter(self.viewport())
        for _top, bucket in self._visible_rows():
            self._paint_row(painter, bucket)
        painter.end()

    def _paint_row(self, painter: QPainter, bucket: list[int]):
        rects = [self._row_rect(i) for i in bucket]
        rects = [r for r in rects if r is not None]
        if not rects:
            return
        left = min(r.left() for r in rects)
        right = max(r.right() for r in rects)
        bottom = max(r.bottom() for r in rects)
        top = bottom - LANE_H
        y = top + LANE_H - 5        # 窄带中线

        painter.setPen(Qt.NoPen)
        painter.setBrush(LANE_BG)
        painter.drawRect(QRectF(left, top, right - left, bottom - top + 1))
        painter.setPen(QPen(LANE_TOP, 1))
        painter.drawLine(QPointF(left, top + 0.5), QPointF(right, top + 0.5))

        # 每帧一根细竖线
        painter.setPen(QPen(FRAME_TICK, 1))
        for fx in self._frame_lines(bucket, rects, left, right):
            painter.drawLine(QPointF(fx, y + 1), QPointF(fx, y + 1 + FRAME_TICK_H))

        painter.setPen(QPen(LINE_COLOR, 1))
        painter.drawLine(left, y, right, y)

        # 刻度：画在格子之间的缝里，朝上
        for n, rect in enumerate(rects):
            if n == 0:
                continue
            x = rect.left()
            major = (bucket[n] % 5 == 0)
            painter.setPen(QPen(MAJOR_COLOR if major else TICK_COLOR, 1))
            painter.drawLine(x, y, x, y - (5 if major else 3))

        # 多段区间：每段一条彩色带。**跨行也要连续**：中间的行从最左填到最右，
        # 起点行从 x(lo) 到行尾，终点行从行首到 x(hi)。
        row_start = self.timeline.frame_at(bucket[0])
        row_end = self.timeline.span(bucket[-1])[1]
        active_color = None
        for lo, hi, color, lane, active in self.ranges:
            if active:
                active_color = color
            if hi < row_start or lo > row_end:
                continue
            x1 = left if lo <= row_start else self._x_for_frame(bucket, rects, lo)
            x2 = right if hi >= row_end else self._x_for_frame(bucket, rects, hi)
            x1 = left if x1 is None else min(max(x1, left), right)
            x2 = right if x2 is None else min(max(x2, left), right)
            if x2 <= x1:
                continue
            off = min(3, int(lane)) * 2
            band = QColor(color)
            if not active:
                band.setAlpha(170)
            painter.setPen(QPen(band, 4 if active else 3))
            painter.drawLine(int(x1), y - off, int(x2), y - off)

        # M 键标记：绿色小菱形（单点，不参与入点/出点）
        row_start = self.timeline.frame_at(bucket[0])
        row_end = self.timeline.span(bucket[-1])[1]
        for frame in self.range_markers:
            if frame < row_start or frame > row_end:
                continue
            x = self._x_for_frame(bucket, rects, frame)
            if x is None or not (left - 4 <= x <= right + 4):
                continue
            xi = int(round(x))
            painter.setPen(QPen(MARKER_COLOR, 1))
            painter.drawLine(xi, y + 1, xi, y - 3)
            painter.setPen(Qt.NoPen)
            painter.setBrush(MARKER_COLOR)
            painter.drawPolygon(QPolygon([QPoint(xi, y - 7), QPoint(xi + 3, y - 4),
                                          QPoint(xi, y - 1), QPoint(xi - 3, y - 4)]))

        # 入点 / 出点手柄（可拖；颜色跟当前段一致）
        if self.in_frame is not None:
            x = self._x_for_frame(bucket, rects, self.in_frame)
            if x is not None and left - HANDLE_HALF_W <= x <= right + HANDLE_HALF_W:
                hot = self._hover == "in" or self._drag == "in"
                self._paint_flag(painter, x, y, "in", hot, active_color)
        if self.out_frame is not None:
            x = self._x_for_frame(bucket, rects, self.out_frame)
            if x is not None and left - HANDLE_HALF_W <= x <= right + HANDLE_HALF_W:
                hot = self._hover == "out" or self._drag == "out"
                self._paint_flag(painter, x, y, "out", hot, active_color)

        # 当前帧：精确位置的小三角（和点击/刻录用同一套映射）
        if self.cursor_frame is not None:
            x = self._x_for_frame(bucket, rects, self.cursor_frame)
            if x is not None and left - 6 <= x <= right + 6:
                xi = int(round(x))
                painter.setPen(QPen(CURSOR_COLOR, 1))
                painter.drawLine(xi, y + 2, xi, y - 2)
                painter.setBrush(CURSOR_COLOR)
                painter.setPen(Qt.NoPen)
                painter.drawPolygon(QPolygon([QPoint(xi - 4, y + 2),
                                              QPoint(xi + 4, y + 2),
                                              QPoint(xi, y + 7)]))

    @staticmethod
    def _paint_flag(painter: QPainter, x: float, y: int, kind: str,
                    hot: bool = False, color: str | None = None):
        """入点=右侧小旗，出点=左侧小旗（都画在时间轴窄带里）。"""
        xi = int(round(x))
        color = QColor("#9DB4FF") if hot else QColor(color or RANGE_COLOR)
        span = 8 if hot else 7
        # 精确帧位置的竖线（同时把可拖区域画清楚）
        painter.setPen(QPen(color, 2 if hot else 1))
        painter.drawLine(xi, y + 3, xi, y - 7)
        if kind == "in":
            poly = QPolygon([QPoint(xi, y + 1), QPoint(xi, y - 7),
                             QPoint(xi + span, y - 3), QPoint(xi, y + 1)])
        else:
            poly = QPolygon([QPoint(xi, y + 1), QPoint(xi, y - 7),
                             QPoint(xi - span, y - 3), QPoint(xi, y + 1)])
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(poly)

    def _item_for_frame(self, frame: int) -> int | None:
        if not self.model() or self.timeline.count() <= 0:
            return None
        count = self.model().rowCount()
        item = self.timeline.index_of(frame)[0]
        if item < 0 or item >= count:
            return None
        return item
