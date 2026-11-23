# -*- coding: utf-8 -*-
"""会话快照 + 「等旧进程退出」的单测（都不依赖 Qt，能无界面跑）。

    python -m unittest tests.test_session -v
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vff import session
from vff.config import DEFAULT_DISPLAY_SIDE, DEFAULT_STEP
from vff.selfupdate import pid_alive, wait_for_pid


def _tab(path="D:/v/a.mp4", **extra) -> dict:
    data = {"path": path, "step": 24, "mode": "step", "threshold": None,
            "display_side": 160, "sidebar_width": 372,
            "cursor_frame": 120, "in_frame": 100, "out_frame": 200,
            "scroll_row": 3,
            "segments": [{"t_in": 1.0, "t_end": 2.5, "name": "开场"}],
            "exports": [{"path": "D:/out/a.mp4", "at": 1.0, "size": 1024,
                         "kind": "帧精确"}]}
    data.update(extra)
    return data


class TestSessionFile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "session.json"

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------ 读写
    def test_roundtrip(self):
        payload = {"version": 1, "saved_at": 123.0, "reason": "update",
                   "window": {"geometry": "QUJD", "maximized": False},
                   "tabs": [_tab(), _tab("D:/v/b.mp4", cursor_frame=None)],
                   "current": 1}
        self.assertTrue(session.save(payload, self.path))
        self.assertTrue(self.path.is_file())
        back = session.load(self.path)
        self.assertEqual(len(back["tabs"]), 2)
        self.assertEqual(back["tabs"][0]["path"], "D:/v/a.mp4")
        self.assertEqual(back["tabs"][0]["segments"][0]["name"], "开场")
        self.assertEqual(back["tabs"][1]["cursor_frame"], None)
        self.assertEqual(back["current"], 1)
        self.assertEqual(back["reason"], "update")
        self.assertEqual(back["window"]["geometry"], "QUJD")

    def test_missing_file_is_empty(self):
        data = session.load(self.tmp / "nope.json")
        self.assertEqual(data["tabs"], [])
        self.assertEqual(data["version"], session.SESSION_VERSION)

    def test_broken_json_is_empty(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(session.load(self.path)["tabs"], [])

    def test_not_an_object_is_empty(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(session.load(self.path)["tabs"], [])

    def test_clear(self):
        session.save({"tabs": [_tab()]}, self.path)
        self.assertTrue(self.path.is_file())
        self.assertTrue(session.clear(self.path))
        self.assertFalse(self.path.is_file())
        self.assertEqual(session.load(self.path)["tabs"], [])

    def test_env_override(self):
        with mock.patch.dict(os.environ,
                             {"PURER_SESSION_FILE": str(self.path)}):
            self.assertEqual(session.session_file(), self.path)

    def test_save_failure_returns_false(self):
        # 目标是一个目录 → 写不进去，必须返回 False 而不是抛异常
        self.assertFalse(session.save({"tabs": []}, self.tmp))

    # ------------------------------------------------------------ 收敛
    def test_sanitize_drops_bad_tabs(self):
        data = session.sanitize({
            "tabs": [None, 42, {"path": ""}, {"path": "   "},
                     {"path": "D:/v/ok.mp4"}],
            "current": 99,
        })
        self.assertEqual(len(data["tabs"]), 1)
        self.assertEqual(data["tabs"][0]["path"], "D:/v/ok.mp4")
        self.assertEqual(data["current"], 0)     # 越界收敛到最后一页

    def test_sanitize_clamps_values(self):
        data = session.sanitize({"tabs": [_tab(step="abc", display_side=9999,
                                               sidebar_width=-5,
                                               mode="weird",
                                               threshold=9.0)]})
        tab = data["tabs"][0]
        self.assertAlmostEqual(tab["step"], float(DEFAULT_STEP))
        self.assertEqual(tab["display_side"], DEFAULT_DISPLAY_SIDE)   # 越界 → 默认
        self.assertIsNone(tab["sidebar_width"])         # 越界 → 不恢复，用全局
        self.assertEqual(tab["mode"], "step")
        self.assertIsNone(tab["threshold"])

    def test_scene_tab_keeps_threshold(self):
        tab = session.sanitize({"tabs": [_tab(mode="scene", threshold=0.45)]})
        tab = tab["tabs"][0]
        self.assertEqual(tab["mode"], "scene")
        self.assertAlmostEqual(tab["threshold"], 0.45)

    def test_bad_segments_and_exports_dropped(self):
        data = session.sanitize({"tabs": [_tab(
            segments=[{"t_in": 2.0, "t_end": 1.0},      # 倒序
                      {"t_in": "x", "t_end": 3},        # 非数字
                      {"t_in": 1.0, "t_end": 2.0, "name": "ok"}],
            exports=[{}, {"path": ""}, {"path": "D:/out/b.mp4"}])]})
        tab = data["tabs"][0]
        self.assertEqual(len(tab["segments"]), 1)
        self.assertEqual(tab["segments"][0]["name"], "ok")
        self.assertEqual(len(tab["exports"]), 1)
        self.assertEqual(tab["exports"][0]["path"], "D:/out/b.mp4")

    def test_tab_limit(self):
        tabs = [_tab(f"D:/v/{i}.mp4") for i in range(session.MAX_TABS + 10)]
        data = session.sanitize({"tabs": tabs})
        self.assertEqual(len(data["tabs"]), session.MAX_TABS)

    def test_huge_geometry_dropped(self):
        data = session.sanitize({"window": {"geometry": "x" * 70000}})
        self.assertEqual(data["window"]["geometry"], "")

    def test_negative_frames_rejected(self):
        tab = session.sanitize({"tabs": [_tab(cursor_frame=-9)]})["tabs"][0]
        self.assertIsNone(tab["cursor_frame"])


class TestProcessWait(unittest.TestCase):
    def test_pid_alive_self(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_pid_alive_garbage(self):
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(None))
        self.assertFalse(pid_alive("abc"))

    def test_wait_for_missing_pid_is_instant(self):
        started = time.monotonic()
        self.assertTrue(wait_for_pid(0, timeout=5.0))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_wait_timeout_when_alive(self):
        started = time.monotonic()
        self.assertFalse(wait_for_pid(os.getpid(), timeout=0.2, interval=0.05))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_dead_pid_detected(self):
        # 起一个立刻退出的子进程，拿到它的 pid
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        self.assertFalse(pid_alive(proc.pid))
        self.assertTrue(wait_for_pid(proc.pid, timeout=2.0))


if __name__ == "__main__":
    unittest.main()
