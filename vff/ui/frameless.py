# -*- coding: utf-8 -*-
"""无边框窗口基类：不要 Windows 那个灰白标题栏，边框也还能拖。

三件事：
  * ``FramelessWindowHint`` —— 原生标题栏去掉，界面自己画（见 ``titlebar.py``）；
  * 窗口边缘 6px 能把窗口拉大拉小 —— 交给系统的 ``QWindow.startSystemResize``，
    DPI 缩放、多屏、贴边都由系统算，比自己算几何靠谱；
  * 边缘的拉伸光标 —— 用全局事件过滤器维护。子控件会吃掉 mouseMove，
    只在 ``mouseMoveEvent`` 里改光标的话，鼠标一进缩略图墙光标就会粘住。

Windows 上再顺手让 DWM 把圆角/深色边框补上（Win10 不支持就忽略）。
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QWidget

BORDER = 6          # 边缘判定宽度（px）。中央控件的外边距 ≥ 这个值才不挡内容


def cursor_for(edges) -> Qt.CursorShape:
    """边框组合 → 拉伸光标（斜角优先）。"""
    left = bool(edges & Qt.LeftEdge)
    right = bool(edges & Qt.RightEdge)
    top = bool(edges & Qt.TopEdge)
    bottom = bool(edges & Qt.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return Qt.SizeBDiagCursor
    if left or right:
        return Qt.SizeHorCursor
    if top or bottom:
        return Qt.SizeVerCursor
    return Qt.ArrowCursor


class FramelessWindow(QMainWindow):
    BORDER = BORDER

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMouseTracking(True)
        self._edge_cursor: Qt.CursorShape | None = None
        self._dwm_done = False
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ------------------------------------------------------------ 中央控件
    def setCentralWidget(self, widget: QWidget | None):     # noqa: N802 (Qt 命名)
        super().setCentralWidget(widget)
        if widget is not None:
            widget.setMouseTracking(True)
            widget.installEventFilter(self)

    # ------------------------------------------------------------ 边框几何
    def _edges_at(self, pos) -> Qt.Edge:
        if self.isMaximized() or self.isFullScreen() or self.isMinimized():
            return Qt.Edge(0)
        margin = self.BORDER
        rect = self.rect()
        edges = Qt.Edge(0)
        if pos.x() <= margin:
            edges |= Qt.LeftEdge
        elif pos.x() >= rect.width() - margin - 1:
            edges |= Qt.RightEdge
        if pos.y() <= margin:
            edges |= Qt.TopEdge
        elif pos.y() >= rect.height() - margin - 1:
            edges |= Qt.BottomEdge
        return edges

    def _start_resize(self, edges) -> bool:
        handle = self.windowHandle()
        if handle is None or not edges:
            return False
        try:
            return bool(handle.startSystemResize(edges))
        except (RuntimeError, TypeError):
            return False

    # ------------------------------------------------------------ 拉伸光标
    def _set_edge_cursor(self, shape: Qt.CursorShape):
        app = QApplication.instance()
        if app is None:
            return
        if shape == Qt.ArrowCursor:
            if self._edge_cursor is not None:
                self._edge_cursor = None
                app.restoreOverrideCursor()
            return
        if self._edge_cursor is None:
            app.setOverrideCursor(shape)
        elif self._edge_cursor != shape:
            app.changeOverrideCursor(shape)
        self._edge_cursor = shape

    def _track_cursor(self, event):
        if self.isMinimized() or self.isMaximized() or self.isFullScreen():
            self._set_edge_cursor(Qt.ArrowCursor)
            return
        point = event.globalPosition().toPoint()
        if not self.frameGeometry().contains(point):
            self._set_edge_cursor(Qt.ArrowCursor)
            return
        self._set_edge_cursor(cursor_for(self._edges_at(self.mapFromGlobal(point))))

    # ------------------------------------------------------------ 事件
    def eventFilter(self, obj, event):        # noqa: N802 (Qt 命名)
        kind = event.type()
        if kind == QEvent.MouseMove:
            widget = obj if isinstance(obj, QWidget) else None
            if widget is not None and widget.window() is self and self.isActiveWindow():
                try:
                    self._track_cursor(event)
                except (AttributeError, RuntimeError):
                    pass
        elif kind == QEvent.MouseButtonPress and obj is self.centralWidget():
            if event.button() == Qt.LeftButton:
                point = event.position().toPoint()
                edges = self._edges_at(obj.mapTo(self, point))
                if edges and self._start_resize(edges):
                    return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):         # noqa: N802
        if event.button() == Qt.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges and self._start_resize(edges):
                event.accept()
                return
        super().mousePressEvent(event)

    def leaveEvent(self, event):              # noqa: N802
        self._set_edge_cursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    # ------------------------------------------------------------ DWM
    def apply_dwm(self):
        """Win11：窗口圆角 + 深色边框（老系统忽略）。"""
        if self._dwm_done or sys.platform != "win32":
            return
        self._dwm_done = True
        try:
            import ctypes

            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            round_pref = ctypes.c_int(2)          # DWMWCP_ROUND
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(round_pref),
                                      ctypes.sizeof(round_pref))
            dark = ctypes.c_int(1)                # 深色边框
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark),
                                      ctypes.sizeof(dark))
        except Exception:      # noqa: BLE001  没有这个属性就当没事
            pass

    def showEvent(self, event):               # noqa: N802
        super().showEvent(event)
        self.apply_dwm()

    def closeEvent(self, event):              # noqa: N802
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._set_edge_cursor(Qt.ArrowCursor)
        super().closeEvent(event)
