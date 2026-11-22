# -*- coding: utf-8 -*-
"""标签页左侧的「缓存红绿灯」：一眼看出当前标签页的缓存状态。

    🔴 加载中      抽帧 / 恢复缓存还没完成（旋转图标）
    🔵 未缓存      图墙已就绪，但还没保存进持久缓存（保存图标，点一下保存）
    🟢 已缓存      持久缓存里已有这一份（对勾图标，点击不再重复保存）
    ⚪ 无          没有打开的标签页 / 抽帧失败

样式跟顶部「检查更新」药丸同一套（圆角胶囊 + 彩色圆点），
只是右边多一个随状态变化的小图标。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .style import ACCENT, DANGER, DIM, OK

# state -> (圆点色, 边框色, 底色, 文字色, 文字)
COLORS = {
    "none":    (DIM,     "#3A4150", "#1C1F26", "#9299A6", "缓存"),
    "loading": (DANGER,  "#5A3A40", "#2A1F22", "#E9A0A5", "加载中"),
    "ready":   (ACCENT,  "#4A5570", "#232B3A", "#A9BEFF", "未缓存"),
    "saved":   (OK,      "#3E6B57", "#1D2A24", OK,        "已缓存"),
}
WIDTH, HEIGHT = 100, 26


class CacheBadge(QWidget):
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cacheBadge")
        self.setFixedSize(WIDTH, HEIGHT)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.ArrowCursor)
        self._state = "none"
        self._angle = 0
        self._spin = QTimer(self)
        self._spin.setInterval(90)
        self._spin.timeout.connect(self._tick)
        self.setToolTip("当前标签页的缓存状态")

    # ------------------------------------------------------------------ 状态
    def state_name(self) -> str:
        return self._state

    def set_state(self, state: str, tip: str = ""):
        if state not in COLORS:
            state = "none"
        changed = state != self._state
        self._state = state
        if state == "loading":
            if not self._spin.isActive():
                self._spin.start()
        else:
            self._spin.stop()
        self.setCursor(Qt.PointingHandCursor if state == "ready"
                       else Qt.ArrowCursor)
        self.setToolTip(tip or self._default_tip(state))
        if changed:
            self.update()

    def _default_tip(self, state: str) -> str:
        if state == "loading":
            return "正在抽帧 / 加载缓存…\n完成后如果还没保存，图标会变成蓝色"
        if state == "ready":
            return "图墙已加载，但还没保存缓存\n点这里保存（下次打开直接加载）"
        if state == "saved":
            return "这一份图墙已经保存到缓存\n下次打开同一个视频会直接加载"
        return "当前没有可保存的缓存"

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    # ------------------------------------------------------------------ 绘制
    def sizeHint(self):                        # noqa: N802
        return QSize(WIDTH, HEIGHT)

    def paintEvent(self, _event):              # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        dot, border, background, foreground, text = COLORS[self._state]
        hover = self.underMouse() and self._state == "ready"
        p.setPen(QPen(QColor(foreground if hover else border), 1))
        p.setBrush(QColor(background))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1,
                                 self.height() - 1), 13, 13)

        cy = self.height() / 2.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(dot))
        p.drawEllipse(QPointF(12, cy), 4.0, 4.0)

        font = QFont(self.font())
        font.setPointSizeF(8.6)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(foreground))
        p.drawText(QRectF(21, 0, 56, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
        self._draw_icon(p, QPointF(83, cy), QColor(foreground))
        p.end()

    def _draw_icon(self, p: QPainter, center: QPointF, color: QColor):
        x, y = center.x(), center.y()
        if self._state == "loading":
            # 旋转的缺口圆环
            pen = QPen(QColor(color.red(), color.green(), color.blue(), 70), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, 5.4, 5.4)
            pen.setColor(color)
            p.setPen(pen)
            p.drawArc(QRectF(x - 5.4, y - 5.4, 10.8, 10.8),
                      int(self._angle * 16), int(250 * 16))
        elif self._state == "ready":
            # 保存：向下箭头 + 托盘
            pen = QPen(color, 1.6)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(x, y - 6), QPointF(x, y + 2))
            p.drawPolyline([QPointF(x - 3.6, y - 1.4), QPointF(x, y + 2.2),
                            QPointF(x + 3.6, y - 1.4)])
            p.drawPolyline([QPointF(x - 5.4, y + 5.4), QPointF(x - 5.4, y + 6.4),
                            QPointF(x + 5.4, y + 6.4), QPointF(x + 5.4, y + 5.4)])
        elif self._state == "saved":
            pen = QPen(color, 1.9)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(x - 5.6, y), QPointF(x - 1.2, y + 4.4),
                            QPointF(x + 6.0, y - 4.6)])
        else:
            pen = QPen(QColor(DIM), 1.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x - 4.5, y), QPointF(x + 4.5, y))

    # ------------------------------------------------------------------ 交互
    def mouseReleaseEvent(self, event):        # noqa: N802
        if (event.button() == Qt.LeftButton
                and self.rect().contains(event.position().toPoint())
                and self._state == "ready"):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):               # noqa: N802
        if self._state == "ready":
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):               # noqa: N802
        if self._state == "ready":
            self.update()
        super().leaveEvent(event)
