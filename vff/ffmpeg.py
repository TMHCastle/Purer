# -*- coding: utf-8 -*-
"""ffmpeg / ffprobe 的定位、命令构造与输出解析。

设计约定（和 LosslessCut 学到的那条）：
  * 命令构造一律返回「参数列表」，不拼字符串；只有 format_command() 负责给人看。
  * 参数构造与输出解析都是纯函数，不依赖 Qt，可直接单测。
  * 所有子进程都从这里出去（UI 走 jobs.Job，CLI 走 run_hidden / stream_progress）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import (CREATE_NO_WINDOW, app_dir, bundle_dir, read_ffmpeg_dir_from_ini)
from .media import MediaInfo, parse_probe_json

FFMPEG_ENV = "PURER_FFMPEG_DIR"
_SENTINEL = object()


# --------------------------------------------------------------------------- #
# 定位
# --------------------------------------------------------------------------- #
def _candidate_dirs() -> list[tuple[Path, str]]:
    """按优先级返回 (目录, 来源说明)。"""
    out: list[tuple[Path, str]] = []
    env = os.environ.get(FFMPEG_ENV)
    if env:
        out.append((Path(env), f"环境变量 {FFMPEG_ENV}"))
    ini = read_ffmpeg_dir_from_ini()
    if ini is not None:
        out.append((ini, "ffmpeg.ini"))
    here = app_dir()
    out += [(here, "程序目录"), (here / "bin", "程序目录\\bin"),
            (here / "ffmpeg", "程序目录\\ffmpeg"),
            (here / "ffmpeg" / "bin", "程序目录\\ffmpeg\\bin")]
    bundle = bundle_dir()
    if bundle is not None:
        out += [(bundle, "打包资源"), (bundle / "bin", "打包资源\\bin")]
    return out


def find_tool(name: str, exe_name: str | None = None) -> tuple[Path | None, str]:
    """找 ffmpeg/ffprobe：环境变量 -> ffmpeg.ini -> 程序目录 -> PATH -> 常见位置。"""
    exe = exe_name or (name + (".exe" if os.name == "nt" else ""))
    for root, source in _candidate_dirs():
        try:
            candidate = root / exe
            if candidate.is_file():
                return candidate, source
        except OSError:
            continue
    found = shutil.which(name)
    if found:
        return Path(found), "PATH"
    # 常见安装位置（不再写死某台机器的路径）
    for base in (Path("C:/ffmpeg"), Path("C:/Program Files/ffmpeg"),
                 Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ffmpeg"):
        try:
            if base.is_dir():
                for candidate in sorted(base.glob("*/bin/" + exe)):
                    if candidate.is_file():
                        return candidate, str(base)
        except OSError:
            continue
    return None, "未找到"


class Tools:
    """全局工具定位结果（启动时 locate() 一次）。"""

    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    ffplay: Path | None = None
    ffmpeg_source: str = ""
    ffprobe_source: str = ""
    ffplay_source: str = ""

    @classmethod
    def locate(cls) -> bool:
        cls.ffmpeg, cls.ffmpeg_source = find_tool("ffmpeg")
        cls.ffprobe, cls.ffprobe_source = find_tool("ffprobe")
        cls.ffplay, cls.ffplay_source = find_tool("ffplay")
        return cls.ffmpeg is not None and cls.ffprobe is not None

    @classmethod
    def describe(cls) -> str:
        text = (f"ffmpeg : {cls.ffmpeg}  （{cls.ffmpeg_source}）\n"
                f"ffprobe: {cls.ffprobe}  （{cls.ffprobe_source}）")
        if cls.ffplay is not None:
            text += f"\nffplay : {cls.ffplay}  （{cls.ffplay_source}）"
        return text


# --------------------------------------------------------------------------- #
# 显示 / 复制命令（Windows 命令行风格；QProcess 不需要它，仅给人看）
# --------------------------------------------------------------------------- #
_NEEDS_QUOTE = re.compile(r'[\s"&<>^|]')


def quote_arg(arg: str) -> str:
    if not _NEEDS_QUOTE.search(arg):
        return arg
    if os.name == "nt":
        return '"' + arg.replace('"', '""') + '"'
    return "'" + arg.replace("'", "'\\''") + "'"


def format_command(args, program: str = "ffmpeg") -> str:
    return " ".join([quote_arg(str(program))] + [quote_arg(str(a)) for a in args])


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run_hidden(cmd, timeout: float = 40):
    return subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW,
                          timeout=timeout)


def stream_progress(args, on_progress=None, on_line=None, timeout: float | None = None,
                    duration: float | None = None):
    """同步跑 ffmpeg，边跑边回调（CLI 用；UI 用 jobs.Job）。

    on_progress(pct: float)  0~1；on_line(str)  每条 stderr。
    返回 (returncode, stderr_text)。
    """
    if Tools.ffmpeg is None:
        raise RuntimeError("找不到 ffmpeg")
    proc = subprocess.Popen([str(Tools.ffmpeg)] + [str(a) for a in args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=CREATE_NO_WINDOW)
    stderr_chunks: list[bytes] = []

    # stderr 在另一条线程读，避免管道写满互锁
    import threading

    def _drain():
        for raw in iter(proc.stderr.readline, b""):
            stderr_chunks.append(raw)
            if on_line is not None:
                try:
                    on_line(raw.decode("utf-8", "replace").rstrip())
                except Exception:  # noqa: BLE001
                    pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            parsed = with_pct(parse_progress_line(line), duration)
            if parsed and on_progress is not None:
                if parsed["type"] == "time" and parsed.get("pct") is not None:
                    on_progress(parsed["pct"])
        proc.wait(timeout=timeout)
    finally:
        reader.join(timeout=2)
        if proc.poll() is None:
            proc.kill()
    return proc.returncode, b"".join(stderr_chunks).decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# 输出解析（纯函数）
# --------------------------------------------------------------------------- #
def parse_progress_line(line: str) -> dict | None:
    """解析 `-progress pipe:1` 的一行。

    返回 {"type": "time", "t": 秒, "pct": None} / {"type": "speed", "speed": "2.5x"}
         / {"type": "end"} / None
    """
    line = line.strip()
    if not line:
        return None
    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
        # 注意：out_time_ms 在很多构建里其实也是微秒，两者都按微秒处理
        raw = line.split("=", 1)[1].strip()
        try:
            return {"type": "time", "t": int(raw) / 1e6, "pct": None}
        except ValueError:
            return None
    if line.startswith("out_time="):
        value = line.split("=", 1)[1].strip()
        match = re.match(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$", value)
        if not match:
            return None
        t = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        return {"type": "time", "t": t, "pct": None}
    if line.startswith("speed="):
        speed = line.split("=", 1)[1].strip()
        return {"type": "speed", "speed": speed} if speed else None
    if line == "progress=end":
        return {"type": "end"}
    return None


def with_pct(parsed: dict | None, duration: float | None) -> dict | None:
    """给 time 事件补上百分比（duration 未知时为 None）。"""
    if not parsed or parsed.get("type") != "time":
        return parsed
    if not duration or duration <= 0:
        return parsed
    parsed["pct"] = max(0.0, min(1.0, parsed["t"] / duration))
    return parsed


def parse_pts_json(text: str, key: str = "frames") -> list[dict]:
    """解析 ffprobe 的 frames/packets 列表，返回 [{t, keyframe}]（按时间排序）。"""
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return []
    items = data.get(key) or []
    out = []
    for item in items:
        try:
            t = float(item.get("pts_time"))
        except (TypeError, ValueError):
            continue
        flags = str(item.get("flags") or "")
        out.append({"t": t, "keyframe": flags.startswith("K")})
    out.sort(key=lambda x: x["t"])
    return out


def snap_before(keyframe_times, t: float, eps: float = 1e-3):
    """返回 <= t 的最后一个关键帧；没有则 None。"""
    best = None
    for k in keyframe_times:
        if k <= t + eps:
            best = k
        else:
            break
    return best


def snap_after(keyframe_times, t: float, eps: float = 1e-3):
    """返回 >= t 的第一个关键帧；没有则 None（智能切割用它当"头段终点"）。"""
    for k in keyframe_times:
        if k >= t - eps:
            return k
    return None


def closest(points, t: float):
    if not points:
        return None
    return min(points, key=lambda p: abs(p["t"] - t))


# --------------------------------------------------------------------------- #
# 参数构造（纯函数）
# --------------------------------------------------------------------------- #
def threads_for(cpu_count: int | None = None) -> int:
    """只占一半线程：ffmpeg 满核会把界面挤卡。"""
    cores = cpu_count or os.cpu_count() or 4
    return max(2, min(8, cores // 2))


def build_probe_args(path: Path) -> list[str]:
    return ["-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,"
            "r_frame_rate,nb_frames,duration,start_time,pix_fmt,has_b_frames,"
            "profile,level,bit_rate,sample_rate,channels:format=duration,start_time",
            "-of", "json", str(path)]


def build_extract_args(path: Path, step: float, tw: int, th: int, outdir: Path,
                       threads: int | None = None,
                       src_fps: float | None = None) -> list[str]:
    """每 step 帧取一帧；缩略图按视频自身比例输出（不补黑边）。

    step 允许小数（如 23.98 = 23.98fps 视频的 1 秒）：
      * 整数 -> `select='not(mod(n,N))'`，按帧号精确取（原路径，字节不变）；
      * 小数 -> `fps=源fps/step`，按**时间**均匀取（帧号无法整除，只能这样做）。
    """
    step = float(step)
    whole = int(round(step))
    scale = f"scale={tw}:{th}:force_original_aspect_ratio=decrease"
    if abs(step - whole) < 1e-6:
        vf = f"select='not(mod(n\\,{whole}))',{scale}"
    else:
        fps_in = float(src_fps) if src_fps and src_fps > 0 else 25.0
        out_fps = max(0.01, fps_in / step)
        vf = f"fps={out_fps:.6f},{scale}"
    return ["-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            "-threads", str(threads if threads is not None else threads_for()),
            "-i", str(path), "-map", "0:v:0", "-vf", vf,
            "-fps_mode", "passthrough", "-q:v", "3", "-start_number", "1", "-y",
            str(outdir / "f_%06d.jpg")]


def build_preview_args(path: Path, t_abs: float, out: Path, max_width: int = 1280) -> list[str]:
    return ["-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, t_abs):.3f}",
            "-i", str(path), "-frames:v", "1",
            "-vf", f"scale='min({max_width},iw)':-2", "-q:v", "2", "-y", str(out)]


def build_export_args(src: Path, start_rel: float, duration: float, out: Path,
                      copy_streams: bool = True) -> list[str]:
    """无损导出：只切不重编码。

    - `-ss` 在 `-i` 前：关键帧快速定位 + 关键帧切割。
      **`start_rel` 是"从文件起点算的秒数"**，不是文件内部的绝对 pts
      （ffmpeg 的 -ss 默认会自己加 start_time，传绝对值会二次偏移）。
    - `-t` 在 `-i` 后，表示从定位点起的时长
    - 只保留视频/音频（`-map 0:v? -map 0:a?`），避免 data/attachment 流把 mux 搞挂
    """
    args = ["-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            "-ss", f"{max(0.0, start_rel):.6f}",
            "-i", str(src),
            "-t", f"{max(0.001, duration):.6f}",
            "-map", "0:v?", "-map", "0:a?"]
    if copy_streams:
        # 注意：这里**不要**加 -avoid_negative_ts make_zero。
        # 实测它会按源 start_time 整体平移输出时间戳，导致 -t 被放大
        # （0.5 秒变成 1.566 秒），拼接后整段变成两倍长。
        args += ["-c", "copy"]
    else:  # P3 预留：切点重编码
        args += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    args += ["-y", str(out)]
    return args


# --------------------------------------------------------------------------- #
# 智能切割（只重编码"起点 → 下一个关键帧"）
# --------------------------------------------------------------------------- #
ENCODER_MAP = {
    "h264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "av1": "libsvtav1",
    "vp9": "libvpx-vp9",
    "vp8": "libvpx",
    "mpeg4": "mpeg4",
    "prores": "prores_ks",
    "mjpeg": "mjpeg",
}

# 这些容器能当"拼接零件"（也是最终输出的白名单）
SPLIT_CONTAINERS = {".mp4", ".mov", ".m4v", ".mkv", ".ts", ".webm"}


def available_encoders() -> set[str]:
    """当前 ffmpeg 里真实存在的编码器（只查一次）。"""
    global _ENCODERS_CACHE
    if _ENCODERS_CACHE is not None:
        return _ENCODERS_CACHE
    found: set[str] = set()
    if Tools.ffmpeg is not None:
        try:
            result = run_hidden([str(Tools.ffmpeg), "-hide_banner", "-encoders"],
                                timeout=20)
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                parts = line.split()
                if len(parts) >= 2 and len(parts[0]) == 6:
                    found.add(parts[1])
        except (OSError, subprocess.SubprocessError):
            pass
    _ENCODERS_CACHE = found
    return found


_ENCODERS_CACHE: set[str] | None = None


def encoder_for(codec: str) -> str | None:
    name = ENCODER_MAP.get((codec or "").lower())
    if not name:
        return None
    encoders = available_encoders()
    if encoders and name not in encoders:
        return None
    return name


def estimate_video_bitrate(info: MediaInfo) -> int | None:
    """源视频码率 × 1.2（重编码块留点余量，别糊）。"""
    bitrate = info.video_bitrate
    if not bitrate:
        try:
            size = Path(info.path).stat().st_size
        except OSError:
            size = 0
        if size > 0 and info.duration > 0:
            bitrate = int(size * 8 / info.duration)
    if not bitrate or bitrate <= 0:
        return None
    return int(bitrate * 1.2)


def build_smart_head_args(src: Path, start_rel: float, duration: float, out: Path,
                          info: MediaInfo, video_bitrate: int,
                          encoder: str) -> list[str]:
    """把 [start, 下一个关键帧) 用同 codec 重编码，其余参数尽量与源一致。

    参考 LosslessCut 的实作，三个细节很关键：
      * `-ss` 放 -i 前（长文件寻址快）；
      * `-i` 后再来一个 `-ss 0`，否则拼接后开头会多一段黑帧；
      * 码率按源码率给，不要用 crf（和后面 copy 的段落画质/体积才接近）。
    """
    args = ["-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            # -t 必须放 -i 前面（作为"只读这么多"的输入限制）：
            # 放到后面时，遇到时间戳不是从 0 开始的容器（.ts/.mts），
            # ffmpeg 会按输出时间戳判断，结果一帧都写不出来（空文件）。
            "-ss", f"{max(0.0, start_rel):.6f}",
            "-t", f"{max(0.001, duration):.6f}",
            "-i", str(src),
            "-ss", "0",
            "-map", "0:v:0", "-map", "0:a?",
            "-ignore_unknown",
            "-c:v", encoder, "-b:v", str(video_bitrate),
            "-preset", "veryfast", "-threads", str(threads_for())]
    if info.pix_fmt:
        args += ["-pix_fmt", info.pix_fmt]
    if info.bframes > 0:
        args += ["-bf", str(info.bframes)]
    args += ["-c:a", "copy", "-y", str(out)]
    return args


def build_concat_args(list_path: Path, out: Path) -> list[str]:
    # 各零件都是从 0 开始的，不需要（也不应该）再平移时间戳
    return ["-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0", "-c", "copy",
            "-y", str(out)]


def build_play_cache_args(src: Path, t_start: float, count: int, stride: int,
                          outdir: Path) -> list[str]:
    """为「选区回放」一次抽出整段（按 stride 抽稀），播放时只读图片。

    低配机上实时解码会卡，所以播放不碰解码器；抽 8fps 的图，一秒钟只要 8 张。
    """
    vf = f"select='not(mod(n\\,{max(1, stride)}))',scale='min(1280,iw)':-2"
    return ["-hide_banner", "-nostats", "-loglevel", "error",
            "-ss", f"{max(0.0, t_start):.6f}",
            "-i", str(src),
            "-vf", vf, "-fps_mode", "passthrough",
            "-frames:v", str(max(1, count)),
            "-q:v", "4", "-start_number", "0", "-y",
            str(outdir / "p_%05d.jpg")]


def build_normalize_args(src: Path, out: Path) -> list[str]:
    """把时间戳不规范/寻址不可靠的源无损转封装一次（不重编码）。

    实测：.ts 直接 `-ss` 会落到错误的关键帧（甚至落空），转成 mkv 后
    就和不带偏移的 mp4 一样可靠。
    """
    return ["-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            "-i", str(src),
            "-map", "0:v:0", "-map", "0:a?",
            "-c", "copy", "-avoid_negative_ts", "make_zero",
            # 不要加 -fflags +genpts：实测会让后续 -ss 落错关键帧（早一个 GOP）
            "-y", str(out)]


def write_concat_list(paths: list[Path], list_path: Path) -> None:
    lines = []
    for p in paths:
        escaped = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_keyframes_args(path: Path, from_s: float, to_s: float) -> list[str]:
    """只读窗口内的关键帧（不扫全片）。"""
    return ["-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
            "-read_intervals", f"{max(0.0, from_s):.3f}%{max(0.0, to_s):.3f}",
            "-show_entries", "frame=pts_time,flags", "-of", "json", "-i", str(path)]


def build_packets_args(path: Path, from_s: float, to_s: float) -> list[str]:
    """只读窗口内的视频包（用于取"选中那一帧"的精确 pts）。"""
    return ["-v", "error", "-select_streams", "v:0",
            "-read_intervals", f"{max(0.0, from_s):.3f}%{max(0.0, to_s):.3f}",
            "-show_entries", "packet=pts_time,flags", "-of", "json", "-i", str(path)]


# --------------------------------------------------------------------------- #
# 高层查询（要跑子进程；纯逻辑已抽到上面）
# --------------------------------------------------------------------------- #
def probe(path: Path) -> MediaInfo:
    if Tools.ffprobe is None:
        raise RuntimeError("找不到 ffprobe")
    result = run_hidden([str(Tools.ffprobe)] + build_probe_args(path), timeout=40)
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(tail[-1] if tail else f"ffprobe 退出码 {result.returncode}")
    try:
        data = json.loads(result.stdout.decode("utf-8", "replace") or "{}")
    except ValueError as exc:
        raise RuntimeError("ffprobe 输出无法解析") from exc
    try:
        return parse_probe_json(data, path)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def read_keyframes(path: Path, from_s: float, to_s: float) -> list[float]:
    if Tools.ffprobe is None:
        return []
    result = run_hidden([str(Tools.ffprobe)] + build_keyframes_args(path, from_s, to_s),
                        timeout=60)
    if result.returncode != 0:
        return []
    frames = parse_pts_json(result.stdout.decode("utf-8", "replace"), "frames")
    # 已经用 -skip_frame nokey 过滤过：这里的每一帧都是关键帧，
    # 不要再依赖 flags 字段（部分 ffprobe 构建不返回它）。
    return [f["t"] for f in frames]


def read_packets(path: Path, from_s: float, to_s: float) -> list[dict]:
    if Tools.ffprobe is None:
        return []
    result = run_hidden([str(Tools.ffprobe)] + build_packets_args(path, from_s, to_s),
                        timeout=60)
    if result.returncode != 0:
        return []
    return parse_pts_json(result.stdout.decode("utf-8", "replace"), "packets")


def export_clip(src: Path, start_rel: float, duration: float, out: Path,
                on_progress=None) -> tuple[bool, str]:
    args = build_export_args(src, start_rel, duration, out)
    code, stderr = stream_progress(args, on_progress=on_progress, timeout=None,
                                   duration=duration)
    if code != 0:
        tail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        return False, (tail[-1] if tail else f"ffmpeg 退出码 {code}")
    return True, ""
