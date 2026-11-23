# -*- coding: utf-8 -*-
"""把旧版本留在 C 盘的缓存搬到 Purer 同盘（一次性，失败可重试）。

旧版本：
  * 临时渲染缓存：``%TEMP%\\Purer_cache``
  * 持久缓存兜底：``%LOCALAPPDATA%\\Purer\\cache``

新版本：
  * 临时缓存：``Purer.exe 同目录\\cache_tmp``
  * 持久缓存：``Purer.exe 同目录\\cache``

搬迁原则：**先复制并校验，成功后才删旧目录**；同一块盘用 rename，跨盘才复制。
只搬 16 位十六进制命名的缓存目录（``f_*.jpg`` 那些），不碰视频/设置。
成功写 ``%LOCALAPPDATA%\\Purer\\_cache_layout_v2.done``，之后不再重复。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .config import APP_ID, cache_root, data_dir, persistent_cache_root

MARKER_NAME = "_cache_layout_v2.done"
LOCK_NAME = "_cache_layout_v2.lock"
LOG_NAME = "_cache_migrate.log"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def legacy_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / f"{APP_ID}_cache"


def legacy_persistent_root() -> Path:
    return data_dir() / "cache"


def _is_digest(name: str) -> bool:
    text = str(name or "")
    return len(text) == 16 and all(ch in "0123456789abcdef" for ch in text)


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return a.drive.lower() == b.drive.lower()
    except (AttributeError, ValueError):
        return False


def _tree_stats(path: Path) -> tuple[int, int]:
    files = total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    files += 1
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return files, total


# --------------------------------------------------------------------------- #
# 单个目录搬迁（先复制校验，成功才删源）
# --------------------------------------------------------------------------- #
def _move_dir_verified(src: Path, dst: Path, errors: list[str]) -> bool:
    if dst.exists():
        errors.append(f"目标已存在：{dst}")
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        errors.append(f"无法创建 {dst.parent}：{exc}")
        return False

    if _same_volume(src, dst):
        try:
            os.rename(src, dst)
            return True
        except OSError:
            pass        # 同盘也可能被占用 / 权限问题，退回复制

    tmp = dst.with_name(dst.name + ".migrating")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        shutil.copytree(src, tmp, copy_function=shutil.copy2)
        if _tree_stats(src) != _tree_stats(tmp):
            raise OSError("复制后文件数/字节数不一致")
        os.replace(tmp, dst)
        shutil.rmtree(src, ignore_errors=True)
        return True
    except OSError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        errors.append(f"复制 {src.name} 失败：{exc}")
        return False


def _migrate_digest_dirs(src: Path, dst: Path) -> dict:
    """把 src 下的 16 位缓存目录搬到 dst；返回统计。"""
    result = {"moved": 0, "removed_old": 0, "skipped": 0, "errors": []}
    try:
        if not src.is_dir():
            return result
    except OSError:
        return result
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result["errors"].append(f"无法创建 {dst}：{exc}")
        return result

    try:
        children = list(src.iterdir())
    except OSError as exc:
        result["errors"].append(f"读不了 {src}：{exc}")
        return result

    for child in children:
        try:
            if not child.is_dir() or not _is_digest(child.name):
                continue
        except OSError:
            continue
        target = dst / child.name
        if target.exists():
            try:
                done = (target / ".done").is_file()
                source_done = (child / ".done").is_file()
            except OSError:
                done = source_done = False
            if done:
                # 目标已有完整缓存（同一 digest = 同一视频同设置）：
                # 保留目标，删掉旧副本，C 盘才算真正腾出来。
                try:
                    shutil.rmtree(child)
                    result["removed_old"] += 1
                except OSError as exc:
                    result["errors"].append(f"旧副本删不掉 {child}：{exc}")
            elif source_done:
                # 目标不完整、源完整：用源替换
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    result["errors"].append(f"删不完整目标 {target}：{exc}")
                    continue
                if _move_dir_verified(child, target, result["errors"]):
                    result["moved"] += 1
            else:
                result["skipped"] += 1
            continue
        if _move_dir_verified(child, target, result["errors"]):
            result["moved"] += 1
        else:
            result["skipped"] += 1
    return result


# --------------------------------------------------------------------------- #
# 索引合并（持久缓存）
# --------------------------------------------------------------------------- #
def _load_index(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_index(root: Path, entries: dict, videos: dict | None = None) -> bool:
    payload = {"version": 1, "entries": entries}
    if videos:
        payload["videos"] = videos
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".json",
                                   dir=str(root))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, root / "_index.json")
        return True
    except OSError:
        return False


def _merge_persistent(src: Path, dst: Path) -> dict:
    result = {"moved": 0, "removed_old": 0, "skipped": 0, "errors": []}
    try:
        if src.resolve() == dst.resolve() or not src.is_dir():
            return result
    except OSError:
        return result

    moved = _migrate_digest_dirs(src, dst)
    for key in ("moved", "removed_old", "skipped"):
        result[key] += moved[key]
    result["errors"].extend(moved["errors"])

    old_data = _load_index(src / "_index.json")
    new_data = _load_index(dst / "_index.json")
    old_entries = old_data.get("entries") if isinstance(old_data, dict) else {}
    new_entries = new_data.get("entries") if isinstance(new_data, dict) else {}
    if not isinstance(old_entries, dict):
        old_entries = {}
    if not isinstance(new_entries, dict):
        new_entries = {}
    merged = dict(new_entries)
    for digest, entry in old_entries.items():
        if not _is_digest(str(digest)) or not isinstance(entry, dict):
            continue
        if (dst / str(digest) / ".done").is_file() and digest not in merged:
            merged[digest] = entry

    old_videos = old_data.get("videos") if isinstance(old_data, dict) else {}
    new_videos = new_data.get("videos") if isinstance(new_data, dict) else {}
    if not isinstance(old_videos, dict):
        old_videos = {}
    if not isinstance(new_videos, dict):
        new_videos = {}
    merged_videos = dict(old_videos)
    for key, record in new_videos.items():
        if not isinstance(record, dict):
            continue
        if key not in merged_videos or not isinstance(merged_videos[key], dict):
            merged_videos[key] = record
            continue
        # 两边都统计过同一个视频：取较大值，避免重复计算使用时长
        current = merged_videos[key]
        current["used_seconds"] = max(
            float(current.get("used_seconds") or 0),
            float(record.get("used_seconds") or 0))
        current["exports"] = max(int(current.get("exports") or 0),
                                 int(record.get("exports") or 0))
        current["last_used_at"] = max(
            float(current.get("last_used_at") or 0),
            float(record.get("last_used_at") or 0))

    if (merged != new_entries or merged_videos != new_videos) \
            and not _write_index(dst, merged, merged_videos):
        result["errors"].append("合并缓存索引失败")
    try:
        (src / "_index.json").unlink(missing_ok=True)
    except OSError:
        pass
    if not result["errors"]:
        try:
            src.rmdir()
        except OSError:
            pass
    return result


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
def _append_log(lines: list[str]):
    try:
        folder = data_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / LOG_NAME).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError:
        pass


def migrate_once(force: bool = False) -> dict:
    """执行一次性迁移；返回统计（方便测试/日志）。"""
    result = {"ran": False, "temp": {}, "persistent": {}, "reason": ""}
    if not force:
        if os.environ.get("PURER_NO_CACHE_MIGRATE") == "1":
            result["reason"] = "disabled"
            return result
        if os.environ.get("PURER_CACHE_DIR") or \
                os.environ.get("PURER_PERSIST_CACHE_DIR"):
            result["reason"] = "env-override"
            return result

    root = data_dir()
    marker = root / MARKER_NAME
    if not force and marker.is_file():
        result["reason"] = "already-done"
        return result
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        result["reason"] = "data-dir-unwritable"
        return result

    lock = root / LOCK_NAME
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
    except FileExistsError:
        result["reason"] = "locked"
        return result
    except OSError:
        result["reason"] = "lock-failed"
        return result

    try:
        result["ran"] = True
        new_temp = cache_root()
        new_persist = persistent_cache_root()
        result["temp"] = _migrate_digest_dirs(legacy_temp_root(), new_temp)
        result["persistent"] = _merge_persistent(
            legacy_persistent_root(), new_persist)

        # 旧的 update 下载目录也一起搬到新临时缓存下
        old_update = legacy_temp_root() / "update"
        if old_update.is_dir():
            update_errors = []
            target = new_temp / "update"
            if not target.exists():
                _move_dir_verified(old_update, target, update_errors)
            result["temp"]["errors"].extend(update_errors)

        errors = result["temp"].get("errors", []) \
            + result["persistent"].get("errors", [])
        summary = (
            f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} cache layout v2\n"
            f"old temp: {legacy_temp_root()}\n"
            f"new temp: {new_temp}\n"
            f"old persistent: {legacy_persistent_root()}\n"
            f"new persistent: {new_persist}\n"
            f"temp: moved={result['temp'].get('moved', 0)} "
            f"removed={result['temp'].get('removed_old', 0)} "
            f"skipped={result['temp'].get('skipped', 0)}\n"
            f"persistent: moved={result['persistent'].get('moved', 0)} "
            f"removed={result['persistent'].get('removed_old', 0)} "
            f"skipped={result['persistent'].get('skipped', 0)}\n"
            + ("errors:\n" + "\n".join(errors) if errors else "ok")
        )
        _append_log([summary])
        if not errors:
            payload = {
                "done_at": time.time(),
                "temp": {k: v for k, v in result["temp"].items()
                         if k != "errors"},
                "persistent": {k: v for k, v in result["persistent"].items()
                               if k != "errors"},
            }
            try:
                marker.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
            except OSError:
                pass
        else:
            result["reason"] = "partial"
        return result
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass
