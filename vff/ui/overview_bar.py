# -*- coding: utf-8 -*-
"""底部「总进度条」：整段视频缩成一条，光标 / 入点 / 出点 / 片段标记都能直接操作。

这里不存任何状态 —— ``TrackPage._sync_overview()`` 每次都把同一份
``cursor_frame`` / ``in_frame`` / ``out_frame`` / ``segments`` / ``markers`` 推过来；
用户在这里点/拖只发信号，回去走的仍是墙上时间轴那套回调
（``_set_cursor`` / ``_on_range_drag`` / ``_end_range_drag``），所以两边永远一致。

操作：
  * 点 / 拖轨道 = 移动光标（精确到帧）；
  * 拖两端的旗子 = 改入点 / 出点；
  * 点琥珀色的片段标记 = 选中并跳到该片段；
  * 点轨道下方的绿色短线 = 跳到 M 标记；
  * 右键 = 设入点 / 设出点 / 清除区间 / 用当前选区新建片段 / 加删标记。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QMenu, QWidget

from ..util import fmt_tc
from .style import ACCENT, DIM, PANEL2, WARN

BAR_H = 46              # 控件总高（轨道 + 下面一行时间）
MARGIN = 16             # 左右留白（贴边不好点）
TRACK_Y = 25            # 轨道中线
TRACK_H = 6             # 轨道粗细
FLAG_TOP = 9            # 小旗旗面顶端
HANDLE_HIT = 9          # 小旗命中宽度（左右各 9px）
MIN_WIDTH = 240

SEG_COLOR = QColor(WARN)
SEG_COLOR.setAlpha(190)
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
    zoomRequested = Signal(int)          # Alt+滚轮：+1 放大 / -1 缩小

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(BAR_H)
        self.setMaximumHeight(BAR_H)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("点/拖：移动光标 · 拖两端小旗：改入点/出点 · 右键：更多")

        self.fps = 25.0
        self.total = 0
        self.cursor_frame: int | None = None
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self._segments: list[tuple[float, float, str]] = []
        self.markers: list[int] = []
        self._drag: str | None = None
        self._scrubbing = False
        self._hover: str | None = None

    # ------------------------------------------------------------------ 数据
    def set_media(self, total_frames: int, fps: float = 25.0):
        total = max(0, int(total_frames or 0))
        fps = max(1e-6, float(fps or 25.0))
        if (total, fps) != (self.total, self.fps):
            self.total, self.fps = total, fps
            self.update()

    def set_state(self, cursor_frame=None, in_frame=None, out_frame=None):
        before = (self.cursor_frame, self.in_frame, self.out_frame)
        self.cursor_frame = None if cursor_frame is None else int(cursor_frame)
        self.in_frame = None if in_frame is None else int(in_frame)
        self.out_frame = None if out_frame is None else int(out_frame)
        if before != (self.cursor_frame, self.in_frame, self.out_frame):
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
            cleaned.append((max(0.0, start), max(0.0, end), name))
        if cleaned != self._segments:
            self._segments = cleaned
            self.update()

    def set_markers(self, markers):
        """M 键标记：单点帧号（和入点/出点无关）。"""
        cleaned = sorted({int(m) for m in (markers or ()) if int(m) >= 0})
        if cleaned != self.markers:
            self.markers = cleaned
            self.update()

    # ------------------------------------------------------------------ 几何
    def _track_left(self) -> float:
        return float(MARGIN)

    def _track_right(self) -> float:
        return float(max(MARGIN + 1, self.width() - MARGIN))

    def _x_for_frame(self, frame: float) -> float:
        span = max(1, self.total - 1)
        usable = max(1.0, self._track_right() - self._track_left())
        frame = max(0.0, min(float(span), float(frame)))
        return self._track_left() + frame / span * usable

    def _frame_at_x(self, x: float) -> int:
        span = max(1, self.total - 1)
        usable = max(1.0, self._track_right() - self._track_left())
        frac = (float(x) - self._track_left()) / usable
        frac = max(0.0, min(1.0, frac))
        return int(round(frac * span))

    def frame_at(self, x: float) -> int:
        """公开给测试/外部用：屏幕 x → 帧号。"""
        return self._frame_at_x(x)

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
        """点在片段标记（轨道上方那条琥珀色细带）上就返回片段序号。"""
        if not self._segments or pos.y() > TRACK_Y - 3:
            return None
        for index, (start, end, _name) in enumerate(self._segments):
            x1 = self._x_for_frame(start * self.fps)
            x2 = self._x_for_frame(end * self.fps)
            if x2 < x1:
                x1, x2 = x2, x1
            if x1 - 4 <= pos.x() <= x2 + 4:
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

        # 底轨
        painter.setBrush(QColor(PANEL2))
        painter.drawRoundedRect(
            QRectF(left, y - TRACK_H / 2, right - left, TRACK_H), 3, 3)

        # 已走的部分（光标左侧）
        if self.cursor_frame is not None and self.total > 0:
            cx = self._x_for_frame(self.cursor_frame)
            width = max(2.0, cx - left)
            painter.setBrush(PROGRESS_COLOR)
            painter.drawRoundedRect(
                QRectF(left, y - TRACK_H / 2, width, TRACK_H), 3, 3)

        # 入点/出点区间
        if self.in_frame is not None and self.out_frame is not None:
            lo, hi = sorted((self.in_frame, self.out_frame))
            x1 = self._x_for_frame(lo)
            x2 = self._x_for_frame(hi)
            painter.setBrush(RANGE_FILL)
            painter.drawRoundedRect(
                QRectF(x1, y - TRACK_H / 2, max(2.0, x2 - x1), TRACK_H), 3, 3)

        # 片段标记（轨道上方一条细带，两端各一个小刻度）
        for start, end, _name in self._segments:
            x1 = self._x_for_frame(start * self.fps)
            x2 = self._x_for_frame(end * self.fps)
            if x2 < x1:
                x1, x2 = x2, x1
            painter.setBrush(SEG_COLOR)
            painter.drawRoundedRect(
                QRectF(x1, y - 11, max(3.0, x2 - x1), 3), 1.5, 1.5)
            painter.drawRect(QRectF(x1, y - 11, 1, 5))
            painter.drawRect(QRectF(max(x1, x2 - 1), y - 11, 1, 5))

        # M 键标记：轨道上方绿色短竖线（单点，不和片段/入出点混）
        for frame in self.markers:
            x = self._x_for_frame(frame)
            painter.setPen(QPen(MARKER_COLOR, 2))
            painter.drawLine(QPointF(x, y - 7), QPointF(x, y - 3))

        # 两端小旗
        if self.in_frame is not None:
            self._paint_flag(painter, self._x_for_frame(self.in_frame),
                             "in", self._hover == "in" or self._drag == "in")
        if self.out_frame is not None:
            self._paint_flag(painter, self._x_for_frame(self.out_frame),
                             "out", self._hover == "out" or self._drag == "out")

        # 光标：竖线 + 小圆点
        if self.cursor_frame is not None and self.total > 0:
            cx = self._x_for_frame(self.cursor_frame)
            painter.setPen(QPen(CURSOR_COLOR, 1.5))
            painter.drawLine(QPointF(cx, y - 12), QPointF(cx, y + 7))
            painter.setPen(Qt.NoPen)
            painter.setBrush(CURSOR_COLOR)
            painter.drawEllipse(QPointF(cx, y), 4.0, 4.0)

        # 底部时间：左「总进度」，右「当前 / 总长」
        font = painter.font()
        font.setPointSizeF(8.0)
        painter.setFont(font)
        painter.setPen(QPen(QColor(DIM), 1))
        if self.width() >= 520:
            painter.drawText(QRectF(left, self.height() - 13, 80, 12),
                             Qt.AlignLeft | Qt.AlignVCenter, "总进度")
        if self.total > 0:
            current = (self.cursor_frame or 0) / self.fps
            total_s = max(0, self.total - 1) / self.fps
            text = f"{fmt_tc(current)} / 总长 {fmt_tc(total_s)}"
        else:
            text = "没有视频"
        painter.drawText(QRectF(left, self.height() - 13, right - left, 12),
                         Qt.AlignRight | Qt.AlignVCenter, text)
        painter.end()

    @staticmethod
    def _paint_flag(painter: QPainter, x: float, kind: str, hot: bool):
        color = FLAG_HOT if hot else RANGE_COLOR
        painter.setPen(QPen(color, 2 if hot else 1))
        painter.drawLine(QPointF(x, TRACK_Y + 3), QPointF(x, FLAG_TOP + 2))
        span = 9 if hot else 8
        if kind == "in":
            points = [QPointF(x, FLAG_TOP), QPointF(x + span, FLAG_TOP + 5),
                      QPointF(x, FLAG_TOP + 10)]
        else:
            points = [QPointF(x, FLAG_TOP), QPointF(x - span, FLAG_TOP + 5),
                      QPointF(x, FLAG_TOP + 10)]
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))

    # ------------------------------------------------------------------ 鼠标
    def wheelEvent(self, event):              # noqa: N802
        """Alt+滚轮缩略图缩放（和墙上时间轴同一套）。"""
        if event.modifiers() & Qt.AltModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoomRequested.emit(1 if delta > 0 else -1)
                event.accept()
                return
        super().wheelEvent(event)

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
            self.setCursor(Qt.PointingHandCursor if marker is not None
                           else Qt.ArrowCursor)
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
        if self.in_frame is not None and self.out_frame is not None:
            lo, hi = sorted((self.in_frame, self.out_frame))
            if lo <= frame <= hi:
                lines.append(f"在选区里（{hi - lo + 1} 帧）")
        if self.in_frame is not None:
            lines.append(f"入点 #{self.in_frame} {fmt_tc(self.in_frame / self.fps)}")
        if self.out_frame is not None:
            lines.append(f"出点 #{self.out_frame} {fmt_tc(self.out_frame / self.fps)}")
        for start, end, name in self._segments:
            x1 = self._x_for_frame(start * self.fps)
            x2 = self._x_for_frame(end * self.fps)
            if min(x1, x2) - 4 <= pos.x() <= max(x1, x2) + 4:
                label = f"片段「{name}」" if name else "片段"
                lines.append(f"{label}：{fmt_tc(start)} → {fmt_tc(end)}")
                break
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

    # ------------------------------------------------------------------ 其它
    def sizeHint(self) -> QSize:              # noqa: N802
        return QSize(800, BAR_H)

    def minimumSizeHint(self) -> QSize:       # noqa: N802
        return QSize(MIN_WIDTH, BAR_H)
