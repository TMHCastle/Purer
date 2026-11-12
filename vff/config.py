# -*- coding: utf-8 -*-
"""常量与路径约定（唯一的"硬编码"出口）。"""

from __future__ import annotations

import configparser
import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "Purer"
APP_TAGLINE = "视频抽帧浏览 / 帧精确切片"
APP_ID = "Purer"
APP_VERSION = "1.1.7"
# 「检查更新」查的是这里（公开仓库，只读 API，不需要 token）
GITHUB_REPO = "TMHCastle/Purer"

DEFAULT_STEP = 24

# 按镜头抽帧：场景检测阈值（越小越灵敏，越容易把推拉/闪光当成切镜头）
SCENE_PRESETS = [("灵敏", 0.20), ("标准", 0.30), ("保守", 0.45)]
SCENE_DEFAULT = 0.30        # 界面默认档位（实际参数见 scenedetect.PRESETS）


def extract_mode_items() -> list[tuple[str, str]]:
    """下拉框条目：``(显示文字, 配置值)``。配置值存成字符串，写进 ini 也方便。"""
    items = [("每 N 帧", "step")]
    for label, value in SCENE_PRESETS:
        items.append((f"按镜头 · {label}", f"scene:{value:.2f}"))
    return items


def parse_extract_mode(text: str) -> tuple[str, float]:
    """``'step'`` / ``'scene:0.30'`` → ``("step"|"scene", 档位)``。"""
    text = str(text or "").strip()
    if text.startswith("scene"):
        try:
            threshold = float(text.split(":", 1)[1])
        except (IndexError, ValueError):
            threshold = SCENE_DEFAULT
        return "scene", max(0.01, min(0.99, threshold))
    return "step", 0.0


def preset_label(value: float) -> str:
    """档位数值 → 中文名（灵敏 / 标准 / 保守）。"""
    for label, preset in SCENE_PRESETS:
        if abs(float(preset) - float(value)) < 1e-9:
            return label
    return f"{float(value):.2f}"


# 抽帧间隔预设：常用整数 + 常见帧率下「约 1 秒」的帧数（含不便输入的小数）
STEP_PRESETS = [1, 2, 4, 6, 8, 12, 24, 30, 48, 96]
STEP_PRESETS_FRACTIONAL = [23.98, 29.97, 59.94]
STEP_MIN = 1.0
STEP_MAX = 3000.0

# 抽取尺寸 = 磁盘上缩略图的长边；一旦定下就不随显示尺寸变化（换皮肤不重抽）
EXTRACT_LONG_SIDE = 480

# 显示尺寸 = 网格里图片的显示长边，可随时改（Ctrl+滚轮 / 下拉 / 滑块），不触发重抽
DISPLAY_PRESETS = [
    ("紧凑", 120),
    ("标准", 160),
    ("大图", 200),
    ("超大", 260),
]
DEFAULT_DISPLAY_SIDE = 160
DISPLAY_MIN = 100
DISPLAY_MAX = 320
DISPLAY_STEP = 20

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".ogv", ".rmvb", ".rm", ".asf",
    ".vob", ".mxf", ".divx", ".f4v", ".m2v", ".dat",
}

CACHE_TTL_DAYS = 7
MAX_THUMBS_WARN = 4000
PREVIEW_DEBOUNCE_MS = 140
JOBS_MAX_CONCURRENT = 1

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def app_dir() -> Path:
    """exe 所在目录（打包后）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_dir() -> Path | None:
    """PyInstaller 解包目录（_MEIPASS）；非打包返回 None。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def cache_root() -> Path:
    """缩略图缓存根目录。PURER_CACHE_DIR 可覆盖（测试/便携用）。"""
    override = os.environ.get("PURER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"{APP_ID}_cache"


def ffmpeg_ini() -> Path:
    return app_dir() / "ffmpeg.ini"


def read_ffmpeg_dir_from_ini() -> Path | None:
    """读取 exe 同目录的 ffmpeg.ini：

    [ffmpeg]
    dir = D:\\path\\to\\bin

    也兼容整个文件只有一行路径的写法。
    """
    ini = ffmpeg_ini()
    if not ini.is_file():
        return None
    try:
        text = ini.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    try:
        parser = configparser.ConfigParser()
        parser.read_string(text)
        for section in ("ffmpeg", "FFmpeg", "DEFAULT"):
            if parser.has_option(section, "dir"):
                value = parser.get(section, "dir").strip()
                if value:
                    return Path(value)
    except (configparser.Error, ValueError):
        pass
    # 单行路径兜底
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()
                  and not ln.strip().startswith(("#", ";", "["))), "")
    return Path(first) if first else None
