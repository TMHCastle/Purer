# -*- coding: utf-8 -*-
"""主窗口：自绘标题栏 + 顶部工具栏 + 多标签页 + 拖放 + 抽帧队列。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QProgressDialog, QPushButton,
                               QSizePolicy, QStackedWidget, QTabWidget,
                               QVBoxLayout, QWidget)

from ..cache import cache_root
from ..config import (APP_NAME, APP_VERSION, DEFAULT_DISPLAY_SIDE, DEFAULT_STEP,
                      DISPLAY_MAX, DISPLAY_MIN, DISPLAY_PRESETS, DISPLAY_STEP,
                      EXTRACT_LONG_SIDE, JOBS_MAX_CONCURRENT, MAX_THUMBS_WARN,
                      STEP_PRESETS, STEP_PRESETS_FRACTIONAL, VIDEO_EXTS)
from ..ffmpeg import probe
from ..jobs import JobQueue
from ..media import find_videos, thumb_size_for
from ..update import RELEASES_PAGE, ReleaseChecker
from ..util import fmt_tc
from ..version import normalize
from ..log import debug, debug_enabled
from .frameless import FramelessWindow
from .style import make_icon
from .titlebar import TitleBar
from .track_page import TrackPage
from .update_badge import UpdateBadge
from .widgets import ArrowCombo, EmptyState, StepCombo

CUSTOM_INDEX = len(DISPLAY_PRESETS)


def update_checks_enabled() -> bool:
    """``PURER_NO_UPDATE=1`` 时完全不联网（离线机器 / 自动化测试用）。"""
    flag = os.environ.get("PURER_NO_UPDATE", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")


def _ui_settings() -> QSettings:
    root = cache_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return QSettings(str(root / "ui.ini"), QSettings.IniFormat)


class MainWindow(FramelessWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(self._title(DEFAULT_STEP))
        self.resize(1440, 880)
        self.setMinimumSize(1024, 720)
        self.setAcceptDrops(True)
        self.setWindowIcon(make_icon())
        self._pages: list[TrackPage] = []
        self._status_prev = ""
        self._job_text = ""
        self._syncing_combo = False
        self.setting = _ui_settings()
        try:
            self._display_side = max(DISPLAY_MIN, min(
                DISPLAY_MAX, int(self.setting.value("ui/display_side",
                                                    DEFAULT_DISPLAY_SIDE))))
            self._sidebar_width = int(self.setting.value("ui/sidebar_width", 372))
        except (TypeError, ValueError):
            self._display_side, self._sidebar_width = DEFAULT_DISPLAY_SIDE, 372

        self.jobs = JobQueue(max_concurrent=JOBS_MAX_CONCURRENT, parent=self)
        self.jobs.jobStarted.connect(self._refresh_status)
        self.jobs.jobFinished.connect(self._refresh_status)

        central = QWidget()
        central.setObjectName("root")
        lay = QVBoxLayout(central)
        lay.setContentsMargins(12, 12, 12, 10)
        lay.setSpacing(8)

        # 顶部卡片：第一行是自己的标题栏（窗口没有系统边框），第二行是原来的工具栏
        self.card = QFrame()
        self.card.setObjectName("topcard")
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        self.titlebar = TitleBar(self.card)
        self.badge = UpdateBadge(self.titlebar)
        self.titlebar.set_center_widget(self.badge)
        card_lay.addWidget(self.titlebar)
        card_lay.addWidget(self._hline())
        card_lay.addWidget(self._build_toolbar())
        lay.addWidget(self.card)

        self.stack = QStackedWidget()
        self.empty = EmptyState()
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tabs)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._refresh_stack()
        self._setup_updater()
        self.titlebar.sync_max_button()
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_files)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._close_current)
        for i in range(len(DISPLAY_PRESETS)):
            QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self,
                      activated=lambda n=i: self._pick_size(n))

    # ------------------------------------------------------------ 工具栏
    def _build_toolbar(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("toolbar")
        frame.setFixedHeight(52)
        bar = QHBoxLayout(frame)
        bar.setContentsMargins(12, 6, 12, 8)
        bar.setSpacing(8)

        btn_open = QPushButton("打开视频…")
        btn_open.setObjectName("primary")
        btn_open.setToolTip("打开一个或多个视频（Ctrl+O）")
        btn_open.clicked.connect(self.open_files)
        btn_dir = QPushButton("添加文件夹")
        btn_dir.setObjectName("ghost")
        btn_dir.setToolTip("把一个文件夹里所有视频加进来")
        btn_dir.clicked.connect(self.open_folder)
        bar.addWidget(btn_open)
        bar.addWidget(btn_dir)

        bar.addSpacing(6)
        bar.addWidget(self._vline())
        bar.addSpacing(6)

        bar.addWidget(QLabel("间隔"))
        self.combo_step = StepCombo(STEP_PRESETS, STEP_PRESETS_FRACTIONAL, DEFAULT_STEP)
        self.combo_step.setFixedWidth(104)
        self.combo_step.setToolTip(
            "每隔多少帧取一张（可直接输入，支持 23.98 这种小数）\n"
            "23.98 / 29.97 / 59.94 = 常见帧率下「约 1 秒」的帧数\n"
            "改动只影响之后打开的视频；当前页可点「↻ 重抽」")
        self.combo_step.valueChanged.connect(self._on_step_changed)
        bar.addWidget(self.combo_step)

        bar.addSpacing(4)
        bar.addWidget(QLabel("缩略图"))
        self.btn_zoom_out = QPushButton("－")
        self.btn_zoom_out.setObjectName("mini")
        self.btn_zoom_out.setFixedWidth(26)
        self.btn_zoom_out.setToolTip("缩小缩略图（Ctrl+滚轮 也可以）")
        self.btn_zoom_out.clicked.connect(lambda: self._zoom(-1))
        self.combo_size = ArrowCombo()
        for label, px in DISPLAY_PRESETS:
            self.combo_size.addItem(f"{label} · {px}", px)
        self.combo_size.addItem("自定义", None)
        self.combo_size.setFixedWidth(118)
        self.combo_size.setToolTip("缩略图「显示」尺寸（Ctrl+1~4 / Ctrl+滚轮实时缩放）\n"
                                   "只改显示大小，不会重新抽帧")
        self._sync_size_combo(self._display_side)
        self.combo_size.currentIndexChanged.connect(self._on_size_changed)
        self.btn_zoom_in = QPushButton("＋")
        self.btn_zoom_in.setObjectName("mini")
        self.btn_zoom_in.setFixedWidth(26)
        self.btn_zoom_in.setToolTip("放大缩略图（Ctrl+滚轮 也可以）")
        self.btn_zoom_in.clicked.connect(lambda: self._zoom(1))
        bar.addWidget(self.btn_zoom_out)
        bar.addWidget(self.combo_size)
        bar.addWidget(self.btn_zoom_in)

        bar.addStretch(1)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("dim")
        self.lbl_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bar.addWidget(self.lbl_status, 1)

        btn_clear = QPushButton("清空全部")
        btn_clear.setObjectName("danger")
        btn_clear.setToolTip("关闭所有标签页")
        btn_clear.clicked.connect(self.clear_all)
        bar.addWidget(btn_clear)
        return frame

    @staticmethod
    def _vline() -> QFrame:
        line = QFrame()
        line.setObjectName("vline")
        return line

    @staticmethod
    def _hline() -> QWidget:
        """顶部卡片里两条行之间的一条细线（左右留白，不顶到圆角）。"""
        host = QWidget()
        host.setObjectName("hlineHost")
        host.setFixedHeight(1)
        row = QHBoxLayout(host)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(0)
        line = QFrame()
        line.setObjectName("hline")
        row.addWidget(line)
        return host

    # ------------------------------------------------------------ 检查更新
    def _setup_updater(self):
        self.updater = ReleaseChecker(self)
        self.updater.finished.connect(self._on_update_result)
        self.badge.clicked.connect(self._on_update_clicked)
        self.badge.recheckRequested.connect(self.check_updates)
        self.titlebar.set_subtitle(f"每 {self._step():g} 帧一帧")
        debug("MainWindow._setup_updater 完成")
        if debug_enabled():
            # 只诊断用：每秒记一行，能看出事件循环有没有停
            self._hb = QTimer(self)
            self._hb.setInterval(1000)
            self._hb.timeout.connect(lambda: debug("heartbeat"))
            self._hb.start()
        if update_checks_enabled():
            # 等界面彻底安静下来再查（刚启动时 spawn 子进程太早容易和首帧绘制撞车）
            QTimer.singleShot(6000, self.check_updates)
        else:
            self.badge.set_state("idle", tip="已用 PURER_NO_UPDATE=1 关闭联网检查")

    def check_updates(self):
        debug("check_updates: 进入")
        if self.badge.state_name() != "checking":
            self.badge.set_state("checking",
                                 tip="正在向 GitHub 查询最新发布版本…")
        debug("check_updates: 药丸已切到「检查中」")
        self.updater.check()
        debug("check_updates: 已交给检查器")

    def _on_update_result(self, result):
        state = result.state()
        tag = normalize(result.tag) or result.tag
        if state == "stale":
            self.badge.set_state(
                "stale", tag=tag, url=result.url or RELEASES_PAGE,
                tip=f"GitHub 上已发布 {tag}\n点击打开发布页下载（右键重新检查）")
        elif state == "latest":
            self.badge.set_state(
                "latest", tag=tag,
                tip=f"当前已经是最新版本 v{APP_VERSION}\n点击重新检查")
        else:
            self.badge.set_state(
                "error",
                tip=f"没查到最新版本：{result.error or '网络不可用'}\n点击重试")

    def _on_update_clicked(self):
        state = self.badge.state_name()
        if state == "stale":
            QDesktopServices.openUrl(QUrl(self.badge.url() or RELEASES_PAGE))
        elif state != "checking":
            self.check_updates()

    def changeEvent(self, event):
        """最大化 / 还原时把「□ ↔ ❐」图标换掉。"""
        if event.type() == QEvent.WindowStateChange:
            self.titlebar.sync_max_button()
        super().changeEvent(event)

    # ------------------------------------------------------------ 设置
    def _step(self) -> float:
        return float(self.combo_step.value())

    def _thumb_size_for(self, info):
        """抽取尺寸：磁盘上的缩略图长边（固定，不随显示尺寸变化）。"""
        return thumb_size_for(info.width, info.height, EXTRACT_LONG_SIDE)

    @staticmethod
    def _title(step: float) -> str:
        return f"{APP_NAME} {APP_VERSION} · 每 {float(step):g} 帧一帧"

    def _on_step_changed(self, v):
        self.setWindowTitle(self._title(v))
        self.titlebar.set_subtitle(f"每 {float(v):g} 帧一帧")

    def _sync_size_combo(self, px: int):
        """把下拉框对齐到当前显示尺寸（不在预设里就显示「自定义 · N」）。"""
        self._syncing_combo = True
        try:
            index = next((i for i, (_, value) in enumerate(DISPLAY_PRESETS)
                          if value == px), CUSTOM_INDEX)
            if index == CUSTOM_INDEX:
                self.combo_size.setItemText(CUSTOM_INDEX, f"自定义 · {px}")
            self.combo_size.setCurrentIndex(index)
        finally:
            self._syncing_combo = False

    def _pick_size(self, index: int):
        if 0 <= index < self.combo_size.count():
            self.combo_size.setCurrentIndex(index)

    def _zoom(self, direction: int):
        """± 按钮：对当前页实时缩放（没有页就改默认值）。"""
        page = self.tabs.currentWidget()
        if isinstance(page, TrackPage):
            page.set_display_side(page.display_side + direction * DISPLAY_STEP)
            self.lbl_status.setText(f"显示尺寸 {page.display_side}px（即时生效，不重抽）")
            return
        self._display_side = max(DISPLAY_MIN, min(
            DISPLAY_MAX, self._display_side + direction * DISPLAY_STEP))
        self._sync_size_combo(self._display_side)
        self.setting.setValue("ui/display_side", self._display_side)

    def _on_size_changed(self, index: int):
        if self._syncing_combo:
            return
        px = self.combo_size.itemData(index)
        if px is None:                     # 「自定义」只是显示占位
            return
        self._display_side = int(px)
        self.setting.setValue("ui/display_side", self._display_side)
        page = self.tabs.currentWidget()
        if isinstance(page, TrackPage):
            page.set_display_side(self._display_side)
        self.lbl_status.setText(f"显示尺寸 {self._display_side}px（已即时生效，不重抽）")

    def _on_page_display_side(self, px: int):
        self._display_side = int(px)
        self.setting.setValue("ui/display_side", self._display_side)
        self._sync_size_combo(self._display_side)

    def _on_splitter_moved(self, *_args):
        page = self.tabs.currentWidget()
        if not isinstance(page, TrackPage):
            return
        width = page.sidebar_width()
        if abs(width - self._sidebar_width) <= 1:
            return
        self._sidebar_width = width
        self.setting.setValue("ui/sidebar_width", width)

    def _refresh_status(self, *_args):
        running = self.jobs.running_count
        pending = self.jobs.pending_count
        if not running and not pending:
            if self.lbl_status.text() == self._job_text:
                self.lbl_status.setText("")
            self._job_text = ""
            return
        parts = []
        if running:
            parts.append(f"{running} 个任务进行中")
        if pending:
            parts.append(f"{pending} 个排队")
        self._job_text = " · ".join(parts)
        self.lbl_status.setText(self._job_text)

    # ------------------------------------------------------------ 拖放
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            if not getattr(self, "_status_prev", None):
                self._status_prev = self.lbl_status.text()
            self.lbl_status.setText("松开即可添加视频 / 文件夹")
            self._refresh_status()

    def dragLeaveEvent(self, e):
        self.lbl_status.setText(self._status_prev or "")
        self._status_prev = ""
        e.accept()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        found = []
        for url in e.mimeData().urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            try:
                if p.is_dir():
                    found.extend(find_videos(p))
                elif p.suffix.lower() in VIDEO_EXTS:
                    found.append(p)
            except OSError:
                continue
        if not found:
            self.lbl_status.setText("没有可识别的视频文件")
            return
        e.acceptProposedAction()
        self.add_videos(found)

    # ------------------------------------------------------------ 打开
    def open_files(self):
        pattern = "视频文件 (" + " ".join("*" + x for x in sorted(VIDEO_EXTS)) + ");;所有文件 (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频", "", pattern)
        if files:
            self.add_videos([Path(f) for f in files])

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹（含视频）")
        if d:
            vids = find_videos(Path(d))
            if not vids:
                QMessageBox.information(self, APP_NAME, "该文件夹里没有找到视频文件。")
                return
            self.add_videos(vids)

    def add_videos(self, paths):
        paths = [Path(p) for p in paths]
        seen = {str(p.path.resolve()).lower() for p in self._pages}
        prog = None
        if len(paths) > 8:
            prog = QProgressDialog("正在读取视频信息…", "取消", 0, len(paths), self)
            prog.setWindowTitle(APP_NAME)
            prog.setWindowModality(Qt.WindowModal)
            prog.setMinimumDuration(200)
        added, skipped = 0, 0
        try:
            for n, p in enumerate(paths, start=1):
                if prog is not None:
                    prog.setValue(n)
                    prog.setLabelText(f"正在读取视频信息…（{n}/{len(paths)}）\n{p.name}")
                    if prog.wasCanceled():
                        break
                try:
                    key = str(p.resolve()).lower()
                except OSError:
                    key = str(p).lower()
                if key in seen:
                    skipped += 1
                    continue
                if self.add_video(p):
                    seen.add(key)
                    added += 1
        finally:
            if prog is not None:
                prog.setValue(len(paths))
                prog.close()
        msg = f"已打开 {added} 个视频"
        if skipped:
            msg += f"，跳过 {skipped} 个（已在标签页中）"
        self.lbl_status.setText(msg)

    def add_video(self, path: Path, index: int | None = None) -> bool:
        try:
            info = probe(path)
        except Exception as e:  # noqa: BLE001
            self.lbl_status.setText(f"⚠ {path.name}：{e}")
            return False
        step = self._step()
        tw, th = self._thumb_size_for(info)
        est = max(1, int(round(info.frames / step))) if info.frames else 1
        if est > MAX_THUMBS_WARN:
            ans = QMessageBox.question(
                self, "缩略图太多",
                f"《{path.name}》\n时长 {fmt_tc(info.duration)}，预计生成 {est} 张缩略图。\n\n"
                f"这可能会占用较多时间和内存（可在顶部把「间隔帧」调大，或用大缩略图设置）。\n\n是否继续？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return False
        page = TrackPage(path, step, tw, th, info, self.jobs,
                         display_side=self._display_side)
        page.reload_requested.connect(self._on_reload)
        page.display_side_changed.connect(self._on_page_display_side)
        page.split.splitterMoved.connect(self._on_splitter_moved)
        page.set_sidebar_width(self._sidebar_width)
        name = path.name if len(path.name) <= 28 else path.name[:26] + "…"
        if index is None:
            idx = self.tabs.addTab(page, name)
        else:
            idx = self.tabs.insertTab(index, page, name)
        self.tabs.setTabToolTip(idx, str(path))
        self._pages.append(page)
        if index is None:
            self.tabs.setCurrentIndex(idx)
        page.lbl_count.setText("排队中…")
        page.start()
        self._refresh_stack()
        self._refresh_status()
        return True

    def _forget_page(self, page):
        if page in self._pages:
            self._pages.remove(page)
        self._refresh_status()

    def _on_reload(self, page):
        idx = self.tabs.indexOf(page)
        if idx < 0:
            return
        path = page.path
        self.close_tab(idx)
        self.add_video(path, index=idx)
        self.tabs.setCurrentIndex(min(idx, self.tabs.count() - 1))

    def close_tab(self, index, keep_count=False):
        page = self.tabs.widget(index)
        if page is None:
            return
        try:
            page.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.tabs.removeTab(index)
        self._forget_page(page)
        page.deleteLater()
        self._refresh_stack()

    def clear_all(self):
        while self.tabs.count():
            self.close_tab(0)
        self.lbl_status.setText("")

    def _close_current(self):
        if self.tabs.count():
            self.close_tab(self.tabs.currentIndex())

    def _on_tab_changed(self, index):
        """非当前页释放大图内存（切回来会自动重新载入）；下拉框对齐当前页显示尺寸。"""
        cur = self.tabs.widget(index)
        for page in self._pages:
            if page is not cur:
                try:
                    page.drop_preview()
                except Exception:  # noqa: BLE001
                    pass
        if isinstance(cur, TrackPage):
            self._sync_size_combo(cur.display_side)

    def _refresh_stack(self):
        self.stack.setCurrentWidget(self.tabs if self.tabs.count() else self.empty)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.tabs.count() == 0:
            self.close()
            return
        super().keyPressEvent(e)

    def closeEvent(self, e):
        for page in list(self._pages):
            try:
                page.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.jobs.cancel_all()
        try:
            self.updater.cancel()          # 别把 curl 子进程留在后台
        except (AttributeError, RuntimeError):
            pass
        super().closeEvent(e)
