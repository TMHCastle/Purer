# -*- coding: utf-8 -*-
"""帧列表模型、绘制委托、大图预览窗、空状态提示。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import (QAbstractListModel, QLocale, QModelIndex, QPoint, QRect,
                            QRectF, QSize, Qt, Signal)
from PySide6.QtGui import (QColor, QDoubleValidator, QFont, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QStyledItemDelegate, QStyle, QVBoxLayout,
                               QWidget)

from ..util import fmt_tc
from .images import thumb_pixmap
from .style import ACCENT, DIM, PANEL2, WALL_BG

THUMB_RADIUS = 4
GAP = 10          # 相邻缩略图之间的视觉间距（左右各半）
TEXT_H = 22
LANE_H = 8        # 时间轴专用窄带高度（画刻度 / 范围条 / 入出点手柄）


# --------------------------------------------------------------------------- #
# 抽帧间隔：预设 + 任意输入
# --------------------------------------------------------------------------- #
class StepCombo(QComboBox):
    """抽帧间隔下拉框：常用预设 + 手输（支持 23.98 这种小数）。

    小数间隔不是「每 N 帧」的整数取模，抽取时改走 `fps=源fps/step`（按时间均匀取），
    所以 23.98 帧 = 23.98fps 视频的「1 秒一张」。
    """

    valueChanged = Signal(float)

    def __init__(self, presets, fractional=(), value=24.0, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setLocale(QLocale.c())
        self.setMaxVisibleItems(15)
        self._value = float(value)
        line = self.lineEdit()
        line.setValidator(QDoubleValidator(1.0, 3000.0, 3, self))
        line.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for v in presets:
            self.addItem(self._text(v), float(v))
        if fractional:
            self.insertSeparator(self.count())
            for v in fractional:
                self.addItem(self._text(v), float(v))
                self.setItemData(self.count() - 1,
                                 f"{float(v):g} 帧 · 常见帧率下约 1 秒",
                                 Qt.ToolTipRole)
        self.set_value(value, notify=False)
        self.activated.connect(self._on_activated)
        line.editingFinished.connect(self._on_edited)

    # ---------------- 值 ----------------
    def value(self) -> float:
        return self._value

    def set_value(self, v, notify: bool = True):
        v = max(1.0, min(3000.0, float(v)))
        changed = abs(v - self._value) > 1e-9
        self._value = v
        index = self._index_of(v)
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setCurrentIndex(-1)
            self.lineEdit().setText(self._text(v))
        if changed and notify:
            self.valueChanged.emit(v)

    def _index_of(self, v) -> int:
        for i in range(self.count()):
            data = self.itemData(i)
            if data is not None and abs(float(data) - v) < 1e-6:
                return i
        return -1

    @staticmethod
    def _text(v) -> str:
        return f"{float(v):g} 帧"

    # ---------------- 交互 ----------------
    def _on_activated(self, index: int):
        data = self.itemData(index)
        if data is not None:
            self.set_value(float(data))

    def _on_edited(self):
        text = self.lineEdit().text().strip().replace("帧", "").strip()
        try:
            value = float(text)
        except ValueError:
            value = self._value
        self.set_value(value)
        self.lineEdit().setText(self._text(self._value))


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
@dataclass
class FrameRef:
    path: str
    t: float          # 相对视频起点（秒）
    frame: int        # 帧号（源视频的帧序号）


class FrameModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[FrameRef] = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        ref = self._items[index.row()]
        if role == Qt.UserRole:
            return ref
        if role == Qt.DisplayRole:
            return fmt_tc(ref.t)
        if role == Qt.ToolTipRole:
            return f"第 {ref.frame} 帧 · {fmt_tc(ref.t)}\n双击看大图"
        return None

    def append(self, refs):
        if not refs:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(refs) - 1)
        self._items.extend(refs)
        self.endInsertRows()

    def clear_items(self):
        self.beginResetModel()
        self._items.clear()
        self.endResetModel()

    def item(self, row):
        return self._items[row] if 0 <= row < len(self._items) else None


class FrameDelegate(QStyledItemDelegate):
    """圆角缩略图 + 等宽时间码 + 悬停/选中状态（无阴影、无厚边框）。"""

    def __init__(self, tw, th, parent=None):
        super().__init__(parent)
        self.tw = tw
        self.th = th
        self.pad = GAP // 2
        self.text_h = TEXT_H

    def sizeHint(self, option, index):
        return QSize(self.tw + 2 * self.pad, self.th + 2 * self.pad + self.text_h + LANE_H)

    def paint(self, painter, option, index):
        ref = index.data(Qt.UserRole)
        r = option.rect
        thumb = QRect(r.left() + self.pad, r.top() + self.pad, self.tw, self.th)
        selected = bool(option.state & QStyle.State_Selected)
        hover = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        path = QPainterPath()
        path.addRoundedRect(QRectF(thumb), THUMB_RADIUS, THUMB_RADIUS)
        painter.fillPath(path, QColor("#0B0D11"))

        if ref is not None:
            pm = thumb_pixmap(ref.path, self.tw, self.th)
            if pm is not None and not pm.isNull():
                if pm.width() != self.tw or pm.height() != self.th:
                    pm = pm.scaled(self.tw, self.th, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation)
                painter.save()
                painter.setClipPath(path)
                x = thumb.left() + (self.tw - pm.width()) // 2
                y = thumb.top() + (self.th - pm.height()) // 2
                painter.drawPixmap(x, y, pm)
                painter.restore()

        if selected:
            painter.fillPath(path, QColor(102, 140, 255, 26))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.drawPath(path)
            self._check_badge(painter, thumb)
        elif hover:
            painter.fillPath(path, QColor(255, 255, 255, 14))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#4A5264"), 1))
            painter.drawPath(path)

        font = QFont(painter.font())
        font.setFamilies(["Cascadia Mono", "Consolas", "Courier New"])
        font.setStyleHint(QFont.Monospace)
        font.setPointSizeF(8.2)
        painter.setFont(font)
        painter.setPen(QColor(ACCENT) if selected else QColor(DIM))
        text = fmt_tc(ref.t) if ref is not None else ""
        painter.drawText(QRect(r.left(), thumb.bottom() + 2, r.width(), self.text_h - 2),
                         Qt.AlignHCenter | Qt.AlignTop, text)
        painter.restore()

    @staticmethod
    def _check_badge(painter: QPainter, thumb: QRect):
        """右上角勾选徽标（14px 圆形）。"""
        cx, cy, rad = thumb.right() - 11, thumb.top() + 11, 8
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(ACCENT))
        painter.drawEllipse(QPoint(cx, cy), rad, rad)
        pen = QPen(QColor("#0E1116"), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(cx - 3, cy, cx - 1, cy + 3)
        painter.drawLine(cx - 1, cy + 3, cx + 4, cy - 3)


# --------------------------------------------------------------------------- #
# 大图窗
# --------------------------------------------------------------------------- #
class PreviewDialog(QDialog):
    def __init__(self, pixmap: QPixmap, caption: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(caption or "画面预览")
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)
        self._src = pixmap
        self.lbl = QLabel(self)
        self.lbl.setAlignment(Qt.AlignCenter)
        self.lbl.setStyleSheet("background:#0B0D11; border-radius:6px;")
        btn_copy = QPushButton("复制画面")
        btn_close = QPushButton("关闭 (Esc)")
        btn_copy.clicked.connect(self._copy)
        btn_close.clicked.connect(self.accept)
        cap = QLabel(caption)
        cap.setObjectName("dim")
        bar = QHBoxLayout()
        bar.addWidget(cap)
        bar.addStretch(1)
        bar.addWidget(btn_copy)
        bar.addWidget(btn_close)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(self.lbl, 1)
        lay.addLayout(bar)
        screen = self.screen() or QApplication.primaryScreen()
        g = screen.availableGeometry()
        self.resize(int(g.width() * 0.86), int(g.height() * 0.88))
        self._fit()

    def _fit(self):
        if self._src.isNull():
            return
        self.lbl.setPixmap(self._src.scaled(self.lbl.size(), Qt.KeepAspectRatio,
                                            Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def _copy(self):
        QApplication.clipboard().setPixmap(self._src)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.accept()
            return
        super().keyPressEvent(e)


# --------------------------------------------------------------------------- #
# 空状态
# --------------------------------------------------------------------------- #
class EmptyState(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(False)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect().adjusted(26, 26, -26, -26))
        p.setPen(QPen(QColor("#2A2F3A"), 2, Qt.DashLine))
        p.setBrush(QColor(WALL_BG))
        p.drawRoundedRect(r, 10, 10)

        # 中间图标：一排"缩略图"
        cell_w, cell_h, gap = 74, 52, 10
        total = cell_w * 4 + gap * 3
        x0 = r.center().x() - total / 2
        y0 = r.center().y() - cell_h - 26
        for i in range(4):
            box = QRectF(x0 + i * (cell_w + gap), y0, cell_w, cell_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(PANEL2 if i != 2 else ACCENT))
            p.drawRoundedRect(box, 4, 4)

        p.setPen(QColor("#C9CFDA"))
        f = QFont(p.font())
        f.setPointSizeF(14)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(r.left(), r.center().y() + 6, r.width(), 40),
                   Qt.AlignCenter, "把视频文件或文件夹拖到这里")

        f.setPointSizeF(9.5)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(r.left(), r.center().y() + 44, r.width(), 30),
                   Qt.AlignCenter, "也可以点左上角「打开视频」")
        p.setPen(QColor("#5B6270"))
        p.drawText(QRectF(r.left(), r.center().y() + 70, r.width(), 30),
                   Qt.AlignCenter,
                   "支持 MP4 / MKV / MOV / TS / AVI / WEBM 等 20+ 种格式")
        p.end()
