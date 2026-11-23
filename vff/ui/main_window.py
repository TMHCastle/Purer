# -*- coding: utf-8 -*-
"""主窗口：自绘标题栏 + 顶部工具栏 + 多标签页 + 拖放 + 抽帧队列。"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from PySide6.QtCore import (QByteArray, QEvent, QProcess, QSettings, QSize,
                            Qt, QTimer, QUrl)
from PySide6.QtGui import (QColor, QDesktopServices, QKeySequence, QPainter,
                           QPalette, QPen, QShortcut)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMenu, QMessageBox, QProgressDialog,
                               QPushButton, QSizePolicy, QStackedWidget, QTabBar,
                               QTabWidget, QVBoxLayout, QWidget)

from ..cache import cache_root
from ..cache_store import CacheStore
from ..config import (APP_NAME, APP_VERSION, DEFAULT_DISPLAY_SIDE, DEFAULT_STEP,
                      DISPLAY_MAX, DISPLAY_MIN, DISPLAY_PRESETS, DISPLAY_STEP,
                      EXTRACT_LONG_SIDE, JOBS_MAX_CONCURRENT, MAX_THUMBS_WARN,
                      STEP_MAX, STEP_MIN, STEP_PRESETS, STEP_PRESETS_FRACTIONAL,
                      VIDEO_EXTS, data_dir, extract_mode_items,
                      parse_extract_mode, preset_label, snap_step)
from ..download import UpdateDownloader
from ..ffmpeg import probe
from ..jobs import JobQueue
from ..media import find_videos, thumb_size_for
from ..selfupdate import (app_exe, apply_update, auto_install_enabled,
                          can_self_update, download_path, verify_file)
from .. import session as session_state
from ..settings import (CACHE_MODE_NEVER, CACHE_MODE_USAGE, Settings,
                        close_prompt_needed, update_apply_choice,
                        update_should_prompt, usage_meets)
from ..update import RELEASES_PAGE, ReleaseChecker
from ..util import fmt_tc
from ..version import normalize
from ..log import debug, debug_enabled
from .cache_list import CacheImportDialog, sorted_entries
from .frameless import FramelessWindow
from .settings_dialog import CloseCacheDialog, SettingsDialog
from .style import BG, TAB_GUIDE, icon_win_close, make_icon
from .tabs import StateTabBar
from .titlebar import TitleBar
from .track_page import TrackPage
from .update_badge import UpdateBadge
from .update_dialog import UpdatePromptDialog
from .widgets import ArrowCombo, EmptyState, StepCombo

CUSTOM_INDEX = len(DISPLAY_PRESETS)
USAGE_TICK_MS = 5000        # 每 5 秒给「当前页签」记一次使用时长


def update_checks_enabled() -> bool:
    """``PURER_NO_UPDATE=1`` 时完全不联网（离线机器 / 自动化测试用）。"""
    flag = os.environ.get("PURER_NO_UPDATE", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")


def _ui_settings() -> QSettings:
    """界面记忆（间隔 / 显示尺寸 / 窗口位置 / 上次打开目录…）。

    放在**持久数据目录**而不是临时缓存里 —— 重启电脑、清理 `%TEMP%` 之后
    「记忆上次设置」还得在。
    """
    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    ini = root / "ui.ini"
    # 从旧位置（临时缓存里的 ui.ini）搬一次记忆，别让用户重启后丢设置
    if not ini.is_file():
        old = cache_root() / "ui.ini"
        try:
            if old.is_file():
                shutil.copy2(old, ini)
        except OSError:
            pass
    return QSettings(str(ini), QSettings.IniFormat)


class TabCloseButton(QPushButton):
    """标签页上的关闭按钮：悬停白色 X，带暗红圆角底。

    做成独立按钮（而不是用 QTabBar 自带的 close-button），这样才能
    通过一个「右侧留白」的容器把它整体往左挪一格，点击区域也更舒服。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tabClose")
        self.setFixedSize(18, 18)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setIconSize(QSize(11, 11))
        self._normal = icon_win_close("#8C94A3", 16)
        self._hover = icon_win_close("#FFFFFF", 16)
        self.setIcon(self._normal)

    def enterEvent(self, event):              # noqa: N802
        self.setIcon(self._hover)
        super().enterEvent(event)

    def leaveEvent(self, event):              # noqa: N802
        self.setIcon(self._normal)
        super().leaveEvent(event)


class StripGuides(QWidget):
    """浮在标签栏上的一层薄片：只画上下两条浅灰引导线。

    为什么不用 QTabWidget.paintEvent：QTabBar 是子控件，会在父控件之后绘制，
    它自己那块底色+基准线会把父控件画的线盖掉（左边红绿灯那块又盖不到，
    于是变成「左边有线、标签上没线」）。放在最上层、且对鼠标透明，才稳定。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, _event):             # noqa: N802
        p = QPainter(self)
        p.setPen(QPen(QColor(TAB_GUIDE), 1))
        p.drawLine(0, 1, self.width() - 1, 1)                 # 上界
        p.drawLine(0, self.height() - 1, self.width() - 1, self.height() - 1)
        p.end()


class TabStrip(QTabWidget):
    """标签栏横条：上下浅灰引导线 + 每个标签按缓存状态上色。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabBar(StateTabBar(self))       # 必须在添加标签之前装
        self.guides = StripGuides(self)
        self.guides.raise_()
        self.tabBar().installEventFilter(self)
        self._sync_guides()

    def _sync_guides(self):
        bar = self.tabBar()
        if bar is None:
            return
        self.guides.setGeometry(0, bar.geometry().top(),
                                self.width(), max(1, bar.height()))
        self.guides.raise_()

    def resizeEvent(self, event):             # noqa: N802
        super().resizeEvent(event)
        self._sync_guides()

    def eventFilter(self, obj, event):        # noqa: N802
        if obj is self.tabBar() and event.type() in (QEvent.Resize, QEvent.Show,
                                                    QEvent.Move):
            self._sync_guides()
        return super().eventFilter(obj, event)


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

        # 会话快照（标签/帧/入出点/片段）：定时落盘，更新重启 / 崩溃后接上
        self._session_reason = "auto"
        self._restoring = False

        # 设置（持久）+ 已保存的缓存库
        self.app_settings = Settings()
        self.cache_store = CacheStore(
            root=self.app_settings.effective_cache_dir(),
            settings=self.app_settings)

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
        self.titlebar.settingsRequested.connect(self.open_settings)
        self.badge = UpdateBadge(self.titlebar)
        self.titlebar.set_center_widget(self.badge)
        card_lay.addWidget(self.titlebar)
        card_lay.addWidget(self._hline())
        card_lay.addWidget(self._build_toolbar())
        lay.addWidget(self.card)

        self.stack = QStackedWidget()
        self.empty = EmptyState()
        self.tabs = TabStrip()
        self.tabs.setTabsClosable(False)   # 关自己画的按钮，见 _install_tab_close
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setUsesScrollButtons(True)
        tab_bar = self.tabs.tabBar()
        tab_bar.setExpanding(False)
        tab_bar.set_state_provider(self._tab_cache_state)
        tab_bar.setContextMenuPolicy(Qt.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._tab_menu)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # 标签栏横条：上下两条浅灰引导线由 StripGuides 画。Fusion 还会在
        # 「第一个标签上沿」自己画一条基准线，用的是 QPalette.Light（深色调色板
        # 没设过 → 默认纯白，就是原来那条刺眼的白线）—— 换成背景色让它消失。
        bar_palette = tab_bar.palette()
        bar_palette.setColor(QPalette.Light, QColor(BG))
        bar_palette.setColor(QPalette.Midlight, QColor(BG))
        tab_bar.setPalette(bar_palette)
        self._tab_states: list[str] = []
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tabs)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self._refresh_stack()
        self._setup_updater()
        self._setup_usage_timer()
        self._setup_state_timer()
        self._refresh_titles()
        self.titlebar.sync_max_button()
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.open_files)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._close_current)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
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
        btn_dir.setObjectName("hintBorder")
        btn_dir.setToolTip("把一个文件夹里所有视频加进来（记住上次选的文件夹）")
        btn_dir.clicked.connect(self.open_folder)
        btn_import = QPushButton("从缓存导入")
        btn_import.setObjectName("hintBorder")
        btn_import.setToolTip("从已经保存的缓存里挑一个视频打开（秒开，不重新抽帧）")
        btn_import.clicked.connect(self.import_from_cache)
        bar.addWidget(btn_open)
        bar.addWidget(btn_dir)
        bar.addWidget(btn_import)

        bar.addSpacing(6)
        bar.addWidget(self._vline())
        bar.addSpacing(6)

        bar.addWidget(QLabel("间隔"))
        self.combo_step = StepCombo(STEP_PRESETS, STEP_PRESETS_FRACTIONAL, DEFAULT_STEP)
        try:
            saved_step = float(self.setting.value("ui/step", DEFAULT_STEP))
        except (TypeError, ValueError):
            saved_step = DEFAULT_STEP
        self.combo_step.set_value(max(STEP_MIN, min(STEP_MAX, saved_step)),
                                  notify=False)
        saved_auto = self.setting.value("ui/step_auto", True)
        self._step_auto = str(saved_auto).lower() not in ("false", "0", "no")
        if self._step_auto:
            self.combo_step.set_auto(True, notify=False)
        self.combo_step.setFixedWidth(104)
        self.combo_step.setToolTip(
            "每隔多少帧取一张（可直接输入，支持 23.98 这种小数）\n"
            "· 默认「跟随视频帧率」= 每 1 秒取 1 张（24fps 取 24、60fps 取 60）\n"
            "· 23.98 / 29.97 / 59.94 = 常见帧率下「约 1 秒」的帧数\n"
            "· 选「按镜头」时这一项会变灰（改用场景检测）")
        self.combo_step.valueChanged.connect(self._on_step_changed)
        self.combo_step.autoPicked.connect(self._on_step_auto_picked)
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
        self._update_prompt = None          # 正在显示的「发现新版本」弹窗
        self._update_manual = False         # 这次检查是不是用户手动点的
        self._update_retries = 0            # 队列没空时等了几轮
        self._update_retry = QTimer(self)
        self._update_retry.setSingleShot(True)
        self._update_retry.setInterval(10000)
        self._update_retry.timeout.connect(self._retry_update_prompt)
        self.badge.clicked.connect(self._on_update_clicked)
        self.badge.recheckRequested.connect(lambda: self.check_updates(manual=True))
        self.titlebar.set_subtitle(self._subtitle(self._step()))
        debug("MainWindow._setup_updater 完成")
        if debug_enabled():
            # 只诊断用：每秒记一行，能看出事件循环有没有停
            self._hb = QTimer(self)
            self._hb.setInterval(1000)
            self._hb.timeout.connect(lambda: debug("heartbeat"))
            self._hb.start()
        prefs = self.app_settings.update_prefs()
        if update_checks_enabled() and prefs.get("auto_check", True):
            # 等界面彻底安静下来再查（刚启动时 spawn 子进程太早容易和首帧绘制撞车）
            QTimer.singleShot(6000, self.check_updates)
        elif not update_checks_enabled():
            self.badge.set_state("idle", tip="已用 PURER_NO_UPDATE=1 关闭联网检查")
        else:
            self.badge.set_state("idle",
                                 tip="设置-更新 里关掉了自动检查\n"
                                     "右键点这里可以手动检查")

    def check_updates(self, manual: bool = False):
        debug(f"check_updates: 进入（manual={manual}）")
        if self.downloader.busy():
            return
        self._update_manual = bool(manual)
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
        if state == "stale":
            self._maybe_prompt_update(result)

    # -------------------------------------------------- 「发现新版本」弹窗
    def _maybe_prompt_update(self, result) -> None:
        """该不该弹窗：策略 + 队列空闲（正在抽帧/导出就先等等）。"""
        if not update_checks_enabled() or self._update_prompt is not None:
            return
        prefs = self.app_settings.update_prefs()
        if not prefs.get("auto_check", True) and not self._update_manual:
            return
        show, why = update_should_prompt(
            prefs, result.tag, manual=self._update_manual)
        if not show:
            debug(f"不弹更新窗：{why}")
            self.lbl_status.setText(f"有新版本 {normalize(result.tag) or ''}（{why}）")
            return
        if self.jobs.busy:
            if self._update_retries >= 6:      # 等了两分钟还没空，这次启动先不弹
                self._update_retries = 0
                debug("队列一直忙，这次启动不弹更新窗")
                return
            self._update_retries += 1
            self._update_retry.start()
            return
        self._update_retries = 0
        self._prompt_update(result)

    def _retry_update_prompt(self):
        result = self._update_result
        if result is None or self._update_prompt is not None:
            return
        if self.jobs.busy:
            if self._update_retries < 6:
                self._update_retries += 1
                self._update_retry.start()
            return
        self._update_retries = 0
        prefs = self.app_settings.update_prefs()
        show, why = update_should_prompt(prefs, result.tag,
                                         manual=self._update_manual)
        if show:
            self._prompt_update(result)
        else:
            debug(f"等队列期间策略变了，不弹：{why}")

    def _prompt_update(self, result) -> None:
        """弹小窗，按用户的选择落策略（不做任何自动决定）。"""
        tag = normalize(result.tag) or result.tag
        dialog = UpdatePromptDialog(
            self, tag=tag, current=APP_VERSION,
            size=int(result.asset_size or 0),
            url=result.url or RELEASES_PAGE)
        self._update_prompt = dialog
        manual = self._update_manual
        try:
            dialog.exec()
        finally:
            self._update_prompt = None
            self._update_manual = False
        choice = dialog.choice
        prefs = update_apply_choice(self.app_settings.update_prefs(),
                                    tag, choice)
        self.app_settings.set_update_prefs(**prefs)
        self.app_settings.save()
        debug(f"更新弹窗选择：{choice}（tag={tag} manual={manual}）")
        if choice == "now":
            self._start_download(result)
        elif choice == "week":
            self.lbl_status.setText(
                f"好，一周后再提醒 {tag}（期间出现更高版本会立刻提醒）")
        elif choice == "skip":
            self.lbl_status.setText(f"已忽略 {tag}（更高版本仍会提醒）")
        elif choice == "mute":
            self.badge.set_state("idle",
                                 tip="更新提醒已关闭（设置-更新 里可以恢复）")
            self.lbl_status.setText("已关闭更新提醒（右键顶部药丸仍可手动检查）")
        else:
            self.lbl_status.setText(f"稍后再说（24 小时内不再提示 {tag}）")

    def _on_update_clicked(self):
        state = self.badge.state_name()
        if state == "downloading":
            return
        if state == "stale":
            self._ask_download()
        elif state == "ready":
            self._ask_install()
        elif state != "checking":
            self.check_updates(manual=True)

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
            "现在下载吗？下载完会先校验 sha256，然后保存当前标签页、"
            "替换程序并自动重启（重启后接着现在的进度）。\n\n"
            "（下载期间可以继续剪片子）",
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
            "现在重启安装吗？（会保存当前标签页，重启后自动接上）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._install_update()

    def _ask_install(self):
        if not self._pending_exe:
            self.check_updates(manual=True)
            return
        tag = self.badge.tag() or "新版本"
        answer = QMessageBox.question(
            self, "安装更新",
            f"现在安装 {tag} 并重启吗？（会保存当前标签页）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes:
            self._install_update()

    def _install_update(self) -> bool:
        """把下载好的 exe 换掉自己，然后「无缝重启」（会话快照 + --resume）。"""
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
        # 1) 先把「我正在干什么」记下来（更新重启后要原样接上）
        self.save_session("update")
        self._flush_all_usage()
        try:
            self.setting.sync()
        except (RuntimeError, OSError):
            pass
        # 2) 换文件（运行中改名 + 挪入；失败会自动回滚，界面还能继续用）
        ok, why = apply_update(self._pending_exe, exe)
        if not ok:
            QMessageBox.warning(
                self, "安装更新",
                f"{why}\n\n可以到发布页手动下载，或把 Purer 放到有写权限的目录再试。")
            QDesktopServices.openUrl(QUrl(RELEASES_PAGE))
            return False
        # 3) 拉起新版本，让它等这个进程退出之后再开窗（避免两个窗口打架）
        started = QProcess.startDetached(
            str(exe), ["--resume", "--after-pid", str(os.getpid())])
        started_ok = started[0] if isinstance(started, tuple) else bool(started)
        if not started_ok:
            QMessageBox.information(
                self, "安装更新",
                "新版本已就位，但没能自动重启。\n"
                "这次的会话已经保存，手动重新打开 Purer 就能接着干。")
            return True
        for page in list(self._pages):
            try:
                page.shutdown()
            except Exception:          # noqa: BLE001
                pass
        self.jobs.cancel_all()
        for worker in ("updater", "downloader"):
            try:
                getattr(self, worker).cancel()      # 别把 curl 子进程留在后台
            except (AttributeError, RuntimeError):
                pass
        QApplication.quit()
        return True

    def changeEvent(self, event):
        """最大化 / 还原时把「□ ↔ ❐」图标换掉。"""
        if event.type() == QEvent.WindowStateChange:
            self.titlebar.sync_max_button()
        super().changeEvent(event)

    # ------------------------------------------------------------ 标签缓存状态
    def _setup_state_timer(self):
        """每 0.9s 刷新一次各标签的缓存状态色（并每 4 拍做一次自动保存）。"""
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(900)
        self._state_timer.timeout.connect(self._on_state_tick)
        self._state_timer.start()
        self._state_ticks = 0
        # 会话快照：30 秒一次。崩溃/断电最多丢半分钟，写盘量只有几 KB。
        self._session_timer = QTimer(self)
        self._session_timer.setInterval(30000)
        self._session_timer.timeout.connect(self._on_session_tick)
        self._session_timer.start()

    def _on_session_tick(self):
        if self._restoring or not self.tabs.count():
            return
        self.save_session("auto")

    def _on_state_tick(self):
        self._refresh_tab_states()
        self._state_ticks += 1
        if self._state_ticks % 4 == 0:        # ≈ 3.6 秒一次，够快也不一直写盘
            self._auto_save_pass()

    def page_cache_state(self, page) -> str:
        """一个页面现在的缓存状态：loading / ready / saved / none。"""
        if not isinstance(page, TrackPage):
            return "none"
        if page.busy or not page._finished:
            return "loading"
        if not page.cache_ready():
            return "none"
        try:
            return ("saved" if self.cache_store.has(page.cache_digest())
                    else "ready")
        except Exception:                      # noqa: BLE001
            return "ready"

    def _tab_cache_state(self, index: int) -> str:
        return self.page_cache_state(self.tabs.widget(index))

    def _refresh_tab_states(self):
        count = self.tabs.count()
        states = [self._tab_cache_state(i) for i in range(count)]
        if states != self._tab_states:
            self._tab_states = states
            bar = self.tabs.tabBar()
            for i, state in enumerate(states):
                page = self.tabs.widget(i)
                tip = {"loading": "正在抽帧 / 加载缓存…",
                       "ready": "图墙已加载，还没保存进缓存（右键标签可以保存）",
                       "saved": "已缓存（下次打开直接加载）",
                       "none": "没有可保存的缓存"}.get(state, "")
                path = getattr(page, "path", "")
                self.tabs.setTabToolTip(i, f"{path}\n{tip}" if tip else str(path))
            bar.refresh_states()

    def _auto_save_pass(self) -> int:
        """按自动保存策略把「已完成但还没进库」的页面悄悄存掉（标签会转绿）。"""
        cfg = self.app_settings.cache()
        mode = cfg.get("mode")
        if mode == CACHE_MODE_NEVER:
            return 0
        if self.jobs.running_count:
            return 0                       # 还在抽帧/导出，别抢磁盘，下一拍再说
        for page in list(self._pages):
            if not isinstance(page, TrackPage) or not page.cache_ready():
                continue
            try:
                if self.cache_store.has(page.cache_digest()):
                    continue
            except Exception:                  # noqa: BLE001
                continue
            if mode == CACHE_MODE_USAGE:
                usage = self.cache_store.usage(page.path)
                usage["used_seconds"] = (float(usage.get("used_seconds") or 0)
                                         + page.used_seconds)
                usage["exports"] = (int(usage.get("exports") or 0)
                                    + page.export_count)
                if not usage_meets(cfg, usage):
                    continue
            # 一次只存一页：大缓存（几千张图）复制/链接要时间，别让界面卡住
            return 1 if self._save_page_cache(page) else 0
        return 0

    def _save_page_manually(self, page) -> None:
        self._flush_usage(page)
        if not self._save_page_cache(page):
            QMessageBox.warning(
                self, "保存缓存",
                "缓存没能保存（目录不可写 / 磁盘空间不足？）。\n"
                f"目录：{self.cache_store.root}")
        self._refresh_tab_states()

    def _tab_menu(self, pos):
        bar = self.tabs.tabBar()
        index = bar.tabAt(pos)
        if index < 0:
            return
        page = self.tabs.widget(index)
        if not isinstance(page, TrackPage):
            return
        menu = self._build_tab_menu(page, index)
        menu.exec(bar.mapToGlobal(pos))

    def _build_tab_menu(self, page, index: int) -> QMenu:
        """标签右键菜单（单独建好再 exec，方便测试直接看条目）。"""
        menu = QMenu(self)
        state = self.page_cache_state(page)
        act_save = menu.addAction("保存这一页的缓存")
        act_save.setEnabled(state == "ready")
        act_save.triggered.connect(lambda: self._save_page_manually(page))
        act_reload = menu.addAction("重新抽帧 / 重新加载")
        act_reload.triggered.connect(lambda: self._on_reload(page))
        menu.addSeparator()
        act_close = menu.addAction("关闭这一页")
        act_close.triggered.connect(lambda: self._close_page(page))
        act_others = menu.addAction("关闭其他")
        act_others.triggered.connect(lambda: self._close_others(index))
        menu.addSeparator()
        act_all = menu.addAction("关闭全部")
        act_all.triggered.connect(self.clear_all)
        return menu

    def _close_others(self, keep_index: int):
        for index in range(self.tabs.count() - 1, -1, -1):
            if index != keep_index:
                self.close_tab(index)

    # ------------------------------------------------------------ 设置 / 缓存
    def open_settings(self):
        dialog = SettingsDialog(
            self, settings=self.app_settings, store=self.cache_store,
            save_now=self._save_rendered_now,
            check_update=(lambda: self.check_updates(manual=True))
            if update_checks_enabled() else None,
            load_cache=self.open_cached_entry)
        dialog.exec()
        self.cache_store.reload()
        self.cache_store.prune()
        self._refresh_tab_states()
        # 设置里刚打开自动检查 / 清掉了忽略 → 立刻查一次（药丸还是灰的才查）
        prefs = self.app_settings.update_prefs()
        if (update_checks_enabled() and prefs.get("auto_check", True)
                and self.badge.state_name() == "idle"):
            self.check_updates(manual=True)

    def _setup_usage_timer(self):
        """给「自动保存用得多 / 导出过」的策略攒数据：只记当前页签。"""
        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(USAGE_TICK_MS)
        self._usage_timer.timeout.connect(self._tick_usage)
        self._usage_timer.start()

    def _tick_usage(self):
        if not self.isActiveWindow():
            return
        page = self.tabs.currentWidget()
        if isinstance(page, TrackPage):
            page.used_seconds += USAGE_TICK_MS / 1000.0

    def _flush_usage(self, page):
        if not isinstance(page, TrackPage):
            return
        seconds, exports = page.used_seconds, page.export_count
        page.used_seconds, page.export_count = 0.0, 0
        if not seconds and not exports:
            return
        try:
            self.cache_store.record_usage(page.path, seconds, exports)
        except Exception:      # noqa: BLE001  统计失败不能影响正常使用
            pass

    def _flush_all_usage(self):
        for page in list(self._pages):
            self._flush_usage(page)

    def _save_page_cache(self, page) -> bool:
        if not isinstance(page, TrackPage) or not page.cache_ready():
            return False
        try:
            ok, _why = self.cache_store.save_dir(
                page.cache_digest(), page.out_dir, page.cache_entry())
        except Exception:      # noqa: BLE001
            return False
        return ok

    def _save_rendered_now(self):
        """设置页的「立即保存本次已渲染的视频」。返回 (保存数, 失败数)。"""
        saved = failed = 0
        for page in [p for p in self._pages
                     if isinstance(p, TrackPage) and p.cache_ready()]:
            self._flush_usage(page)
            if self._save_page_cache(page):
                saved += 1
            else:
                failed += 1
        return saved, failed

    def _uncached_pages(self):
        """本次渲染完成、但持久库里还没有的页签。"""
        return [p for p in self._pages
                if isinstance(p, TrackPage) and p.cache_ready()
                and not self.cache_store.has(p.cache_digest())]

    def _apply_cache_policy(self, cfg: dict, pages) -> tuple[int, int]:
        """按策略保存；返回 ``(保存数, 失败数)``。"""
        mode = cfg.get("mode")
        if mode == CACHE_MODE_NEVER:
            return 0, 0
        saved = failed = 0
        for page in pages:
            self._flush_usage(page)
            if mode == CACHE_MODE_USAGE:
                usage = self.cache_store.usage(page.path)
                if not usage_meets(cfg, usage):
                    continue
            if self._save_page_cache(page):
                saved += 1
            else:
                failed += 1
        self.cache_store.prune()
        return saved, failed

    def _handle_close_cache(self) -> bool:
        """关窗口前处理缓存。返回 False = 用户取消，不要关。"""
        pages = self._uncached_pages()
        if not pages:
            self._flush_all_usage()
            return True
        cfg = self.app_settings.cache()
        if not close_prompt_needed(cfg):
            self._apply_cache_policy(cfg, pages)
            self._flush_all_usage()
            return True
        dialog = CloseCacheDialog(self, pages, cfg, self.cache_store)
        if dialog.exec() != QDialog.Accepted:
            return False
        values = dialog.values()
        if dialog.action == "skip":
            if values.get("skip_close_prompt") != cfg.get("skip_close_prompt"):
                self.app_settings.update_cache(
                    skip_close_prompt=values["skip_close_prompt"])
                self.app_settings.save()
            self._flush_all_usage()
            return True
        merged = dict(cfg)
        merged.update(values)
        self.app_settings.update_cache(**values)
        if not self.app_settings.save():
            self.lbl_status.setText("⚠ 缓存设置没能保存（目录不可写）")
        _saved, failed = self._apply_cache_policy(merged, pages)
        if failed:
            QMessageBox.warning(
                self, "保存缓存",
                f"{failed} 个视频的缓存没能保存（缓存目录不可写 / 磁盘空间不足？）。\n"
                f"目录：{self.cache_store.root}")
        self._flush_all_usage()
        return True

    # ------------------------------------------------------------ 设置
    def _step(self) -> float:
        return float(self.combo_step.value())

    def _step_for(self, info) -> float:
        """新页面用多大的间隔：默认 = 源视频帧率（1 秒 1 帧）。"""
        if getattr(self, "_step_auto", True):
            return snap_step(getattr(info, "fps", 0.0))
        return self._step()

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
        """手动选了数字 → 固定间隔，不再跟随视频帧率。"""
        self._step_auto = False
        try:
            self.setting.setValue("ui/step_auto", False)
            self.setting.setValue("ui/step", float(v))
        except (TypeError, ValueError):
            pass
        self._refresh_titles()

    def _on_step_auto_picked(self):
        """选了「跟随视频帧率」→ 之后新开的视频都按它自己的帧率抽。"""
        self._step_auto = True
        self.setting.setValue("ui/step_auto", True)
        page = self.tabs.currentWidget()
        if isinstance(page, TrackPage):
            self.combo_step.set_value(page.step, notify=False)
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
                f"按镜头抽帧（灵敏度 {preset_label(self._scene_threshold)}）："
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
        self._refresh_tab_states()
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
    def _last_dir(self, key: str, fallback: str = "") -> str:
        """上次用过的目录（不存在就退回兜底 / 用户目录）。"""
        for candidate in (key, fallback):
            if not candidate:
                continue
            value = str(self.setting.value(candidate, "") or "")
            if not value:
                continue
            try:
                if Path(value).is_dir():
                    return value
            except OSError:
                continue
        return str(Path.home())

    def _remember_dir(self, key: str, path) -> None:
        try:
            folder = Path(path)
            if folder.is_file():
                folder = folder.parent
            if folder.is_dir():
                self.setting.setValue(key, str(folder))
        except OSError:
            pass

    def open_files(self):
        pattern = "视频文件 (" + " ".join("*" + x for x in sorted(VIDEO_EXTS)) + ");;所有文件 (*.*)"
        start = self._last_dir("paths/last_file_dir", "paths/last_folder_dir")
        files, _ = QFileDialog.getOpenFileNames(self, "选择视频", start, pattern)
        if files:
            self._remember_dir("paths/last_file_dir", files[0])
            self.add_videos([Path(f) for f in files])

    def open_folder(self):
        start = self._last_dir("paths/last_folder_dir", "paths/last_file_dir")
        d = QFileDialog.getExistingDirectory(self, "选择文件夹（含视频）", start)
        if d:
            self._remember_dir("paths/last_folder_dir", d)
            vids = find_videos(Path(d))
            if not vids:
                QMessageBox.information(self, APP_NAME, "该文件夹里没有找到视频文件。")
                return
            self.add_videos(vids)

    # ------------------------------------------------------- 从缓存导入
    def import_from_cache(self):
        """列出已保存的缓存，选一条直接打开对应视频（不重新抽帧）。"""
        if not sorted_entries(self.cache_store):
            QMessageBox.information(
                self, "从缓存导入",
                "还没有保存过缓存。\n\n"
                "打开视频后，标签页左边的红绿灯变成蓝色时点一下就能保存。")
            return
        CacheImportDialog(self, self.cache_store, self.open_cached_entry).exec()

    def open_cached_entry(self, entry: dict) -> bool:
        """按缓存条目里的设置打开原视频（命中持久缓存 -> 秒开）。"""
        video = Path(str(entry.get("video") or ""))
        if not video.is_file():
            QMessageBox.warning(
                self, "从缓存导入",
                f"原视频不在了：\n{video}\n\n"
                "这条缓存可以到 设置-缓存 → 缓存条目 里删除。")
            return False
        try:
            info = probe(video)
        except Exception as e:  # noqa: BLE001
            self.lbl_status.setText(f"⚠ {video.name}：{e}")
            return False
        for existing in self._pages:
            try:
                if existing.path.resolve() == video.resolve():
                    self.tabs.setCurrentWidget(existing)
                    return True
            except OSError:
                continue
        try:
            step = max(STEP_MIN, min(STEP_MAX, float(entry.get("step") or DEFAULT_STEP)))
        except (TypeError, ValueError):
            step = DEFAULT_STEP
        threshold = None
        if entry.get("mode") == "scene":
            try:
                threshold = max(0.01, min(0.99, float(entry.get("threshold") or 0.30)))
            except (TypeError, ValueError):
                threshold = 0.30
        try:
            tw = int(entry.get("tw") or 0)
            th = int(entry.get("th") or 0)
        except (TypeError, ValueError):
            tw = th = 0
        if tw <= 0 or th <= 0:
            tw, th = self._thumb_size_for(info)
        page = TrackPage(video, step, tw, th, info, self.jobs,
                         display_side=self._display_side,
                         scene_threshold=threshold,
                         store=self.cache_store)
        if not self._insert_page(page):
            return False
        self._refresh_tab_states()
        return True

    def add_videos(self, paths):
        paths = [Path(p) for p in paths]
        if paths:
            self._remember_dir("paths/last_file_dir", paths[0])
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

    def add_video(self, path: Path, index: int | None = None,
                  step: float | None = None) -> bool:
        try:
            info = probe(path)
        except Exception as e:  # noqa: BLE001
            self.lbl_status.setText(f"⚠ {path.name}：{e}")
            return False
        if step is None:
            step = self._step_for(info)
            if self._step_auto:
                # 顶部的间隔跟着这个视频的帧率走（用户能直接看到「25 帧」）
                self.combo_step.set_value(step, notify=False)
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
                         scene_threshold=threshold,
                         store=self.cache_store)
        return self._insert_page(page, index=index)

    def _insert_page(self, page: TrackPage, index: int | None = None,
                     sidebar_width: int | None = None) -> bool:
        """把已经建好的 TrackPage 接进标签栏并开始加载。"""
        page.reload_requested.connect(self._on_reload)
        page.display_side_changed.connect(self._on_page_display_side)
        page.split.splitterMoved.connect(self._on_splitter_moved)
        page.set_sidebar_width(self._sidebar_width if sidebar_width is None
                               else sidebar_width)
        name = page.path.name if len(page.path.name) <= 28 \
            else page.path.name[:26] + "…"
        if index is None:
            idx = self.tabs.addTab(page, name)
        else:
            idx = self.tabs.insertTab(index, page, name)
        self.tabs.setTabToolTip(idx, str(page.path))
        self._install_tab_close(idx, page)
        self._pages.append(page)
        if index is None:
            self.tabs.setCurrentIndex(idx)
        page.lbl_count.setText("排队中…")
        page.start()
        self._refresh_stack()
        self._refresh_status()
        self._refresh_tab_states()
        if not self._restoring:
            self.save_session("tabs")
        return True

    def _install_tab_close(self, index: int, page: TrackPage):
        """每个标签右侧一个自绘关闭按钮（用 36px 容器把它往左让出 18px）。"""
        holder = QWidget()
        holder.setObjectName("tabCloseHolder")
        holder.setFixedWidth(36)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        button = TabCloseButton(holder)
        button.setToolTip("关闭这个标签页（Ctrl+W）")
        button.clicked.connect(lambda _=False, p=page: self._close_page(p))
        row.addWidget(button, 0, Qt.AlignLeft | Qt.AlignVCenter)
        row.addStretch(1)
        self.tabs.tabBar().setTabButton(index, QTabBar.RightSide, holder)

    def _close_page(self, page: TrackPage):
        index = self.tabs.indexOf(page)
        if index >= 0:
            self.close_tab(index)

    def _forget_page(self, page):
        if page in self._pages:
            self._pages.remove(page)
        self._refresh_status()

    def _on_reload(self, page):
        idx = self.tabs.indexOf(page)
        if idx < 0:
            return
        path = page.path
        step = page.step
        self.close_tab(idx)
        self.add_video(path, index=idx, step=step)
        self.tabs.setCurrentIndex(min(idx, self.tabs.count() - 1))

    def close_tab(self, index, keep_count=False):
        page = self.tabs.widget(index)
        if page is None:
            return
        self._flush_usage(page)
        try:
            page.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.tabs.removeTab(index)
        self._forget_page(page)
        page.deleteLater()
        self._refresh_stack()
        if not self._restoring:
            self.save_session("tabs")

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
            if self._step_auto:
                self.combo_step.set_value(cur.step, notify=False)
        self._refresh_tab_states()

    def _refresh_stack(self):
        self.stack.setCurrentWidget(self.tabs if self.tabs.count() else self.empty)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.tabs.count() == 0:
            self.close()
            return
        super().keyPressEvent(e)

    # ------------------------------------------------------------ 会话快照
    def session_payload(self) -> dict:
        """把当前界面打包成 session.py 的结构。"""
        tabs = []
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            if not isinstance(page, TrackPage):
                continue
            try:
                tabs.append(page.session_snapshot())
            except Exception:          # noqa: BLE001  某一页坏了不能拖累其他
                continue
        try:
            geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except RuntimeError:
            geometry = ""
        return {
            "version": session_state.SESSION_VERSION,
            "saved_at": time.time(),
            "reason": self._session_reason,
            "window": {"geometry": geometry,
                       "maximized": bool(self.isMaximized())},
            "tabs": tabs,
            "current": max(0, self.tabs.currentIndex()),
        }

    def save_session(self, reason: str = "auto") -> bool:
        """落盘（原子写）；失败不提示 —— 丢会话可以接受，弹窗打断不行。"""
        self._session_reason = reason
        try:
            return session_state.save(self.session_payload())
        except Exception:              # noqa: BLE001
            return False

    def restore_session(self) -> int:
        """按 session.json 重建标签页；返回成功恢复的页数。"""
        data = session_state.load()
        entries = data.get("tabs") or []
        if not entries:
            return 0
        window = data.get("window") or {}
        if window.get("geometry"):
            self._apply_session_geometry(window)
        self._restoring = True
        restored, missing = 0, []
        try:
            for entry in entries:
                try:
                    if self._restore_tab(entry):
                        restored += 1
                    else:
                        missing.append(str(entry.get("path") or "?"))
                except Exception:      # noqa: BLE001  单个标签失败不影响其他
                    missing.append(str(entry.get("path") or "?"))
        finally:
            self._restoring = False
        if restored:
            self._select_restored(data, entries)
            self.save_session("auto")
            text = f"已恢复上次的 {restored} 个标签页"
            if missing:
                text += f"（{len(missing)} 个视频找不到，已跳过）"
            self.lbl_status.setText(text)
        elif missing:
            self.lbl_status.setText(
                f"上次的 {len(missing)} 个视频都不在了，没有恢复任何标签页")
        return restored

    def _restore_tab(self, entry: dict) -> bool:
        """按会话里的一份记录重建一个 TrackPage（缓存命中则秒开）。"""
        path = Path(str(entry.get("path") or ""))
        if not path.is_file():
            return False
        try:
            info = probe(path)
        except Exception:              # noqa: BLE001
            return False
        try:
            step = max(STEP_MIN, min(STEP_MAX, float(entry.get("step")
                                                     or DEFAULT_STEP)))
        except (TypeError, ValueError):
            step = float(DEFAULT_STEP)
        threshold = None
        if entry.get("mode") == "scene":
            try:
                threshold = max(0.01, min(0.99,
                                           float(entry.get("threshold") or 0.30)))
            except (TypeError, ValueError):
                threshold = 0.30
        try:
            side = int(entry.get("display_side") or DEFAULT_DISPLAY_SIDE)
        except (TypeError, ValueError):
            side = DEFAULT_DISPLAY_SIDE
        tw, th = self._thumb_size_for(info)
        page = TrackPage(path, step, tw, th, info, self.jobs,
                         display_side=side,
                         scene_threshold=threshold,
                         store=self.cache_store,
                         restore=entry)
        return self._insert_page(page, sidebar_width=entry.get("sidebar_width"))

    def _select_restored(self, data: dict, entries: list) -> None:
        """切回上次停留的那个视频（按路径匹配，跳过恢复失败的页）。"""
        try:
            wanted = entries[int(data.get("current") or 0)].get("path")
        except (IndexError, TypeError, ValueError):
            wanted = None
        if not wanted:
            return
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            if isinstance(page, TrackPage) and str(page.path) == str(wanted):
                self.tabs.setCurrentIndex(index)
                return

    def _apply_session_geometry(self, window: dict) -> bool:
        raw = str(window.get("geometry") or "")
        if not raw:
            return False
        try:
            state = QByteArray.fromBase64(raw.encode("ascii"))
            if state.isEmpty():
                return False
            self.restoreGeometry(state)
        except (RuntimeError, ValueError):
            return False
        screens = QApplication.screens() or []
        if screens and not any(s.availableGeometry().intersects(self.frameGeometry())
                               for s in screens):
            return False
        if window.get("maximized"):
            self.showMaximized()
        return True

    def restore_geometry(self) -> bool:
        """恢复上一次的窗口大小 / 位置（超出所有屏幕就放弃，用默认尺寸）。"""
        state = self.setting.value("ui/window_geometry")
        if not isinstance(state, QByteArray) or state.isEmpty():
            return False
        try:
            self.restoreGeometry(state)
        except RuntimeError:
            return False
        screens = QApplication.screens() or []
        if screens and not any(s.availableGeometry().intersects(self.frameGeometry())
                               for s in screens):
            return False
        maximized = self.setting.value("ui/window_maximized", False)
        if maximized is True or str(maximized).lower() in ("1", "true", "yes"):
            self.showMaximized()
        return True

    def closeEvent(self, e):
        if not self._handle_close_cache():
            e.ignore()
            return
        self.save_session("close")          # 关窗前把标签/帧/片段记下来
        try:
            self.setting.setValue("ui/window_geometry", self.saveGeometry())
            self.setting.setValue("ui/window_maximized", self.isMaximized())
            self.setting.sync()
        except (RuntimeError, OSError):
            pass
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
