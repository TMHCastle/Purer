# -*- coding: utf-8 -*-
"""标签栏（自绘）：每个标签的边框 / 底色跟着「这个视频的缓存状态」走。

    🔴 加载中   边框红、底色淡淡一抹红
    🔵 未缓存   边框蓝、底色淡淡一抹蓝（图墙好了但还没进缓存库）
    🟢 已缓存   边框绿、底色淡淡一抹绿
    ⚪ 无       中性灰边框（抽帧失败 / 还没开始）

底色只是「在原来的标签底色上混一点状态色」，选中 / 悬停 / 普通三档依旧分得开；
文字仍然交给 Qt 画（QSS 只管颜色和内边距），所以省略号、图标、关闭按钮都照旧。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QTabBar

from .style import ACCENT, DANGER, OK, TAB_MARGIN_BOTTOM, TAB_MARGIN_RIGHT, \
    TAB_MARGIN_TOP, TAB_RADIUS

# 状态 → 边框色（None = 中性）
STATE_COLORS = {
    "loading": DANGER,
    "ready": ACCENT,
    "saved": OK,
    "none": None,
}

TAB_BG = "#1B1F27"
TAB_BG_HOVER = "#252A33"
TAB_BG_SELECTED = "#2D3442"
TAB_BORDER = "#252A33"
TAB_BORDER_SELECTED = "#4A5570"

# 状态色混进底色的比例（"淡淡的"）
TINT_SELECTED = 0.13
TINT_NORMAL = 0.08


def _blend(base: QColor, over: QColor, ratio: float) -> QColor:
    ratio = max(0.0, min(1.0, float(ratio)))
    return QColor(
        int(round(base.red() * (1 - ratio) + over.red() * ratio)),
        int(round(base.green() * (1 - ratio) + over.green() * ratio)),
        int(round(base.blue() * (1 - ratio) + over.blue() * ratio)))


class StateTabBar(QTabBar):
    """按缓存状态给标签上色的标签栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setExpanding(False)
        self._hover = -1
        self._state_provider = None

    # ------------------------------------------------------------------ 状态
    def set_state_provider(self, provider):
        """``provider(index) -> "loading" / "ready" / "saved" / "none"``。"""
        self._state_provider = provider
        self.update()

    def state_of(self, index: int) -> str:
        if self._state_provider is None:
            return "none"
        try:
            return str(self._state_provider(index) or "none")
        except Exception:                          # noqa: BLE001
            return "none"

    def refresh_states(self):
        self.update()

    # ------------------------------------------------------------------ 悬停
    def mouseMoveEvent(self, event):               # noqa: N802
        index = self.tabAt(event.position().toPoint())
        if index != self._hover:
            self._hover = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):                   # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event):                   # noqa: N802
        self._paint_cards()
        super().paintEvent(event)

    def _paint_cards(self):
        current = self.currentIndex()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        try:
            for index in range(self.count()):
                rect = self.tabRect(index)
                if rect.isNull():
                    continue
                body = QRectF(rect.adjusted(0, TAB_MARGIN_TOP,
                                            -TAB_MARGIN_RIGHT,
                                            -TAB_MARGIN_BOTTOM))
                body.adjust(0.5, 0.5, -0.5, -0.5)
                selected = index == current
                hover = (index == self._hover) and not selected
                base = QColor(TAB_BG_SELECTED if selected
                              else (TAB_BG_HOVER if hover else TAB_BG))
                color = STATE_COLORS.get(self.state_of(index))
                if color:
                    base = _blend(base, QColor(color),
                                  TINT_SELECTED if selected else TINT_NORMAL)
                    pen = QPen(QColor(color), 1.3 if selected else 1.0)
                else:
                    pen = QPen(QColor(TAB_BORDER_SELECTED if selected
                                      else TAB_BORDER), 1.0)
                p.setPen(pen)
                p.setBrush(base)
                p.drawRoundedRect(body, TAB_RADIUS, TAB_RADIUS)
        finally:
            p.end()
