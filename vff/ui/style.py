# -*- coding: utf-8 -*-
"""图标、调色板与全局样式（Modern Dark Media Studio）。

颜色只有一个来源：这里的常量；QSS 里写的是同一组值的字面量
（Qt 的样式表不支持变量），改动时两处一起改。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QPainter, QPalette, QPen, QPixmap)

# --------------------------------------------------------------------- 调色板
BG = "#17191E"          # 主背景
TOOLBAR = "#20232A"     # 顶部工具栏
PANEL = "#242832"       # 面板
PANEL2 = "#2B303A"      # 次级背景 / 悬停
TEXT = "#E8EAF0"        # 主文字
DIM = "#9299A6"         # 次级文字
ACCENT = "#668CFF"      # 强调色
OK = "#45C995"
WARN = "#E5B65C"
DANGER = "#E56B72"
BORDER = "#343A46"
WALL_BG = "#12151A"     # 关键帧区底色（比主背景更深，突出缩略图）

MONO = '"Cascadia Mono","JetBrains Mono","Consolas","Courier New",monospace'
UI_FONT = '"Microsoft YaHei UI","Segoe UI",sans-serif'

RADIUS_PANEL = 8
RADIUS_BUTTON = 6
CTRL_H = 32


def dark_palette() -> QPalette:
    """深色调色板：让对话框、菜单、滚动条等原生控件跟着变深。"""
    p = QPalette()
    p.setColor(QPalette.Window, QColor(BG))
    p.setColor(QPalette.WindowText, QColor(TEXT))
    p.setColor(QPalette.Base, QColor("#14171C"))
    p.setColor(QPalette.AlternateBase, QColor(PANEL))
    p.setColor(QPalette.ToolTipBase, QColor(PANEL))
    p.setColor(QPalette.ToolTipText, QColor(TEXT))
    p.setColor(QPalette.Text, QColor(TEXT))
    p.setColor(QPalette.Button, QColor(PANEL))
    p.setColor(QPalette.ButtonText, QColor(TEXT))
    p.setColor(QPalette.BrightText, QColor("#FFFFFF"))
    p.setColor(QPalette.Link, QColor(ACCENT))
    p.setColor(QPalette.Highlight, QColor(ACCENT))
    p.setColor(QPalette.HighlightedText, QColor("#0E1116"))
    p.setColor(QPalette.PlaceholderText, QColor(DIM))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#5B6270"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5B6270"))
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor("#5B6270"))
    return p


def _icon_pix(size: int, draw) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    draw(p)
    p.end()
    return pm


def icon_timecode(color: str = DIM, size: int = 18) -> QIcon:
    """时钟：复制时间码。"""
    def draw(p: QPainter):
        p.setPen(QPen(QColor(color), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(2.6, 2.6, 12.8, 12.8))
        p.drawLine(QPointF(9, 5.4), QPointF(9, 9.6))
        p.drawLine(QPointF(9, 9.6), QPointF(11.6, 11.0))
    return QIcon(_icon_pix(size, draw))


def icon_photo(color: str = DIM, size: int = 18) -> QIcon:
    """照片：复制画面。"""
    def draw(p: QPainter):
        p.setPen(QPen(QColor(color), 1.3))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(2.0, 3.6, 14.0, 10.8), 1.6, 1.6)
        p.drawEllipse(QPointF(6.2, 7.2), 1.15, 1.15)
        p.drawPolyline([QPointF(3.2, 13.2), QPointF(7.4, 9.4),
                        QPointF(10.4, 12.0), QPointF(12.9, 9.9),
                        QPointF(15.2, 13.2)])
    return QIcon(_icon_pix(size, draw))


def make_icon() -> QIcon:
    pm = QPixmap(256, 256)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(PANEL))
    p.drawRoundedRect(4, 4, 248, 248, 44, 44)
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(16, 16, 224, 224, 34, 34)
    p.setBrush(QColor("#12151A"))
    for row in range(3):
        for col in range(3):
            p.drawRoundedRect(40 + col * 60, 46 + row * 60, 44, 36, 6, 6)
    p.setBrush(QColor(WARN))
    p.drawEllipse(96, 100, 64, 56)
    p.end()
    return QIcon(pm)


DARK_QSS = f"""
QWidget {{
    color:{TEXT};
    font-family:{UI_FONT};
    font-size:9.5pt;
}}
QMainWindow, QDialog, QWidget#root {{ background:{BG}; }}

/* ------------------------------------------------------------ 面板 */
QFrame#panel {{
    background:{PANEL};
    border:1px solid #2C3240;
    border-radius:{RADIUS_PANEL}px;
}}
QFrame#toolbar {{
    background:{TOOLBAR};
    border:1px solid #262B35;
    border-radius:{RADIUS_PANEL}px;
}}
QFrame#vline {{ background:#2E3440; max-width:1px; min-width:1px; border:none; }}
QFrame#hline {{ background:#2E3440; max-height:1px; min-height:1px; border:none; }}

/* ------------------------------------------------------------ 文字层级 */
QLabel {{ background:transparent; }}
QLabel#title {{ font-size:10pt; font-weight:600; color:{TEXT}; }}
QLabel#section {{ color:{DIM}; font-size:8.6pt; font-weight:600; }}
QLabel#value {{ font-family:{MONO}; color:{TEXT}; font-size:9.4pt; }}
QLabel#valueDim {{ font-family:{MONO}; color:{DIM}; font-size:9.2pt; }}
QLabel#key {{ color:#7E879A; font-size:8.8pt; }}
QLabel#dim {{ color:{DIM}; }}
QLabel#hint {{ color:#6E7686; font-size:8.6pt; }}
QLabel#accent {{ color:{ACCENT}; font-weight:600; }}
QLabel#ok {{ color:{OK}; }}
QLabel#warn {{ color:{WARN}; }}
QLabel#danger {{ color:{DANGER}; }}
QLabel#exportLabel {{ color:{DIM}; font-size:9pt; }}

/* ------------------------------------------------------------ 按钮 */
QPushButton {{
    background:{PANEL2};
    color:{TEXT};
    border:1px solid transparent;
    border-radius:{RADIUS_BUTTON}px;
    padding:0 10px;
    min-height:{CTRL_H - 2}px;
}}
QPushButton:hover {{ background:#39404F; border-color:#4A5570; }}
QPushButton:pressed {{ background:#252A33; }}
QPushButton:disabled {{ color:#5B6270; background:#232830; border-color:transparent; }}
QPushButton:focus {{ outline:none; }}

QPushButton#primary {{
    background:{ACCENT}; color:#0E1116; border:1px solid {ACCENT}; font-weight:600;
}}
QPushButton#primary:hover {{ background:#7E9EFF; border-color:#7E9EFF; }}
QPushButton#primary:disabled {{ background:#2E3646; color:#5B6270; border-color:#39435A; }}

QPushButton#ghost {{
    background:transparent; border-color:transparent; color:{DIM}; padding:0 8px;
}}
QPushButton#ghost:hover {{ background:{PANEL2}; color:{TEXT}; }}
QPushButton#ghost:disabled {{ background:transparent; color:#4E5563; }}

QPushButton#danger {{ color:#B9BFCB; }}
QPushButton#danger:hover {{ background:#3A2A2E; color:{DANGER}; border-color:#5A3A40; }}

QPushButton#step {{
    background:{PANEL2}; padding:0; min-width:44px;
    font-family:{MONO}; font-size:9.6pt;
}}
QPushButton#step:hover {{ background:#39404F; }}
QPushButton#mini {{
    min-height:24px; padding:0 8px; font-size:8.6pt; color:{DIM};
    background:transparent; border-color:{BORDER};
}}
QPushButton#mini:hover {{ color:{TEXT}; background:{PANEL2}; }}

QPushButton#icon {{
    background:transparent; border:none; padding:0;
    min-height:22px; min-width:26px; max-height:22px; border-radius:4px;
}}
QPushButton#icon:hover {{ background:{PANEL2}; }}
QPushButton#icon:pressed {{ background:#252A33; }}

QLabel#chip {{
    background:#2C3448; color:{ACCENT}; border-radius:3px;
    padding:0 5px; font-size:8pt; max-height:16px;
}}
QLabel#chipOut {{ background:#3B3326; color:{WARN}; }}
QLabel#thumbBar {{
    font-family:{MONO}; font-size:8.8pt; color:{TEXT};
}}
QLabel#thumbBarDim {{
    font-family:{MONO}; font-size:8.6pt; color:{DIM};
}}

/* ------------------------------------------------------------ 输入 */
QSpinBox, QComboBox {{
    background:{TOOLBAR}; border:1px solid {BORDER}; border-radius:{RADIUS_BUTTON}px;
    padding:0 8px; min-height:{CTRL_H - 2}px; color:{TEXT};
    selection-background-color:{ACCENT}; selection-color:#0E1116;
}}
QSpinBox:hover, QComboBox:hover {{ border-color:#414B5E; }}
QSpinBox:focus, QComboBox:focus {{ border-color:{ACCENT}; }}
QSpinBox::up-button, QSpinBox::down-button {{ width:14px; background:transparent; border:none; }}
QComboBox::drop-down {{ border:none; width:18px; }}
QComboBox::down-arrow {{
    image:none; width:0; height:0;
    border-left:4px solid transparent; border-right:4px solid transparent;
    border-top:5px solid {DIM};
}}
QComboBox QAbstractItemView {{
    background:{PANEL}; border:1px solid {BORDER}; border-radius:6px;
    selection-background-color:{PANEL2}; selection-color:{TEXT}; padding:4px; outline:none;
}}

/* ------------------------------------------------------------ 标签栏 */
QTabWidget::pane {{ border:none; background:transparent; }}
QTabBar {{ background:transparent; }}
QTabBar::tab {{
    background:#1C1F26; color:{DIM};
    border:1px solid #262B35; border-bottom:none;
    border-top-left-radius:{RADIUS_PANEL}px; border-top-right-radius:{RADIUS_PANEL}px;
    padding:6px 14px; margin-right:4px; margin-top:2px; min-height:24px;
}}
QTabBar::tab:selected {{
    background:{PANEL}; color:{TEXT};
    border-color:{BORDER}; border-top:2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{ background:#20242C; color:#C9CFDA; }}
QTabBar QToolButton {{ background:{PANEL2}; border:1px solid {BORDER}; border-radius:4px; }}

/* ------------------------------------------------------------ 图墙 */
QListView {{
    background:{WALL_BG};
    border:1px solid #262B35;
    border-radius:{RADIUS_PANEL}px;
    outline:none;
}}

/* ------------------------------------------------------------ 片段列表 */
QScrollArea#segScroll {{ background:transparent; border:none; }}
QFrame#segRow {{
    background:{PANEL2};
    border:1px solid transparent;
    border-radius:6px;
}}
QFrame#segRow:hover {{ background:#333947; border-color:#414B5E; }}
QFrame#segRow[active="true"] {{ border-color:{ACCENT}; background:#2C3448; }}
QLabel#segNum {{
    color:{ACCENT}; font-family:{MONO}; font-size:9pt; font-weight:600;
}}
QLabel#segMain {{ font-family:{MONO}; color:{TEXT}; font-size:9pt; }}
QLabel#segDur {{ font-family:{MONO}; color:{DIM}; font-size:8.4pt; }}
QPushButton#segDel {{
    background:transparent; border:none; color:#6E7686;
    min-height:22px; min-width:22px; max-width:22px; padding:0; border-radius:4px;
}}
QPushButton#segDel:hover {{ color:{DANGER}; background:#3A2A2E; }}

/* ------------------------------------------------------------ 其他 */
QProgressBar {{
    background:#1B1F27; border:none; border-radius:3px; height:6px;
    text-align:center; font-size:8pt; color:{DIM};
}}
QProgressBar::chunk {{ background:{ACCENT}; border-radius:3px; }}

QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
QScrollBar::handle:vertical {{ background:#3A4150; min-height:28px; border-radius:4px; }}
QScrollBar::handle:vertical:hover {{ background:#4A5264; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
QScrollBar:horizontal {{ background:transparent; height:10px; margin:2px; }}
QScrollBar::handle:horizontal {{ background:#3A4150; min-width:28px; border-radius:4px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background:transparent; }}

QMenu {{ background:{PANEL}; border:1px solid {BORDER}; border-radius:6px; padding:4px; }}
QMenu::item {{ padding:6px 22px 6px 14px; border-radius:4px; }}
QMenu::item:selected {{ background:{PANEL2}; }}
QMenu::separator {{ height:1px; background:{BORDER}; margin:4px 8px; }}

QToolTip {{
    background:{PANEL}; color:{TEXT}; border:1px solid {BORDER};
    border-radius:4px; padding:5px 8px;
}}

QPlainTextEdit {{
    background:#12151A; border:1px solid #262B35; border-radius:6px;
    font-family:{MONO}; font-size:9pt; color:{TEXT};
    selection-background-color:{ACCENT}; selection-color:#0E1116;
}}
QCheckBox {{ color:#C8CCD2; spacing:8px; }}
QCheckBox::indicator {{
    width:15px; height:15px; border-radius:4px;
    border:1px solid {BORDER}; background:#1B1F27;
}}
QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
QCheckBox::indicator:disabled {{ background:#232830; border-color:#2D333F; }}

QSplitter::handle {{ background:transparent; }}
QSplitter::handle:horizontal {{ width:8px; }}
QSplitter::handle:hover {{ background:{BORDER}; border-radius:3px; }}
QSplitter::handle:pressed {{ background:{ACCENT}; border-radius:3px; }}
"""
