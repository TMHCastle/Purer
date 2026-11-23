# -*- coding: utf-8 -*-
"""「发现新版本」小弹窗（四个选择，一次问清）。

    ┌──────────────────────────────────────────┐
    │ [图标]  发现新版本 v1.2.0                 │
    │         当前 v1.1.10 · 43.8 MB            │
    │                                          │
    │ 更新会先保存当前标签页，换好程序后自动接上， │
    │ 不会丢掉正在剪的片段。                     │
    │                                          │
    │ [ 立即更新 ]            [ 一周后提醒我 ]   │
    │ [ 忽略此版本 ]          [ 不再提醒更新 ]   │
    │                                          │
    │ 查看更新说明                         稍后 │
    └──────────────────────────────────────────┘

调用方只关心 ``dialog.choice``：``now`` / ``week`` / ``skip`` / ``mute`` /
``later``（点 ✕ 或「稍后」都算 later，不改任何策略）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from ..config import APP_NAME, APP_VERSION
from ..util import human_size
from .style import make_icon


def _btn(text: str, style: str = "hintBorder", tip: str = "") -> QPushButton:
    button = QPushButton(text)
    if style:
        button.setObjectName(style)
    if tip:
        button.setToolTip(tip)
    button.setCursor(Qt.PointingHandCursor)
    return button


class UpdatePromptDialog(QDialog):
    """小、直白、四个选择都摆出来（不藏二级菜单）。"""

    def __init__(self, parent=None, tag: str = "", current: str = APP_VERSION,
                 size: int = 0, url: str = ""):
        super().__init__(parent)
        self.setObjectName("updatePrompt")
        self.setWindowTitle(f"发现新版本 · {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(470)
        self.choice = "later"
        self._url = str(url or "")
        self.tag = str(tag or "")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        # ---------------------------------------------------------- 头部
        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(make_icon().pixmap(38, 38))
        icon.setFixedSize(38, 38)
        head.addWidget(icon, 0, Qt.AlignTop)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel(f"发现新版本 {self.tag or ''}".rstrip())
        font = QFont(title.font())
        font.setPointSizeF(font.pointSizeF() + 2.5)
        font.setWeight(QFont.DemiBold)
        title.setFont(font)
        titles.addWidget(title)
        meta = f"当前 v{current}"
        if size:
            meta += f" · 新版本 {human_size(int(size))}"
        subtitle = QLabel(meta)
        subtitle.setObjectName("dim")
        titles.addWidget(subtitle)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        # ---------------------------------------------------------- 说明
        body = QLabel(
            "更新会先保存当前标签页（入点出点、片段、导出记录都在），"
            "换好程序后自动接着干，不用重新打开视频。")
        body.setWordWrap(True)
        lay.addWidget(body)

        note = QLabel("下载期间可以继续剪片子；更新包下载完会先校验 sha256。")
        note.setObjectName("cfgNote")
        note.setWordWrap(True)
        lay.addWidget(note)

        # ---------------------------------------------------------- 四个选择
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        btn_now = _btn("立即更新", "primary",
                       "下载新版本，下好后自动重启并恢复当前会话")
        btn_now.clicked.connect(lambda: self._pick("now"))
        btn_week = _btn("一周后提醒我", "hintBorder",
                        f"7 天后如果还没更新，再问你一次（出现更高版本会立刻提醒）")
        btn_week.clicked.connect(lambda: self._pick("week"))
        btn_skip = _btn("忽略此版本", "hintBorder",
                        f"不再提示 {self.tag}；以后有更高的版本仍会提醒")
        btn_skip.clicked.connect(lambda: self._pick("skip"))
        btn_mute = _btn("不再提醒更新", "hintBorder",
                        "所有更新通知都关掉（右键顶部「检查更新」仍可手动查）")
        btn_mute.clicked.connect(lambda: self._pick("mute"))

        grid.addWidget(btn_now, 0, 0)
        grid.addWidget(btn_week, 0, 1)
        grid.addWidget(btn_skip, 1, 0)
        grid.addWidget(btn_mute, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        # ---------------------------------------------------------- 底部
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.btn_notes = _btn("查看更新说明", "ghost")
        self.btn_notes.clicked.connect(self._open_notes)
        self.btn_notes.setVisible(bool(self._url))
        foot.addWidget(self.btn_notes)
        foot.addStretch(1)
        btn_later = _btn("稍后", "ghost", "这次先不更新（24 小时内不再问）")
        btn_later.clicked.connect(lambda: self._pick("later"))
        foot.addWidget(btn_later)
        lay.addLayout(foot)

        self.btn_now = btn_now
        self.btn_week = btn_week
        self.btn_skip = btn_skip
        self.btn_mute = btn_mute
        self.btn_later = btn_later
        self.btn_now.setDefault(True)

    # ------------------------------------------------------------------ 选择
    def _pick(self, choice: str):
        self.choice = choice
        self.accept()

    def _open_notes(self):
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))

    def reject(self):                      # noqa: N802  Esc / ✕
        self.choice = "later"
        super().reject()
