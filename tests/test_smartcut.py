# -*- coding: utf-8 -*-
"""智能切割（帧精确）端到端测试。

用"每秒一种纯色"的片源，看切出来的第一帧到底是不是你点的那一帧：

    python -m unittest tests.test_smartcut -v

需要 ffmpeg；没有就自动跳过。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vff import ffmpeg, smartcut  # noqa: E402
from vff.media import MediaInfo  # noqa: E402

HAS_FFMPEG = ffmpeg.Tools.locate()


def make_colors(path: Path) -> None:
    """0-1s 红 / 1-2s 绿 / 2-3s 蓝 / 3-4s 黄，每 0.5 秒一个关键帧。"""
    colors = ["red", "green", "blue", "yellow"]
    inputs = []
    for color in colors:
        inputs += ["-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=30:d=1"]
    graph = "".join(f"[{i}:v]" for i in range(len(colors)))
    graph += f"concat=n={len(colors)}:v=1:a=0[v]"
    subprocess.run(
        [str(ffmpeg.Tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         *inputs, "-filter_complex", graph, "-map", "[v]",
         "-c:v", "libx264", "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, timeout=120)


def rgb_at(path: Path, t: float) -> tuple[int, int, int]:
    raw = subprocess.run(
        [str(ffmpeg.Tools.ffmpeg), "-v", "error",
         "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=1:1:160:120,format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, timeout=60).stdout
    if len(raw) < 3:
        raise AssertionError(f"取不到 {path.name} @ {t}s 的画面")
    return raw[0], raw[1], raw[2]


def is_blue(rgb) -> bool:
    r, g, b = rgb
    return b > 150 and r < 100 and g < 100


def is_yellow(rgb) -> bool:
    r, g, b = rgb
    return r > 150 and g > 150 and b < 100


def run_stages(stages) -> None:
    for stage in stages:
        result = subprocess.run([str(ffmpeg.Tools.ffmpeg), *stage["args"]],
                                capture_output=True, timeout=300)
        if result.returncode != 0:
            raise AssertionError(
                f"阶段失败 {stage['label']}: "
                + result.stderr.decode("utf-8", "replace")[-400:])


@unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg")
class TestSmartCut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.src = cls.dir / "colors.mp4"
        make_colors(cls.src)
        cls.info = ffmpeg.probe(cls.src)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_plan_picks_next_keyframe(self):
        out = self.dir / "plan.mp4"
        plan = smartcut.plan(self.src, self.info, 2.2, 3.5, out, exact=True,
                             lossless_start=2.0)
        self.assertEqual(plan.mode, "smart")
        self.assertAlmostEqual(plan.start, 2.2, places=3)
        self.assertAlmostEqual(plan.head_end, 2.5, places=2)   # 2.2 之后的下一个关键帧
        self.assertAlmostEqual(plan.head_duration, 0.3, places=2)
        self.assertFalse(plan.reason)

    def test_unsupported_codec_falls_back(self):
        out = self.dir / "x.mp4"
        fake = MediaInfo(Path("x.mp4"), 10, 30, 320, 240, 300, codec="theora")
        plan = smartcut.plan(Path("x.mp4"), fake, 1.0, 3.0, out, exact=True)
        self.assertEqual(plan.mode, "copy")
        self.assertIn("重编码器", plan.reason)

    def test_multi_video_stream_falls_back(self):
        out = self.dir / "x.mp4"
        fake = MediaInfo(Path("x.mp4"), 10, 30, 320, 240, 300, codec="h264",
                         video_streams=2)
        plan = smartcut.plan(Path("x.mp4"), fake, 1.0, 3.0, out, exact=True)
        self.assertEqual(plan.mode, "copy")
        self.assertIn("视频流", plan.reason)

    def test_bad_container_falls_back(self):
        fake = MediaInfo(Path("x.mp4"), 10, 30, 320, 240, 300, codec="h264")
        plan = smartcut.plan(Path("x.mp4"), fake, 1.0, 3.0,
                             self.dir / "x.rmvb", exact=True)
        self.assertEqual(plan.mode, "copy")
        self.assertIn("容器", plan.reason)

    def test_smart_cut_is_frame_accurate(self):
        """核心：起点 2.2s（蓝段中间），第一帧必须是蓝；+0.9s 已经到黄。"""
        out = self.dir / "smart.mp4"
        plan = smartcut.plan(self.src, self.info, 2.2, 3.5, out, exact=True,
                             lossless_start=2.0)
        stages, temp = smartcut.build_stages(self.src, self.info, out, plan)
        try:
            run_stages(stages)
            good, why = smartcut.verify(out, plan.duration,
                                        self.info.frame_duration, plan.head_duration)
            self.assertTrue(good, why)

            info = ffmpeg.probe(out)
            # 1.3 秒（不是无损的 1.5 秒）——起点真的从 2.0 挪到了 2.2
            self.assertAlmostEqual(info.duration, 1.3, delta=0.12)
            self.assertTrue(is_blue(rgb_at(out, 0.05)), rgb_at(out, 0.05))
            self.assertTrue(is_blue(rgb_at(out, 0.7)), rgb_at(out, 0.7))
            # 3.1s = 黄（无损模式这里还是 2.9s 的蓝）
            self.assertTrue(is_yellow(rgb_at(out, 0.9)), rgb_at(out, 0.9))
        finally:
            smartcut.cleanup(temp)

    def test_lossless_is_one_gop_early(self):
        """对照组：无损从 2.0s 开始，+0.9s 仍是蓝，时长 1.5 秒。"""
        out = self.dir / "copy.mp4"
        plan = smartcut.plan(self.src, self.info, 2.2, 3.5, out, exact=False,
                             lossless_start=2.0)
        self.assertEqual(plan.mode, "copy")
        stages, temp = smartcut.build_stages(self.src, self.info, out, plan)
        try:
            run_stages(stages)
            info = ffmpeg.probe(out)
            self.assertAlmostEqual(info.duration, 1.5, delta=0.12)
            self.assertTrue(is_blue(rgb_at(out, 0.05)), rgb_at(out, 0.05))
            self.assertTrue(is_blue(rgb_at(out, 0.9)), rgb_at(out, 0.9))
        finally:
            smartcut.cleanup(temp)


    def test_ts_needs_normalize_and_cuts_frame_accurate(self):
        """真实场景：.ts 的 start_time != 0，必须先无损转封装再切。"""
        ts = self.dir / "colors.ts"
        subprocess.run([str(ffmpeg.Tools.ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-y", "-i", str(self.src), "-c", "copy", "-f", "mpegts", str(ts)],
                       check=True, timeout=120)
        ts_info = ffmpeg.probe(ts)
        self.assertTrue(smartcut.needs_normalize(ts_info, ts))
        self.assertGreater(ts_info.start_time, 0.5)

        norm_dir = Path(tempfile.mkdtemp(dir=str(self.dir)))
        norm = norm_dir / "norm.mkv"
        run_stages([{"args": ffmpeg.build_normalize_args(ts, norm),
                     "duration": ts_info.duration, "label": "norm"}])
        info = ffmpeg.probe(norm)
        self.assertLess(info.start_time, 0.2)   # 时间戳被拉回起点附近

        out = self.dir / "ts_smart.mp4"
        plan = smartcut.plan(norm, info, 2.2, 3.5, out, exact=True,
                             lossless_start=2.0)
        self.assertEqual(plan.mode, "smart")
        stages, temp = smartcut.build_stages(norm, info, out, plan)
        try:
            run_stages(stages)
            good, why = smartcut.verify(out, plan.duration, info.frame_duration,
                                        plan.head_duration)
            self.assertTrue(good, why)
            out_info = ffmpeg.probe(out)
            self.assertAlmostEqual(out_info.duration, 1.3, delta=0.15)
            self.assertTrue(is_blue(rgb_at(out, 0.05)), rgb_at(out, 0.05))
            self.assertTrue(is_yellow(rgb_at(out, 0.9)), rgb_at(out, 0.9))
        finally:
            smartcut.cleanup(temp + [norm_dir])


if __name__ == "__main__":
    unittest.main(verbosity=2)
