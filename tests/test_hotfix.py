# -*- coding: utf-8 -*-
"""窄热补丁通道的单测（纯逻辑，不开窗口）。

    python -m unittest tests.test_hotfix -v

覆盖：版本比较 / 兼容性、pack.json 读取、zip 校验（缺文件、坏 JSON、路径穿越、
exe_min 不符）、解包、安装状态、过期清理、GitHub 资产识别。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import purer_boot
from vff import hotfix
from vff.update import CheckResult, code_asset_from_json, result_from_json


def _make_zip(path: Path, files: dict[str, str], pack: dict | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        if pack is not None:
            archive.writestr("pack.json",
                             json.dumps(pack, ensure_ascii=False))
    return path


class TestVersionMath(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(purer_boot.parse_version("v1.2.10"), (1, 2, 10))
        self.assertEqual(purer_boot.parse_version("1.2.0.0"), (1, 2, 0, 0))
        self.assertEqual(purer_boot.parse_version("abc"), ())

    def test_newer(self):
        self.assertTrue(purer_boot.is_newer("1.2.10", "1.2.9"))
        self.assertFalse(purer_boot.is_newer("1.2.0", "1.2.0"))
        self.assertFalse(purer_boot.is_newer("", "1.2.0"))
        self.assertTrue(purer_boot.is_newer("1.3", "1.2.99"))

    def test_compatible(self):
        exe = "1.2.0"
        self.assertTrue(purer_boot.compatible(
            {"version": "1.2.1", "exe_min": "1.2.0"}, exe)[0])
        # 同版本 = 已过期（exe 里的代码就是它）
        self.assertFalse(purer_boot.compatible(
            {"version": "1.2.0", "exe_min": "1.2.0"}, exe)[0])
        # exe 太旧
        self.assertFalse(purer_boot.compatible(
            {"version": "1.4.0", "exe_min": "1.3.0"}, exe)[0])
        # 拿不到 exe 版本
        self.assertFalse(purer_boot.compatible(
            {"version": "1.2.1", "exe_min": "1.2.0"}, "")[0])


class _CodeEnv(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.code = self.tmp / "code"
        self.code.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "PURER_CODE_DIR": str(self.code),
            "PURER_EXE_VERSION": "1.2.0",
        })
        self._env.start()
        self._active = purer_boot._ACTIVE
        purer_boot._ACTIVE = {}
        purer_boot.uninstall_finders()

    def tearDown(self):
        purer_boot._ACTIVE = self._active
        purer_boot.uninstall_finders()
        self._env.stop()
        self._tmp.cleanup()

    def put_pack(self, version="1.2.1", exe_min="1.2.0", **extra) -> dict:
        folder = purer_boot.pack_dir()
        (folder / "vff").mkdir(parents=True, exist_ok=True)
        (folder / "vff" / "__init__.py").write_text("", encoding="utf-8")
        meta = {"version": version, "exe_min": exe_min, "built_at": 0,
                "files": 1, "note": ""}
        meta.update(extra)
        (folder / "pack.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta


class TestPackInstall(_CodeEnv):
    def test_read_missing(self):
        self.assertEqual(purer_boot.read_pack(), {})

    def test_read_broken_json(self):
        purer_boot.pack_dir().mkdir(parents=True)
        (purer_boot.pack_dir() / "pack.json").write_text("{ nope", encoding="utf-8")
        self.assertEqual(purer_boot.read_pack(), {})

    def test_install_activates_and_versions(self):
        self.put_pack()
        info = purer_boot.install()
        self.assertEqual(info.get("version"), "1.2.1")
        self.assertTrue(purer_boot.active_pack())
        self.assertEqual(purer_boot.effective_version(), "1.2.1")
        self.assertTrue(any(isinstance(f, purer_boot.OverlayFinder)
                            for f in __import__("sys").meta_path))

    def test_install_skips_expired_pack(self):
        self.put_pack(version="1.2.0")
        self.assertEqual(purer_boot.install(), {})
        self.assertFalse(purer_boot.active_pack())
        self.assertEqual(purer_boot.effective_version(), "1.2.0")

    def test_install_skips_incomplete_pack(self):
        purer_boot.pack_dir().mkdir(parents=True)
        (purer_boot.pack_dir() / "pack.json").write_text(
            json.dumps({"version": "1.2.1", "exe_min": "1.2.0"}), encoding="utf-8")
        self.assertEqual(purer_boot.install(), {})   # 缺 vff/__init__.py

    def test_prune_removes_expired_pack(self):
        self.put_pack(version="1.2.0")
        self.assertTrue(purer_boot.prune_stale())
        self.assertFalse(purer_boot.pack_dir().exists())

    def test_prune_keeps_fresh_pack(self):
        self.put_pack(version="1.2.1")
        self.assertFalse(purer_boot.prune_stale())
        self.assertTrue(purer_boot.pack_dir().is_dir())


class TestZipValidation(_CodeEnv):
    def good_zip(self, name="pack.zip", version="1.2.1", exe_min="1.2.0",
                 extra=None) -> Path:
        files = {"vff/__init__.py": "", "vff/config.py": "X = 1\n"}
        if extra:
            files.update(extra)
        return _make_zip(self.tmp / name, files,
                         {"version": version, "exe_min": exe_min, "files": 2})

    def test_ok(self):
        info, why = hotfix.validate_zip(self.good_zip())
        self.assertEqual(why, "")
        self.assertEqual(info["version"], "1.2.1")

    def test_missing_file(self):
        info, why = hotfix.validate_zip(self.tmp / "nope.zip")
        self.assertEqual(info, {})
        self.assertIn("不存在", why)

    def test_not_a_zip(self):
        bad = self.tmp / "bad.zip"
        bad.write_bytes(b"not a zip")
        self.assertEqual(hotfix.validate_zip(bad)[0], {})

    def test_missing_pack_json(self):
        target = _make_zip(self.tmp / "a.zip", {"vff/__init__.py": ""})
        info, why = hotfix.validate_zip(target)
        self.assertEqual(info, {})
        self.assertIn("pack.json", why)

    def test_missing_init(self):
        target = _make_zip(self.tmp / "b.zip", {"vff/config.py": ""},
                           {"version": "1.2.1", "exe_min": "1.2.0"})
        info, why = hotfix.validate_zip(target)
        self.assertEqual(info, {})
        self.assertIn("__init__", why)

    def test_broken_pack_json(self):
        target = _make_zip(self.tmp / "c.zip", {"vff/__init__.py": ""})
        with zipfile.ZipFile(target, "a") as archive:
            archive.writestr("pack.json", "{ broken")
        self.assertEqual(hotfix.validate_zip(target)[0], {})

    def test_bad_version(self):
        target = self.good_zip(version="abc")
        info, why = hotfix.validate_zip(target)
        self.assertEqual(info, {})
        self.assertIn("版本号", why)

    def test_exe_too_old(self):
        target = self.good_zip(exe_min="1.5.0")
        info, why = hotfix.validate_zip(target)
        self.assertEqual(info, {})
        self.assertIn("1.5.0", why)

    def test_zip_slip_rejected(self):
        target = self.good_zip(extra={"../evil.py": "x"})
        info, why = hotfix.validate_zip(target)
        self.assertEqual(info, {})
        self.assertIn("不安全", why)

    def test_stage_extracts(self):
        staged, why = hotfix.stage(self.good_zip())
        self.assertEqual(why, "")
        self.assertTrue((staged / "vff" / "config.py").is_file())
        self.assertTrue((staged / "pack.json").is_file())
        # 再来一次：旧的暂存目录要被清掉
        (staged / "junk.txt").write_text("x", encoding="utf-8")
        staged2, _why = hotfix.stage(self.good_zip("pack2.zip"))
        self.assertEqual(staged, staged2)
        self.assertFalse((staged2 / "junk.txt").exists())

    def test_state_reports(self):
        self.put_pack()
        purer_boot.install()
        info = hotfix.state()
        self.assertTrue(info["active"])
        self.assertEqual(info["effective_version"], "1.2.1")
        self.assertEqual(info["exe_version"], "1.2.0")
        # 清掉包文件：即使 finder 还挂着，文件没了 state 也应如实反映
        hotfix.purge_files()
        self.assertFalse(purer_boot.pack_dir().exists())


class TestReleaseAssets(unittest.TestCase):
    def test_code_asset_picked(self):
        data = {"assets": [
            {"name": "Purer.exe", "browser_download_url": "u1", "size": 10,
             "digest": "sha256:a"},
            {"name": "Purer-code-1.2.1.zip", "browser_download_url": "u2",
             "size": 20, "digest": "sha256:b"},
        ]}
        code = code_asset_from_json(data)
        self.assertEqual(code["url"], "u2")
        self.assertEqual(code["size"], 20)
        self.assertEqual(code["digest"], "sha256:b")

    def test_no_code_asset(self):
        self.assertEqual(code_asset_from_json({"assets": []}), {})

    def test_result_carries_both(self):
        body = json.dumps({
            "tag_name": "v1.2.1",
            "html_url": "https://example.invalid/r",
            "assets": [
                {"name": "Purer.exe", "browser_download_url": "u1",
                 "size": 1000, "digest": "sha256:a"},
                {"name": "Purer-code-1.2.1.zip", "browser_download_url": "u2",
                 "size": 200, "digest": "sha256:b"},
            ],
        })
        result = result_from_json(body)
        self.assertEqual(result.tag, "v1.2.1")
        self.assertTrue(result.can_install())
        self.assertTrue(result.can_hotfix())
        self.assertEqual(result.code_size, 200)

    def test_state_uses_effective_version(self):
        # exe 1.2.0 跑着 1.2.1 的热补丁 → 再看 v1.2.1 就不算新版本
        result = CheckResult(True, "v1.2.1", "")
        with mock.patch("vff.update.effective_version", return_value="1.2.1"):
            self.assertEqual(result.state(), "latest")
        with mock.patch("vff.update.effective_version", return_value="1.2.0"):
            self.assertEqual(result.state(), "stale")


if __name__ == "__main__":
    unittest.main()
