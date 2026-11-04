# -*- coding: utf-8 -*-
"""媒体信息解析与文件发现（纯逻辑，不依赖 Qt / 子进程）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VIDEO_EXTS


def rate(text) -> float:
    """解析 ffprobe 的帧率字段（"30000/1001" 或 "25"）。"""
    try:
        if not text:
            return 0.0
        if "/" in str(text):
            a, b = str(text).split("/", 1)
            b = float(b)
            return float(a) / b if b else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


@dataclass
class MediaInfo:
    path: Path
    duration: float
    fps: float
    width: int
    height: int
    frames: int
    codec: str = ""
    audio: str = ""
    start_time: float = 0.0
    # 智能切割（只重编码切点）需要的编码参数
    pix_fmt: str = ""
    bframes: int = 0
    profile: str = ""
    level: str = ""
    video_streams: int = 1
    video_bitrate: int = 0
    audio_rate: int = 0
    audio_channels: int = 0
    audio_bitrate: int = 0

    @property
    def resolution(self) -> str:
        return f"{self.width}×{self.height}"

    @property
    def frame_duration(self) -> float:
        return 1.0 / self.fps if self.fps > 0 else 0.04

    def absolute_time(self, relative: float) -> float:
        """页面上的时间码是"相对视频起点"的 -> 文件内部的绝对 pts。

        ffprobe 的 `-read_intervals` 用的是**绝对**时间戳，所以查关键帧/包要用它。
        """
        return max(0.0, self.start_time + max(0.0, relative))

    def relative_time(self, absolute: float) -> float:
        """文件内部绝对 pts -> 页面时间码。

        注意：ffmpeg 的 `-ss`（放在 -i 前）默认是"从文件起点算的偏移量"
        （`-seek_timestamp` 关闭时的行为），不能喂绝对时间戳，
        否则 start_time != 0 的 .ts/.mxf 会被**二次偏移**，切到片尾甚至切出空文件。
        """
        return max(0.0, absolute - self.start_time)


def parse_probe_json(data: dict, path: Path) -> MediaInfo:
    """把 `ffprobe -of json` 的结果变成 MediaInfo（纯函数，便于单测）。"""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None and streams:
        video = streams[0]
    if video is None:
        raise ValueError("没有视频轨道")

    fps = (rate(video.get("avg_frame_rate")) or rate(video.get("r_frame_rate"))
           or 25.0)

    duration = 0.0
    for raw in (video.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(raw)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    try:
        frames = int(video.get("nb_frames") or 0)
    except (TypeError, ValueError):
        frames = 0
    if frames <= 0 and duration > 0 and fps > 0:
        frames = int(round(duration * fps))

    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    audio = (audio_stream or {}).get("codec_name") or ""

    start_time = 0.0
    for raw in (video.get("start_time"), (data.get("format") or {}).get("start_time")):
        try:
            start_time = float(raw)
            break
        except (TypeError, ValueError):
            continue
    if start_time < 0 or start_time != start_time:  # 负数/NaN 一律按 0
        start_time = 0.0

    def _int(value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return MediaInfo(
        Path(path), duration, fps,
        _int(video.get("width")), _int(video.get("height")), frames,
        video.get("codec_name") or "", audio, start_time,
        pix_fmt=video.get("pix_fmt") or "",
        bframes=_int(video.get("has_b_frames")),
        profile=str(video.get("profile") or ""),
        level=str(video.get("level") or ""),
        video_streams=sum(1 for s in streams if s.get("codec_type") == "video"),
        video_bitrate=_int(video.get("bit_rate")),
        audio_rate=_int((audio_stream or {}).get("sample_rate")),
        audio_channels=_int((audio_stream or {}).get("channels")),
        audio_bitrate=_int((audio_stream or {}).get("bit_rate")),
    )


def find_videos(folder: Path, limit: int = 400) -> list[Path]:
    """递归找视频（按路径排序，最多 limit 个）。"""
    out: list[Path] = []
    try:
        for p in sorted(folder.rglob("*")):
            if len(out) >= limit:
                break
            try:
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                    out.append(p)
            except OSError:
                continue
    except OSError:
        pass
    return out


def thumb_size_for(width: int, height: int, long_side: int) -> tuple[int, int]:
    """按视频自身宽高比算缩略图尺寸（长边 = 预设，短边按比例，不补黑边）。"""
    if width <= 0 or height <= 0:
        return long_side, max(48, int(round(long_side * 9 / 16)))
    ratio = width / height
    if ratio >= 1.0:
        tw, th = float(long_side), long_side / ratio
    else:
        tw, th = long_side * ratio, float(long_side)
    if min(tw, th) < 48:  # 超宽/超窄：把短边抬到 48，长边同比缩
        k = 48.0 / min(tw, th)
        tw, th = tw * k, th * k
    return max(16, int(round(tw))), max(16, int(round(th)))
