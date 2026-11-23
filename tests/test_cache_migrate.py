# -*- coding: utf-8 -*-
"""缓存搬家单测（纯文件操作，不碰真实缓存）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vff import cache_migrate


def _digest(n: int) -> str:
    return f"{n:016x}"


def _make_entry(root: Path, digest: str, text: bytes = b"x" * 100) -> Path:
    folder = root / digest
    folder.mkdir(parents=True)
    (folder / "f_000001.jpg").write_bytes(text)
    (folder / ".done").write_bytes(b"done")
    return folder


class TestCacheMigrate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_digest_dir(self):
        old, new = self.base / "old", self.base / "new"
        digest = _digest(1)
        _make_entry(old, digest)
        result = cache_migrate._migrate_digest_dirs(old, new)
        self.assertEqual(result["moved"], 1)
        self.assertFalse((old / digest).exists())
        self.assertTrue((new / digest / ".done").is_file())
        self.assertEqual((new / digest / "f_000001.jpg").read_bytes(), b"x" * 100)

    def test_cross_volume_copy_verifies_then_deletes(self):
        old, new = self.base / "old", self.base / "new"
        digest = _digest(2)
        _make_entry(old, digest, b"y" * 333)
        with mock.patch.object(cache_migrate, "_same_volume",
                               return_value=False):
            result = cache_migrate._migrate_digest_dirs(old, new)
        self.assertEqual(result["moved"], 1)
        self.assertFalse((old / digest).exists())
        self.assertEqual((new / digest / "f_000001.jpg").stat().st_size, 333)

    def test_complete_target_wins_and_old_is_removed(self):
        old, new = self.base / "old", self.base / "new"
        digest = _digest(3)
        _make_entry(old, digest, b"old")
        _make_entry(new, digest, b"new")
        result = cache_migrate._migrate_digest_dirs(old, new)
        self.assertEqual(result["removed_old"], 1)
        self.assertFalse((old / digest).exists())
        self.assertEqual((new / digest / "f_000001.jpg").read_bytes(), b"new")

    def test_persistent_index_merged(self):
        old, new = self.base / "old_persist", self.base / "new_persist"
        old_d, new_d = _digest(4), _digest(5)
        _make_entry(old, old_d)
        _make_entry(new, new_d)
        (old / "_index.json").write_text(json.dumps({
            "version": 1,
            "entries": {old_d: {"id": old_d, "bytes": 100}},
        }), encoding="utf-8")
        (new / "_index.json").write_text(json.dumps({
            "version": 1,
            "entries": {new_d: {"id": new_d, "bytes": 100}},
        }), encoding="utf-8")
        result = cache_migrate._merge_persistent(old, new)
        self.assertFalse(result["errors"], result["errors"])
        data = json.loads((new / "_index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data["entries"]), {old_d, new_d})
        self.assertTrue((new / old_d / ".done").is_file())

    def test_migrate_once_writes_marker(self):
        old_temp = self.base / "legacy_temp"
        new_temp = self.base / "cache_tmp"
        old_persist = self.base / "legacy_persist"
        new_persist = self.base / "cache"
        data_dir = self.base / "data"
        old_d = _digest(6)
        _make_entry(old_temp, old_d)
        with mock.patch.object(cache_migrate, "legacy_temp_root",
                               return_value=old_temp), \
                mock.patch.object(cache_migrate, "cache_root",
                                  return_value=new_temp), \
                mock.patch.object(cache_migrate, "legacy_persistent_root",
                                  return_value=old_persist), \
                mock.patch.object(cache_migrate, "persistent_cache_root",
                                  return_value=new_persist), \
                mock.patch.object(cache_migrate, "data_dir",
                                  return_value=data_dir):
            result = cache_migrate.migrate_once(force=True)
        self.assertTrue(result["ran"])
        self.assertFalse(result["temp"]["errors"], result["temp"]["errors"])
        self.assertTrue((new_temp / old_d / ".done").is_file())
        self.assertTrue((data_dir / cache_migrate.MARKER_NAME).is_file())


if __name__ == "__main__":
    unittest.main()
