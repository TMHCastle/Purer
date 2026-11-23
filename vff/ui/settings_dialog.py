# -*- coding: utf-8 -*-
"""设置窗（关于 / 缓存）+ 关闭前「要不要保存缓存」的提示。

关于页：版本信息、库与运行环境、各种目录 —— 一眼能看清「这是哪个版本、
用的哪个 ffmpeg、缓存放在哪」。
缓存页：自动保存策略（三个选项）、数量 / 使用阈值、体积上限、关闭前提醒开关。
"""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

import purer_boot

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy, QSpinBox,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import APP_NAME, APP_TAGLINE, APP_VERSION
from ..config import effective_version
from ..cache import cache_root, temp_cache_bytes, wall_bytes
from ..cache_store import INDEX_NAME, CacheStore
from ..config import GITHUB_REPO, app_dir, persistent_cache_root
from ..ffmpeg import Tools, tool_version
from ..settings import (CACHE_MODE_COUNT, CACHE_MODE_LABELS, CACHE_MODE_NEVER,
                        CACHE_MODE_USAGE, DEFAULT_UPDATE,
                        default_cache_limit_bytes, update_clear_skip)
from ..update import RELEASES_PAGE
from ..util import human_size
from .cache_list import CacheEntryTable, sorted_entries
from .style import make_icon


# --------------------------------------------------------------------------- #
# 小组件
# --------------------------------------------------------------------------- #
def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
    """一块「卡片」：标题 + 说明 + 内容（内容靠调用方往里塞）。"""
    box = QFrame()
    box.setObjectName("cfgCard")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(12, 10, 12, 12)
    lay.setSpacing(6)
    head = QLabel(title)
    head.setObjectName("cfgHead")
    lay.addWidget(head)
    if hint:
        note = QLabel(hint)
        note.setObjectName("cfgNote")
        note.setWordWrap(True)
        lay.addWidget(note)
    return box, lay


def _row_with_label(key: str, value: str,
                    tooltip: str = "") -> tuple[QWidget, QLabel]:
    """一行「键：值」，返回值标签（需要在运行中更新时用）。"""
    host = QWidget()
    line = QHBoxLayout(host)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(10)
    label = QLabel(key)
    label.setObjectName("cfgKey")
    label.setFixedWidth(96)
    value_label = QLabel(value or "—")
    value_label.setObjectName("cfgVal")
    value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    value_label.setWordWrap(True)
    value_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    if tooltip or value:
        value_label.setToolTip(tooltip or value)
    line.addWidget(label, 0, Qt.AlignTop)
    line.addWidget(value_label, 1)
    return host, value_label


def _row(key: str, value: str, tooltip: str = "") -> QWidget:
    """一行「键：值」，值可选中复制。"""
    return _row_with_label(key, value, tooltip)[0]


def _button(text: str, slot, primary: bool = False, ghost: bool = False) -> QPushButton:
    btn = QPushButton(text)
    if primary:
        btn.setObjectName("primary")
    elif ghost:
        # 这些按钮默认是「只有文字、看不出能点」的样式 -> 加白色圆角外框
        btn.setObjectName("hintBorder")
    btn.clicked.connect(slot)
    return btn


def _disk_hint(path: Path) -> str:
    from ..settings import disk_total_bytes
    total = disk_total_bytes(path)
    return f"磁盘的 0.5%（磁盘 {human_size(total)}）" if total else "磁盘的 0.5%"


def elide_path(text, limit: int = 56) -> str:
    """长路径中间省略（悬停仍能看到完整路径）。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = value[: max(8, limit // 4)]
    tail = value[-(limit - len(head) - 1):]
    return f"{head}…{tail}"


# --------------------------------------------------------------------------- #
# 设置窗
# --------------------------------------------------------------------------- #
class SettingsDialog(QDialog):
    def __init__(self, parent=None, settings=None, store: CacheStore | None = None,
                 save_now=None, check_update=None, load_cache=None):
        super().__init__(parent)
        self.setObjectName("settingsDialog")
        self.setWindowTitle(f"设置 · {APP_NAME}")
        self.setModal(True)
        width, height = 820, 780
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            width = max(680, min(width, area.width() - 60))
            height = max(560, min(height, area.height() - 60))
        self.resize(width, height)
        self.settings = settings
        self.store = store
        self._save_now = save_now
        self._check_update = check_update
        self._load_cache = load_cache
        # 改动即时落盘：控件回调里只是「排个队」，250ms 内合并成一次写盘
        self._autosave_armed = False
        self._persist_timer = QTimer(self)
        self._persist_timer.setSingleShot(True)
        self._persist_timer.setInterval(220)
        self._persist_timer.timeout.connect(self._persist)

        head = QHBoxLayout()
        head.setSpacing(10)
        icon = QLabel()
        icon.setPixmap(make_icon().pixmap(34, 34))
        head.addWidget(icon)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("设置")
        font = QFont(title.font())
        font.setPointSizeF(font.pointSizeF() + 3)
        font.setWeight(QFont.DemiBold)
        title.setFont(font)
        sub = QLabel(f"{APP_NAME} v{effective_version()} · {APP_TAGLINE}")
        sub.setObjectName("dim")
        titles.addWidget(title)
        titles.addWidget(sub)
        head.addLayout(titles, 1)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("settingsTabs")
        self.about_page = self._build_about()
        self.update_page = self._build_update()
        self.cache_page = self._build_cache()
        # 缓存页在左边，并且默认打开它（最常用的设置）
        self.tabs.addTab(self.cache_page, "缓存")
        self.tabs.addTab(self.update_page, "更新")
        self.tabs.addTab(self.about_page, "关于")
        self.tabs.setCurrentIndex(0)

        self.lbl_saved = QLabel("改动会立即保存")
        self.lbl_saved.setObjectName("cfgNote")
        btn_close = _button("完成", self.accept)
        btn_close.setDefault(True)
        bar = QHBoxLayout()
        bar.addWidget(self.lbl_saved)     # 左：保存状态（✓ 已保存 / ⚠ 写不进去）
        bar.addStretch(1)
        bar.addWidget(btn_close)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        lay.addLayout(head)
        lay.addWidget(self.tabs, 1)
        lay.addLayout(bar)
        self._refresh_summary()

    # ------------------------------------------------------------ 关于
    def _build_about(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("cfgScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("cfgBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(10)

        # ---- 版本信息 ----
        box, box_lay = _card("版本信息")
        frozen = bool(getattr(sys, "frozen", False))
        active = purer_boot.active_pack()
        rows = [
            ("当前版本", f"v{effective_version()}"
             + (f"（exe v{APP_VERSION} + 热补丁 v{active['version']}）"
                if active else "")),
            ("运行方式", "打包 exe" if frozen else "源码运行（python Purer.py）"),
            ("发布仓库", f"github.com/{GITHUB_REPO}"),
            ("更新检查", "启动后自动查一次 GitHub 发布（可点顶部药丸）"),
        ]
        for key, value in rows:
            box_lay.addWidget(_row(key, value))
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(_button("打开发布页", self._open_releases, ghost=True))
        if self._check_update is not None:
            actions.addWidget(_button("立即检查更新", self._check_now, ghost=True))
        actions.addStretch(1)
        box_lay.addLayout(actions)
        lay.addWidget(box)

        # ---- 库 / 运行环境 ----
        box2, box2_lay = _card(
            "库与运行环境",
            "切片的帧精确拼接由 ffmpeg 完成；界面是 Qt（PySide6）。")
        ff = Tools.ffmpeg
        fp = Tools.ffprobe
        for key, value, tip in (
                ("ffmpeg", tool_version(ff) or ("未找到" if not ff else "未知版本"),
                 str(ff or "")),
                ("ffprobe", tool_version(fp) or ("未找到" if not fp else "未知版本"),
                 str(fp or "")),
                ("ffmpeg 位置", elide_path(ff or "未找到"), str(ff or "")),
                ("ffmpeg 来源", Tools.ffmpeg_source or "—", ""),
                ("PySide6 / Qt", f"{self._version_of('PySide6')} / "
                 f"{self._version_of('PySide6.QtCore', attr='qVersion')}", ""),
                ("Python", sys.version.split()[0], sys.version.splitlines()[0]),
                ("系统", f"{platform.system()} {platform.release()}",
                 platform.platform()),
        ):
            box2_lay.addWidget(_row(key, value, tip))
        lay.addWidget(box2)

        # ---- 位置 ----
        saved_root = self._cache_dir()
        box3, box3_lay = _card("位置")
        box3_lay.addWidget(_row("程序目录", elide_path(app_dir()), str(app_dir())))
        saved_row, self.lbl_about_cache = _row_with_label(
            "已保存缓存", elide_path(saved_root), str(saved_root))
        box3_lay.addWidget(saved_row)
        box3_lay.addWidget(_row("临时缓存", elide_path(cache_root()),
                                str(cache_root())))
        box3_lay.addWidget(_row("设置文件",
                                elide_path(self.settings.path if self.settings
                                           else "—"),
                                str(self.settings.path) if self.settings else ""))
        paths = QHBoxLayout()
        paths.setSpacing(8)
        paths.addWidget(_button("打开程序目录",
                                lambda: self._open_dir(app_dir()), ghost=True))
        paths.addWidget(_button("打开缓存目录",
                                lambda: self._open_dir(self._cache_dir()),
                                ghost=True))
        paths.addWidget(_button("复制诊断信息", self._copy_info, ghost=True))
        paths.addStretch(1)
        box3_lay.addLayout(paths)
        lay.addWidget(box3)

        lay.addStretch(1)
        scroll.setWidget(body)
        return scroll

    @staticmethod
    def _version_of(module_name: str, attr: str | None = None) -> str:
        try:
            module = __import__(module_name, fromlist=["*"])
            if attr:
                value = getattr(module, attr, None)
                return str(value() if callable(value) else value or "—")
            return str(getattr(module, "__version__", "—"))
        except Exception:      # noqa: BLE001
            return "—"

    def _open_releases(self):
        QDesktopServices.openUrl(QUrl(RELEASES_PAGE))

    def _open_dir(self, path: Path):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _cache_dir(self) -> Path:
        return self.store.root if self.store is not None else persistent_cache_root()

    def _check_now(self):
        callback = self._check_update
        if callback is None:
            return
        self.accept()
        callback()

    def _copy_info(self):
        lines = [f"{APP_NAME} v{effective_version()} · {APP_TAGLINE}",
                 f"程序目录: {app_dir()}",
                 f"已保存缓存: {persistent_cache_root()}",
                 f"临时缓存: {cache_root()}",
                 f"设置文件: {self.settings.path if self.settings else '—'}",
                 Tools.describe(),
                 f"PySide6: {self._version_of('PySide6')} · "
                 f"Qt: {self._version_of('PySide6.QtCore', attr='qVersion')}",
                 f"Python: {sys.version.split()[0]} · "
                 f"{platform.system()} {platform.release()}"]
        QApplication.clipboard().setText("\n".join(lines))

    # ------------------------------------------------------------ 缓存
    def _build_cache(self) -> QWidget:
        cfg = self.settings.cache() if self.settings else {}
        scroll = QScrollArea()
        scroll.setObjectName("cfgScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.cache_scroll = scroll
        body = QWidget()
        body.setObjectName("cfgBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(10)

        # ---- 保存策略 ----
        box, box_lay = _card(
            "保存渲染缓存",
            "抽好的缩略图墙先放在临时目录（同视频同设置再开秒开）；"
            "下面决定关闭时要不要「保存」下来 —— 保存过的跨重启也能直接加载。")
        self.radio_never = QRadioButton(CACHE_MODE_LABELS[CACHE_MODE_NEVER])
        self.radio_never.setToolTip("临时缓存仍然会让最近打开过的视频秒开，"
                                    "但重启电脑 / 清理临时文件后就没了")
        box_lay.addWidget(self.radio_never)

        row_count = QHBoxLayout()
        row_count.setSpacing(8)
        self.radio_count = QRadioButton("自动保存最近使用的，最多")
        self.spin_videos = QSpinBox()
        self.spin_videos.setRange(1, 9999)
        self.spin_videos.setSuffix(" 个视频")
        self.spin_videos.setFixedWidth(120)
        row_count.addWidget(self.radio_count)
        row_count.addWidget(self.spin_videos)
        row_count.addStretch(1)
        box_lay.addLayout(row_count)

        row_usage = QHBoxLayout()
        row_usage.setSpacing(8)
        self.radio_usage = QRadioButton("自动保存使用超过")
        self.spin_minutes = QSpinBox()
        self.spin_minutes.setRange(1, 100000)
        self.spin_minutes.setSuffix(" 分钟")
        self.spin_minutes.setFixedWidth(112)
        row_usage.addWidget(self.radio_usage)
        row_usage.addWidget(self.spin_minutes)
        row_usage.addWidget(QLabel("、或导出超过"))
        self.spin_exports = QSpinBox()
        self.spin_exports.setRange(1, 9999)
        self.spin_exports.setSuffix(" 次")
        self.spin_exports.setFixedWidth(92)
        row_usage.addWidget(self.spin_exports)
        row_usage.addWidget(QLabel("的素材"))
        row_usage.addStretch(1)
        box_lay.addLayout(row_usage)

        self.check_skip = QCheckBox("关闭前不再提醒未保存的视频缓存")
        self.check_skip.setToolTip(
            "只对「自动保存」策略生效；选「所有缓存都不自动保存」时每次关闭都会问")
        self._skip_pref = bool(cfg.get("skip_close_prompt"))
        self._syncing_skip = False
        self.check_skip.setChecked(self._skip_pref)
        self.check_skip.toggled.connect(self._on_skip_toggled)
        box_lay.addWidget(self.check_skip)
        self.lbl_skip_note = QLabel(
            "只有选择上面两种自动保存策略时这个选项才生效 —— "
            "默认「不自动保存」，关闭时仍会提示。")
        self.lbl_skip_note.setObjectName("cfgNote")
        self.lbl_skip_note.setWordWrap(True)
        box_lay.addWidget(self.lbl_skip_note)

        self.btn_reset_policy = _button("恢复默认（不自动保存 · 关闭时提示）",
                                        self._reset_policy, ghost=True)
        box_lay.addWidget(self.btn_reset_policy, 0, Qt.AlignLeft)

        mode = cfg.get("mode", CACHE_MODE_NEVER)
        (self.radio_never if mode == CACHE_MODE_NEVER else
         self.radio_usage if mode == CACHE_MODE_USAGE else
         self.radio_count).setChecked(True)
        self.spin_videos.setValue(int(cfg.get("max_videos") or 20))
        self.spin_minutes.setValue(int(cfg.get("usage_minutes") or 10))
        self.spin_exports.setValue(int(cfg.get("usage_exports") or 2))
        for radio in (self.radio_never, self.radio_count, self.radio_usage):
            radio.toggled.connect(self._sync_enabled)
        self._sync_enabled()
        lay.addWidget(box)

        # ---- 体积上限 ----
        box2, box2_lay = _card(
            "最大缓存体积",
            "超过上限时按「最久没使用」自动删除。默认取缓存所在磁盘总量的一小部分，"
            "可以随时改成固定值。")
        limit_row = QHBoxLayout()
        limit_row.setSpacing(8)
        self.spin_limit = QSpinBox()
        self.spin_limit.setRange(128, 32 * 1024)         # 128 MB ~ 32 GB
        self.spin_limit.setSingleStep(256)
        self.spin_limit.setSuffix(" MB")
        self.spin_limit.setFixedWidth(140)
        self.lbl_limit_hint = QLabel("")
        self.lbl_limit_hint.setObjectName("cfgNote")
        limit_row.addWidget(self.spin_limit)
        limit_row.addWidget(self.lbl_limit_hint, 1)
        limit_row.addWidget(_button(
            "恢复默认", self._reset_limit, ghost=True))
        box2_lay.addLayout(limit_row)
        cache_dir = self._cache_dir()
        configured = cfg.get("max_bytes")
        default_bytes = default_cache_limit_bytes(cache_dir)
        self.spin_limit.setValue(max(128, int((configured or default_bytes)
                                             / (1024 * 1024))))
        self.lbl_limit_hint.setText(
            f"默认 = {_disk_hint(cache_dir)} ≈ {human_size(default_bytes)}")
        lay.addWidget(box2)
        # 控件都建好了：从现在开始任何改动都即时落盘（见 _schedule_persist）
        self._autosave_armed = True
        for widget in (self.radio_never, self.radio_count, self.radio_usage):
            widget.toggled.connect(self._schedule_persist)
        for spin in (self.spin_videos, self.spin_minutes, self.spin_exports,
                     self.spin_limit):
            spin.valueChanged.connect(self._schedule_persist)
        self.check_skip.toggled.connect(self._schedule_persist)

        # ---- 现状 ----
        box3, box3_lay = _card("当前缓存")
        self.lbl_summary = QLabel("")
        self.lbl_summary.setObjectName("cfgVal")
        self.lbl_summary.setWordWrap(True)
        box3_lay.addWidget(self.lbl_summary)
        self.lbl_temp = QLabel("")
        self.lbl_temp.setObjectName("cfgNote")
        self.lbl_temp.setWordWrap(True)
        box3_lay.addWidget(self.lbl_temp)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(_button("打开缓存文件夹",
                                  lambda: self._open_dir(self._cache_dir()),
                                  ghost=True))
        self.btn_save_now = _button("立即保存本次已渲染的视频", self._save_rendered)
        actions.addWidget(self.btn_save_now)
        self.btn_clear = _button("清空已保存缓存", self._clear_cache)
        self.btn_clear.setObjectName("danger")
        actions.addWidget(self.btn_clear)
        actions.addStretch(1)
        box3_lay.addLayout(actions)
        lay.addWidget(box3)

        # ---- 缓存条目（可展开：逐条加载 / 删除） ----
        box_entries, entries_lay = _card(
            "缓存条目",
            "展开后可以逐个查看已保存的缓存；双击或「加载视频」直接打开，"
            "「删除缓存」只删缓存本身。")
        self.btn_entries = QPushButton("▸ 展开已保存的缓存")
        self.btn_entries.setObjectName("hintBorder")
        self.btn_entries.setCheckable(True)
        self.btn_entries.setCursor(Qt.PointingHandCursor)
        self.btn_entries.toggled.connect(self._toggle_entries)
        entries_lay.addWidget(self.btn_entries, 0, Qt.AlignLeft)
        self.entry_table = CacheEntryTable(self.store, self) if self.store else None
        if self.entry_table is not None:
            self.entry_table.setVisible(False)
            self.entry_table.openRequested.connect(self._open_entry)
            self.entry_table.changed.connect(self._on_entries_changed)
            entries_lay.addWidget(self.entry_table)
        self._refresh_entries_label()
        lay.addWidget(box_entries)

        # ---- 缓存位置（放在最后：改得最少） ----
        box_pos, pos_lay = _card(
            "缓存位置",
            "保存下来的图墙放在这里。换位置时可以选「迁移」，把已有的缓存一起搬过去。")
        pos_row = QHBoxLayout()
        pos_row.setSpacing(8)
        self.lbl_dir = QLabel(elide_path(cache_dir))
        self.lbl_dir.setObjectName("cfgVal")
        self.lbl_dir.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_dir.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_dir.setToolTip(str(cache_dir))
        pos_row.addWidget(self.lbl_dir, 1)
        pos_row.addWidget(_button("更改…", self._change_dir, ghost=True))
        pos_row.addWidget(_button("打开", lambda: self._open_dir(self._cache_dir()),
                                  ghost=True))
        pos_row.addWidget(_button("恢复默认", self._restore_dir, ghost=True))
        pos_lay.addLayout(pos_row)
        lay.addWidget(box_pos)

        lay.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _sync_enabled(self, *_args):
        never = self.radio_never.isChecked()
        self.spin_videos.setEnabled(self.radio_count.isChecked())
        usage = self.radio_usage.isChecked()
        self.spin_minutes.setEnabled(usage)
        self.spin_exports.setEnabled(usage)
        self._syncing_skip = True
        try:
            self.check_skip.setEnabled(not never)
            self.check_skip.setChecked(False if never else self._skip_pref)
        finally:
            self._syncing_skip = False
        if hasattr(self, "lbl_skip_note"):
            self.lbl_skip_note.setVisible(never)

    def _on_skip_toggled(self, checked: bool):
        if not getattr(self, "_syncing_skip", False):
            self._skip_pref = bool(checked)

    def _reset_policy(self):
        self.radio_never.setChecked(True)
        self.spin_videos.setValue(20)
        self.spin_minutes.setValue(10)
        self.spin_exports.setValue(2)
        self._skip_pref = False
        self._sync_enabled()

    def _reset_limit(self):
        default_bytes = default_cache_limit_bytes(self._cache_dir())
        self.spin_limit.setValue(max(128, int(default_bytes / (1024 * 1024))))

    # ------------------------------------------------------------ 缓存位置
    def _change_dir(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "选择缓存保存位置（建议用空文件夹）", str(self._cache_dir()))
        if chosen:
            self._switch_dir(Path(chosen))

    def _restore_dir(self):
        default = persistent_cache_root()
        try:
            same = default.resolve() == self._cache_dir().resolve()
        except OSError:
            same = default == self._cache_dir()
        if same:
            QMessageBox.information(self, "缓存位置", "现在用的就是默认位置。")
            return
        self._switch_dir(default)

    def _switch_dir(self, target: Path):
        if self.store is None:
            return
        current = self.store.root
        try:
            if target.resolve() == current.resolve():
                return
        except OSError:
            pass

        count_before = self.store.summary()["count"]
        moved = False
        if (target / INDEX_NAME).is_file():
            answer = QMessageBox.question(
                self, "缓存位置",
                f"这个位置以前保存过 Purer 缓存：\n{target}\n\n"
                "直接切换过去吗？（里面的缓存会重新出现在「当前缓存」里）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if answer != QMessageBox.Yes:
                return
            ok, why = self.store.retarget(target, move=False)
        else:
            try:
                if target.exists() and any(target.iterdir()):
                    QMessageBox.warning(
                        self, "缓存位置",
                        "这个文件夹不是空的。\n请新建一个空文件夹，"
                        "或选一个以前专门存 Purer 缓存的目录。")
                    return
            except OSError as e:
                QMessageBox.warning(self, "缓存位置", f"读不了这个文件夹：{e}")
                return
            move = False
            if count_before:
                answer = QMessageBox.question(
                    self, "缓存位置",
                    f"把已保存的 {count_before} 个缓存一起搬到新位置吗？\n\n"
                    f"新位置：{target}\n\n"
                    "「是」= 复制过去并清理旧位置；"
                    "「否」= 只换位置，旧缓存留在原处（新位置从空开始）。",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes)
                if answer == QMessageBox.Cancel:
                    return
                move = answer == QMessageBox.Yes
            ok, why = self.store.retarget(target, move=move)
            moved = move
        if not ok:
            QMessageBox.warning(self, "缓存位置", why)
            return

        default = persistent_cache_root()
        try:
            is_default = target.resolve() == default.resolve()
        except OSError:
            is_default = target == default
        if self.settings is not None:
            self.settings.update_cache(dir=None if is_default else str(target))
            self._persist()          # 顺带把「✓ 已保存」状态刷对
            self.store.prune()

        self.lbl_dir.setText(elide_path(target))
        self.lbl_dir.setToolTip(str(target))
        if hasattr(self, "lbl_about_cache"):
            self.lbl_about_cache.setText(elide_path(target))
            self.lbl_about_cache.setToolTip(str(target))
        default_bytes = default_cache_limit_bytes(target)
        self.lbl_limit_hint.setText(
            f"默认 = {_disk_hint(target)} ≈ {human_size(default_bytes)}")
        if self.settings is not None and self.settings.cache().get("max_bytes") is None:
            self.spin_limit.setValue(max(128, int(default_bytes / (1024 * 1024))))
        self._refresh_summary()
        if count_before and not moved:
            QMessageBox.information(
                self, "缓存位置",
                f"已切换到新位置。\n原位置的 {count_before} 个缓存没有删除：\n"
                f"{current}\n\n需要的话可以手动清理。")

    def _refresh_summary(self):
        if self.store is None:
            return
        summary = self.store.summary()
        limit = int(self.spin_limit.value()) * 1024 * 1024
        pct = (summary["bytes"] / limit * 100) if limit else 0
        self.lbl_summary.setText(
            f"已保存 {summary['count']} 个视频的缓存 · "
            f"{human_size(summary['bytes'])} / 上限 {human_size(limit)}"
            f"（{pct:.0f}%）· 共 {summary['frames']} 张缩略图")
        self.lbl_temp.setText(
            f"临时渲染缓存：{human_size(temp_cache_bytes())}（{cache_root()}）\n"
            f"已保存的缓存：{human_size(summary['bytes'])}（{self.store.root}）")
        self._refresh_entries_label()

    # ------------------------------------------------------------ 缓存条目
    def _toggle_entries(self, checked: bool):
        if self.entry_table is None:
            return
        self.entry_table.setVisible(checked)
        if checked:
            self.entry_table.refresh()
            QTimer.singleShot(
                0, lambda: self.cache_scroll.ensureWidgetVisible(
                    self.entry_table, 0, 24))
        self._refresh_entries_label()

    def _refresh_entries_label(self):
        if not hasattr(self, "btn_entries"):
            return
        count = len(sorted_entries(self.store)) if self.store is not None else 0
        expanded = self.btn_entries.isChecked()
        self.btn_entries.setText(
            f"{'▾ 收起' if expanded else '▸ 展开'}已保存的缓存（{count}）")

    def _on_entries_changed(self):
        self._refresh_summary()

    def _open_entry(self, entry: dict):
        callback = self._load_cache
        if callback is None:
            return
        self.accept()
        QTimer.singleShot(0, lambda: callback(entry))

    def _save_rendered(self):
        if self._save_now is None:
            QMessageBox.information(self, "保存缓存", "当前没有可保存的渲染结果。")
            return
        saved, failed = self._save_now()
        if self.entry_table is not None:
            self.entry_table.refresh()
        self._refresh_summary()
        if saved:
            QMessageBox.information(
                self, "保存缓存",
                f"已保存 {saved} 个视频的缓存。"
                + (f"\n{failed} 个没能保存。" if failed else ""))
        else:
            QMessageBox.information(
                self, "保存缓存",
                "这次没有可保存的渲染结果（要等抽帧完成，而且之前没保存过）。"
                + (f"\n{failed} 个没能保存。" if failed else ""))

    def _clear_cache(self):
        if self.store is None:
            return
        summary = self.store.summary()
        if not summary["count"]:
            QMessageBox.information(self, "清空缓存", "现在没有已保存的缓存。")
            return
        answer = QMessageBox.question(
            self, "清空缓存",
            f"删除已保存的 {summary['count']} 个视频缓存"
            f"（{human_size(summary['bytes'])}）？\n\n"
            "只删缓存，不动任何视频文件。之后打开这些视频需要重新抽帧。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.store.clear()
        if self.entry_table is not None:
            self.entry_table.refresh()
        self._refresh_summary()

    # ------------------------------------------------------------ 更新
    def _build_update(self) -> QWidget:
        prefs = (self.settings.update_prefs() if self.settings
                 else dict(DEFAULT_UPDATE))
        scroll = QScrollArea()
        scroll.setObjectName("cfgScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("cfgBody")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(10)

        box, box_lay = _card(
            "更新提醒",
            "发现新版本时会弹一个小窗问一次：立即更新 / 一周后提醒 / 忽略此版本 / "
            "不再提醒。更新会先保存当前标签页，换好程序后自动接上。")
        self.check_auto_update = QCheckBox("启动后自动检查更新")
        self.check_auto_update.setToolTip(
            "关掉后不再自动联网检查；右键点击顶部「检查更新」仍可手动查")
        self.check_auto_update.setChecked(bool(prefs.get("auto_check", True)))
        self.check_auto_update.toggled.connect(self._schedule_persist)
        box_lay.addWidget(self.check_auto_update)

        self.check_mute_update = QCheckBox("不再弹出更新提醒（忽略所有更新通知）")
        self.check_mute_update.setToolTip(
            "弹窗全关掉，顶部药丸也不再自动变橙；手动检查不受影响")
        self.check_mute_update.setChecked(bool(prefs.get("muted", False)))
        self.check_mute_update.toggled.connect(self._schedule_persist)
        box_lay.addWidget(self.check_mute_update)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.lbl_update_state = QLabel("")
        self.lbl_update_state.setObjectName("cfgNote")
        self.lbl_update_state.setWordWrap(True)
        row.addWidget(self.lbl_update_state, 1)
        self.btn_reset_update = _button("恢复更新提醒", self._reset_update_prefs,
                                        ghost=True)
        self.btn_reset_update.setToolTip("清掉「忽略的版本 / 不再提醒 / 一周后 remind」")
        row.addWidget(self.btn_reset_update)
        box_lay.addLayout(row)
        lay.addWidget(box)

        box2, box2_lay = _card(
            "版本与热补丁",
            "更新包从 GitHub Releases 下载，下载完先校验 sha256 再替换。"
            "「热补丁」只换 Python 代码，装完不用重启；改到界面资源 / 打包数据时只能整包更新。")
        self.lbl_update_ver = QLabel(f"当前版本 v{effective_version()}")
        self.lbl_update_ver.setObjectName("cfgVal")
        box2_lay.addWidget(self.lbl_update_ver)
        self.lbl_hotfix = QLabel("")
        self.lbl_hotfix.setObjectName("cfgNote")
        self.lbl_hotfix.setWordWrap(True)
        box2_lay.addWidget(self.lbl_hotfix)
        check_row = QHBoxLayout()
        check_row.setSpacing(8)
        self.btn_check_now = _button("立即检查", self._check_now, ghost=True)
        self.btn_check_now.setEnabled(self._check_update is not None)
        check_row.addWidget(self.btn_check_now)
        self.btn_drop_hotfix = _button("移除热补丁", self._drop_hotfix, ghost=True)
        self.btn_drop_hotfix.setToolTip(
            "删掉代码包文件，下次启动回到 exe 内置代码（当前窗口不受影响）")
        check_row.addWidget(self.btn_drop_hotfix)
        self.lbl_update_check = QLabel("")
        self.lbl_update_check.setObjectName("cfgNote")
        check_row.addWidget(self.lbl_update_check, 1)
        box2_lay.addLayout(check_row)
        lay.addWidget(box2)

        lay.addStretch(1)
        scroll.setWidget(body)
        self._refresh_update_labels()
        return scroll

    def _refresh_hotfix_label(self):
        if not hasattr(self, "lbl_hotfix"):
            return
        active = purer_boot.active_pack()
        installed = purer_boot.read_pack()
        if active:
            text = (f"热补丁 v{active.get('version')} 生效中"
                    f"（exe v{APP_VERSION}）"
                    f" · {purer_boot.pack_dir()}")
        elif installed:
            text = (f"代码包 v{installed.get('version')} 已就位但未生效"
                    f"（{purer_boot.last_error() or '需要重启'}）")
        else:
            text = "没有热补丁，正在运行 exe 内置代码"
        self.lbl_hotfix.setText(text)
        self.btn_drop_hotfix.setEnabled(bool(active or installed))

    def _drop_hotfix(self):
        from .. import hotfix
        hotfix.purge_files()
        self.lbl_update_check.setText("已删除代码包，下次启动回到内置代码")
        self._refresh_hotfix_label()

    def _refresh_update_labels(self):
        if not hasattr(self, "lbl_update_state"):
            return
        prefs = (self.settings.update_prefs() if self.settings
                 else dict(DEFAULT_UPDATE))
        parts = []
        if prefs.get("muted"):
            parts.append("已关闭所有更新提醒")
        if prefs.get("skip_version"):
            parts.append(f"已忽略 {prefs['skip_version']}")
        try:
            snooze = float(prefs.get("snooze_until") or 0)
        except (TypeError, ValueError):
            snooze = 0.0
        if snooze > time.time():
            parts.append("推迟到 "
                         + time.strftime("%m-%d %H:%M", time.localtime(snooze)))
        self.lbl_update_state.setText(" · ".join(parts) if parts
                                      else "目前没有忽略任何版本")
        self._refresh_hotfix_label()

    def _reset_update_prefs(self):
        if self.settings is None:
            return
        self.settings.set_update_prefs(
            **update_clear_skip(self.settings.update_prefs(), now=time.time()))
        self.settings.save()
        self.check_mute_update.setChecked(False)
        self._refresh_update_labels()

    def _check_now(self):
        if self._check_update is None:
            return
        try:
            self._check_update()
        except Exception:                  # noqa: BLE001
            pass
        self.lbl_update_check.setText(
            "已开始检查…关掉设置后看顶部的「检查更新」药丸")

    def _update_values(self) -> dict:
        prefs = (self.settings.update_prefs() if self.settings
                 else dict(DEFAULT_UPDATE))
        prefs["auto_check"] = bool(self.check_auto_update.isChecked())
        prefs["muted"] = bool(self.check_mute_update.isChecked())
        return prefs

    # ------------------------------------------------------------ 取值 / 保存
    def values(self) -> dict:
        mode = (CACHE_MODE_NEVER if self.radio_never.isChecked() else
                CACHE_MODE_USAGE if self.radio_usage.isChecked() else
                CACHE_MODE_COUNT)
        default_bytes = default_cache_limit_bytes(self._cache_dir())
        chosen = int(self.spin_limit.value()) * 1024 * 1024
        return {
            "mode": mode,
            "max_videos": int(self.spin_videos.value()),
            "usage_minutes": int(self.spin_minutes.value()),
            "usage_exports": int(self.spin_exports.value()),
            "max_bytes": None if abs(chosen - default_bytes) <= 1024 * 1024
            else chosen,
            # 「不再提醒」只对自动保存策略有效；不自动保存时一律按未勾选存
            "skip_close_prompt": (False if mode == CACHE_MODE_NEVER
                                  else self.check_skip.isChecked()),
        }

    def accept(self):
        self._persist_timer.stop()
        ok = self._persist(warn=True)
        if self.store is not None:
            self.store.prune()
        super().accept()

    def reject(self):
        # 用 Esc / 点标题栏的叉关掉也要把改动作数保存下来 ——
        # 「关掉就丢」正是之前「设置-缓存 不能正确保存」的根因。
        self._persist_timer.stop()
        self._persist()
        if self.store is not None:
            self.store.prune()
        super().reject()

    def closeEvent(self, event):               # noqa: N802
        self._persist_timer.stop()
        self._persist()
        super().closeEvent(event)

    # ------------------------------------------------------------ 即时保存
    def _schedule_persist(self, *_args):
        if not self._autosave_armed or self.settings is None:
            return
        self._persist_timer.start()

    def _persist(self, warn: bool = False) -> bool:
        """把当前界面上的选项写入 settings.json（任何关闭方式都会走这里）。"""
        if self.settings is None:
            return True
        try:
            values = self.values()
        except Exception:                      # noqa: BLE001  界面还没搭完
            return False
        self.settings.update_cache(**values)
        try:
            if hasattr(self, "check_auto_update"):
                self.settings.set_update_prefs(**self._update_values())
        except Exception:                      # noqa: BLE001  更新页还没搭好
            pass
        ok = self.settings.save()
        if hasattr(self, "lbl_saved"):
            if ok:
                self.lbl_saved.setText("✓ 已自动保存")
                self.lbl_saved.setToolTip(f"已写入 {self.settings.path}")
            else:
                self.lbl_saved.setText("⚠ 没能写入磁盘")
                self.lbl_saved.setToolTip(str(self.settings.path))
        if not ok and warn:
            QMessageBox.warning(
                self, "设置",
                "设置没能写入磁盘（目录不可写？）。\n"
                f"文件：{self.settings.path}\n"
                "本次运行会照常生效，但下次启动可能恢复默认值。")
        self._refresh_update_labels()
        return ok


# --------------------------------------------------------------------------- #
# 关闭前提示
# --------------------------------------------------------------------------- #
class CloseCacheDialog(QDialog):
    """关窗口时的「这些视频的缓存还没保存，要怎么办？」。"""

    def __init__(self, parent, pages, cache_cfg: dict, store: CacheStore | None = None):
        super().__init__(parent)
        self.setWindowTitle("保存视频缓存？")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.action = "cancel"        # cancel / apply / skip
        self._pages = list(pages)
        self._store = store

        total_bytes = 0
        total_frames = 0
        names = []
        for page in self._pages:
            try:
                size = wall_bytes(page.out_dir) if page.out_dir else 0
            except OSError:
                size = 0
            frames = page.model.rowCount()
            total_bytes += size
            total_frames += frames
            names.append(f"· {page.path.name}　{frames} 张 · {human_size(size)}")
            if len(names) >= 6:
                break
        if len(self._pages) > 6:
            names.append(f"…… 还有 {len(self._pages) - 6} 个")

        head = QLabel(f"有 {len(self._pages)} 个视频的缓存还没保存")
        font = QFont(head.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.DemiBold)
        head.setFont(font)
        body = QLabel(
            f"这些视频的图墙共 {total_frames} 张、约 {human_size(total_bytes)}。\n"
            "保存下来，下次打开同样的视频就能直接加载；不保存的话，"
            "临时缓存被系统清理后需要重新抽帧。\n\n" + "\n".join(names))
        body.setWordWrap(True)
        body.setObjectName("exportLabel")
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)

        box = QFrame()
        box.setObjectName("cfgCard")
        box_lay = QVBoxLayout(box)
        box_lay.setContentsMargins(12, 10, 12, 12)
        box_lay.setSpacing(8)
        tip = QLabel("以后自动保存哪些缓存？")
        tip.setObjectName("cfgHead")
        box_lay.addWidget(tip)

        self.radio_never = QRadioButton(CACHE_MODE_LABELS[CACHE_MODE_NEVER])
        box_lay.addWidget(self.radio_never)

        row_count = QHBoxLayout()
        row_count.setSpacing(8)
        self.radio_count = QRadioButton("自动保存最近使用的，最多")
        self.spin_videos = QSpinBox()
        self.spin_videos.setRange(1, 9999)
        self.spin_videos.setSuffix(" 个视频")
        self.spin_videos.setFixedWidth(120)
        row_count.addWidget(self.radio_count)
        row_count.addWidget(self.spin_videos)
        row_count.addStretch(1)
        box_lay.addLayout(row_count)

        row_usage = QHBoxLayout()
        row_usage.setSpacing(8)
        self.radio_usage = QRadioButton("自动保存使用超过")
        self.spin_minutes = QSpinBox()
        self.spin_minutes.setRange(1, 100000)
        self.spin_minutes.setSuffix(" 分钟")
        self.spin_minutes.setFixedWidth(112)
        row_usage.addWidget(self.radio_usage)
        row_usage.addWidget(self.spin_minutes)
        row_usage.addWidget(QLabel("、或导出超过"))
        self.spin_exports = QSpinBox()
        self.spin_exports.setRange(1, 9999)
        self.spin_exports.setSuffix(" 次")
        self.spin_exports.setFixedWidth(92)
        row_usage.addWidget(self.spin_exports)
        row_usage.addWidget(QLabel("的素材"))
        row_usage.addStretch(1)
        box_lay.addLayout(row_usage)

        self.check_skip = QCheckBox("不再显示这个提醒")
        self.check_skip.setToolTip("勾选后关闭时不再弹窗，直接按上面选中的策略执行"
                                   "（也可以随时在 设置-缓存 里改回来）")
        self._skip_pref = bool(cache_cfg.get("skip_close_prompt"))
        self._syncing_skip = False
        self.check_skip.setChecked(self._skip_pref)
        self.check_skip.toggled.connect(self._on_skip_toggled)
        box_lay.addWidget(self.check_skip)
        self.lbl_skip_note = QLabel(
            "选「所有缓存都不自动保存」时这个提醒每次关闭都会出现"
            "（没有可自动执行的策略）。")
        self.lbl_skip_note.setObjectName("cfgNote")
        self.lbl_skip_note.setWordWrap(True)
        box_lay.addWidget(self.lbl_skip_note)

        mode = cache_cfg.get("mode", CACHE_MODE_NEVER)
        (self.radio_never if mode == CACHE_MODE_NEVER else
         self.radio_usage if mode == CACHE_MODE_USAGE else
         self.radio_count).setChecked(True)
        self.spin_videos.setValue(int(cache_cfg.get("max_videos") or 20))
        self.spin_minutes.setValue(int(cache_cfg.get("usage_minutes") or 10))
        self.spin_exports.setValue(int(cache_cfg.get("usage_exports") or 2))
        for radio in (self.radio_never, self.radio_count, self.radio_usage):
            radio.toggled.connect(self._sync_enabled)
        self._sync_enabled()

        btn_cancel = _button("取消", self.reject, ghost=True)
        btn_skip = _button("仅这次不保存", self._skip)
        btn_apply = _button("确定并关闭", self._apply, primary=True)
        btn_apply.setDefault(True)
        bar = QHBoxLayout()
        bar.addWidget(btn_cancel)
        bar.addStretch(1)
        bar.addWidget(btn_skip)
        bar.addWidget(btn_apply)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)
        lay.addWidget(head)
        lay.addWidget(body)
        lay.addWidget(box)
        lay.addLayout(bar)

    def _sync_enabled(self, *_args):
        never = self.radio_never.isChecked()
        self.spin_videos.setEnabled(self.radio_count.isChecked())
        usage = self.radio_usage.isChecked()
        self.spin_minutes.setEnabled(usage)
        self.spin_exports.setEnabled(usage)
        self._syncing_skip = True
        try:
            self.check_skip.setEnabled(not never)
            self.check_skip.setChecked(False if never else self._skip_pref)
        finally:
            self._syncing_skip = False
        self.lbl_skip_note.setVisible(never)

    def _on_skip_toggled(self, checked: bool):
        if not getattr(self, "_syncing_skip", False):
            self._skip_pref = bool(checked)

    def _apply(self):
        self.action = "apply"
        self.accept()

    def _skip(self):
        self.action = "skip"
        self.accept()

    def values(self) -> dict:
        mode = (CACHE_MODE_NEVER if self.radio_never.isChecked() else
                CACHE_MODE_USAGE if self.radio_usage.isChecked() else
                CACHE_MODE_COUNT)
        return {
            "mode": mode,
            "max_videos": int(self.spin_videos.value()),
            "usage_minutes": int(self.spin_minutes.value()),
            "usage_exports": int(self.spin_exports.value()),
            "skip_close_prompt": (False if mode == CACHE_MODE_NEVER
                                  else self.check_skip.isChecked()),
        }
