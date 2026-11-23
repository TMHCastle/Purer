# -*- coding: utf-8 -*-
"""应用设置（持久化 JSON；纯逻辑，不依赖 Qt，方便单测）。

和 :mod:`vff.cache` 的 ``ui.ini`` 不同，这个文件**必须跨重启可靠**：
它决定「关闭时要不要保存图墙缓存」「最多留多少」这些东西。
默认位置 ``%LOCALAPPDATA%\\Purer\\settings.json``（见 ``config.settings_file``）。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from .config import (CACHE_LIMIT_FRACTION, CACHE_LIMIT_MAX, CACHE_LIMIT_MIN,
                     persistent_cache_root, settings_file)

SETTINGS_VERSION = 1

# 自动保存图墙缓存的三种策略（对应关闭时提示里的三个选项）
CACHE_MODE_NEVER = "never"      # 所有缓存都不自动保存
CACHE_MODE_COUNT = "count"      # 自动保存，最多 N 个视频
CACHE_MODE_USAGE = "usage"      # 使用超过 X 分钟、或导出超过 Y 次才保存
CACHE_MODES = (CACHE_MODE_NEVER, CACHE_MODE_COUNT, CACHE_MODE_USAGE)
CACHE_MODE_LABELS = {
    CACHE_MODE_NEVER: "所有缓存都不自动保存",
    CACHE_MODE_COUNT: "自动保存最近使用的，最多 N 个视频",
    CACHE_MODE_USAGE: "自动保存用得多 / 导出过的素材",
}

DEFAULT_CACHE = {
    "mode": CACHE_MODE_NEVER,   # 默认：不自动保存，关闭时先问一次
    "dir": None,                # None = 默认目录（config.persistent_cache_root）
    "max_videos": 20,
    "usage_minutes": 10,
    "usage_exports": 2,
    "max_bytes": None,          # None = 自动（磁盘总量的 CACHE_LIMIT_FRACTION）
    "skip_close_prompt": False,
}

# ---------------------------------------------------------------- 更新提醒
# 「发现新版本」弹窗记住了什么：自动检查 / 静音 / 忽略的版本 / 推迟到什么时候。
UPDATE_PROMPT_COOLDOWN = 24 * 3600      # 同一版本 24 小时内只问一次
UPDATE_SNOOZE_DAYS = 7                  # 「一周后提醒我」
DEFAULT_UPDATE = {
    "auto_check": True,          # 启动后自动检查（PURER_NO_UPDATE=1 仍最高优先）
    "muted": False,              # 忽略所有更新通知（手动检查不受影响）
    "skip_version": "",          # 忽略这个版本（更高版本仍会提示）
    "snooze_until": 0.0,         # 推迟到什么时候（时间戳）
    "snooze_version": "",        # 推迟的是哪个版本
    "last_prompt_at": {},        # {tag: 上次弹窗时间}
}


def _clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def usage_meets(cache_cfg: dict | None, usage: dict | None) -> bool:
    """「用得多 / 导出过」策略：使用时长或导出次数任一达标就算合格。"""
    cfg = dict(DEFAULT_CACHE)
    cfg.update(cache_cfg or {})
    usage = usage or {}
    minutes = int(cfg.get("usage_minutes") or DEFAULT_CACHE["usage_minutes"])
    exports = int(cfg.get("usage_exports") or DEFAULT_CACHE["usage_exports"])
    used = float(usage.get("used_seconds") or 0.0)
    count = int(usage.get("exports") or 0)
    return used >= minutes * 60 or count >= exports


def close_prompt_needed(cache_cfg: dict | None) -> bool:
    """关闭前要不要弹「保存缓存？」。

    规则：只有**选了自动保存策略**并且勾了「不再提醒」才静默执行；
    默认（所有缓存都不自动保存）每次都会问一次 —— 不保存也要让用户知道。
    """
    cfg = dict(DEFAULT_CACHE)
    cfg.update(cache_cfg or {})
    if cfg.get("mode") == CACHE_MODE_NEVER:
        return True
    return not bool(cfg.get("skip_close_prompt"))


def normalized_cache(raw: dict | None) -> dict:
    """把读到的（可能是旧版 / 手改过的）配置收敛成合法值。"""
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or DEFAULT_CACHE["mode"]).strip().lower()
    if mode not in CACHE_MODES:
        mode = DEFAULT_CACHE["mode"]
    max_bytes = raw.get("max_bytes")
    if max_bytes is not None:
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            max_bytes = None
        else:
            if max_bytes <= 0:
                max_bytes = None
            else:
                max_bytes = max(
                    CACHE_LIMIT_MIN // 2,
                    min(CACHE_LIMIT_MAX * 4, max_bytes))
    cache_dir = raw.get("dir")
    if cache_dir:
        try:
            cache_dir = str(Path(str(cache_dir)).expanduser())
        except (OSError, ValueError):
            cache_dir = None
    else:
        cache_dir = None
    return {
        "mode": mode,
        "dir": cache_dir,
        "max_videos": _clamp_int(raw.get("max_videos"), 1, 9999,
                                 DEFAULT_CACHE["max_videos"]),
        "usage_minutes": _clamp_int(raw.get("usage_minutes"), 1, 100000,
                                    DEFAULT_CACHE["usage_minutes"]),
        "usage_exports": _clamp_int(raw.get("usage_exports"), 1, 9999,
                                    DEFAULT_CACHE["usage_exports"]),
        "max_bytes": max_bytes,
        "skip_close_prompt": bool(raw.get("skip_close_prompt", False)),
    }


def normalized_update(raw: dict | None) -> dict:
    """更新提醒设置的收敛（只认已知字段，坏值退回默认）。"""
    raw = raw if isinstance(raw, dict) else {}
    prompts = {}
    for tag, stamp in (raw.get("last_prompt_at") or {}).items():
        text = str(tag or "").strip()[:32]
        try:
            value = float(stamp)
        except (TypeError, ValueError):
            continue
        if text:
            prompts[text] = value
    if len(prompts) > 12:                      # 只留最近 12 个版本
        prompts = dict(sorted(prompts.items(), key=lambda kv: kv[1])[-12:])
    try:
        snooze = float(raw.get("snooze_until") or 0.0)
    except (TypeError, ValueError):
        snooze = 0.0
    if snooze < 0 or snooze > 10 ** 11:
        snooze = 0.0
    return {
        "auto_check": bool(raw.get("auto_check", True)),
        "muted": bool(raw.get("muted", False)),
        "skip_version": str(raw.get("skip_version") or "").strip()[:32],
        "snooze_until": snooze,
        "snooze_version": str(raw.get("snooze_version") or "").strip()[:32],
        "last_prompt_at": prompts,
    }


def update_should_prompt(prefs: dict | None, tag: str, now: float | None = None,
                         manual: bool = False) -> tuple[bool, str]:
    """要不要弹「发现新版本」？→ ``(弹不弹, 原因)``（原因用于调试和状态栏）。

    优先级：没有版本号 → 手动检查直接弹 → 静音 → 忽略的版本 → 推迟期 →
    同一版本 24 小时内问过。
    """
    from .version import is_newer, normalize

    cfg = normalized_update(prefs)
    text = str(tag or "").strip()
    if not normalize(text):
        return False, "没有可用的版本号"
    if manual:
        return True, "手动检查"
    if cfg["muted"]:
        return False, "已关闭更新提醒"
    skip = cfg["skip_version"]
    if skip and not is_newer(text, skip):
        return False, f"已忽略 {skip}"
    if cfg["snooze_until"] > (now if now is not None else _now()):
        snoozed = cfg["snooze_version"]
        if not snoozed or not is_newer(text, snoozed):
            return False, "推迟中"
    stamp = cfg["last_prompt_at"].get(normalize(text))
    if stamp and (now if now is not None else _now()) - stamp < UPDATE_PROMPT_COOLDOWN:
        return False, "今天已经问过"
    return True, ""


def update_apply_choice(prefs: dict | None, tag: str, choice: str,
                        now: float | None = None,
                        days: int = UPDATE_SNOOZE_DAYS) -> dict:
    """把用户点的那一下记进设置（返回新的 prefs，交给调用方保存）。

    ``now`` = 立即更新；``later`` = 这次先不装；``week`` = 一周后提醒；
    ``skip`` = 忽略这个版本；``mute`` = 忽略所有更新通知。
    """
    from .version import normalize

    cfg = normalized_update(prefs)
    stamp = _now() if now is None else float(now)
    key = normalize(tag) or str(tag or "").strip()

    if choice == "week":
        cfg["snooze_until"] = stamp + max(1, int(days)) * 86400
        cfg["snooze_version"] = key
    elif choice == "skip":
        cfg["skip_version"] = key
    elif choice == "mute":
        cfg["muted"] = True
    if key:
        prompts = dict(cfg["last_prompt_at"])
        prompts[key] = stamp
        cfg["last_prompt_at"] = prompts
    return normalized_update(cfg)


def update_clear_skip(prefs: dict | None, now: float | None = None) -> dict:
    """「恢复更新提醒」：清掉忽略的版本 / 静音 / 推迟。"""
    cfg = normalized_update(prefs)
    cfg.update({"muted": False, "skip_version": "", "snooze_until": 0.0,
                "snooze_version": ""})
    if now is not None:
        cfg["last_prompt_at"] = {}
    return normalized_update(cfg)


def _now() -> float:
    import time
    return time.time()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _existing_parent(path: Path) -> Path:
    """找最近的已存在目录（用于「磁盘总量」这类查询）。"""
    current = Path(path)
    for _ in range(8):
        try:
            if current.exists():
                return current
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return Path.home()


def disk_total_bytes(path: Path | None = None) -> int:
    """缓存所在磁盘的总容量（拿不到就算 0）。"""
    target = _existing_parent(path or settings_file().parent)
    try:
        return int(shutil.disk_usage(str(target)).total)
    except (OSError, ValueError):
        return 0


def default_cache_limit_bytes(path: Path | None = None) -> int:
    """没设过上限时的默认值 = 磁盘总量的一定比例（并夹在合理区间）。"""
    total = disk_total_bytes(path)
    if total <= 0:
        return 1024 * 1024 * 1024        # 1 GB
    value = int(total * CACHE_LIMIT_FRACTION)
    return max(CACHE_LIMIT_MIN, min(CACHE_LIMIT_MAX, value))


class Settings:
    """一个很薄的 JSON 配置对象。

    读失败一律退回默认值（不抛异常 —— 设置坏了不能影响打开视频）；
    写失败返回 False（由界面提示，不静默）。
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else settings_file()
        self._data = _read_json(self.path)
        if not isinstance(self._data.get("cache"), dict):
            self._data["cache"] = {}
        self._data["cache"] = normalized_cache(self._data["cache"])
        self._data["update"] = normalized_update(self._data.get("update"))

    # ------------------------------------------------------------ 读写
    def cache(self) -> dict:
        """缓存设置的副本（改它不会影响已保存的值，要配合 update_cache）。"""
        return dict(self._data.get("cache") or DEFAULT_CACHE)

    def update_cache(self, **values) -> dict:
        merged = dict(self._data.get("cache") or {})
        merged.update(values)
        self._data["cache"] = normalized_cache(merged)
        return dict(self._data["cache"])

    # ------------------------------------------------------------ 更新提醒
    def update_prefs(self) -> dict:
        return dict(self._data.get("update") or DEFAULT_UPDATE)

    def set_update_prefs(self, **values) -> dict:
        merged = dict(self._data.get("update") or {})
        merged.update(values)
        self._data["update"] = normalized_update(merged)
        return dict(self._data["update"])

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    def save(self) -> bool:
        """原子写（临时文件 + replace）。返回是否成功。"""
        payload = dict(self._data)
        payload["version"] = SETTINGS_VERSION
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".json",
                                       dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        return True

    # ------------------------------------------------------------ 便捷判定
    def effective_cache_dir(self) -> Path:
        """已保存缓存放哪（设置里改过就用改过的，否则用默认目录）。"""
        configured = self.cache().get("dir")
        if configured:
            try:
                return Path(str(configured)).expanduser()
            except (OSError, ValueError):
                pass
        return persistent_cache_root()

    def effective_cache_limit(self) -> int:
        """当前生效的缓存上限（没设过就按缓存所在磁盘的比例算）。"""
        configured = self.cache().get("max_bytes")
        if configured:
            return int(configured)
        return default_cache_limit_bytes(self.effective_cache_dir())
