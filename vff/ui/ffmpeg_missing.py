# -*- coding: utf-8 -*-
"""没有 ffmpeg 时的引导窗：下载地址 + 指定目录 + 自动搜索 + 重新检测。

目标：用户在这一屏里就能完成「下载 → 解压 → 让 Purer 找到它」，
不用去别处查文档，也不用重启程序（能重启更好，但不是必须）。
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QPushButton, QVBoxLayout)

from ..ffmpeg import (FFMPEG_ENV, _exe_name, Tools, check_tool_dir, refresh_path,
                      scan_for_ffmpeg, write_ffmpeg_ini)

OFFICIAL = "https://ffmpeg.org/download.html"
GYAN_PAGE = "https://www.gyan.dev/ffmpeg/builds/"
GYAN_ZIP = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
BTBN = "https://github.com/BtbN/FFmpeg-Builds/releases"
BTBN_ZIP = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            "ffmpeg-master-latest-win64-gpl.zip")
WINGET = "winget install --id Gyan.FFmpeg -e"

# 国内镜像：npmmirror（cnpm）同步的 KarinJS/FFmpeg-Builds 静态包。
# 2026-11-11 实测：直连（不走代理）下载 140 MB 的包约 10 秒（14 MB/s），
# 包内 bin\ 里 ffmpeg.exe + ffprobe.exe + ffplay.exe 齐全，sha256 与发布值一致。
# 官方源（gyan.dev / GitHub）在国内实测只有 30–50 KB/s，基本等于不可用。
MIRROR_INDEX = "https://registry.npmmirror.com/-/binary/ffmpeg-builds/"
MIRROR_VER = "8.1.3"
MIRROR_ZIP = (f"https://registry.npmmirror.com/-/binary/ffmpeg-builds/v{MIRROR_VER}/"
              f"ffmpeg-{MIRROR_VER}-win32-x64-gpl.tar.xz")
MIRROR_SUM = (f"https://registry.npmmirror.com/-/binary/ffmpeg-builds/v{MIRROR_VER}/"
              f"ffmpeg-{MIRROR_VER}.sha256.txt")
MIRROR_MB = 140
# GitHub 加速站（大文件走这里比直连快，但不如上面的镜像站）
GH_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/")

_LINK = 'style="color:#7E9EFF;text-decoration:none;"'


def _link(url: str, text: str) -> str:
    return f'<a href="{url}" {_LINK}>{text}</a>'


class FFmpegMissingDialog(QDialog):
    """返回 resolved=True 表示已经找到 ffmpeg/ffprobe，可以继续启动。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("没有找到 ffmpeg")
        self.setMinimumWidth(660)
        self.setModal(True)
        self.resolved = False

        head = QLabel("没有找到 ffmpeg / ffprobe")
        head.setStyleSheet("font-size:12pt;font-weight:600;")

        intro = QLabel(
            "Purer 用 ffmpeg 解码视频、抓帧和切片。Windows 不自带它，"
            "需要先装一次（下载的是压缩包，解压即用，不用装安装程序）。")
        intro.setWordWrap(True)
        intro.setObjectName("exportLabel")

        # ---------------- ① 下载 ----------------
        step1 = QLabel("① 下载（推荐第一个：国内镜像，不用梯子）")
        step1.setStyleSheet("font-weight:600;")

        # 国内镜像（实测可直连，几 MB/s ~ 十几 MB/s）
        mirror = QLabel(
            f'<b>国内镜像</b>（npmmirror 同步，约 {MIRROR_MB} MB，直连几十秒下完）：'
            f'{_link(MIRROR_ZIP, f"ffmpeg-{MIRROR_VER}-win32-x64-gpl.tar.xz")}'
            f'　·　{_link(MIRROR_INDEX, "看更新版本")}'
            f'　·　{_link(MIRROR_SUM, "sha256 校验值")}')
        mirror.setOpenExternalLinks(True)
        mirror.setTextInteractionFlags(Qt.TextBrowserInteraction)
        mirror.setWordWrap(True)

        mirror_note = QLabel(
            "解压：Win10/11 自带 tar —— 在下载目录 Shift+右键 → 打开 PowerShell → "
            "<code>tar -xJf 文件名</code>；或者装个 7-Zip 右键解压。"
            "解开后里面是 <code>bin\\ffmpeg.exe</code> 和 <code>bin\\ffprobe.exe</code>（两个都要有）。")
        mirror_note.setWordWrap(True)
        mirror_note.setObjectName("hint")

        btn_copy_url = QPushButton("复制镜像链接")
        btn_copy_url.setObjectName("ghost")
        btn_copy_url.setFixedWidth(104)
        btn_copy_url.clicked.connect(self._copy_mirror)
        mirror_row = QHBoxLayout()
        mirror_row.setContentsMargins(0, 0, 0, 0)
        mirror_row.addWidget(mirror, 1)
        mirror_row.addWidget(btn_copy_url, 0, Qt.AlignTop)

        # 官方 / 国外（国内常很慢或要梯子）
        links = QLabel(
            f'官方 / 国外源（国内实测只有几十 KB/s，可能要梯子）：'
            f'{_link(OFFICIAL, "ffmpeg 官网")}'
            f'　·　{_link(GYAN_PAGE, "gyan.dev（Windows 推荐）")}'
            f'　·　{_link(BTBN, "BtbN 构建（GitHub）")}'
            f'<br>GitHub 慢的话走加速站：'
            f'{_link(GH_MIRRORS[0] + BTBN_ZIP, "ghfast.top")}'
            f'　·　{_link(GH_MIRRORS[1] + BTBN_ZIP, "gh-proxy.com")}'
            f'　·　或直接下 {_link(GYAN_ZIP, "gyan.dev 的 zip")}')
        links.setOpenExternalLinks(True)
        links.setTextInteractionFlags(Qt.TextBrowserInteraction)
        links.setWordWrap(True)

        cmd = QLabel(
            f'也可以 winget 装（走的还是国外 CDN，可能很慢）：'
            f'<span style="font-family:Consolas,monospace;color:#E8EAF0;">{WINGET}</span>')
        cmd.setTextInteractionFlags(Qt.TextSelectableByMouse)
        btn_copy = QPushButton("复制命令")
        btn_copy.setObjectName("ghost")
        btn_copy.setFixedWidth(84)
        btn_copy.clicked.connect(self._copy_winget)
        cmd_row = QHBoxLayout()
        cmd_row.setContentsMargins(0, 0, 0, 0)
        cmd_row.addWidget(cmd, 1)
        cmd_row.addWidget(btn_copy)

        # ---------------- ② 让程序找到它 ----------------
        step2 = QLabel("② 让 Purer 找到它（解压完点下面任一个）")
        step2.setStyleSheet("font-weight:600;")

        btn_pick = QPushButton("选择 ffmpeg 所在文件夹…")
        btn_pick.setObjectName("primary")
        btn_pick.setMinimumHeight(32)
        btn_pick.clicked.connect(self._pick_dir)
        btn_scan = QPushButton("自动搜索本机")
        btn_scan.setMinimumHeight(32)
        btn_scan.clicked.connect(self._auto_scan)
        pick_row = QHBoxLayout()
        pick_row.setContentsMargins(0, 0, 0, 0)
        pick_row.addWidget(btn_pick)
        pick_row.addWidget(btn_scan)
        pick_row.addStretch(1)

        tips = QLabel(
            "· 解压到固定目录（比如 <code>D:\\ffmpeg</code>）再选它，别放下载文件夹。<br>"
            "· 选好后会把路径写进程序目录的 <code>ffmpeg.ini</code>，以后再打开直接可用。<br>"
            "· 也可以自己把 <code>…\\bin</code> 加进系统 PATH，然后点「重新检测」。")
        tips.setWordWrap(True)
        tips.setObjectName("hint")

        # ---------------- 状态 + 按钮 ----------------
        self.status = QLabel("状态：未找到 ffmpeg / ffprobe")
        self.status.setWordWrap(True)
        self.status.setObjectName("warn")

        btn_recheck = QPushButton("重新检测")
        btn_recheck.clicked.connect(self._recheck)
        btn_quit = QPushButton("退出")
        btn_quit.clicked.connect(self.reject)
        bar = QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(btn_recheck)
        bar.addWidget(btn_quit)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)
        lay.addWidget(head)
        lay.addWidget(intro)
        lay.addSpacing(4)
        lay.addWidget(step1)
        lay.addLayout(mirror_row)
        lay.addWidget(mirror_note)
        lay.addWidget(links)
        lay.addLayout(cmd_row)
        lay.addSpacing(6)
        lay.addWidget(step2)
        lay.addLayout(pick_row)
        lay.addWidget(tips)
        lay.addSpacing(4)
        lay.addLayout(bar)

        self._btn_recheck = btn_recheck
        btn_recheck.setDefault(True)
        self._placed = False

    def showEvent(self, event):
        """居中到主屏（父窗口还没建，否则会跑到鼠标所在的那块屏上）。"""
        super().showEvent(event)
        if self._placed:
            return
        self._placed = True
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.center() - self.rect().center())

    # ------------------------------------------------------------------ 动作
    def _copy_winget(self) -> None:
        QApplication.clipboard().setText(WINGET)
        self._ok("已复制命令，粘贴到 PowerShell 或 CMD 里执行")

    def _copy_mirror(self) -> None:
        QApplication.clipboard().setText(MIRROR_ZIP)
        self._ok("已复制国内镜像链接，可以粘到浏览器/下载工具里直接下")

    def _pick_dir(self) -> None:
        start = str(Path.home() / "Downloads")
        chosen = QFileDialog.getExistingDirectory(
            self, "选择 ffmpeg 所在文件夹（含 ffmpeg.exe 的那一层）", start)
        if not chosen:
            return
        folder = check_tool_dir(Path(chosen))
        if folder is None:
            self._bad(f"这个目录里没有 ffmpeg.exe：{chosen}\n"
                      "请选到解压出来的 bin 文件夹，或解压后的根目录。")
            return
        if not (folder / _exe_name("ffprobe")).is_file():
            self._bad(f"找到了 ffmpeg，但同目录没有 ffprobe：{folder}\n"
                      "请确认下载的是完整包（full / essentials build）。")
            return
        self._use_dir(folder)

    def _auto_scan(self) -> None:
        self._ok("正在搜索本机（最多 3 秒）…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            folder = scan_for_ffmpeg()
        finally:
            QApplication.restoreOverrideCursor()
        if folder is None:
            self._bad("没搜到。常见原因是还没解压，或者装在别的磁盘上。\n"
                      "请点「选择 ffmpeg 所在文件夹…」手动指定。")
            return
        self._use_dir(folder)

    def _recheck(self) -> None:
        refresh_path()
        if Tools.locate(deep=True):
            self._finish("")
            return
        self._bad("还是没找到。\n"
                  "· 如果刚改过 PATH：请关掉本窗口重开程序（新 PATH 只对新进程生效）；\n"
                  "· 或者点「选择 ffmpeg 所在文件夹…」，这样不用改 PATH。")

    def _use_dir(self, folder: Path) -> None:
        note = ""
        try:
            ini = write_ffmpeg_ini(folder)
            note = f"（已记住：{ini}）"
        except OSError as exc:
            # 程序目录不可写（比如装在 Program Files）：至少让本次会话能用
            os.environ[FFMPEG_ENV] = str(folder)
            note = f"（无法写入 ffmpeg.ini：{exc}；本次直接生效）"
        refresh_path()
        if Tools.locate(deep=False):
            self._finish(f"已找到 {folder} {note}")
        else:
            self._bad(f"目录已记录，但没读到 ffmpeg/ffprobe：{folder}")

    def _finish(self, message: str) -> None:
        self.resolved = True
        if message:
            self.status.setStyleSheet("color:#45C995;")
            self.status.setText(f"✅ {message}")
            QApplication.processEvents()
            QApplication.processEvents()
        self.accept()

    # ------------------------------------------------------------------ 状态
    def _ok(self, text: str) -> None:
        self.status.setStyleSheet("color:#E5B65C;")
        self.status.setText(text)

    def _bad(self, text: str) -> None:
        self.status.setStyleSheet("color:#E56B72;")
        self.status.setText("⚠ " + text)


def ensure_ffmpeg(parent=None) -> bool:
    """缺少 ffmpeg 时弹引导窗；返回 True 表示可以继续启动。"""
    if Tools.locate(deep=True):
        return True
    dialog = FFmpegMissingDialog(parent)
    dialog.exec()
    return bool(dialog.resolved and Tools.locate(deep=False))
