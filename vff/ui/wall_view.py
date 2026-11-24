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

from PySide6.QtCore import QPoint, Qt, Signal
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

HANDLE_HALF_W = 8      # 手柄命中宽度（左右各 8px）
HANDLE_TOL_Y = 9       # 垂直命中容差


class WallView(QListView):
    zoomRequested = Signal(int)          # Alt+滚轮：+1 放大 / -1 缩小
    rangeMoved = Signal(str, int)        # 拖动入/出点："in"/"out", 帧号
    rangeEdited = Signal(str)            # 拖动结束
    rangeDragged = Signal(int, int)      # 随意框选进行中：(按下点, 当前点)
    rangeSelected = Signal(int, int)     # 随意框选松手：按下点 → 松手点
    markerActivated = Signal(int)        # 点了绿色标记：跳到该帧

    def __init__(self, parent=None):
        super().__init__(parent)
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self.cursor_frame: int | None = None
        self.range_markers: list[int] = []   # M 键加的标记（和入点/出点无关）
        self.timeline: Timeline = StepTimeline(24)
        self._drag: str | None = None
        self._drag_bucket: list[int] | None = None
        self._drag_rects: list = []
        self._hover: str | None = None
        # 随意框选：按「鼠标按下点 / 当前点」换算帧，而不是格子的左右边界
        self._press_pos: QPoint | None = None
        self._press_frame: int | None = None
        self._last_frame: int | None = None
        self._boxing = False
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
    def _x_for_frame(self, bucket, rects, frame: int) -> float | None:
        """某一帧在这一行上的精确 x（可能落在两个格子中间）。"""
        timeline = self.timeline
        if not rects or timeline.count() <= 0:
            return None
        centers = {item: r.center().x() for item, r in zip(bucket, rects)}
        timeline = self.timeline
        index, frac = timeline.index_of(frame)
        cur = centers.get(index)
        nxt = centers.get(index + 1)
        if cur is None:
            return None
        if nxt is not None:
            return cur + frac * (nxt - cur)
        return float(cur)          # 这一行里没有下一格：钉在格心（避免画到行外）

    def _frame_at_x(self, bucket, rects, x: float) -> int | None:
        timeline = self.timeline
        centers = [r.center().x() for r in rects]
        if not centers or timeline.count() <= 0:
            return None
        if len(centers) == 1:
            # 一行只有一格（按镜头只抽到 1 张，或最后一行只剩一张）：
            # 按格内位置映射到这一格覆盖的帧范围，否则拖不动蓝旗
            lo, hi = timeline.span(bucket[0])
            rect = rects[0]
            frac = (x - rect.left()) / max(1.0, float(rect.width()))
            return int(round(lo + min(1.0, max(0.0, frac)) * max(0.0, hi - lo)))
        if x <= centers[0]:
            gap = max(1, centers[1] - centers[0])
            f0, f1 = timeline.frame_at(bucket[0]), timeline.frame_at(bucket[1])
            return int(round(f0 + (x - centers[0]) / gap * (f1 - f0)))
        if x >= centers[-1]:
            gap = max(1, centers[-1] - centers[-2])
            f0, f1 = timeline.frame_at(bucket[-2]), timeline.frame_at(bucket[-1])
            return int(round(f1 + (x - centers[-1]) / gap * (f1 - f0)))
        for i in range(len(centers) - 1):
            if centers[i] <= x <= centers[i + 1]:
                gap = max(1, centers[i + 1] - centers[i])
                f0, f1 = timeline.frame_at(bucket[i]), timeline.frame_at(bucket[i + 1])
                return int(round(f0 + (x - centers[i]) / gap * (f1 - f0)))
        return None

    def _row_at_y(self, y: int):
        """鼠标 y 落在哪一行的「时间轴窄带」上。"""
        for _top, bucket in self._visible_rows():
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if not rects:
                continue
            line_y = max(r.bottom() for r in rects) - LANE_H // 2
            if abs(y - line_y) <= HANDLE_TOL_Y:
                return bucket, rects
        return None

    def _nearest_row(self, y: int):
        """拖动用：离鼠标纵向最近的那一行（自动吸附，不要求对准窄带）。

        `_row_at_y` 是「命中判定」（要精确落在窄带 ±9px 内）；
        拖动时如果还要求精确，鼠标一旦离开窄带就再也拖不动了 ——
        而纵向拖动本来就该一路跨行，所以这里取最近的一行。
        """
        best = None
        for _top, bucket in self._visible_rows():
            rects = [self._row_rect(i) for i in bucket]
            rects = [r for r in rects if r is not None]
            if not rects:
                continue
            line_y = max(r.bottom() for r in rects) - LANE_H // 2
            dist = abs(y - line_y)
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
        self._last_frame = None
        self._boxing = False
        if event.button() == Qt.LeftButton:
            kind = self._handle_at(event.position().toPoint())
            if kind:
                hit = self._row_at_y(event.position().toPoint().y())
                if hit is not None:
                    self._drag = kind
                    self._drag_bucket, self._drag_rects = hit
                    self._hover = kind
                    self.viewport().setCursor(Qt.ClosedHandCursor)
                    self.viewport().update()
                    event.accept()
                    return
            # 不拖小旗 = 可能开始随意框选：先记住按下点的精确帧
            pos = event.position().toPoint()
            marker = self.marker_at(pos)
            if marker is not None:
                self.markerActivated.emit(marker)
                event.accept()
                return
            self._press_pos = pos
            self._press_frame = self._frame_at_pos(pos)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._drag:
            # 横竖都能拖：纵向自动吸附到最近的那一行，横向决定这一行里的帧号
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
        if self._press_pos is not None and self._press_frame is not None:
            if (not self._boxing
                    and (pos - self._press_pos).manhattanLength() >= 4):
                self._boxing = True
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
        # 继续交给 QListView：保留原生框选高亮和边缘自动滚动
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
        had, start, end = self._boxing, self._press_frame, self._last_frame
        # 先让 QListView 收尾（此时 _boxing 仍为 True，TrackPage 会忽略它的多选信号）
        super().mouseReleaseEvent(event)
        self._boxing = False
        self._press_pos = None
        self._press_frame = None
        self._last_frame = None
        if had and start is not None and end is not None:
            # 最后再发一次精确区间：按下点 → 松手点，覆盖原生「整格」多选
            self.rangeSelected.emit(start, end)
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
        if len(rects) < 2:
            return
        left = min(r.left() for r in rects)
        right = max(r.right() for r in rects)
        bottom = max(r.bottom() for r in rects)
        y = bottom - LANE_H // 2        # 窄带中线

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

        # 范围条：严格按入点/出点的精确位置
        if self.in_frame is not None and self.out_frame is not None:
            lo = min(self.in_frame, self.out_frame)
            hi = max(self.in_frame, self.out_frame)
            row_start = self.timeline.frame_at(bucket[0])
            row_end = self.timeline.span(bucket[-1])[1]
            if hi >= row_start and lo <= row_end:
                x1 = self._x_for_frame(bucket, rects, lo)
                x2 = self._x_for_frame(bucket, rects, hi)
                x1 = left if x1 is None else min(max(x1, left), right)
                x2 = left if x2 is None else min(max(x2, left), right)
                if x2 > x1:
                    painter.setPen(QPen(RANGE_COLOR, 3))
                    painter.drawLine(int(x1), y, int(x2), y)

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

        # 入点 / 出点手柄（可拖）
        if self.in_frame is not None:
            x = self._x_for_frame(bucket, rects, self.in_frame)
            if x is not None and left - HANDLE_HALF_W <= x <= right + HANDLE_HALF_W:
                hot = self._hover == "in" or self._drag == "in"
                self._paint_flag(painter, x, y, "in", hot)
        if self.out_frame is not None:
            x = self._x_for_frame(bucket, rects, self.out_frame)
            if x is not None and left - HANDLE_HALF_W <= x <= right + HANDLE_HALF_W:
                hot = self._hover == "out" or self._drag == "out"
                self._paint_flag(painter, x, y, "out", hot)

        # 当前帧：精确位置的小三角
        if self.cursor_frame is not None:
            index, frac = self.timeline.index_of(self.cursor_frame)
            offsets = {idx: rects[n] for n, idx in enumerate(bucket) if n < len(rects)}
            if index in offsets:
                x = offsets[index].center().x()
                nxt = offsets.get(index + 1)
                if nxt is not None:
                    x += frac * (nxt.center().x() - x)
                painter.setPen(QPen(CURSOR_COLOR, 1))
                painter.drawLine(int(x), y + 2, int(x), y - 2)
                painter.setBrush(CURSOR_COLOR)
                painter.setPen(Qt.NoPen)
                painter.drawPolygon(QPolygon([QPoint(int(x) - 4, y + 2),
                                              QPoint(int(x) + 4, y + 2),
                                              QPoint(int(x), y + 7)]))

    @staticmethod
    def _paint_flag(painter: QPainter, x: float, y: int, kind: str, hot: bool = False):
        """入点=右侧小旗，出点=左侧小旗（都画在时间轴窄带里）。"""
        xi = int(round(x))
        color = QColor("#9DB4FF") if hot else RANGE_COLOR
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
