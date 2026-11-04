# -*- coding: utf-8 -*-
"""智能切割：只重编码"你点的起点 → 下一个关键帧"，其余原样复制。

为什么需要它：无损切割只能从关键帧开始，GOP 2 秒就早 2 秒、GOP 30 秒就早 30 秒。
把那一小段（通常 0.5–3 秒）用同 codec 重编码后和原始码流拼接，就能既"帧精确"又
保住 99% 的原始画质。代价是切点附近被重压缩一次。

稳定性策略（重要）：
  * 参数不全就**不猜**：codec 不支持 / 多视频流 / 码率未知 / 容器不对 -> 直接退回无损；
  * 拼接后做**校验**（时长、能否解码、拼缝附近能否解码），不过就自动退回无损；
  * 所有中间文件放在系统临时目录，成功失败都会删。
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg
from .media import MediaInfo


@dataclass
class Plan:
    mode: str = "copy"                 # copy | smart
    start: float = 0.0                 # 相对视频起点（= 页面时间码）
    end: float = 0.0
    reason: str = ""                   # 为什么退回无损
    head_end: float | None = None      # 头段结束（下一个关键帧，相对时间）
    head_duration: float = 0.0
    estimate_seconds: float = 0.0
    encoder: str = ""
    bitrate: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


# 这些容器的时间戳/寻址不可靠（实测 .ts 的 -ss 会落到错误的关键帧，甚至落空）
UNRELIABLE_CONTAINERS = {".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".vob", ".mxf",
                         ".rmvb", ".rm", ".asf", ".wmv", ".flv", ".divx", ".m2v"}


def needs_normalize(info: MediaInfo, src: Path) -> bool:
    """是否要先无损转封装一次。"""
    if src.suffix.lower() in UNRELIABLE_CONTAINERS:
        return True
    return abs(info.start_time) > 0.05


def plan(path: Path, info: MediaInfo, start_rel: float, end_rel: float,
         out: Path, exact: bool = True,
         lossless_start: float | None = None) -> Plan:
    """决定走智能切割还是无损复制。

    `start_rel` 是**真正想要的切点**（帧级）；`lossless_start` 是吸附到关键帧后的
    退路起点（不传就退回 start_rel）。
    """
    copy_start = lossless_start if lossless_start is not None else start_rel
    result = Plan(mode="copy", start=copy_start, end=end_rel)
    if not exact:
        return result

    if info.video_streams != 1:
        result.reason = f"源文件有 {info.video_streams} 条视频流，智能切割只支持单视频流"
        return result
    encoder = ffmpeg.encoder_for(info.codec)
    if encoder is None:
        result.reason = f"视频编码 {info.codec or '未知'} 没有可用的重编码器"
        return result
    if out.suffix.lower() not in ffmpeg.SPLIT_CONTAINERS:
        result.reason = f"容器 {out.suffix or '无扩展名'} 不支持分段拼接"
        return result
    bitrate = ffmpeg.estimate_video_bitrate(info)
    if not bitrate:
        result.reason = "拿不到视频码率，无法匹配画质"
        return result

    start_abs = info.absolute_time(start_rel)
    keyframes = ffmpeg.read_keyframes(path, max(0.0, start_abs - 1.0), start_abs + 120.0)
    if not keyframes:
        keyframes = ffmpeg.read_keyframes(path, max(0.0, start_abs - 1.0), start_abs + 600.0)
    next_kf = ffmpeg.snap_after(keyframes, start_abs)
    if next_kf is None:
        result.reason = "起点之后找不到关键帧"
        return result

    head_end = info.relative_time(next_kf)
    if head_end - start_rel <= 1e-3:
        result.reason = "起点正好在关键帧上，不需要重编码"
        return result
    if head_end >= end_rel:
        head_end = end_rel  # 整段都在一个 GOP 里：整段重编码

    result.mode = "smart"
    result.start = start_rel
    result.head_end = head_end
    result.head_duration = max(0.0, head_end - start_rel)
    result.encoder = encoder
    result.bitrate = bitrate
    # 粗估：veryfast 1080p 大约 2 倍实时；低配按 1 倍实时保守估
    result.estimate_seconds = max(0.4, result.head_duration * 1.0 + 0.3)
    return result


def build_stages(src: Path, info: MediaInfo, out: Path, cut_plan: Plan):
    """返回 (stages, temp_paths)。stages 直接喂给 Job。"""
    if cut_plan.mode != "smart" or cut_plan.head_end is None:
        return ([{
            "args": ffmpeg.build_export_args(src, cut_plan.start,
                                             cut_plan.duration, out),
            "duration": cut_plan.duration,
            "label": "复制",
        }], [])

    temp_dir = Path(tempfile.mkdtemp(prefix="purer_cut_"))
    temp_paths = [temp_dir]
    suffix = out.suffix or ".mp4"
    head_path = temp_dir / f"head{suffix}"
    tail_path = temp_dir / f"tail{suffix}"
    list_path = temp_dir / "list.txt"

    head_dur = max(0.001, cut_plan.head_end - cut_plan.start)
    stages = [{
        "args": ffmpeg.build_smart_head_args(src, cut_plan.start, head_dur,
                                             head_path, info, cut_plan.bitrate,
                                             cut_plan.encoder),
        "duration": head_dur,
        "label": f"编码头段 {head_dur:.2f}s",
    }]

    parts = [head_path]
    tail_dur = 0.0
    if cut_plan.end - cut_plan.head_end > 0.001:
        tail_dur = cut_plan.end - cut_plan.head_end
        stages.append({
            "args": ffmpeg.build_export_args(src, cut_plan.head_end, tail_dur,
                                             tail_path),
            "duration": tail_dur,
            "label": f"复制主体 {tail_dur:.2f}s",
        })
        parts.append(tail_path)

    ffmpeg.write_concat_list(parts, list_path)
    temp_paths.extend(parts + [list_path])
    stages.append({
        "args": ffmpeg.build_concat_args(list_path, out),
        "duration": 0.3,
        "label": "拼接",
    })
    return stages, temp_paths


def verify(out: Path, expected_duration: float, frame_duration: float = 0.04,
           junction: float | None = None) -> tuple[bool, str]:
    """拼接后校验：能不能读、时长对不对、拼缝附近能不能解码。"""
    try:
        if not out.exists() or out.stat().st_size < 2048:
            return False, "输出文件为空"
    except OSError as e:
        return False, f"读不到输出：{e}"

    try:
        info = ffmpeg.probe(out)
    except Exception as e:  # noqa: BLE001
        return False, f"输出无法解析：{e}"
    if info.duration <= 0:
        return False, "输出时长为 0"

    tolerance = max(0.45, frame_duration * 3)
    if abs(info.duration - expected_duration) > tolerance:
        return False, (f"时长不符（{info.duration:.2f}s，期望 "
                       f"{expected_duration:.2f}s）")

    # 拼缝附近解一小段：能暴露花屏/坏帧
    if junction is not None and junction > 0:
        start = max(0.0, junction - 0.4)
        args = ["-v", "error", "-xerror", "-ss", f"{start:.3f}", "-t", "0.8",
                "-i", str(out), "-f", "null", "-"]
        result = ffmpeg.run_hidden([str(ffmpeg.Tools.ffmpeg)] + args, timeout=60)
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            return False, "拼缝处解码失败" + (f"：{tail[-1]}" if tail else "")
    return True, ""


def cleanup(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
        except OSError:
            pass
