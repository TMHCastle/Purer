# -*- coding: utf-8 -*-
"""常量与路径约定（唯一的"硬编码"出口）。"""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path

APP_NAME = "Purer"
APP_TAGLINE = "视频抽帧浏览 / 帧精确切片"
APP_ID = "Purer"
APP_VERSION = "1.2.9"
# 热补丁包的兼容基线：以后打的代码包只支持 **>= 这个版本** 的 exe。
# 动了引导层 / 打包数据 / 依赖（这些热补丁改不了的东西）才需要往上抬。
CODE_BASE_VERSION = "1.2.0"
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
STEP_PRESETS = [1, 2, 3, 4, 6, 8, 12, 24, 25, 30, 50, 60, 120]
STEP_PRESETS_FRACTIONAL = [23.98, 29.97, 59.94]
STEP_MIN = 1.0
STEP_MAX = 3000.0


def snap_step(fps: float | None) -> float:
    """「一秒一帧」的间隔 = 源视频帧率（能对上常见帧率就用常见值）。

    23.976 → 23.98（预设里的写法）、24/25/30/50/60/120 原样、
    其他帧率取整（例如 14.99 → 15）；拿不到帧率就退回 DEFAULT_STEP。
    """
    try:
        value = float(fps or 0.0)
    except (TypeError, ValueError):
        return float(DEFAULT_STEP)
    if value <= 0:
        return float(DEFAULT_STEP)
    for preset in STEP_PRESETS_FRACTIONAL:
        if abs(value - float(preset)) < 0.01:
            return float(preset)
    return float(max(STEP_MIN, min(STEP_MAX, round(value))))


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

# 持久缓存（「保存下来的图墙」）默认上限 = 所在磁盘总量的这个比例。
# 用户可以在 设置-缓存 里改；这里只是「没设过」时的默认值。
CACHE_LIMIT_FRACTION = 0.005
CACHE_LIMIT_MIN = 256 * 1024 * 1024        # 256 MB
CACHE_LIMIT_MAX = 8 * 1024 * 1024 * 1024   # 8 GB


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


_TEMP_CACHE_ROOT: Path | None = None


def cache_root() -> Path:
    """缩略图缓存根目录。``PURER_CACHE_DIR`` 可覆盖（测试/便携用）。

    默认放在 **Purer 同目录的 ``cache_tmp\\``**（和持久缓存同盘，硬链接才生效；
    也避免把几百 MB 塞进 C 盘 ``%TEMP%``）。程序目录不可写时才退回数据目录。
    """
    override = os.environ.get("PURER_CACHE_DIR")
    if override:
        return Path(override)
    global _TEMP_CACHE_ROOT
    if _TEMP_CACHE_ROOT is not None:
        return _TEMP_CACHE_ROOT
    candidate = app_dir() / "cache_tmp"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".purer_write_test"
        probe.write_bytes(b"")
        probe.unlink()
        _TEMP_CACHE_ROOT = candidate
        return candidate
    except OSError:
        fallback = data_dir() / "cache_tmp"
        _TEMP_CACHE_ROOT = fallback
        return fallback


def data_dir() -> Path:
    """持久数据目录（设置文件 + 缓存不可写时的兜底）。

    ``%LOCALAPPDATA%\\Purer``；``PURER_DATA_DIR`` 可覆盖（测试 / 便携用）。
    故意不放在临时目录，也不默认放在 exe 旁边 —— 设置文件要保证可写。
    """
    override = os.environ.get("PURER_DATA_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / APP_ID
    return Path.home() / f".{APP_ID.lower()}"


_PERSIST_ROOT: Path | None = None


def settings_file() -> Path:
    """应用设置（自动保存策略 / 缓存上限 / 关闭提醒开关…）的唯一文件。"""
    override = os.environ.get("PURER_SETTINGS_FILE")
    if override:
        return Path(override)
    return data_dir() / "settings.json"


def persistent_cache_root() -> Path:
    """默认缓存位置：**Purer 同目录**下的 ``cache``（exe / 源码根旁边）。

    ``PURER_PERSIST_CACHE_DIR`` 可覆盖；程序目录不可写（例如装在
    ``Program Files``）时自动退回用户数据目录，不让「保存缓存」整块失效。
    """
    global _PERSIST_ROOT
    override = os.environ.get("PURER_PERSIST_CACHE_DIR")
    if override:
        return Path(override)
    if _PERSIST_ROOT is not None:
        return _PERSIST_ROOT
    candidate = app_dir() / "cache"
    chosen = candidate
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".purer_write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        chosen = data_dir() / "cache"
    _PERSIST_ROOT = chosen
    return chosen


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


# --------------------------------------------------------------------------- #
# 版本（exe 里编死的 + 热补丁包可能更高）
# --------------------------------------------------------------------------- #
def effective_version() -> str:
    """对外显示的版本：有生效的热补丁包取包版本，否则就是 exe 版本。"""
    try:
        import purer_boot
    except Exception:          # noqa: BLE001  源码 / 单测环境没有引导层
        return APP_VERSION
    try:
        return purer_boot.effective_version(APP_VERSION) or APP_VERSION
    except Exception:          # noqa: BLE001
        return APP_VERSION
