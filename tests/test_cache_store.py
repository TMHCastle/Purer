# -*- coding: utf-8 -*-
"""设置 / 持久缓存库 / 批量导出命名 的单测。

    python -m unittest tests.test_cache_store -v

这些模块都不依赖 Qt，所以能在没有界面的机器上跑。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vff.cache import cache_digest, clean_cache
from vff.cache_store import INDEX_NAME, CacheStore, video_key
from vff.config import CACHE_LIMIT_MAX, CACHE_LIMIT_MIN
from vff.export_names import (clean_user_name, default_filename, plan_filenames,
                              series_label, strip_video_suffix)
from vff.settings import (CACHE_MODE_COUNT, CACHE_MODE_NEVER, CACHE_MODE_USAGE,
                          Settings, close_prompt_needed, default_cache_limit_bytes,
                          normalized_cache, usage_meets)


class _TempEnv(unittest.TestCase):
    """给每个用例一个独立的「数据目录」，绝不碰真实用户设置 / 缓存。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "PURER_DATA_DIR": str(self.tmp / "data"),
            "PURER_PERSIST_CACHE_DIR": str(self.tmp / "cache"),
            "PURER_SETTINGS_FILE": str(self.tmp / "settings.json"),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


# --------------------------------------------------------------------------- #
class TestSettings(_TempEnv):
    def test_defaults(self):
        settings = Settings(self.tmp / "settings.json")
        cfg = settings.cache()
        self.assertEqual(cfg["mode"], CACHE_MODE_NEVER)
        self.assertIsNone(cfg["dir"])
        self.assertEqual(cfg["max_videos"], 20)
        self.assertEqual(cfg["usage_minutes"], 10)
        self.assertEqual(cfg["usage_exports"], 2)
        self.assertIsNone(cfg["max_bytes"])
        self.assertFalse(cfg["skip_close_prompt"])

    def test_roundtrip(self):
        path = self.tmp / "settings.json"
        settings = Settings(path)
        settings.update_cache(mode=CACHE_MODE_USAGE, usage_minutes=7,
                              usage_exports=4, max_videos=5,
                              max_bytes=1234 * 1024 * 1024,
                              dir=str(self.tmp / "mycache"),
                              skip_close_prompt=True)
        self.assertTrue(settings.save())
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["cache"]["mode"], CACHE_MODE_USAGE)

        again = Settings(path)
        cfg = again.cache()
        self.assertEqual(cfg["usage_minutes"], 7)
        self.assertEqual(cfg["usage_exports"], 4)
        self.assertEqual(cfg["max_videos"], 5)
        self.assertEqual(cfg["max_bytes"], 1234 * 1024 * 1024)
        self.assertEqual(cfg["dir"], str(self.tmp / "mycache"))
        self.assertTrue(cfg["skip_close_prompt"])
        self.assertEqual(again.effective_cache_dir(), self.tmp / "mycache")

    def test_bad_values_are_clamped(self):
        path = self.tmp / "settings.json"
        path.write_text(json.dumps({"cache": {
            "mode": "nonsense", "max_videos": 0, "usage_minutes": -3,
            "usage_exports": "x", "max_bytes": -1, "skip_close_prompt": 1,
            "dir": "",
        }}), encoding="utf-8")
        cfg = Settings(path).cache()
        self.assertEqual(cfg["mode"], CACHE_MODE_NEVER)
        self.assertEqual(cfg["max_videos"], 1)
        self.assertEqual(cfg["usage_minutes"], 1)
        self.assertEqual(cfg["usage_exports"], 2)
        self.assertIsNone(cfg["max_bytes"])
        self.assertIsNone(cfg["dir"])
        self.assertTrue(cfg["skip_close_prompt"])

    def test_broken_json_falls_back(self):
        path = self.tmp / "settings.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(Settings(path).cache()["mode"], CACHE_MODE_NEVER)

    def test_close_prompt_needed(self):
        # 默认（不自动保存）：每次关闭都问
        self.assertTrue(close_prompt_needed({}))
        self.assertTrue(close_prompt_needed({"mode": CACHE_MODE_NEVER,
                                             "skip_close_prompt": True}))
        # 选了自动保存 + 勾了不再提醒 -> 静默
        self.assertFalse(close_prompt_needed({"mode": CACHE_MODE_COUNT,
                                              "skip_close_prompt": True}))
        self.assertFalse(close_prompt_needed({"mode": CACHE_MODE_USAGE,
                                              "skip_close_prompt": True}))
        # 选了自动保存但没勾 -> 还是问
        self.assertTrue(close_prompt_needed({"mode": CACHE_MODE_COUNT,
                                             "skip_close_prompt": False}))

    def test_default_limit_uses_disk_fraction(self):
        value = default_cache_limit_bytes(self.tmp)
        self.assertGreaterEqual(value, CACHE_LIMIT_MIN)
        self.assertLessEqual(value, CACHE_LIMIT_MAX)

        settings = Settings(self.tmp / "settings.json")
        # 没设过 -> 用默认；设过 -> 用设的
        self.assertEqual(settings.effective_cache_limit(),
                         default_cache_limit_bytes())
        settings.update_cache(max_bytes=777 * 1024 * 1024)
        self.assertEqual(settings.effective_cache_limit(), 777 * 1024 * 1024)

    def test_normalized_cache_keeps_all_modes(self):
        for mode in (CACHE_MODE_NEVER, CACHE_MODE_COUNT, CACHE_MODE_USAGE):
            self.assertEqual(normalized_cache({"mode": mode})["mode"], mode)

    def test_usage_meets(self):
        cfg = {"usage_minutes": 10, "usage_exports": 2}
        self.assertFalse(usage_meets(cfg, {"used_seconds": 599, "exports": 1}))
        self.assertTrue(usage_meets(cfg, {"used_seconds": 600, "exports": 0}))
        self.assertTrue(usage_meets(cfg, {"used_seconds": 0, "exports": 2}))
        # 缺字段 / 脏数据都按 0 处理
        self.assertFalse(usage_meets({}, {"used_seconds": 0, "exports": 0}))
        self.assertFalse(usage_meets(cfg, None))


# --------------------------------------------------------------------------- #
class TestExportNames(unittest.TestCase):
    def test_series_label(self):
        self.assertEqual(series_label(0), "Cut - 1")
        self.assertEqual(series_label(11), "Cut - 12")

    def test_default_name_prepends_original(self):
        self.assertEqual(default_filename(0, "旅行", ".mp4"), "Cut - 1_旅行.mp4")
        self.assertEqual(default_filename(1, "a:b", ".mkv"), "Cut - 2_a-b.mkv")

    def test_user_name_keeps_source_extension(self):
        self.assertEqual(clean_user_name("开场", ".mp4"), "开场.mp4")
        # 用户自己打了扩展名 -> 去掉，统一用源视频的容器
        self.assertEqual(clean_user_name("开场.mp4", ".mkv"), "开场.mkv")
        self.assertEqual(clean_user_name("   ", ".mp4"), "")
        self.assertEqual(clean_user_name("a/b:c", ".mp4"), "a-b-c.mp4")

    def test_strip_video_suffix(self):
        self.assertEqual(strip_video_suffix("x.MP4"), "x")
        self.assertEqual(strip_video_suffix("x"), "x")
        self.assertEqual(strip_video_suffix("x.unknown"), "x.unknown")

    def test_plan_filenames(self):
        names = plan_filenames(["", "", "手动名"], "原片", ".mp4")
        self.assertEqual(names, ["Cut - 1_原片.mp4", "Cut - 2_原片.mp4",
                                 "手动名.mp4"])

    def test_plan_filenames_avoids_duplicates(self):
        names = plan_filenames(["同名", "同名", ""], "原片", ".mp4",
                               taken={"cut - 3_原片.mp4"})
        self.assertEqual(names, ["同名.mp4", "同名 (2).mp4",
                                 "Cut - 3_原片 (2).mp4"])

    def test_plan_filenames_bad_input_falls_back(self):
        self.assertEqual(plan_filenames(["???"], "原片", ".mp4"),
                         ["Cut - 1_原片.mp4"])


# --------------------------------------------------------------------------- #
class TestCacheStore(_TempEnv):
    def make_cache(self, name="src", frames=3) -> Path:
        folder = self.tmp / name
        folder.mkdir(parents=True, exist_ok=True)
        for i in range(1, frames + 1):
            (folder / f"f_{i:06d}.jpg").write_bytes(b"jpeg" * (i + 1))
        (folder / ".done").write_text(str(frames), encoding="utf-8")
        (folder / "scene.json").write_text('{"frames":[0,1]}', encoding="utf-8")
        # 回放 / 预览子目录不该被保存
        (folder / "strip").mkdir(exist_ok=True)
        (folder / "strip" / "f_000001.jpg").write_bytes(b"strip")
        (folder / "play").mkdir(exist_ok=True)
        (folder / "play" / "f_000001.jpg").write_bytes(b"play")
        return folder

    def meta(self, video="D:/videos/a.mp4", frames=3):
        return {"video": video, "video_size": 10, "video_mtime": 123,
                "mode": "step", "step": 24, "threshold": None,
                "tw": 320, "th": 180, "frames": frames}

    def store(self) -> CacheStore:
        settings = Settings(self.tmp / "settings.json")
        settings.update_cache(mode=CACHE_MODE_NEVER)
        return CacheStore(self.tmp / "cache", settings)

    def test_save_and_materialize(self):
        store = self.store()
        src = self.make_cache()
        digest = "0123456789abcdef"
        ok, why = store.save_dir(digest, src, self.meta())
        self.assertTrue(ok, why)
        self.assertTrue(store.has(digest))
        entry = store.entry(digest)
        self.assertEqual(entry["count"], 3)
        self.assertEqual(entry["video"], "D:/videos/a.mp4")
        self.assertGreater(entry["bytes"], 0)
        # strip / play 不进入持久库
        saved = sorted(p.name for p in store.dir_for(digest).iterdir())
        self.assertIn("f_000001.jpg", saved)
        self.assertIn(".done", saved)
        self.assertIn("scene.json", saved)
        self.assertNotIn("strip", saved)
        self.assertNotIn("play", saved)

        dest = self.tmp / "restored"
        self.assertTrue(store.materialize(digest, dest))
        self.assertEqual(sorted(p.name for p in dest.glob("f_*.jpg")),
                         ["f_000001.jpg", "f_000002.jpg", "f_000003.jpg"])
        self.assertTrue((dest / ".done").is_file())
        self.assertEqual((dest / "f_000002.jpg").read_bytes(),
                         (src / "f_000002.jpg").read_bytes())
        # 命中计数 + 最近使用时间会被更新
        self.assertEqual(store.entry(digest)["hits"], 1)
        # 临时目录删掉后，持久库仍然可用（硬链接/复制的意义）
        import shutil
        shutil.rmtree(src)
        self.assertTrue(store.has(digest))
        dest2 = self.tmp / "restored2"
        self.assertTrue(store.materialize(digest, dest2))
        self.assertEqual(len(list(dest2.glob("f_*.jpg"))), 3)

    def test_incomplete_or_missing_is_not_cache(self):
        store = self.store()
        src = self.make_cache()
        (src / ".done").unlink()
        ok, _why = store.save_dir("aaaaaaaabbbbbbbb", src, self.meta())
        self.assertFalse(ok)
        self.assertFalse(store.has("aaaaaaaabbbbbbbb"))
        # 索引里有、目录被删了 -> has() 返回 False 并清理
        (src / ".done").write_text("3", encoding="utf-8")
        ok, _why = store.save_dir("0123456789abcdef", src, self.meta())
        self.assertTrue(ok)
        import shutil
        shutil.rmtree(store.dir_for("0123456789abcdef"))
        self.assertFalse(store.has("0123456789abcdef"))
        self.assertIsNone(store.entry("0123456789abcdef"))

    def test_prune_by_count_and_bytes(self):
        store = self.store()
        settings = store.settings
        settings.update_cache(mode=CACHE_MODE_COUNT, max_videos=99,
                              max_bytes=10 * 1024 * 1024)
        for i in range(4):
            src = self.make_cache(f"src{i}")
            ok, why = store.save_dir(f"{i:016x}", src, self.meta(f"D:/v{i}.mp4"))
            self.assertTrue(ok, why)
            entry = store._data["entries"][f"{i:016x}"]
            entry["last_used_at"] = time.time() + i      # 3 最新
            store._save_index()
        settings.update_cache(max_videos=2)
        removed = store.prune()
        self.assertGreaterEqual(removed, 2)
        self.assertEqual(store.summary()["count"], 2)
        self.assertTrue(store.has(f"{3:016x}"))
        self.assertTrue(store.has(f"{2:016x}"))

        # 体积上限：只留最新那一个（条目体积都远大于 1 字节）
        store.prune(max_bytes=1, max_count=0)
        self.assertLessEqual(store.summary()["count"], 1)

    def test_retarget_moves_and_adopts(self):
        store = self.store()
        src = self.make_cache()
        store.save_dir("0123456789abcdef", src, self.meta())
        new_root = self.tmp / "newcache"
        ok, why = store.retarget(new_root, move=True)
        self.assertTrue(ok, why)
        self.assertEqual(store.root, new_root)
        self.assertTrue(store.has("0123456789abcdef"))
        self.assertTrue((new_root / "0123456789abcdef" / ".done").is_file())
        self.assertFalse((self.tmp / "cache" / "0123456789abcdef").exists())
        self.assertTrue((new_root / INDEX_NAME).is_file())

        # 目标里有索引 -> 直接接管（不去搬原来的）
        other_root = self.tmp / "other"
        settings2 = Settings(self.tmp / "settings2.json")
        other = CacheStore(other_root, settings2)
        src2 = self.make_cache("src2")
        other.save_dir("fedcba9876543210", src2, self.meta("D:/v2.mp4"))
        ok, why = store.retarget(other_root, move=False)
        self.assertTrue(ok, why)
        self.assertEqual(store.root, other_root)
        self.assertTrue(store.has("fedcba9876543210"))

        # 非空且没有索引 -> 拒绝
        dirty = self.tmp / "dirty"
        dirty.mkdir()
        (dirty / "something.txt").write_text("x", encoding="utf-8")
        ok, why = store.retarget(dirty, move=False)
        self.assertFalse(ok)
        self.assertTrue(why)
        self.assertEqual(store.root, other_root)

    def test_usage_accumulates(self):
        store = self.store()
        self.assertEqual(store.usage("D:/v.mp4")["exports"], 0)
        store.record_usage("D:/v.mp4", seconds=30, exports=1)
        store.record_usage("D:/v.mp4", seconds=15.5, exports=2)
        usage = store.usage("D:/v.mp4")
        self.assertAlmostEqual(usage["used_seconds"], 45.5, places=3)
        self.assertEqual(usage["exports"], 3)
        # 负值不会把统计减回去
        store.record_usage("D:/v.mp4", seconds=-100, exports=-5)
        usage = store.usage("D:/v.mp4")
        self.assertAlmostEqual(usage["used_seconds"], 45.5, places=3)
        self.assertEqual(usage["exports"], 3)

    def test_clear_and_remove_missing(self):
        store = self.store()
        video = self.tmp / "real.mp4"
        video.write_bytes(b"x")
        src = self.make_cache()
        store.save_dir("0123456789abcdef", src, self.meta(str(video)))
        self.assertEqual(store.summary()["count"], 1)
        self.assertEqual(store.clear(), 1)
        self.assertEqual(store.summary()["count"], 0)
        self.assertFalse(store.dir_for("0123456789abcdef").exists())

        store.save_dir("0123456789abcdef", src, self.meta(str(video)))
        video.unlink()
        self.assertEqual(store.remove_missing(), 1)
        self.assertEqual(store.summary()["count"], 0)

    def test_video_key(self):
        self.assertEqual(video_key(""), "")
        a = video_key("D:/A.mp4")
        self.assertEqual(a, video_key("d:/a.mp4"))
        self.assertTrue(a.endswith("a.mp4"))


class TestCacheDigest(unittest.TestCase):
    def test_digest_is_stable_and_setting_sensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"0123456789")
            a = cache_digest(video, 24, 320, 180)
            self.assertEqual(a, cache_digest(video, 24, 320, 180))
            self.assertNotEqual(a, cache_digest(video, 12, 320, 180))
            self.assertNotEqual(a, cache_digest(video, 24, 320, 180,
                                                mode="scene", threshold=0.3))
            self.assertEqual(len(a), 16)

    def test_clean_cache_keeps_update_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"PURER_CACHE_DIR": tmp}):
                root = Path(tmp)
                old = root / "old"
                update = root / "update"
                old.mkdir()
                update.mkdir()
                stale = time.time() - 30 * 86400
                os.utime(old, (stale, stale))
                os.utime(update, (stale, stale))
                self.assertEqual(clean_cache(days=7), 1)
                self.assertFalse(old.exists())
                self.assertTrue(update.exists())


if __name__ == "__main__":
    unittest.main()
