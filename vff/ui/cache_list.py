# -*- coding: utf-8 -*-
"""已保存缓存的条目表（「从缓存导入」窗 / 设置-缓存 共用）。

一个条目 = 一个视频 + 一组抽帧设置（每 N 帧 / 按镜头档位 / 尺寸）。
表里直接显示视频名、设置、张数、体积、保存时间；双击或「加载视频」打开，
「删除缓存」只删缓存本身，不动视频文件。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ..util import human_size
from .style import DIM, WARN

COLUMNS = ("视频", "设置", "缩略图", "大小", "保存时间")


def setting_label(entry: dict) -> str:
    """条目里的抽帧设置 -> 「每 24 帧」/「按镜头 · 0.30」。"""
    if entry.get("mode") == "scene":
        try:
            threshold = f"{float(entry.get('threshold')):.2f}"
        except (TypeError, ValueError):
            threshold = "?"
        return f"按镜头 · {threshold}"
    try:
        step = f"{float(entry.get('step')):g}"
    except (TypeError, ValueError):
        step = "?"
    return f"每 {step} 帧"


def time_text(stamp) -> str:
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def sorted_entries(store) -> list[dict]:
    entries = [entry for entry in store.entries()
               if store.has(str(entry.get("id")))]
    entries.sort(key=lambda e: float(e.get("last_used_at") or 0), reverse=True)
    return entries


class CacheEntryTable(QWidget):
    """表格 + 「加载视频 / 删除缓存」两个按钮。"""

    openRequested = Signal(dict)
    changed = Signal()          # 删除之后通知外层刷新数量 / 体积

    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self._entries: list[dict] = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setObjectName("cacheTable")
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(128)
        self.table.setMaximumHeight(250)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for index in range(1, len(COLUMNS)):
            header.setSectionResizeMode(index, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        lay.addWidget(self.table)

        self.lbl_hint = QLabel("还没有保存的缓存")
        self.lbl_hint.setObjectName("cfgNote")
        lay.addWidget(self.lbl_hint)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.btn_open = QPushButton("加载视频")
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_delete = QPushButton("删除缓存")
        self.btn_delete.setObjectName("danger")
        self.btn_delete.clicked.connect(self.remove_selected)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_delete)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.refresh()

    # ------------------------------------------------------------------ 数据
    def refresh(self):
        self._entries = sorted_entries(self.store)
        self.table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            video = Path(str(entry.get("video") or ""))
            missing = not video.is_file()
            name = video.name or "（未知视频）"
            item_name = QTableWidgetItem(name + ("　· 原文件不在" if missing else ""))
            item_name.setToolTip(str(video))
            if missing:
                item_name.setForeground(QColor(WARN))
            item_name.setData(Qt.UserRole, entry.get("id"))
            self.table.setItem(row, 0, item_name)
            for column, text in enumerate((
                    setting_label(entry),
                    f"{int(entry.get('count') or 0)} 张",
                    human_size(int(entry.get('bytes') or 0)),
                    time_text(entry.get("saved_at"))), start=1):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(DIM))
                self.table.setItem(row, column, item)
        self.lbl_hint.setVisible(not self._entries)
        self.lbl_hint.setText("还没有保存的缓存" if not self._entries
                              else f"共 {len(self._entries)} 条（双击一行也可以加载）")
        if self._entries:
            self.table.selectRow(0)
        self._sync_buttons()

    def selected_entry(self) -> dict | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    # ------------------------------------------------------------------ 操作
    def _sync_buttons(self):
        has = self.selected_entry() is not None
        self.btn_open.setEnabled(has)
        self.btn_delete.setEnabled(has)

    def _on_double_click(self, row: int, _column: int):
        if 0 <= row < len(self._entries):
            self.openRequested.emit(self._entries[row])

    def open_selected(self):
        entry = self.selected_entry()
        if entry is not None:
            self.openRequested.emit(entry)

    def remove_selected(self):
        entry = self.selected_entry()
        if entry is None:
            return
        video = Path(str(entry.get("video") or ""))
        answer = QMessageBox.question(
            self, "删除缓存",
            f"删除这条缓存吗？\n\n{video.name}\n"
            f"{setting_label(entry)} · {int(entry.get('count') or 0)} 张 · "
            f"{human_size(int(entry.get('bytes') or 0))}\n\n"
            "只删缓存，不会删除视频文件。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.store.drop(str(entry.get("id")))
        self.refresh()
        self.changed.emit()


class CacheImportDialog(QDialog):
    """「从缓存导入」：列出现有缓存，选一条直接打开对应视频。"""

    def __init__(self, parent, store, open_cb):
        super().__init__(parent)
        self.setWindowTitle("从缓存导入")
        self.setModal(True)
        self.resize(820, 540)
        self._open_cb = open_cb

        head = QLabel("从缓存导入")
        font = QFont(head.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.DemiBold)
        head.setFont(font)
        hint = QLabel(
            "挑一条已经保存的缓存，直接打开对应视频（不会再重新抽帧）。\n"
            "原视频不在的条目会标成黄色，可以到 设置-缓存 里删掉。")
        hint.setObjectName("dim")
        hint.setWordWrap(True)

        self.table = CacheEntryTable(store, self)
        self.table.table.setMaximumHeight(10000)
        self.table.openRequested.connect(self._open)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.reject)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)
        lay.addWidget(head)
        lay.addWidget(hint)
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)

    def _open(self, entry: dict):
        callback = self._open_cb
        self.accept()
        if callback is not None:
            QTimer.singleShot(0, lambda: callback(entry))
