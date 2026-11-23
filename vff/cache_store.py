# -*- coding: utf-8 -*-
"""持久缓存库：把渲染好的「图墙」存下来，下次打开同一个视频直接加载。

为什么不直接用 ``%TEMP%`` 里的那份：
  * 临时目录随时可能被系统 / 清理软件删掉；
  * 这里还带一套「保存策略」（不保存 / 最多 N 个 / 按使用情况）和体积上限，
    所以需要自己的索引（每个条目的视频、大小、最后使用时间、命中次数）。

结构::

    <persistent_cache_root>/
        _index.json          # {"version":1,"entries":{id: {...}},"videos":{...}}
        <id>/                # 一个视频 + 一组设置 = 一个条目
            f_000001.jpg
            .done            # 抽帧完成标记（永远最后写 / 最后复制）
            scene.json       # 按镜头抽帧时才有

渲染本身仍发生在临时目录；保存时优先用**硬链接**（同一块磁盘上不额外占空间，
临时目录被清理也不会影响这里），跨盘时才退回复制。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .cache import dir_bytes
from .config import persistent_cache_root
from .settings import CACHE_MODE_COUNT, Settings

INDEX_NAME = "_index.json"
INDEX_VERSION = 1
STATE_NAME = "segments.json"     # 片段列表 / 入点出点 / 导出记录（跟着缓存一起存）
_TMP_PREFIX = "."
_DIGEST = re.compile(r"^[0-9a-f]{16}$")


def video_key(path) -> str:
    """「同一个视频」的判定键：绝对路径小写（Windows 大小写不敏感）。"""
    if not path:
        return ""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return str(resolved).lower()


def _share_or_copy(src: Path, dst: Path) -> None:
    """优先硬链接（不额外占空间、临时目录删了也不影响），失败再复制。"""
    try:
        if dst.exists():
            dst.unlink()
    except OSError:
        pass
    try:
        os.link(src, dst)
        return
    except (OSError, AttributeError, NotImplementedError):
        pass
    shutil.copy2(src, dst)


def _copy_tree_linked(src: Path, dst: Path) -> None:
    """把一个缓存条目目录复制到 dst（文件优先硬链接，失败再复制）。"""
    dst.mkdir(parents=True, exist_ok=False)
    for item in src.iterdir():
        if item.is_file():
            _share_or_copy(item, dst / item.name)


class CacheStore:
    """索引 + 条目目录的管理器（单进程、主线程使用）。"""

    def __init__(self, root: Path | None = None, settings: Settings | None = None):
        self.root = Path(root) if root is not None else persistent_cache_root()
        self.settings = settings if settings is not None else Settings()
        self.index_path = self.root / INDEX_NAME
        self._data = self._load_index()

    # ------------------------------------------------------------ 索引
    def _load_index(self) -> dict:
        data: dict = {}
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}
        entries = data.get("entries")
        videos = data.get("videos")
        return {
            "version": INDEX_VERSION,
            "entries": entries if isinstance(entries, dict) else {},
            "videos": videos if isinstance(videos, dict) else {},
        }

    def _save_index(self) -> bool:
        payload = dict(self._data)
        payload["version"] = INDEX_VERSION
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".index-", suffix=".json",
                                       dir=str(self.root))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(tmp, self.index_path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        return True

    def reload(self) -> None:
        self._data = self._load_index()

    def retarget(self, new_root: Path, move: bool = True) -> tuple[bool, str]:
        """把「已保存缓存」换到另一个目录。

        * 目标里有 ``_index.json``（以前用过的缓存目录）→ 直接接管它的索引；
        * 否则目标必须为空；``move=True`` 时先把现有条目复制（硬链接优先）过去，
          全部成功才切换，然后删掉旧目录；
        * 任何一步失败都保持原状。
        """
        new_root = Path(new_root)
        try:
            same = new_root.resolve() == self.root.resolve()
        except OSError:
            same = new_root == self.root
        if same:
            return True, ""
        try:
            new_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"无法创建缓存目录：{e}"
        adopt = new_root / INDEX_NAME
        if not move and adopt.is_file():
            self.root = new_root
            self.index_path = adopt
            self.reload()
            self.prune()
            return True, ""
        try:
            if any(new_root.iterdir()):
                return False, "目标文件夹不是空的（请选一个空文件夹，或以前用过的缓存目录）"
        except OSError as e:
            return False, f"读不了目标文件夹：{e}"

        old_root = self.root
        staged: list[tuple[str, Path]] = []
        if move:
            try:
                for digest in list(self._data["entries"]):
                    src = old_root / digest
                    if not (src / ".done").is_file():
                        self._data["entries"].pop(digest, None)
                        continue
                    tmp = new_root / f".{digest}.tmp"
                    shutil.rmtree(tmp, ignore_errors=True)
                    _copy_tree_linked(src, tmp)
                    staged.append((digest, tmp))
            except OSError as e:
                for _digest, tmp in staged:
                    shutil.rmtree(tmp, ignore_errors=True)
                return False, f"迁移缓存失败：{e}"
            for digest, tmp in staged:
                dest = new_root / digest
                shutil.rmtree(dest, ignore_errors=True)
                try:
                    os.replace(tmp, dest)
                except OSError as e:
                    for _digest, left in staged:
                        shutil.rmtree(left, ignore_errors=True)
                    return False, f"写入新位置失败：{e}"
            for digest, _tmp in staged:
                shutil.rmtree(old_root / digest, ignore_errors=True)

        self.root = new_root
        self.index_path = new_root / INDEX_NAME
        self._save_index()
        self.prune()
        return True, ""

    # ------------------------------------------------------------ 条目
    def entry(self, digest: str) -> dict | None:
        entry = self._data["entries"].get(str(digest))
        return dict(entry) if isinstance(entry, dict) else None

    def entries(self) -> list[dict]:
        """所有条目（副本；界面列表用）。"""
        return [dict(entry) for entry in self._data["entries"].values()
                if isinstance(entry, dict)]

    def has(self, digest: str) -> bool:
        """条目存在且磁盘上真的可用（目录 + .done 都在）。"""
        if not _DIGEST.match(str(digest or "")):
            return False
        entry = self._data["entries"].get(digest)
        if not isinstance(entry, dict):
            return False
        folder = self.root / digest
        if not (folder / ".done").is_file():
            self._remove_entry(digest, delete_dir=False)
            return False
        return True

    def dir_for(self, digest: str) -> Path:
        return self.root / digest

    def save_dir(self, digest: str, src_dir: Path,
                 meta: dict | None = None) -> tuple[bool, str]:
        """把临时目录里「已完成」的图墙存进库。返回 ``(成功, 原因)``。"""
        src = Path(src_dir)
        if not _DIGEST.match(str(digest or "")):
            return False, "缓存 id 不合法"
        if not (src / ".done").is_file():
            return False, "抽帧还没完成"
        try:
            frames = sorted(p for p in src.glob("f_*.jpg") if p.is_file())
        except OSError as e:
            return False, str(e)
        if not frames:
            return False, "没有可保存的缩略图"

        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return False, f"无法创建缓存目录：{e}"
        target = self.root / digest
        tmp = self.root / f".{digest}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        total = 0
        try:
            tmp.mkdir()
            for item in frames:
                _share_or_copy(item, tmp / item.name)
                total += item.stat().st_size
            for extra in ("scene.json", STATE_NAME, ".done"):
                item = src / extra
                if item.is_file():
                    _share_or_copy(item, tmp / extra)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, f"写入缓存失败：{e}"
        # Windows 不能 os.replace 覆盖非空目录 → 先删旧目录（旧硬链接仍被临时目录保住）
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            os.replace(tmp, target)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, f"替换旧缓存失败：{e}"

        now = time.time()
        old = self._data["entries"].get(digest)
        entry = {
            "id": digest,
            "video": (meta or {}).get("video", ""),
            "video_key": video_key((meta or {}).get("video") or ""),
            "video_size": int((meta or {}).get("video_size") or 0),
            "video_mtime": int((meta or {}).get("video_mtime") or 0),
            "mode": (meta or {}).get("mode", "step"),
            "step": (meta or {}).get("step"),
            "threshold": (meta or {}).get("threshold"),
            "tw": int((meta or {}).get("tw") or 0),
            "th": int((meta or {}).get("th") or 0),
            "frames": int((meta or {}).get("frames") or 0),
            "count": len(frames),
            "bytes": int(total),
            "saved_at": now,
            "last_used_at": now,
            "hits": int((old or {}).get("hits") or 0),
            "first_saved_at": (old or {}).get("first_saved_at") or now,
            "segments": int((old or {}).get("segments") or 0),
        }
        if (old or {}).get("state_at"):
            entry["state_at"] = old["state_at"]
        self._data["entries"][digest] = entry
        self._save_index()
        self.prune()
        return True, ""

    def materialize(self, digest: str, dest_dir: Path) -> bool:
        """把库里的条目恢复到 ``dest_dir``（硬链接优先），供本次会话直接使用。"""
        if not self.has(digest):
            return False
        src = self.root / digest
        dest = Path(dest_dir)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".done").unlink(missing_ok=True)   # 数据齐了以后才重新写
            # 目标目录可能残留上一次的图（帧数不同会错位），先清干净
            for stale in dest.glob("f_*.jpg"):
                stale.unlink(missing_ok=True)
            (dest / "scene.json").unlink(missing_ok=True)
            (dest / STATE_NAME).unlink(missing_ok=True)
            frames = sorted(p for p in src.glob("f_*.jpg") if p.is_file())
            if not frames:
                return False
            for item in frames:
                _share_or_copy(item, dest / item.name)
            for extra in ("scene.json", STATE_NAME):
                item = src / extra
                if item.is_file():
                    _share_or_copy(item, dest / extra)
            _share_or_copy(src / ".done", dest / ".done")
        except OSError:
            return False
        entry = self._data["entries"].get(digest)
        if isinstance(entry, dict):
            entry["last_used_at"] = time.time()
            entry["hits"] = int(entry.get("hits") or 0) + 1
            self._save_index()
        return True

    # ------------------------------------------------------------ 片段状态
    def read_state(self, digest: str) -> dict:
        """读某个缓存条目里的「片段列表 / 入点出点 / 导出记录」。"""
        if not _DIGEST.match(str(digest or "")):
            return {}
        path = self.root / str(digest) / STATE_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_state(self, digest: str, payload: dict) -> bool:
        """把片段状态写进已有条目的目录（原子写；条目不在就返回 False）。"""
        digest = str(digest or "")
        if not _DIGEST.match(digest):
            return False
        folder = self.root / digest
        if not (folder / ".done").is_file():
            return False
        payload = dict(payload or {})
        try:
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json",
                                       dir=str(folder))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                os.replace(tmp, folder / STATE_NAME)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            return False
        entry = self._data["entries"].get(digest)
        if isinstance(entry, dict):
            entry["state_at"] = time.time()
            entry["segments"] = len(payload.get("segments") or [])
            self._save_index()
        return True

    def touch(self, digest: str, hits: int = 1) -> bool:
        """只更新「最近使用时间 / 命中次数」（临时缓存命中时用）。"""
        entry = self._data["entries"].get(str(digest or ""))
        if not isinstance(entry, dict):
            return False
        entry["last_used_at"] = time.time()
        entry["hits"] = int(entry.get("hits") or 0) + max(0, int(hits))
        self._save_index()
        return True

    # ------------------------------------------------------------ 使用统计
    def usage(self, path) -> dict:
        record = self._data["videos"].get(video_key(path))
        if not isinstance(record, dict):
            return {"used_seconds": 0.0, "exports": 0, "last_used_at": 0.0}
        return {
            "used_seconds": float(record.get("used_seconds") or 0.0),
            "exports": int(record.get("exports") or 0),
            "last_used_at": float(record.get("last_used_at") or 0.0),
        }

    def record_usage(self, path, seconds: float = 0.0,
                     exports: int = 0, when: float | None = None) -> dict:
        """累加「这个视频用了多久 / 导出过几次」，并顺带更新条目的最近使用时间。"""
        key = video_key(path)
        record = self._data["videos"].get(key)
        if not isinstance(record, dict):
            record = {"used_seconds": 0.0, "exports": 0, "last_used_at": 0.0}
        record["used_seconds"] = max(
            0.0, float(record.get("used_seconds") or 0.0) + max(0.0, float(seconds)))
        record["exports"] = max(
            0, int(record.get("exports") or 0) + max(0, int(exports)))
        record["last_used_at"] = float(when or time.time())
        self._data["videos"][key] = record
        for entry in self._data["entries"].values():
            if isinstance(entry, dict) and entry.get("video_key") == key:
                entry["last_used_at"] = record["last_used_at"]
        self._save_index()
        return self.usage(path)

    # ------------------------------------------------------------ 统计 / 清理
    def total_bytes(self) -> int:
        return sum(int((e or {}).get("bytes") or 0)
                   for e in self._data["entries"].values()
                   if isinstance(e, dict))

    def summary(self) -> dict:
        entries = [e for e in self._data["entries"].values()
                   if isinstance(e, dict)]
        return {
            "count": len(entries),
            "bytes": self.total_bytes(),
            "frames": sum(int(e.get("count") or 0) for e in entries),
            "root": str(self.root),
            "limit": self.settings.effective_cache_limit(),
        }

    def _remove_entry(self, digest: str, delete_dir: bool = True,
                      save: bool = True) -> None:
        self._data["entries"].pop(digest, None)
        if delete_dir:
            shutil.rmtree(self.root / digest, ignore_errors=True)
        if save:
            self._save_index()

    def drop(self, digest: str) -> None:
        if digest in self._data["entries"]:
            self._remove_entry(digest, delete_dir=True)

    def clear(self, keep: tuple[str, ...] = ()) -> int:
        """删除所有已保存的缓存（``keep`` 里的不动）。返回删除条目数。"""
        removed = 0
        for digest in list(self._data["entries"]):
            if digest in keep:
                continue
            self._remove_entry(digest, delete_dir=True, save=False)
            removed += 1
        self._save_index()
        return removed

    def prune(self, max_bytes: int | None = None,
              max_count: int | None = None) -> int:
        """按「最近使用优先」收敛到上限内（条目数 + 总体积）。"""
        cache_cfg = self.settings.cache()
        if max_count is None:
            max_count = (int(cache_cfg["max_videos"])
                         if cache_cfg["mode"] == CACHE_MODE_COUNT else 0)
        if max_bytes is None:
            max_bytes = self.settings.effective_cache_limit()

        entries = [(digest, entry)
                   for digest, entry in self._data["entries"].items()
                   if isinstance(entry, dict)]
        entries.sort(key=lambda item: float(item[1].get("last_used_at") or 0),
                     reverse=True)
        removed = 0
        kept = 0
        used = 0
        for digest, entry in entries:
            size = int(entry.get("bytes") or 0)
            too_many = bool(max_count) and kept >= int(max_count)
            too_big = bool(max_bytes) and kept > 0 and used + size > int(max_bytes)
            missing = not (self.root / digest / ".done").is_file()
            if missing or too_many or too_big:
                self._remove_entry(digest, delete_dir=True, save=False)
                removed += 1
                continue
            kept += 1
            used += size
        if removed:
            self._save_index()
        # 很久没碰过的「使用统计」也顺手扔掉（只留 180 天），避免索引越滚越大
        cutoff = time.time() - 180 * 86400
        stale_videos = [key for key, record in self._data["videos"].items()
                        if not isinstance(record, dict)
                        or float(record.get("last_used_at") or 0) < cutoff]
        for key in stale_videos:
            self._data["videos"].pop(key, None)
        if stale_videos:
            self._save_index()
        return removed

    def remove_missing(self) -> int:
        """删掉「原视频已经不在了」的条目（用户删了素材之后的手动清理）。"""
        removed = 0
        for digest, entry in list(self._data["entries"].items()):
            if not isinstance(entry, dict):
                continue
            video = entry.get("video") or ""
            try:
                alive = bool(video) and Path(video).is_file()
            except OSError:
                alive = False
            if not alive:
                self._remove_entry(digest, delete_dir=True, save=False)
                removed += 1
        if removed:
            self._save_index()
        return removed

    def measured_bytes(self) -> int:
        """真正扫一遍磁盘（设置页展示用；比索引慢，但能发现残留文件）。"""
        return dir_bytes(self.root)
