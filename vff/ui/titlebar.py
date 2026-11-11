# -*- coding: utf-8 -*-
"""自绘标题栏（无边框窗口的第一行）。

布局：``[图标] Purer [v1.1.4] 每 24 帧一帧  ……  正中留空给更新药丸 ……  — □ ✕``

正中间那块是**绝对居中**的：更新药丸不放进 QHBoxLayout，而是作为子控件在
``resizeEvent`` 里按 ``(width - hint) / 2`` 摆好 —— 否则左右两侧宽度不一样，
药丸会被挤偏（"正中心"就不成立了）。

整行都能拖：按下超过 4px 才开始移动（这样点窗口按钮、点药丸不会被当成拖动）；
双击 = 最大化/还原；最大化时往下拖会先还原再跟手；拖到屏幕顶边松手 = 最大化。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                               QWidget)

from ..config import APP_NAME, APP_VERSION
from .style import (icon_win_close, icon_win_max, icon_win_min, icon_win_restore,
                    make_icon)

TITLE_H = 38
BTN_W, BTN_H = 36, 26
ICON_PX = 12


class WindowButton(QPushButton):
    """标题栏右侧的窗口按钮：悬停换一张更亮的图标。"""

    def __init__(self, icon, icon_hover, tip: str, parent=None):
        super().__init__(parent)
        self.setObjectName("winBtn")
        self.setFixedSize(BTN_W, BTN_H)
        self.setIconSize(QSize(ICON_PX, ICON_PX))
        self.setIcon(icon)
        self.setToolTip(tip)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._normal, self._hover = icon, icon_hover

    def set_icons(self, icon, icon_hover):
        self._normal, self._hover = icon, icon_hover
        self.setIcon(icon)

    def enterEvent(self, event):              # noqa: N802
        self.setIcon(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event):              # noqa: N802
        self.setIcon(self._normal)
        super().leaveEvent(event)


class TitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("titlebar")
        self.setFixedHeight(TITLE_H)
        self._center: QWidget | None = None
        self._press_global: QPoint | None = None
        self._drag_origin = QPoint()
        self._press_local = QPoint()
        self._dragging = False

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 4, 8, 4)
        row.setSpacing(8)

        self.icon = QLabel(self)
        self.icon.setPixmap(make_icon().pixmap(18, 18))
        self.brand = QLabel(APP_NAME, self)
        self.brand.setObjectName("title")
        self.ver = QLabel(f"v{APP_VERSION}", self)
        self.ver.setObjectName("verChip")
        self.subtitle = QLabel("", self)
        self.subtitle.setObjectName("hint")

        row.addWidget(self.icon)
        row.addWidget(self.brand)
        row.addWidget(self.ver)
        row.addSpacing(4)
        row.addWidget(self.subtitle)
        row.addStretch(1)

        self.btn_min = WindowButton(icon_win_min(), icon_win_min("#E8EAF0"),
                                    "最小化", self)
        self.btn_max = WindowButton(icon_win_max(), icon_win_max("#E8EAF0"),
                                    "最大化", self)
        self.btn_close = WindowButton(icon_win_close(), icon_win_close("#FF7B84"),
                                      "关闭 (Alt+F4)", self)
        self.btn_close.setObjectName("winBtnClose")
        for button in (self.btn_min, self.btn_max, self.btn_close):
            row.addWidget(button)
        self.btn_min.clicked.connect(lambda: self.window().showMinimized())
        self.btn_max.clicked.connect(self.toggle_max)
        self.btn_close.clicked.connect(lambda: self.window().close())

    # ------------------------------------------------------ 正中那块「宿主」
    def set_center_widget(self, widget: QWidget):
        """把窗口正中的位置上交给外部控件（更新药丸）。"""
        self._center = widget
        widget.setParent(self)
        changed = getattr(widget, "sizeChanged", None)
        if changed is not None:
            changed.connect(self.recenter)
        self.recenter()

    def center_widget(self) -> QWidget | None:
        return self._center

    def set_subtitle(self, text: str):
        self.subtitle.setText(text or "")

    def recenter(self):
        widget = self._center
        if widget is None:
            return
        hint = widget.sizeHint()
        x = max(0, (self.width() - hint.width()) // 2)
        y = max(0, (self.height() - hint.height()) // 2)
        widget.setGeometry(x, y, hint.width(), hint.height())
        widget.raise_()

    def resizeEvent(self, event):              # noqa: N802
        super().resizeEvent(event)
        self.recenter()

    # ---------------------------------------------------------- 最大化状态
    def sync_max_button(self):
        window = self.window()
        if window.isMaximized():
            self.btn_max.set_icons(icon_win_restore(), icon_win_restore("#E8EAF0"))
            self.btn_max.setToolTip("还原")
        else:
            self.btn_max.set_icons(icon_win_max(), icon_win_max("#E8EAF0"))
            self.btn_max.setToolTip("最大化")

    def toggle_max(self):
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()
        self.sync_max_button()

    # ---------------------------------------------------------------- 拖动
    def mousePressEvent(self, event):           # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_global = event.globalPosition().toPoint()
        self._press_local = event.position().toPoint()
        self._drag_origin = self.window().pos()
        self._dragging = False
        event.accept()

    def mouseMoveEvent(self, event):            # noqa: N802
        if not (event.buttons() & Qt.LeftButton) or self._press_global is None:
            super().mouseMoveEvent(event)
            return
        window = self.window()
        point = event.globalPosition().toPoint()
        if not self._dragging:
            if (point - self._press_global).manhattanLength() < 4:
                return
            self._dragging = True
            if window.isMaximized() or window.isFullScreen():
                # 最大化时拖标题：先还原，指针仍停在这行的同一相对位置
                ratio = self._press_local.x() / max(1, window.width())
                window.showNormal()
                self.sync_max_button()
                window.move(QPoint(point.x() - int(window.width() * ratio),
                                   point.y() - self._press_local.y()))
                self._drag_origin = window.pos()
                self._press_global = point
        window.move(self._drag_origin + (point - self._press_global))
        event.accept()

    def mouseReleaseEvent(self, event):         # noqa: N802
        if event.button() == Qt.LeftButton and self._dragging:
            self._snap_if_top()
        self._dragging = False
        self._press_global = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):     # noqa: N802
        if event.button() == Qt.LeftButton:
            self.toggle_max()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _snap_if_top(self):
        """拖到屏幕顶边松手 = 最大化（Windows 的老习惯）。"""
        window = self.window()
        screen = window.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        if window.frameGeometry().top() <= area.top() + 2:
            window.showMaximized()
            self.sync_max_button()
