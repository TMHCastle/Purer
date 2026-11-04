# -*- coding: utf-8 -*-
"""诊断对话框与导出确认（P0：错误可读、命令可复制）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)


class LogDialog(QDialog):
    """显示完整命令 + 完整 stderr，并可一键复制。"""

    def __init__(self, parent, title: str, summary: str, command: str,
                 detail: str, ok: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 620)
        self._command = command
        self._detail = detail

        head = QLabel(summary)
        head.setWordWrap(True)
        head.setObjectName("exportLabel")

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        body = f"$ {command}\n\n{detail}".strip() if command else detail
        self.text.setPlainText(body)

        btn_cmd = QPushButton("复制命令")
        btn_detail = QPushButton("复制错误信息")
        btn_close = QPushButton("关闭")
        btn_cmd.clicked.connect(lambda: QApplication.clipboard().setText(self._command))
        btn_detail.clicked.connect(lambda: QApplication.clipboard().setText(self._detail))
        btn_close.clicked.connect(self.accept)
        if ok:
            btn_detail.setText("复制输出")

        bar = QHBoxLayout()
        bar.addWidget(btn_cmd)
        bar.addWidget(btn_detail)
        bar.addStretch(1)
        bar.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.addWidget(head)
        lay.addWidget(self.text, 1)
        lay.addLayout(bar)

    def showEvent(self, e):
        super().showEvent(e)
        self.text.setFocus()


def show_log(parent, title, summary, command, detail, ok=False) -> None:
    LogDialog(parent, title, summary, command, detail, ok=ok).exec()


def ask_export(parent, headline: str, detail: str, exact_available: bool,
               exact_note: str = "") -> tuple[bool, bool]:
    """导出确认：把"两种切法各自切在哪"讲清楚，并让用户选是否帧精确。"""
    dialog = QDialog(parent)
    dialog.setWindowTitle("导出片段")
    dialog.setMinimumWidth(560)

    head = QLabel(headline)
    head.setWordWrap(True)
    head.setStyleSheet("font-weight:600;")

    body = QLabel(detail)
    body.setWordWrap(True)
    body.setTextInteractionFlags(Qt.TextSelectableByMouse)
    body.setObjectName("exportLabel")

    check = QCheckBox("精确到帧（推荐）：只重编码切点附近，中段保持原始码流")
    check.setChecked(exact_available)
    check.setEnabled(exact_available)
    note = QLabel(exact_note)
    note.setWordWrap(True)
    note.setObjectName("dim")
    if not exact_available:
        note.setText("不可用：" + (exact_note or "该文件不支持"))
        note.setStyleSheet("color:#c08a4a;")

    btn_cancel = QPushButton("取消")
    btn_ok = QPushButton("开始导出")
    btn_ok.setObjectName("primary")
    btn_ok.setDefault(True)
    btn_cancel.clicked.connect(dialog.reject)
    btn_ok.clicked.connect(dialog.accept)
    bar = QHBoxLayout()
    bar.addStretch(1)
    bar.addWidget(btn_cancel)
    bar.addWidget(btn_ok)

    lay = QVBoxLayout(dialog)
    lay.setContentsMargins(14, 14, 14, 12)
    lay.setSpacing(10)
    lay.addWidget(head)
    lay.addWidget(body)
    lay.addWidget(check)
    lay.addWidget(note)
    lay.addLayout(bar)

    accepted = dialog.exec() == QDialog.Accepted
    return accepted, bool(accepted and check.isChecked())
