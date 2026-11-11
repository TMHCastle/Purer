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

from .widgets import LANE_H

LINE_COLOR = QColor("#2E3442")
TICK_COLOR = QColor("#454D5E")
MAJOR_COLOR = QColor("#5E6879")
RANGE_COLOR = QColor("#668CFF")
CURSOR_COLOR = QColor("#E5B65C")

HANDLE_HALF_W = 8      # 手柄命中宽度（左右各 8px）
HANDLE_TOL_Y = 9       # 垂直命中容差


class WallView(QListView):
    zoomRequested = Signal(int)          # Ctrl+滚轮：+1 放大 / -1 缩小
    rangeMoved = Signal(str, int)        # 拖动入/出点："in"/"out", 帧号
    rangeEdited = Signal(str)            # 拖动结束

    def __init__(self, parent=None):
        super().__init__(parent)
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self.cursor_frame: int | None = None
        self.step = 24
        self._drag: str | None = None
        self._drag_bucket: list[int] | None = None
        self._drag_rects: list = []
        self._hover: str | None = None

    # ---------------- 外部设置 ----------------
    def set_markers(self, in_frame=None, out_frame=None, cursor_frame=None):
        self.in_frame = in_frame
        self.out_frame = out_frame
        self.cursor_frame = cursor_frame
        self.viewport().update()

    # ---------------- 滚轮 ----------------
    def wheelEvent(self, event):
        # Ctrl+滚轮只改显示尺寸；普通滚轮照常滚动
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoomRequested.emit(1 if delta > 0 else -1)
                event.accept()
                return
        super().wheelEvent(event)

    # ---------------- 可见范围 ----------------
    def _row_rect(self, row: int):
        index = self.model().index(row, 0) if self.model() else None
        if index is None or not index.isValid():
            return None
        return self.visualRect(index)

    def _first_visible(self) -> int:
        count = self.model().rowCount() if self.model() else 0
        if count == 0:
            return 0
        lo, hi, found = 0, count - 1, count
        while lo <= hi:
            mid = (lo + hi) // 2
            rect = self._row_rect(mid)
            if rect is not None and rect.bottom() >= 0:
                found = mid
                hi = mid - 1
            else:
                lo = mid + 1
        return min(found, count - 1)

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
        if not rects or self.step <= 0:
            return None
        centers = {item: r.center().x() for item, r in zip(bucket, rects)}
        pos = frame / max(1, self.step)
        item = int(pos)
        frac = pos - item
        cur = centers.get(item)
        nxt = centers.get(item + 1)
        prev = centers.get(item - 1)
        if cur is not None and nxt is not None:
            return cur + frac * (nxt - cur)
        if cur is not None and prev is not None:
            return cur + frac * (cur - prev)
        return cur

    def _frame_at_x(self, bucket, rects, x: float) -> int | None:
        centers = [r.center().x() for r in rects]
        if not centers or self.step <= 0:
            return None
        if len(centers) == 1:
            return int(round(bucket[0] * self.step))
        if x <= centers[0]:
            gap = max(1, centers[1] - centers[0])
            return int(round((bucket[0] + (x - centers[0]) / gap) * self.step))
        if x >= centers[-1]:
            gap = max(1, centers[-1] - centers[-2])
            return int(round((bucket[-2] + (x - centers[-2]) / gap) * self.step))
        for i in range(len(centers) - 1):
            if centers[i] <= x <= centers[i + 1]:
                gap = max(1, centers[i + 1] - centers[i])
                return int(round((bucket[i] + (x - centers[i]) / gap) * self.step))
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
        lo = bucket[0] * self.step
        hi = (bucket[-1] + 1) * self.step - 1
        return int(max(lo, min(hi, frame)))

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
        super().mouseReleaseEvent(event)

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
            row_start = bucket[0] * self.step
            row_end = (bucket[-1] + 1) * self.step
            if hi >= row_start and lo <= row_end:
                x1 = self._x_for_frame(bucket, rects, lo)
                x2 = self._x_for_frame(bucket, rects, hi)
                x1 = left if x1 is None else min(max(x1, left), right)
                x2 = left if x2 is None else min(max(x2, left), right)
                if x2 > x1:
                    painter.setPen(QPen(RANGE_COLOR, 3))
                    painter.drawLine(int(x1), y, int(x2), y)

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
            pos = self.cursor_frame / max(1, self.step)
            item = int(pos)
            offsets = {idx: rects[n] for n, idx in enumerate(bucket) if n < len(rects)}
            if item in offsets:
                frac = max(0.0, pos - item)
                x = offsets[item].center().x()
                nxt = offsets.get(item + 1)
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
        if not self.model() or self.step <= 0:
            return None
        count = self.model().rowCount()
        item = int(round(frame / self.step))
        if item < 0 or item >= count:
            return None
        return item
