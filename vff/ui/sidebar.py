# -*- coding: utf-8 -*-
"""右侧固定操作面板（V1.3.1：信息密度优先）。

五个模块自上而下：
    A 视频信息（两行 + 次级图标操作）
    B 预览（按视频原始宽高比自适应 + 一行工具栏）
    C 当前帧（两行紧凑按钮）
    D 区间（一行时间码 + 帧数/时长）
    E 片段（缩略图列表，唯一滚动区 + 固定底栏）

高度策略：A/C/D 固定紧凑，B 按原始比例取高（并给 E 让位），E 吃掉剩余空间。
**整个面板不滚动** —— 片段再多也只在 E 的列表里滚。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .images import thumb_pixmap
from .style import icon_photo, icon_timecode

SIDEBAR_MIN = 340
SIDEBAR_MAX = 520
PREVIEW_MIN = 64
PREVIEW_MAX = 280
E_MIN_GOOD = 300
E_MIN_SMALL = 220
PREVIEW_FLOOR = 100
SEG_ROW_H = 44
SEG_THUMB = (52, 30)


def panel() -> QFrame:
    frame = QFrame()
    frame.setObjectName("panel")
    frame.setFrameShape(QFrame.NoFrame)
    return frame


def section(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("section")
    return label


def value(text: str = "—", dim: bool = False) -> QLabel:
    label = QLabel(text)
    label.setObjectName("valueDim" if dim else "value")
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _time(t: float) -> str:
    """0:01.2 / 12:03.4 / 1:02:03.4 —— 片段列表用短格式。"""
    t = max(0.0, float(t))
    if t >= 3600:
        return f"{int(t // 3600)}:{int((t % 3600) // 60):02d}:{t % 60:04.1f}"
    return f"{int(t // 60)}:{t % 60:04.1f}"


class SegmentRow(QFrame):
    """一条片段：缩略图 / 序号 / 起止 / 时长 / 删除。"""

    clicked = Signal(int)
    removed = Signal(int)

    def __init__(self, index: int, t1: float, t2: float, thumb: str | None = None,
                 parent=None):
        super().__init__(parent)
        self.index = index
        self.setObjectName("segRow")
        self.setFixedHeight(SEG_ROW_H)
        self.setProperty("active", "false")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(f"第 {index + 1} 段 · {_time(t1)} → {_time(t2)}"
                        f"（{max(0.0, t2 - t1):.2f} 秒）\n点击跳到这一段")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 5, 4)
        lay.setSpacing(7)

        tw, th = SEG_THUMB
        pic = QLabel()
        pic.setFixedSize(tw, th)
        pic.setAlignment(Qt.AlignCenter)
        pic.setStyleSheet("background:#0B0D11; border-radius:3px;")
        if thumb:
            pm = thumb_pixmap(thumb, tw, th)
            if pm is not None and not pm.isNull():
                pic.setPixmap(pm)
        pic.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        num = QLabel(f"{index + 1:02d}")
        num.setObjectName("segNum")
        num.setFixedWidth(15)
        num.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        mid = QVBoxLayout()
        mid.setContentsMargins(0, 0, 0, 0)
        mid.setSpacing(0)
        main = QLabel(f"{_time(t1)} → {_time(t2)}")
        main.setObjectName("segMain")
        sub = QLabel(f"{max(0.0, t2 - t1):.2f} 秒")
        sub.setObjectName("segDur")
        mid.addWidget(main)
        mid.addWidget(sub)

        rm = QPushButton("✕")
        rm.setObjectName("segDel")
        rm.setToolTip("删除这一段")
        rm.clicked.connect(lambda: self.removed.emit(self.index))

        lay.addWidget(pic)
        lay.addWidget(num)
        lay.addLayout(mid, 1)
        lay.addWidget(rm)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)

    def set_active(self, active: bool):
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class EditSidebar(QWidget):
    """固定宽度（可拖拽调整）；只有片段列表内部滚动。"""

    stopRequested = Signal()
    reloadRequested = Signal()
    commandRequested = Signal()
    stepRequested = Signal(int)
    setInRequested = Signal()
    setOutRequested = Signal()
    copyTimecodeRequested = Signal()
    copyFrameRequested = Signal()
    playRequested = Signal()
    exportRequested = Signal()
    revealRequested = Signal()
    addSegmentRequested = Signal()
    clearSegmentsRequested = Signal()
    removeSegmentRequested = Signal(int)
    selectSegmentRequested = Signal(int)
    exportAllSegmentsRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setMinimumWidth(SIDEBAR_MIN)
        self.setMaximumWidth(SIDEBAR_MAX)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._pixmap: QPixmap | None = None
        self._aspect = 16 / 9
        self._rows: list[SegmentRow] = []
        self._segments: list[tuple[float, float, str | None]] = []
        self._active = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self.p_info = self._build_info()
        self.p_preview = self._build_preview()
        self.p_frame = self._build_frame()
        self.p_range = self._build_range()
        self.p_seg = self._build_segments()

        root.addWidget(self.p_info)
        root.addWidget(self.p_preview)
        root.addWidget(self.p_frame)
        root.addWidget(self.p_range)
        root.addWidget(self.p_seg, 1)

        self.set_extract_progress(0, False, False)
        self.set_play_state(None)
        self.set_segments([])

    # ------------------------------------------------------------ 模块 A
    def _build_info(self) -> QFrame:
        box = panel()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(9, 6, 9, 6)
        lay.setSpacing(1)

        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(section("视频"))
        head.addStretch(1)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setObjectName("mini")
        self.btn_stop.setToolTip("停止抽帧")
        self.btn_stop.clicked.connect(self.stopRequested)
        self.btn_reload = QPushButton("↻ 重抽")
        self.btn_reload.setObjectName("mini")
        self.btn_reload.setToolTip("按当前「间隔帧」重新抽取这个视频（显示尺寸不受影响）")
        self.btn_reload.clicked.connect(self.reloadRequested)
        self.btn_cmd = QPushButton("命令")
        self.btn_cmd.setObjectName("mini")
        self.btn_cmd.setToolTip("查看 / 复制本次抽帧用的 ffmpeg 命令")
        self.btn_cmd.clicked.connect(self.commandRequested)
        head.addWidget(self.btn_stop)
        head.addWidget(self.btn_reload)
        head.addWidget(self.btn_cmd)
        lay.addLayout(head)

        self.lbl_info = value("读取视频信息…")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.lbl_info2 = QLabel("")
        self.lbl_info2.setObjectName("valueDim")
        self.lbl_info2.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_count = QLabel("")
        self.lbl_count.setObjectName("dim")
        self.lbl_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_count.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        row2.addWidget(self.lbl_info2, 1)
        row2.addWidget(self.lbl_count)
        lay.addLayout(row2)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFixedHeight(4)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        return box

    # ------------------------------------------------------------ 模块 B
    def _build_preview(self) -> QFrame:
        box = panel()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(6, 6, 6, 5)
        lay.setSpacing(4)

        self.image = QLabel("点击左侧任意一帧\n在这里预览")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setFixedHeight(150)
        self.image.setMinimumWidth(120)
        self.image.setStyleSheet(
            "background:#0B0D11; border:1px solid #262B35; border-radius:5px;"
            "color:#5B6270; font-size:9pt;")
        self.image.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        holder = QHBoxLayout()
        holder.setContentsMargins(0, 0, 0, 0)
        holder.addStretch(1)
        holder.addWidget(self.image)
        holder.addStretch(1)
        lay.addLayout(holder)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.lbl_frame = QLabel("—")
        self.lbl_frame.setObjectName("thumbBar")
        self.lbl_frame.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bar.addWidget(self.lbl_frame, 1)

        self.lbl_marks = QLabel("")
        self.lbl_marks.setVisible(False)
        bar.addWidget(self.lbl_marks)

        self.btn_copy_tc = QPushButton()
        self.btn_copy_img = QPushButton()
        for btn, icon, tip in (
                (self.btn_copy_tc, icon_timecode(), "复制当前帧时间码"),
                (self.btn_copy_img, icon_photo(), "复制当前画面到剪贴板")):
            btn.setObjectName("icon")
            btn.setIcon(icon)
            btn.setIconSize(QSize(16, 16))
            btn.setToolTip(tip)
            btn.setFixedSize(24, 22)
            bar.addWidget(btn)
        self.btn_copy_tc.clicked.connect(self.copyTimecodeRequested)
        self.btn_copy_img.clicked.connect(self.copyFrameRequested)
        lay.addLayout(bar)

        self.play_bar = QProgressBar()
        self.play_bar.setRange(0, 100)
        self.play_bar.setValue(0)
        self.play_bar.setFixedHeight(3)
        self.play_bar.setTextVisible(False)
        lay.addWidget(self.play_bar)
        return box

    # ------------------------------------------------------------ 模块 C
    def _build_frame(self) -> QFrame:
        box = panel()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)

        step = QHBoxLayout()
        step.setSpacing(4)
        self.btn_prev_s = QPushButton("-1s")
        self.btn_next_s = QPushButton("+1s")
        self.btn_prev = QPushButton("◀")
        self.btn_next = QPushButton("▶")
        for btn, tip, delta in (
                (self.btn_prev_s, "退 1 秒（Shift+←）", -1000),
                (self.btn_prev, "上一帧（←）", -1),
                (self.btn_next, "下一帧（→）", 1),
                (self.btn_next_s, "进 1 秒（Shift+→）", 1000)):
            btn.setObjectName("step")
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, d=delta: self.stepRequested.emit(d))
            step.addWidget(btn, 1)
        lay.addLayout(step)

        marks = QHBoxLayout()
        marks.setSpacing(4)
        self.btn_in = QPushButton("设入点  I")
        self.btn_out = QPushButton("设出点  O")
        self.btn_in.clicked.connect(self.setInRequested)
        self.btn_out.clicked.connect(self.setOutRequested)
        marks.addWidget(self.btn_in, 1)
        marks.addWidget(self.btn_out, 1)
        lay.addLayout(marks)
        return box

    # ------------------------------------------------------------ 模块 D
    def _build_range(self) -> QFrame:
        box = panel()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        line = QHBoxLayout()
        line.setSpacing(6)
        self.lbl_range = QLabel("")
        self.lbl_range.setObjectName("thumbBarDim")
        self.lbl_range.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_range.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_dur = QLabel("")
        self.lbl_dur.setObjectName("accent")
        self.lbl_dur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_dur.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        line.addWidget(self.lbl_range, 1)
        line.addWidget(self.lbl_dur)
        lay.addLayout(line)

        act = QHBoxLayout()
        act.setSpacing(4)
        self.btn_play = QPushButton("▶ 播放区间")
        self.btn_play.setEnabled(False)
        self.btn_play.setToolTip("在右侧预览里回放入点→出点（空格）")
        self.btn_play.clicked.connect(self.playRequested)
        self.btn_export = QPushButton("✂ 导出片段")
        self.btn_export.setObjectName("primary")
        self.btn_export.setEnabled(False)
        self.btn_export.setToolTip("按入点/出点切一段（I/O 或 Shift 框两帧）")
        self.btn_export.clicked.connect(self.exportRequested)
        act.addWidget(self.btn_play, 1)
        act.addWidget(self.btn_export, 1)
        lay.addLayout(act)
        return box

    # ------------------------------------------------------------ 模块 E
    def _build_segments(self) -> QFrame:
        box = panel()
        box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        box.setMinimumHeight(150)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.lbl_seg_title = section("片段")
        head.addWidget(self.lbl_seg_title)
        head.addStretch(1)
        self.btn_clear_seg = QPushButton("清空")
        self.btn_clear_seg.setObjectName("mini")
        self.btn_clear_seg.setToolTip("清空片段列表")
        self.btn_clear_seg.clicked.connect(self.clearSegmentsRequested)
        self.btn_add_seg = QPushButton("＋ 加入  A")
        self.btn_add_seg.setObjectName("mini")
        self.btn_add_seg.setToolTip("把当前入点→出点加入片段列表（快捷键 A）")
        self.btn_add_seg.clicked.connect(self.addSegmentRequested)
        head.addWidget(self.btn_clear_seg)
        head.addWidget(self.btn_add_seg)
        lay.addLayout(head)

        self.seg_scroll = QScrollArea()
        self.seg_scroll.setObjectName("segScroll")
        self.seg_scroll.setWidgetResizable(True)
        self.seg_scroll.setFrameShape(QFrame.NoFrame)
        self.seg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.seg_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.seg_scroll.setMinimumHeight(44)
        body = QWidget()
        body.setObjectName("segBody")
        self.seg_lay = QVBoxLayout(body)
        self.seg_lay.setContentsMargins(0, 0, 2, 0)
        self.seg_lay.setSpacing(3)
        self.seg_lay.addStretch(1)
        self.seg_scroll.setWidget(body)
        lay.addWidget(self.seg_scroll, 1)

        self.btn_export_all = QPushButton("✂ 导出全部")
        self.btn_export_all.setObjectName("primary")
        self.btn_export_all.setEnabled(False)
        self.btn_export_all.setToolTip("把列表里的片段一次性导出（每段单独校验）")
        self.btn_export_all.clicked.connect(self.exportAllSegmentsRequested)
        lay.addWidget(self.btn_export_all)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("exportLabel")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMaximumHeight(30)
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Maximum)
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_status)

        self.exp_bar = QProgressBar()
        self.exp_bar.setRange(0, 100)
        self.exp_bar.setValue(0)
        self.exp_bar.setFixedHeight(4)
        self.exp_bar.setTextVisible(False)
        self.exp_bar.setVisible(False)
        lay.addWidget(self.exp_bar)

        self.btn_reveal = QPushButton("📂 打开所在文件夹")
        self.btn_reveal.setObjectName("mini")
        self.btn_reveal.setVisible(False)
        self.btn_reveal.clicked.connect(self.revealRequested)
        lay.addWidget(self.btn_reveal)
        return box

    # ------------------------------------------------------------ 数据接口
    def set_video_info(self, line1: str, line2: str = ""):
        self.lbl_info.setText(line1)
        self.lbl_info2.setText(line2)
        self.lbl_info2.setVisible(bool(line2))

    def set_aspect(self, width: int, height: int):
        if width > 0 and height > 0:
            self._aspect = width / float(height)
            self._apply_preview_height()

    def set_count(self, text: str):
        self.lbl_count.setText(text)

    def set_extract_progress(self, value: int, visible: bool, running: bool = True):
        self.bar.setVisible(visible)
        if value >= 0:
            self.bar.setValue(value)
        self.btn_stop.setVisible(running)

    def set_frame(self, pixmap: QPixmap | None, title: str):
        self._pixmap = pixmap if (pixmap is not None and not pixmap.isNull()) else None
        self.lbl_frame.setText(title)
        if self._pixmap is None:
            self.image.setText("取不到这一帧")
            self.image.setPixmap(QPixmap())
        else:
            self.image.setText("")
            self._fit()

    def set_frame_marks(self, marks: list[str]):
        if not marks:
            self.lbl_marks.setVisible(False)
            return
        text = " · ".join(marks)
        self.lbl_marks.setText(text)
        color = "#668CFF" if "入点" in text else "#E5B65C"
        self.lbl_marks.setStyleSheet(f"color:{color}; font-size:8.2pt;")
        self.lbl_marks.setVisible(True)

    def set_range(self, main: str, sub: str = ""):
        self.lbl_range.setText(main)
        self.lbl_dur.setText(sub)

    def set_range_text(self, text: str):
        self.lbl_range.setText(text)

    def set_range_enabled(self, can_play: bool, can_export: bool):
        self.btn_play.setEnabled(can_play)
        self.btn_export.setEnabled(can_export)

    def set_play_state(self, playing: bool | None, progress: float = -1.0):
        """playing=None 表示关闭播放进度条。"""
        if playing is None:
            self.play_bar.setVisible(False)
            self.btn_play.setText("▶ 播放")
        else:
            self.play_bar.setVisible(True)
            self.play_bar.setValue(int(max(0.0, min(100.0, progress * 100))))
            self.btn_play.setText("⏸ 暂停" if playing else "▶ 继续")
        self._apply_preview_height()

    def set_export_text(self, text: str):
        self.lbl_status.setText(text)
        self.lbl_status.setToolTip(text)
        self.lbl_status.setVisible(bool(text))

    def set_export_progress(self, value: int):
        if value < 0:
            self.exp_bar.setVisible(False)
            return
        self.exp_bar.setVisible(True)
        self.exp_bar.setValue(value)

    def set_reveal_visible(self, visible: bool):
        self.btn_reveal.setVisible(visible)

    # ------------------------------------------------------------ 片段
    def set_segments(self, segments: list):
        """segments: [(t_in, t_end, 缩略图路径或 None), ...]"""
        self._segments = list(segments)
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        while self.seg_lay.count() > 1:          # 末尾留 stretch
            item = self.seg_lay.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for i, seg in enumerate(segments):
            t1, t2 = seg[0], seg[1]
            thumb = seg[2] if len(seg) > 2 else None
            row = SegmentRow(i, t1, t2, thumb, self)
            row.clicked.connect(self.selectSegmentRequested)
            row.removed.connect(self.removeSegmentRequested)
            self.seg_lay.insertWidget(self.seg_lay.count() - 1, row)
            self._rows.append(row)
        if not segments:
            hint = QLabel("还没有片段\n设好入点 / 出点后按 A 加入")
            hint.setObjectName("hint")
            hint.setAlignment(Qt.AlignCenter)
            hint.setMinimumHeight(48)
            self.seg_lay.insertWidget(0, hint)
        self._active = -1
        self._update_summary()
        busy = not self.btn_add_seg.isEnabled()
        self.btn_export_all.setEnabled(bool(segments) and not busy)
        self.btn_export_all.setText(f"✂ 导出全部（{len(segments)}）" if segments
                                    else "✂ 导出全部")
        self.btn_clear_seg.setVisible(bool(segments))

    def set_active_segment(self, index: int):
        self._active = index
        for i, row in enumerate(self._rows):
            row.set_active(i == index)

    def _update_summary(self):
        count = len(self._segments)
        if not count:
            self.lbl_seg_title.setText("片段")
            return
        total = sum(max(0.0, s[1] - s[0]) for s in self._segments)
        self.lbl_seg_title.setText(f"片段 · {count} 段 · {_time(total)}")

    def set_segments_busy(self, busy: bool):
        self.btn_add_seg.setEnabled(not busy)
        self.btn_clear_seg.setEnabled(not busy and bool(self._segments))
        self.btn_export_all.setEnabled(not busy and bool(self._segments))

    def set_busy(self, busy: bool):
        """给批量导出用的别名（沿用旧调用点）。"""
        self.set_segments_busy(busy)

    # ------------------------------------------------------------ 绘制
    def _fit(self):
        if self._pixmap is None:
            return
        area = self.image.size()
        if area.width() < 20 or area.height() < 20:
            return
        self.image.setPixmap(self._pixmap.scaled(
            area, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_preview_height()
        self._fit()

    def _apply_preview_height(self):
        """预览高度 = 原始比例算出的自然高度，并保证片段列表有足够空间。"""
        total = self.height()
        lay = self.layout()
        if total < 100 or lay is None:
            return
        try:
            if self.lbl_info2.text():
                self.lbl_info2.setVisible(total >= 700)
            chrome = self.p_preview.sizeHint().height() - self.image.height()
            used = chrome
            for box in (self.p_info, self.p_frame, self.p_range):
                used += box.minimumSizeHint().height()
            margins = lay.contentsMargins()
            used += margins.top() + margins.bottom()
            used += lay.spacing() * (lay.count() - 1)
            room = total - used                      # 预留给 preview + 片段列表
            avail_w = max(120, self.p_preview.width() - 14)
            natural = int(round(avail_w / self._aspect)) if self._aspect > 0 else 180
            # 先保证列表有 300px；只有当预览会被压到 100 以下时才退回 220
            target = min(natural, room - E_MIN_GOOD, PREVIEW_MAX)
            if target < PREVIEW_FLOOR:
                target = min(natural, room - E_MIN_SMALL, PREVIEW_MAX)
        except RuntimeError:
            return
        target = max(PREVIEW_MIN, target)
        avail_w = max(120, self.p_preview.width() - 14)
        width = max(120, min(avail_w, int(round(target * (self._aspect or 1.78)))))
        if (abs(self.image.height() - target) > 2
                or abs(self.image.width() - width) > 2):
            self.image.setFixedSize(width, target)
            self._fit()
