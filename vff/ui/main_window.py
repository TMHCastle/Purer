# -*- coding: utf-8 -*-
"""主窗口：自绘标题栏 + 顶部工具栏 + 多标签页 + 拖放 + 抽帧队列。"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QEvent, QProcess, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QMessageBox, QProgressDialog, QPushButton,
                               QSizePolicy, QStackedWidget, QTabWidget,
                               QVBoxLayout, QWidget)

from ..cache import cache_root
from ..config import (APP_NAME, APP_VERSION, DEFAULT_DISPLAY_SIDE, DEFAULT_STEP,
                      DISPLAY_MAX, DISPLAY_MIN, DISPLAY_PRESETS, DISPLAY_STEP,
                      EXTRACT_LONG_SIDE, JOBS_MAX_CONCURRENT, MAX_THUMBS_WARN,
                      STEP_PRESETS, STEP_PRESETS_FRACTIONAL, VIDEO_EXTS,
                      extract_mode_items, parse_extract_mode)
from ..download import UpdateDownloader
from ..ffmpeg import probe
from ..jobs import JobQueue
from ..media import find_videos, thumb_size_for
from ..selfupdate import (app_exe, apply_update, auto_install_enabled,
                          can_self_update, download_path, verify_file)
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
        self._extract_mode, self._scene_threshold = parse_extract_mode(
            str(self.setting.value("ui/extract_mode", "step")))

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
        self._refresh_titles()
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
            "选「按镜头」时这一项会变灰（改用场景检测）")
        self.combo_step.valueChanged.connect(self._on_step_changed)
        bar.addWidget(self.combo_step)

        bar.addSpacing(4)
        self.combo_mode = ArrowCombo()
        self.combo_mode.setFixedWidth(126)
        self.combo_mode.setToolTip(
            "抽帧方式：\n"
            "· 每 N 帧取一张（固定间隔，时间轴等距）\n"
            "· 按镜头取一张（场景检测，一张图 = 一个镜头）——\n"
            "  灵敏 20% 容易把推拉/闪光也当切镜头，保守 45% 只认硬切")
        for label, value in extract_mode_items():
            self.combo_mode.addItem(label, value)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        self._sync_mode_combo()
        bar.addWidget(self.combo_mode)

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
        self.downloader = UpdateDownloader(self)
        self.downloader.progress.connect(self._on_download_progress)
        self.downloader.finished.connect(self._on_download_finished)
        self._update_result = None          # 最近一次「有新版本」的结果
        self._pending_exe = ""              # 已下载并校验通过的新 exe
        self.badge.clicked.connect(self._on_update_clicked)
        self.badge.recheckRequested.connect(self.check_updates)
        self.titlebar.set_subtitle(self._subtitle(self._step()))
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
        if self.downloader.busy():
            return
        if self.badge.state_name() != "checking":
            self.badge.set_state("checking",
                                 tip="正在向 GitHub 查询最新发布版本…")
        self._update_result = None
        self.updater.check()
        debug("check_updates: 已交给检查器")

    def _on_update_result(self, result):
        state = result.state()
        tag = normalize(result.tag) or result.tag
        if state == "stale":
            self._update_result = result
            if result.can_install() and auto_install_enabled():
                hint = "点击下载并自动安装（右键重新检查）"
            else:
                hint = "点击打开发布页下载（右键重新检查）"
            self.badge.set_state(
                "stale", tag=tag, url=result.url or RELEASES_PAGE,
                tip=f"GitHub 上已发布 {tag}\n{hint}")
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
        if state == "downloading":
            return
        if state == "stale":
            self._ask_download()
        elif state == "ready":
            self._ask_install()
        elif state != "checking":
            self.check_updates()

    # -------------------------------------------------- 下载 → 校验 → 替换重启
    def _ask_download(self):
        result = self._update_result
        if result is None or not result.can_install() or not auto_install_enabled():
            QDesktopServices.openUrl(QUrl(self.badge.url() or RELEASES_PAGE))
            return
        ok, why = can_self_update()
        if not ok:
            self.badge.set_state("stale", tag=normalize(result.tag) or result.tag,
                                 url=result.url or RELEASES_PAGE,
                                 tip=f"{why}\n已改为打开发布页，手动下载即可")
            QDesktopServices.openUrl(QUrl(result.url or RELEASES_PAGE))
            return
        tag = normalize(result.tag) or result.tag
        size = f"{result.asset_size / 1048576:.1f} MB" if result.asset_size else "未知大小"
        answer = QMessageBox.question(
            self, "下载并安装更新",
            f"最新版本 {tag}（{size}）\n\n"
            "现在下载吗？下载完会先校验 sha256，然后关闭本程序、"
            "替换 exe 并自动重启。\n\n（下载期间可以继续剪片子）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._start_download(result)

    def _start_download(self, result) -> bool:
        tag = normalize(result.tag) or result.tag
        dest = download_path(result.tag)
        self.badge.set_state("downloading", tag=tag, detail="0%",
                             tip=f"正在下载 {tag}（{result.asset_size} 字节）")
        started = self.downloader.start(result.asset_url, result.asset_size,
                                        result.digest, dest)
        if not started:
            self.badge.set_state("error", tip="下载没能开始，点击重试")
        return started

    def _on_download_progress(self, pct: int, got: int, total: int):
        self.badge.set_progress(pct if pct >= 0 else 0)

    def _on_download_finished(self, ok: bool, path: str, error: str):
        result = self._update_result
        tag = normalize(result.tag) if result is not None else ""
        if not ok:
            self.badge.set_state(
                "error", tip=f"下载失败：{error}\n点击重试（右键重新检查）")
            return
        self._pending_exe = path
        self.badge.set_state(
            "ready", tag=tag,
            tip=f"新版本 {tag} 已下载并校验通过\n点击立即安装并重启")
        answer = QMessageBox.question(
            self, "更新已就绪",
            f"新版本 {tag} 已下载完成，sha256 校验通过。\n\n"
            "现在重启安装吗？（会关闭本程序再启动新版本）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._install_update()

    def _ask_install(self):
        if not self._pending_exe:
            self.check_updates()
            return
        tag = self.badge.tag() or "新版本"
        answer = QMessageBox.question(
            self, "安装更新",
            f"现在安装 {tag} 并重启吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._install_update()

    def _install_update(self) -> bool:
        """把下载好的 exe 换掉自己，然后重启新版本。"""
        if not self._pending_exe:
            return False
        exe = app_exe()
        if exe is None:
            QMessageBox.information(self, "安装更新",
                                    "源码运行时不能自更新。")
            return False
        ok, why = verify_file(self._pending_exe, 0,
                              (self._update_result.digest
                               if self._update_result else ""))
        if not ok:
            QMessageBox.warning(self, "安装更新", f"安装包校验失败：{why}")
            QDesktopServices.openUrl(QUrl(RELEASES_PAGE))
            return False
        ok, why = apply_update(self._pending_exe, exe)
        if not ok:
            QMessageBox.warning(
                self, "安装更新",
                f"{why}\n\n可以到发布页手动下载，或把 Purer 放到有写权限的目录再试。")
            QDesktopServices.openUrl(QUrl(RELEASES_PAGE))
            return False
        started = QProcess.startDetached(str(exe))
        started_ok = started[0] if isinstance(started, tuple) else bool(started)
        if not started_ok:
            QMessageBox.information(self, "安装更新",
                                    "新版本已就位，请手动重新打开 Purer。")
        QApplication.quit()
        return True

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
    def _title(step: float, mode: str = "step") -> str:
        if mode == "scene":
            return f"{APP_NAME} {APP_VERSION} · 按镜头抽帧"
        return f"{APP_NAME} {APP_VERSION} · 每 {float(step):g} 帧一帧"

    def _subtitle(self, step: float) -> str:
        if self._extract_mode == "scene":
            return f"按镜头 · 阈值 {self._scene_threshold:.2f}"
        return f"每 {float(step):g} 帧一帧"

    def _refresh_titles(self):
        self.setWindowTitle(self._title(self._step(), self._extract_mode))
        if hasattr(self, "titlebar"):
            self.titlebar.set_subtitle(self._subtitle(self._step()))

    def _on_step_changed(self, v):
        self._refresh_titles()

    # ------------------------------------------------------------ 抽帧方式
    def _sync_mode_combo(self):
        want = "step" if self._extract_mode != "scene" else f"scene:{self._scene_threshold:.2f}"
        for i in range(self.combo_mode.count()):
            if self.combo_mode.itemData(i) == want:
                self.combo_mode.setCurrentIndex(i)
                break
        self.combo_step.setEnabled(self._extract_mode != "scene")

    def _on_mode_changed(self, index: int):
        value = self.combo_mode.itemData(index)
        if not value:
            return
        self._extract_mode, self._scene_threshold = parse_extract_mode(value)
        scene = self._extract_mode == "scene"
        self.setting.setValue("ui/extract_mode",
                              f"scene:{self._scene_threshold:.2f}" if scene else "step")
        self.combo_step.setEnabled(not scene)
        if hasattr(self, "titlebar"):
            self._refresh_titles()
        if not hasattr(self, "lbl_status"):
            return                      # 工具栏还在搭（恢复上次选择时）
        if scene:
            self.lbl_status.setText(
                f"按镜头抽帧（阈值 {self._scene_threshold:.2f}）："
                "一张缩略图 = 一个镜头 · 只影响之后打开的视频")
        else:
            self.lbl_status.setText("按固定间隔抽帧（每 N 帧一张）")

    def _scene_threshold_or_none(self):
        return self._scene_threshold if self._extract_mode == "scene" else None

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
        threshold = self._scene_threshold_or_none()
        if threshold is None:
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
                         display_side=self._display_side,
                         scene_threshold=threshold)
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
        for worker in ("updater", "downloader"):
            try:
                getattr(self, worker).cancel()      # 别把 curl 子进程留在后台
            except (AttributeError, RuntimeError):
                pass
        super().closeEvent(e)
