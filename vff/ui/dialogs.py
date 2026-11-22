# -*- coding: utf-8 -*-
"""诊断对话框与导出确认（P0：错误可读、命令可复制）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..export_names import series_label


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


def ask_batch_export(parent, headline: str, rows: list[str],
                     prefill: list[str], defaults: list[str],
                     exact_available: bool, exact_note: str = "",
                     folder: str = "") -> tuple[bool, bool, list[str]]:
    """批量导出确认：每一段都能改素材名（留空 = 自动系列名），并选精确/无损。

    ``rows``     每段的时间区间说明（不含名字）
    ``prefill``  已经填过的名字（再次导出时记住）
    ``defaults`` 留空时用的默认文件名（已含扩展名，也已避开重名）
    """
    dialog = QDialog(parent)
    dialog.setWindowTitle("导出全部片段")
    dialog.setMinimumWidth(660)
    dialog.resize(700, min(620, 260 + 46 * max(1, len(rows))))

    head = QLabel(headline)
    head.setWordWrap(True)
    head.setStyleSheet("font-weight:600;")
    hint = QLabel(
        "每一段可以自己起名；留空就用系列名："
        + "、".join(series_label(i) for i in range(min(3, max(1, len(rows)))))
        + " …（加在原视频名前面）。重名会自动加 (2)、(3)，不会覆盖已有文件。")
    hint.setWordWrap(True)
    hint.setObjectName("dim")

    body = QWidget()
    body.setObjectName("cfgBody")
    grid = QGridLayout(body)
    grid.setContentsMargins(2, 2, 8, 2)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(7)
    grid.addWidget(QLabel("#"), 0, 0)
    grid.addWidget(QLabel("时间区间"), 0, 1)
    grid.addWidget(QLabel("素材名（留空 = 自动）"), 0, 2)
    edits: list[QLineEdit] = []
    for i, row in enumerate(rows):
        num = QLabel(f"{i + 1}")
        num.setObjectName("dim")
        num.setFixedWidth(18)
        span = QLabel(row)
        span.setObjectName("thumbBarDim")
        edit = QLineEdit()
        edit.setPlaceholderText(defaults[i] if i < len(defaults) else "")
        edit.setClearButtonEnabled(True)
        if i < len(prefill) and prefill[i]:
            edit.setText(prefill[i])
        edit.setToolTip(f"留空 = {defaults[i] if i < len(defaults) else ''}")
        grid.addWidget(num, i + 1, 0)
        grid.addWidget(span, i + 1, 1)
        grid.addWidget(edit, i + 1, 2)
        edits.append(edit)
    grid.setColumnStretch(2, 1)

    scroll = QScrollArea()
    scroll.setObjectName("cfgScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(body)
    scroll.setMinimumHeight(120)

    check = QCheckBox("精确到帧（推荐）：只重编码切点附近，中段保持原始码流")
    check.setChecked(exact_available)
    check.setEnabled(exact_available)
    note = QLabel(exact_note)
    note.setWordWrap(True)
    note.setObjectName("dim")
    if not exact_available:
        note.setText("不可用：" + (exact_note or "该文件不支持"))
        note.setStyleSheet("color:#c08a4a;")

    folder_label = QLabel(f"输出到：{folder}" if folder else "")
    folder_label.setObjectName("dim")
    folder_label.setWordWrap(True)

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
    lay.setSpacing(8)
    lay.addWidget(head)
    if folder:
        lay.addWidget(folder_label)
    lay.addWidget(hint)
    lay.addWidget(scroll, 1)
    lay.addWidget(check)
    lay.addWidget(note)
    lay.addLayout(bar)

    accepted = dialog.exec() == QDialog.Accepted
    texts = [edit.text().strip() for edit in edits] if accepted else []
    return accepted, bool(accepted and check.isChecked()), texts
