# -*- coding: utf-8 -*-
"""纯函数单测（不需要 ffmpeg、不需要 Qt、不碰真实缓存）。

运行：
    python -m unittest discover -s tests -v
    python tests\\test_pure.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vff import cache, ffmpeg  # noqa: E402
from vff.media import find_videos, parse_probe_json, rate, thumb_size_for  # noqa: E402
from vff.util import (fmt_tc, fmt_tc_compact, human_size, parse_time_arg,  # noqa: E402
                      safe_filename)
from vff.version import is_newer, normalize, parse_version, state_for  # noqa: E402
from vff.update import CheckResult, curl_path, pick_proxy  # noqa: E402


class TestTimecode(unittest.TestCase):
    def test_fmt_tc(self):
        self.assertEqual(fmt_tc(0), "00:00:00.000")
        self.assertEqual(fmt_tc(1.5), "00:00:01.500")
        self.assertEqual(fmt_tc(3661.25), "01:01:01.250")
        self.assertEqual(fmt_tc(-5), "00:00:00.000")
        self.assertEqual(fmt_tc(None), "--:--:--")

    def test_compact_is_filename_safe(self):
        self.assertEqual(fmt_tc_compact(3661.25), "01-01-01.250")
        self.assertNotIn(":", fmt_tc_compact(123.456))

    def test_parse_time_arg(self):
        self.assertAlmostEqual(parse_time_arg("12.5"), 12.5)
        self.assertAlmostEqual(parse_time_arg("00:01:23.456"), 83.456)
        self.assertAlmostEqual(parse_time_arg("1:23"), 83.0)
        self.assertAlmostEqual(parse_time_arg("0"), 0.0)
        with self.assertRaises(ValueError):
            parse_time_arg("-1")
        with self.assertRaises(ValueError):
            parse_time_arg("1:2:3:4")
        with self.assertRaises(ValueError):
            parse_time_arg("")


class TestFilenames(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(safe_filename('a:b*c?d"e'), "a-b-c-d-e")
        self.assertEqual(safe_filename("  name.  "), "name")
        self.assertEqual(safe_filename("///"), "output")
        self.assertEqual(safe_filename("a///b"), "a-b")
        self.assertEqual(safe_filename("中文 名字"), "中文 名字")

    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1536), "1.5 KB")
        self.assertEqual(human_size(5 * 1024 ** 2), "5.0 MB")


class TestProgressParsing(unittest.TestCase):
    def test_us(self):
        parsed = ffmpeg.parse_progress_line("out_time_us=2500000")
        self.assertEqual(parsed["t"], 2.5)
        self.assertIsNone(parsed["pct"])

    def test_ms_is_also_microseconds(self):
        # ffmpeg 的 out_time_ms 实际上也是微秒（历史遗留），必须按微秒算
        parsed = ffmpeg.parse_progress_line("out_time_ms=2500000")
        self.assertEqual(parsed["t"], 2.5)

    def test_clock_format(self):
        parsed = ffmpeg.parse_progress_line("out_time=01:02:03.500000")
        self.assertAlmostEqual(parsed["t"], 3723.5)

    def test_speed_and_end(self):
        self.assertEqual(ffmpeg.parse_progress_line("speed=2.5x")["speed"], "2.5x")
        self.assertEqual(ffmpeg.parse_progress_line("progress=end")["type"], "end")
        self.assertIsNone(ffmpeg.parse_progress_line("frame=42"))
        self.assertIsNone(ffmpeg.parse_progress_line("")
                          )

    def test_with_pct(self):
        parsed = ffmpeg.with_pct(ffmpeg.parse_progress_line("out_time_us=5000000"), 10)
        self.assertAlmostEqual(parsed["pct"], 0.5)
        # 未知/0 时长不能炸，也不能算出百分比
        self.assertIsNone(ffmpeg.with_pct(ffmpeg.parse_progress_line("out_time_us=5"), None)["pct"])
        self.assertIsNone(ffmpeg.with_pct(ffmpeg.parse_progress_line("out_time_us=5"), 0)["pct"])
        # 越界要夹紧
        self.assertEqual(ffmpeg.with_pct(ffmpeg.parse_progress_line("out_time_us=99000000"), 10)["pct"], 1.0)
        self.assertEqual(ffmpeg.with_pct(ffmpeg.parse_progress_line("out_time_us=0"), 10)["pct"], 0.0)


class TestPtsParsing(unittest.TestCase):
    def test_frames(self):
        text = json.dumps({"frames": [
            {"pts_time": "2.0", "flags": "K_"},
            {"pts_time": "1.0", "flags": "__"},
            {"pts_time": "1.5", "flags": "_D"},
        ]})
        parsed = ffmpeg.parse_pts_json(text, "frames")
        self.assertEqual([p["t"] for p in parsed], [1.0, 1.5, 2.0])
        self.assertEqual([p["keyframe"] for p in parsed], [False, False, True])

    def test_packets_and_bad_input(self):
        text = json.dumps({"packets": [
            {"pts_time": "0.4", "flags": "K_"},
            {"pts_time": "not-a-number", "flags": "__"},
            {"pts_time": "0.8", "flags": "__"},
        ]})
        self.assertEqual([p["t"] for p in ffmpeg.parse_pts_json(text, "packets")], [0.4, 0.8])
        self.assertEqual(ffmpeg.parse_pts_json("", "packets"), [])
        self.assertEqual(ffmpeg.parse_pts_json("{oops", "packets"), [])
        self.assertEqual(ffmpeg.parse_pts_json("{}", "packets"), [])

    def test_snap_and_closest(self):
        kfs = [0.0, 2.0, 4.0, 6.0]
        self.assertEqual(ffmpeg.snap_before(kfs, 5.0), 4.0)
        self.assertEqual(ffmpeg.snap_before(kfs, 4.0), 4.0)      # 正好在关键帧
        self.assertEqual(ffmpeg.snap_before(kfs, 4.0005), 4.0)   # 容差内
        self.assertEqual(ffmpeg.snap_before(kfs, 0.0), 0.0)      # 第一个就是 0
        self.assertIsNone(ffmpeg.snap_before(kfs, -1))
        self.assertIsNone(ffmpeg.snap_before([], 3))
        points = [{"t": 1.0}, {"t": 2.0}, {"t": 9.0}]
        self.assertEqual(ffmpeg.closest(points, 2.6)["t"], 2.0)
        self.assertIsNone(ffmpeg.closest([], 1.0))


class TestArgBuilders(unittest.TestCase):
    def test_threads_never_eats_all_cores(self):
        self.assertEqual(ffmpeg.threads_for(4), 2)
        self.assertEqual(ffmpeg.threads_for(16), 8)
        self.assertEqual(ffmpeg.threads_for(2), 2)
        self.assertGreaterEqual(ffmpeg.threads_for(1), 2)

    def test_extract_args(self):
        args = ffmpeg.build_extract_args(Path("C:/v/a.mp4"), 24, 320, 180,
                                         Path("C:/out"), threads=4)
        joined = " ".join(args)
        # 默认写法用单引号保护逗号，**不留反斜杠**：
        # 有些 ffmpeg 构建会把 `\,` 原样塞进表达式 → "Missing ')'" → 抽帧全失败
        self.assertIn("select='not(mod(n,24))'", joined)
        self.assertNotIn("\\", args[args.index("-vf") + 1])
        self.assertIn("scale=320:180:force_original_aspect_ratio=decrease", joined)
        self.assertIn("-threads 4", joined)
        self.assertIn("-map 0:v:0", joined)
        self.assertEqual(args[-1], "C:\\out\\f_%06d.jpg")
        self.assertIn("-progress pipe:1", joined)

    def test_extract_variants_are_ordered_and_deduped(self):
        path, out = Path("C:/v/a.mp4"), Path("C:/out")
        variants = ffmpeg.build_extract_variants(path, 24, 320, 180, out, threads=4)
        self.assertEqual(variants[0],
                         ffmpeg.build_extract_args(path, 24, 320, 180, out, threads=4))
        self.assertGreater(len(variants), 1)
        self.assertEqual(len({tuple(v) for v in variants}), len(variants))
        joined = [" ".join(v) for v in variants]
        # 备选里既有「反斜杠转义」写法，也有「fps 兜底」写法（完全不含转义/逗号）
        self.assertTrue(any("not(mod(n\\,24))" in j for j in joined))
        self.assertTrue(any("fps=" in j for j in joined))
        # 老 ffmpeg 不认 -fps_mode 时退到 -vsync 0
        self.assertTrue(any("-vsync 0" in j for j in joined))
        for args in variants:
            self.assertEqual(args[-1], "C:\\out\\f_%06d.jpg")

    def test_fractional_step_uses_fps_filter(self):
        args = ffmpeg.build_extract_args(Path("a.mp4"), 23.98, 320, 180, Path("C:/out"),
                                         src_fps=23.976)
        joined = " ".join(args)
        self.assertIn("fps=0.999833", joined)
        self.assertNotIn("select=", joined)

    def test_command_rejected_only_for_syntax_problems(self):
        # 某电脑上格式工厂自带 ffmpeg 的真实报错（2026-11-11）
        bad = ("[Parsed_select_0 @ 1] [Eval @ 2] Missing ')' or too many args in "
               "'mod(n\\,24))'\n[Parsed_select_0 @ 1] Error while parsing expression "
               "'not(mod(n\\,24))'\n[AVFilterGraph @ 3] Error initializing filters")
        self.assertTrue(ffmpeg.command_rejected(bad))
        self.assertTrue(ffmpeg.command_rejected("Unrecognized option 'fps_mode'."))
        self.assertTrue(ffmpeg.command_rejected("No such filter: '24))'"))
        # 内容本身的问题不重试
        self.assertFalse(ffmpeg.command_rejected("Invalid data found when processing input"))
        self.assertFalse(ffmpeg.command_rejected("No space left on device"))
        self.assertFalse(ffmpeg.command_rejected(""))

    def test_preview_args_clamps_negative_time(self):
        args = ffmpeg.build_preview_args(Path("a.mp4"), -3.0, Path("p.jpg"))
        self.assertEqual(args[args.index("-ss") + 1], "0.000")
        self.assertIn("scale='min(1280,iw)':-2", " ".join(args))

    def test_export_args_order_and_clamps(self):
        args = ffmpeg.build_export_args(Path("a.mp4"), 10.0, 5.25, Path("o.mp4"))
        # -ss 必须在 -i 之前（关键帧快速定位），-t 必须在 -i 之后
        self.assertLess(args.index("-ss"), args.index("-i"))
        self.assertGreater(args.index("-t"), args.index("-i"))
        self.assertEqual(args[args.index("-t") + 1], "5.250000")
        joined = " ".join(args)
        self.assertIn("-c copy", joined)
        # 故意不加 -avoid_negative_ts make_zero：它会让 -t 被放大（拼接后时长翻倍）
        self.assertNotIn("-avoid_negative_ts", joined)
        self.assertIn("-map 0:v?", joined)
        self.assertIn("-map 0:a?", joined)
        self.assertEqual(args[-1], "o.mp4")
        self.assertEqual(args[-2], "-y")

    def test_export_args_never_negative(self):
        args = ffmpeg.build_export_args(Path("a.mp4"), -5, -1, Path("o.mp4"))
        self.assertEqual(args[args.index("-ss") + 1], "0.000000")
        self.assertEqual(args[args.index("-t") + 1], "0.001000")

    def test_keyframe_and_packet_ranges(self):
        kf = ffmpeg.build_keyframes_args(Path("a.ts"), 3.0, 9.5)
        self.assertLess(kf.index("-skip_frame"), kf.index("-i"))
        self.assertEqual(kf[kf.index("-read_intervals") + 1], "3.000%9.500")
        pk = ffmpeg.build_packets_args(Path("a.ts"), -4.0, 2.0)
        self.assertEqual(pk[pk.index("-read_intervals") + 1], "0.000%2.000")

    def test_quote_and_format_command(self):
        self.assertEqual(ffmpeg.quote_arg("plain"), "plain")
        self.assertEqual(ffmpeg.quote_arg("a b"), '"a b"' if os.name == "nt" else "'a b'")
        if os.name == "nt":
            self.assertEqual(ffmpeg.quote_arg('say "hi"'), '"say ""hi"""')
        cmd = ffmpeg.format_command(["-i", "C:/a b/c.mp4", "-y", "out.mp4"])
        self.assertTrue(cmd.startswith("ffmpeg "))
        self.assertIn("C:/a b/c.mp4", cmd)


class TestProbeParsing(unittest.TestCase):
    def test_normal(self):
        data = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1920,
                 "height": 1080, "avg_frame_rate": "30000/1001",
                 "nb_frames": "300", "duration": "10.01", "start_time": "0.0"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "10.01", "start_time": "0.0"},
        }
        info = parse_probe_json(data, Path("x.mp4"))
        self.assertAlmostEqual(info.fps, 29.97, places=2)
        self.assertEqual(info.frames, 300)
        self.assertEqual(info.resolution, "1920×1080")
        self.assertEqual(info.audio, "aac")
        self.assertAlmostEqual(info.frame_duration, 1 / 29.97, places=3)

    def test_frames_estimated_from_duration(self):
        data = {"streams": [{"codec_type": "video", "width": 640, "height": 360,
                             "avg_frame_rate": "25/1", "duration": "4.0"}]}
        info = parse_probe_json(data, Path("x.mp4"))
        self.assertEqual(info.frames, 100)

    def test_start_time_offset_and_negative_clamp(self):
        data = {"streams": [{"codec_type": "video", "avg_frame_rate": "25",
                             "duration": "4", "start_time": "10.0"}]}
        info = parse_probe_json(data, Path("x.ts"))
        self.assertEqual(info.start_time, 10.0)
        # 页面时间码是相对起点 -> ffprobe 的 -read_intervals 要加偏移
        self.assertEqual(info.absolute_time(1.0), 11.0)
        self.assertEqual(info.absolute_time(-1.0), 10.0)
        # ... 而 ffmpeg 的 -ss 要的是相对值，必须能转回来（.ts 二次偏移的回归测试）
        self.assertEqual(info.relative_time(11.0), 1.0)
        self.assertAlmostEqual(info.relative_time(info.absolute_time(2.5)), 2.5, places=6)
        self.assertEqual(info.relative_time(0.0), 0.0)

        data["streams"][0]["start_time"] = "-0.02"
        neg = parse_probe_json(data, Path("x.mp4"))
        self.assertEqual(neg.start_time, 0.0)
        self.assertEqual(neg.absolute_time(1.0), 1.0)

    def test_no_video_stream(self):
        with self.assertRaises(ValueError):
            parse_probe_json({"streams": []}, Path("x.mp4"))
        with self.assertRaises(ValueError):
            parse_probe_json({}, Path("x.mp4"))

    def test_rate(self):
        self.assertAlmostEqual(rate("30000/1001"), 29.97, places=2)
        self.assertEqual(rate("25"), 25.0)
        self.assertEqual(rate("0/0"), 0.0)
        self.assertEqual(rate(None), 0.0)
        self.assertEqual(rate("abc"), 0.0)


class TestThumbSize(unittest.TestCase):
    def test_landscape(self):
        self.assertEqual(thumb_size_for(1920, 1080, 320), (320, 180))

    def test_portrait(self):
        self.assertEqual(thumb_size_for(1080, 1920, 240), (135, 240))

    def test_square_and_unknown(self):
        self.assertEqual(thumb_size_for(100, 100, 320), (320, 320))
        self.assertEqual(thumb_size_for(0, 0, 320), (320, 180))

    def test_ultrawide_keeps_short_side_usable(self):
        tw, th = thumb_size_for(3840, 800, 320)
        self.assertGreaterEqual(min(tw, th), 48)


class TestFindVideos(unittest.TestCase):
    def test_recursive_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sub").mkdir()
            for name in ("a.mp4", "b.MKV", "c.txt", "sub/d.ts"):
                (root / name).write_bytes(b"x")
            found = find_videos(root)
            self.assertEqual(sorted(p.name for p in found), ["a.mp4", "b.MKV", "d.ts"])
            self.assertEqual(len(find_videos(root, limit=1)), 1)

    def test_missing_dir_is_safe(self):
        self.assertEqual(find_videos(Path("Z:/definitely/not/here")), [])


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("PURER_CACHE_DIR")
        os.environ["PURER_CACHE_DIR"] = self._tmp.name
        self.video = Path(self._tmp.name) / "v.mp4"
        self.video.write_bytes(b"0123456789")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PURER_CACHE_DIR", None)
        else:
            os.environ["PURER_CACHE_DIR"] = self._old
        self._tmp.cleanup()

    def test_key_is_stable_and_sensitive(self):
        a = cache.cache_dir_for(self.video, 24, 320, 180)
        b = cache.cache_dir_for(self.video, 24, 320, 180)
        self.assertEqual(a, b)
        self.assertNotEqual(a, cache.cache_dir_for(self.video, 12, 320, 180))
        self.assertNotEqual(a, cache.cache_dir_for(self.video, 24, 480, 270))
        # 文件改动（mtime/size）要换 key，否则会拿到旧帧
        time.sleep(0.01)
        self.video.write_bytes(b"0123456789!")
        self.assertNotEqual(a, cache.cache_dir_for(self.video, 24, 320, 180))
        self.assertTrue(str(a).startswith(self._tmp.name))

    def test_clean_cache_only_removes_old(self):
        root = Path(self._tmp.name)
        old = root / "old"
        new = root / "new"
        old.mkdir()
        new.mkdir()
        stale = time.time() - 8 * 86400
        os.utime(old, (stale, stale))
        removed = cache.clean_cache(days=7)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())


class TestFfmpegLocation(unittest.TestCase):
    def test_ini_section(self):
        from vff import config
        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "ffmpeg.ini"
            ini.write_text("[ffmpeg]\ndir=D:\\\\tools\\\\bin\n", encoding="utf-8")
            original = config.app_dir
            config.app_dir = lambda: Path(tmp)
            try:
                self.assertEqual(config.read_ffmpeg_dir_from_ini(), Path("D:\\tools\\bin"))
            finally:
                config.app_dir = original

    def test_ini_single_line_and_missing(self):
        from vff import config
        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "ffmpeg.ini"
            ini.write_text("# 注释\nD:\\ff\\bin\n", encoding="utf-8")
            original = config.app_dir
            config.app_dir = lambda: Path(tmp)
            try:
                self.assertEqual(config.read_ffmpeg_dir_from_ini(), Path("D:\\ff\\bin"))
                ini.unlink()
                self.assertIsNone(config.read_ffmpeg_dir_from_ini())
            finally:
                config.app_dir = original

    def test_env_overrides_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg.exe"
            fake.write_bytes(b"")
            old = os.environ.get("PURER_FFMPEG_DIR")
            os.environ["PURER_FFMPEG_DIR"] = tmp
            try:
                found, source = ffmpeg.find_tool("ffmpeg")
                self.assertEqual(found, fake)
                self.assertIn("PURER_FFMPEG_DIR", source)
            finally:
                if old is None:
                    os.environ.pop("PURER_FFMPEG_DIR", None)
                else:
                    os.environ["PURER_FFMPEG_DIR"] = old


class TestVersions(unittest.TestCase):
    """检查更新：只比数字；查不到绝不能报「已是最新」。"""

    def test_parse(self):
        self.assertEqual(parse_version("v1.1.4"), (1, 1, 4))
        self.assertEqual(parse_version("Purer.1.2"), (1, 2))
        self.assertEqual(parse_version("1.1.4-beta.2"), (1, 1, 4))
        self.assertEqual(parse_version("  v9 "), (9,))
        self.assertEqual(parse_version(""), ())
        self.assertEqual(parse_version(None), ())
        self.assertEqual(parse_version("latest"), ())

    def test_normalize(self):
        self.assertEqual(normalize("Purer.1.1.4"), "v1.1.4")
        self.assertEqual(normalize("v1.1.3"), "v1.1.3")
        self.assertEqual(normalize("无聊"), "")

    def test_is_newer(self):
        self.assertTrue(is_newer("v1.1.4", "1.1.3"))
        self.assertTrue(is_newer("v1.10.0", "1.9.9"))
        self.assertFalse(is_newer("v1.1.3", "1.1.3"))
        self.assertFalse(is_newer("1.1.4", "1.1.4.0"))     # 缺位按 0 补齐
        self.assertFalse(is_newer("v1.0.9", "1.1.0"))
        self.assertFalse(is_newer("", "1.1.3"))
        self.assertFalse(is_newer("oops", "1.1.3"))

    def test_state_for_three_colors(self):
        self.assertEqual(state_for(True, "v1.2.0", "1.1.4"), "stale")
        self.assertEqual(state_for(True, "v1.1.4", "1.1.4"), "latest")
        self.assertEqual(state_for(True, "v1.1.3", "1.1.4"), "latest")
        self.assertEqual(state_for(False, "", "1.1.4"), "error")
        self.assertEqual(state_for(True, "", "1.1.4"), "error")      # 拿不到 tag
        self.assertEqual(state_for(False, "v9.9", "1.1.4"), "error")


class TestUpdateRoutes(unittest.TestCase):
    """检查更新：代理串解析 + 三色状态映射（传输层不打网络）。"""

    def test_pick_proxy(self):
        self.assertEqual(pick_proxy("127.0.0.1:7897"), "127.0.0.1:7897")
        self.assertEqual(pick_proxy("http=10.0.0.1:8080;https=10.0.0.2:8443"),
                         "10.0.0.2:8443")
        self.assertEqual(pick_proxy("http=10.0.0.1:8080"), "10.0.0.1:8080")
        self.assertEqual(pick_proxy("socks=10.0.0.3:1080"), "10.0.0.3:1080")
        self.assertEqual(pick_proxy(""), "")
        self.assertEqual(pick_proxy(None), "")

    def test_result_state(self):
        self.assertEqual(CheckResult(True, "v2.0.0").state("1.1.4"), "stale")
        self.assertEqual(CheckResult(True, "v1.1.4").state("1.1.4"), "latest")
        self.assertEqual(CheckResult(False, "", "", "断网").state("1.1.4"), "error")
        self.assertEqual(CheckResult(True, "").state("1.1.4"), "error")

    def test_curl_is_wired(self):
        """Windows 自带 curl，是「检查更新」的首选通道。"""
        path = curl_path()
        self.assertTrue(path.lower().endswith("curl.exe"), path)


class TestFfmpegGuide(unittest.TestCase):
    """没有 ffmpeg 时的兜底：扫盘、目录识别、写 ini。"""

    @staticmethod
    def _pair(folder: Path, names=("ffmpeg.exe", "ffprobe.exe")) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"fake")
        return folder

    def test_scan_finds_nested_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deep = self._pair(root / "Software" / "Other"
                              / "ffmpeg-7.1-full_build" / "bin")
            found = ffmpeg.scan_for_ffmpeg(roots=[root], max_depth=4, budget=5)
            self.assertEqual(found, deep)

    def test_scan_needs_both_exes(self):
        """只有 ffmpeg.exe 的地方很多（播放器/下载工具），不能当成命中。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pair(root / "Free Download Manager", names=("ffmpeg.exe",))
            self.assertIsNone(ffmpeg.scan_for_ffmpeg(roots=[root], max_depth=3))

    def test_scan_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pair(root / "a" / "b" / "c" / "d" / "bin")
            self.assertIsNone(ffmpeg.scan_for_ffmpeg(roots=[root], max_depth=2))
            self.assertIsNotNone(ffmpeg.scan_for_ffmpeg(roots=[root], max_depth=5))

    def test_check_tool_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unzipped = root / "ffmpeg-7.1"
            bin_dir = self._pair(unzipped / "bin")
            self.assertEqual(ffmpeg.check_tool_dir(bin_dir), bin_dir)
            self.assertEqual(ffmpeg.check_tool_dir(unzipped), bin_dir)
            # 选到上一级也能顺着 */bin 找到
            self.assertEqual(ffmpeg.check_tool_dir(root), bin_dir)
            empty = root / "empty"
            empty.mkdir()
            self.assertIsNone(ffmpeg.check_tool_dir(empty))

    def test_write_ini_roundtrip(self):
        from vff import config
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            bin_dir = self._pair(Path(tmp) / "ffmpeg" / "bin")
            old_config, old_ff = config.app_dir, ffmpeg.app_dir
            config.app_dir = lambda: app
            ffmpeg.app_dir = lambda: app
            try:
                ini = ffmpeg.write_ffmpeg_ini(bin_dir)
                self.assertTrue(ini.is_file())
                self.assertEqual(config.read_ffmpeg_dir_from_ini(), bin_dir)
                found, source = ffmpeg.find_tool("ffmpeg")
                self.assertEqual(found, bin_dir / "ffmpeg.exe")
                self.assertEqual(source, "ffmpeg.ini")
            finally:
                config.app_dir, ffmpeg.app_dir = old_config, old_ff

    def test_refresh_path_keeps_entries(self):
        before = os.environ.get("PATH", "")
        after = ffmpeg.refresh_path()
        self.assertTrue(after)
        old_items = {p.strip().lower().rstrip("\\")
                     for p in before.split(os.pathsep) if p.strip()}
        new_items = {p.strip().lower().rstrip("\\")
                     for p in after.split(os.pathsep) if p.strip()}
        self.assertTrue(old_items <= new_items)


if __name__ == "__main__":
    unittest.main(verbosity=2)
