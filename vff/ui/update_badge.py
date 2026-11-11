# -*- coding: utf-8 -*-
"""顶部卡片正中的那颗「检查更新」药丸。

颜色只有三种含义，跟约定一一对应：
    绿  ``latest``   已经是最新版本
    橙  ``stale``    有新版本（点一下打开发布页）
    灰  ``error``    没查成（网络/限流/被墙都算），点一下重试
另外 ``checking`` 用蓝色小圆点呼吸，表示正在查。

右键 = 重新检查（``stale`` 时左键是去下载，需要另一个入口来复查）。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from ..config import APP_VERSION
from .style import ACCENT, DIM, OK, WARN

# state -> (圆点色, 边框色, 底色, 文字色)
COLORS = {
    "idle":     ("#6E7686", "#3A4150", "#20242C", "#B9BFCB"),
    "checking": (ACCENT,    "#3A4150", "#20242C", DIM),
    "latest":   (OK,        OK,        "rgba(69,201,149,30)",  OK),
    "stale":    (WARN,      WARN,      "rgba(229,182,92,34)",  WARN),
    "error":    ("#6E7686", "#3A4150", "#1C1F26", "#9299A6"),
    # 自动更新：下载中用强调蓝，下好了用绿色（点一下就去装）
    "downloading": (ACCENT, ACCENT, "rgba(102,140,255,34)", "#A9BEFF"),
    "ready":       (OK,     OK,     "rgba(69,201,149,38)",  OK),
}


class UpdateBadge(QPushButton):
    recheckRequested = Signal()
    sizeChanged = Signal()          # 文案变了 → 让标题栏重新摆正中的位置

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("updateBadge")
        self.setFocusPolicy(Qt.NoFocus)
        self._state = "idle"
        self._tag = ""
        self._url = ""
        self._blink_on = True

        row = QHBoxLayout(self)
        row.setContentsMargins(11, 0, 13, 0)
        row.setSpacing(7)
        self._dot = QLabel(self)
        self._dot.setFixedSize(8, 8)
        self._label = QLabel(self)
        for child in (self._dot, self._label):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            row.addWidget(child)

        self._pulse = QTimer(self)
        self._pulse.setInterval(420)
        self._pulse.timeout.connect(self._blink)

        self.set_state("idle")

    # ------------------------------------------------------------------ 状态
    def sizeHint(self):                        # noqa: N802
        """QPushButton 的 sizeHint 只看自己的 text（这里是空的），
        而药丸的内容全在子布局里 —— 必须自己算，否则会被压成一条缝。"""
        layout = self.layout()
        base = layout.sizeHint() if layout is not None else QSize(0, 0)
        return QSize(base.width(), max(base.height(), 26))

    def state_name(self) -> str:
        return self._state

    def tag(self) -> str:
        return self._tag

    def url(self) -> str:
        return self._url

    def label_text(self) -> str:
        return self._label.text()

    def dot_color(self) -> str:
        return COLORS[self._state][0]

    def set_state(self, state: str, tag: str = "", url: str = "",
                  tip: str = "", detail: str = ""):
        if state not in COLORS:
            state = "idle"
        self._state = state
        self._tag = tag
        self._url = url

        self._apply_colors()
        self._label.setText(self._text_for(state, tag, detail))
        self.setToolTip(tip or self._default_tip(state, tag))
        if state in ("checking", "downloading"):
            self.setCursor(Qt.ArrowCursor)
            self._blink_on = True
            self._pulse.start()
        else:
            self._pulse.stop()
            self._blink_on = True
            self._dot.setStyleSheet(
                f"background:{COLORS[state][0]};border-radius:4px;")
            self.setCursor(Qt.PointingHandCursor)
        self.adjustSize()
        self.sizeChanged.emit()          # 文案长短会变 → 让标题栏重新居中

    def _apply_colors(self):
        _dot, border, background, foreground = COLORS[self._state]
        self._label.setStyleSheet(f"color:{foreground};font-size:9pt;")
        self._dot.setStyleSheet(
            f"background:{COLORS[self._state][0]};border-radius:4px;")
        self.setStyleSheet(
            f"QPushButton#updateBadge {{ background:{background};"
            f" border:1px solid {border}; border-radius:13px; padding:0;"
            f" min-height:26px; max-height:26px; }}"
            f"QPushButton#updateBadge:hover {{ border-color:{foreground}; }}"
            "QPushButton#updateBadge:pressed { background:#161920; }"
        )

    def set_progress(self, pct: int):
        """下载中只改数字（不动样式，避免每 400ms 重新 polish 一遍）。"""
        if self._state != "downloading":
            return
        text = self._text_for("downloading", self._tag, f"{max(0, int(pct))}%")
        if text != self._label.text():
            self._label.setText(text)
            self.adjustSize()
            self.sizeChanged.emit()

    def _text_for(self, state: str, tag: str, detail: str = "") -> str:
        if state == "latest":
            return f"已是最新 v{APP_VERSION}"
        if state == "stale":
            return f"有新版本 {tag}" if tag else "有新版本"
        if state == "downloading":
            return f"下载中 {detail or '…'}"
        if state == "ready":
            return f"已下载 {tag} · 重启安装" if tag else "已下载 · 重启安装"
        if state == "checking":
            return "检查更新中…"
        if state == "error":
            return "检查更新失败"
        return "检查更新"

    def _default_tip(self, state: str, tag: str) -> str:
        if state == "checking":
            return "正在向 GitHub 查询最新发布版本…"
        if state == "latest":
            return f"当前就是最新版 v{APP_VERSION}\n点击重新检查"
        if state == "stale":
            return (f"GitHub 上有更新的版本 {tag or ''}\n"
                    "点击下载并自动安装（右键重新检查）")
        if state == "downloading":
            return "正在后台下载新版本，界面可以继续用\n下载完会校验 sha256 再装"
        if state == "ready":
            return (f"新版本 {tag or ''} 已下载并校验通过\n"
                    "点击立即安装并重启")
        if state == "error":
            return "上次没查到（网络/限流），点击重试"
        return "点一下看看 GitHub 上有没有新版本"

    def _blink(self):
        """检查中 / 下载中：圆点呼吸，明确「还在跑」。"""
        self._blink_on = not self._blink_on
        base = COLORS[self._state][0] if self._state in ("checking", "downloading") \
            else COLORS["checking"][0]
        color = base if self._blink_on else "#2B3548"
        self._dot.setStyleSheet(f"background:{color};border-radius:4px;")

    # ------------------------------------------------------------------ 交互
    def contextMenuEvent(self, event):          # noqa: N802
        self.recheckRequested.emit()
        event.accept()
