# -*- coding: utf-8 -*-
"""镜头检测：不再用 ffmpeg 的 ``scene`` 分数，改成**直方图变化**。

为什么换：ffmpeg 的 ``scene`` 分数本质是「相邻两帧的像素差」，所以
摇镜头、推拉、快速运动、闪光都会被判成切镜头（**连贯镜头被切碎**）；
而淡入淡出/叠化这种"渐变"，单帧差又不够大（**该切的地方没分开**）。

这里的做法（很像 PySceneDetect 的 ContentDetector）：

1. 用 ffmpeg 把画面缩到 32×18 的灰度并通过管道吐出来（一帧 576 字节，
   30 分钟片子总共 ~26 MB，很快）；
2. 每帧算一个 64 档灰度直方图，相邻帧算 L1 距离 —— 摇镜头时直方图几乎不变，
   换镜头时才跳；
3. 判切点：**局部极大 + 自适应阈值**（整片均值 + kσ，再夹一个绝对下限）；
4. 叠化/淡入淡出：单帧跳不动，但 ``lag`` 帧的累计变化很大 → 在渐变结束时补一个切点；
5. 同一次切换报两次的合并（0.35 秒内只留分数最高的）。

全部是纯函数（除了采样器），方便单测。
"""

from __future__ import annotations

import math
from bisect import bisect_right

from PySide6.QtCore import QObject, QProcess, Signal

SAMPLE_W = 32
SAMPLE_H = 18
SAMPLE_BYTES = SAMPLE_W * SAMPLE_H
BINS = 64                  # 灰度 0-255 压成 64 档（b >> 2）

CUT_ABS = 0.16             # 硬切绝对下限（L1 距离，0..1）
CUT_SIGMA = 3.0            # 自适应阈值：mean + k*σ
GRADUAL = 0.26             # 渐变（叠化/淡入）在 lag 帧内的累计变化
MIN_GAP_S = 0.35           # 两个切点至少隔这么久


# --------------------------------------------------------------------------- #
# ffmpeg 采样
# --------------------------------------------------------------------------- #
def build_sample_args(path, width: int = SAMPLE_W, height: int = SAMPLE_H,
                      threads: int | None = None) -> list[str]:
    """把整片缩成小灰度图从 stdout 吐出来（只解码，不编码，很快）。"""
    args = ["-hide_banner", "-nostats", "-loglevel", "error"]
    if threads is not None:
        args += ["-threads", str(threads)]
    args += ["-i", str(path), "-map", "0:v:0",
             "-vf", f"scale={width}:{height}:flags=area,format=gray",
             "-f", "rawvideo", "-"]
    return args


def hist_of(frame: bytes) -> list[int]:
    """1 帧灰度 → 64 档直方图。"""
    hist = [0] * BINS
    for value in frame:
        hist[value >> 2] += 1
    return hist


def hist_distance(a, b, total: int = SAMPLE_BYTES) -> float:
    """两个直方图的 L1 距离，归一到 0..1（0 = 完全一样）。"""
    if not a or not b or total <= 0:
        return 0.0
    return sum(abs(x - y) for x, y in zip(a, b)) / (2.0 * total)


# --------------------------------------------------------------------------- #
# 判切点（纯函数）
# --------------------------------------------------------------------------- #
def change_series(histograms) -> list[float]:
    """d[i] = 第 i 帧与第 i-1 帧的直方图距离（d[0] = 0）。"""
    out = [0.0]
    for i in range(1, len(histograms)):
        out.append(hist_distance(histograms[i], histograms[i - 1]))
    return out


def adaptive_threshold(values, sigma: float = CUT_SIGMA, floor: float = CUT_ABS) -> float:
    """整片自适应阈值：均值 + kσ，并夹在 [floor, 0.85]。"""
    data = [v for v in values if v > 0]
    if not data:
        return floor
    mean = sum(data) / len(data)
    var = sum((v - mean) ** 2 for v in data) / len(data)
    return max(floor, min(0.85, mean + sigma * math.sqrt(var)))


def detect_cuts(histograms, fps: float, *, lag: int = 6,
                sigma: float = CUT_SIGMA, floor: float = CUT_ABS,
                gap_s: float = MIN_GAP_S) -> list[dict]:
    """返回 ``[{"frame": int, "score": float, "kind": "cut"|"gradual"}, ...]``。

    * 硬切：``d1[i]`` 是局部极大且超过自适应阈值（运动/闪光不是极大值，被滤掉）；
    * 渐变：``d_lag[i]``（隔 lag 帧的累计变化）连续超阈值的**整段**算一次切换，
      切点放在这段的末尾（画面稳定下来那一帧）；
    * 合并：``gap_s`` 内只留分数最高的那个。
    """
    count = len(histograms)
    if count < 2 or fps <= 0:
        return [{"frame": 0, "score": 1.0, "kind": "start"}] if count else []
    d1 = change_series(histograms)
    threshold = adaptive_threshold(d1, sigma=sigma, floor=floor)
    lag = max(1, int(lag))

    candidates: list[dict] = []
    for i in range(1, count):
        if i < gap_s * fps:               # 片头附近不算切点（第一个镜头从 0 开始）
            continue
        score = d1[i]
        if score < threshold:
            continue
        prev = d1[i - 1] if i >= 1 else 0.0
        nxt = d1[i + 1] if i + 1 < count else 0.0
        # 必须是**严格**的局部极大：单帧闪白/闪光前后两帧变化一样大，两帧都会被挡掉
        if score <= prev or score <= nxt:
            continue
        # 闪白只有 1 帧：前帧 与 后两帧 其实一样（真切换前后是不一样的两段）
        after = min(count - 1, i + 2)
        if hist_distance(histograms[i - 1], histograms[after]) < threshold * 0.55:
            continue
        candidates.append({"frame": i, "score": score, "kind": "cut"})

    # 渐变（叠化/淡入淡出/转场特效）：d_lag 超阈值的连续区间 = 一次切换
    dlag = [0.0] * count
    for i in range(lag, count):
        dlag[i] = hist_distance(histograms[i], histograms[i - lag])
    gradual: list[dict] = []
    i = max(lag, int(gap_s * fps))
    while i < count:
        if dlag[i] <= GRADUAL:
            i += 1
            continue
        end = i
        while end + 1 < count and dlag[end + 1] > GRADUAL:
            end += 1
        # 判断"真的是换场"：窗口前 vs 窗口后的画面要不一样
        before = histograms[max(0, i - lag - 2)]
        after = histograms[min(count - 1, end + 2)]
        if hist_distance(before, after) < GRADUAL * 0.8:
            i = end + 1
            continue                     # 内容没变（闪白/抖动/运动），不是转场
        # 转场要"落定"：末帧与下一帧应该很像
        if hist_distance(histograms[end],
                         histograms[min(count - 1, end + 1)]) > 0.20:
            i = end + 1
            continue
        # 过渡结束那一帧（再往前挪半个窗口，别把切点放到过渡后太久）
        frame = max(1, end - lag // 2)
        peak = max(dlag[i:end + 1])
        if not any(abs(c["frame"] - frame) <= gap_s * fps for c in candidates):
            gradual.append({"frame": frame, "score": peak, "kind": "gradual"})
        i = end + 1

    merged: list[dict] = []
    for item in sorted(candidates + gradual, key=lambda c: c["frame"]):
        if merged and (item["frame"] - merged[-1]["frame"]) < gap_s * fps:
            if item["score"] > merged[-1]["score"]:
                merged[-1] = item
            continue
        merged.append(item)
    return [{"frame": 0, "score": 1.0, "kind": "start"}] + merged


def frames_to_times(frames, fps: float) -> list[float]:
    if fps <= 0:
        return []
    return [max(0.0, f / fps) for f in frames]


def nearest_index(frames, frame: int) -> int:
    index = bisect_right(frames, frame) - 1
    return max(0, index)


# 三个"灵敏度"档：值越大越保守（少切、但可能漏掉转场）
PRESETS = {
    0.20: (2.4, 0.11),     # 灵敏
    0.30: (3.0, 0.16),     # 标准
    0.45: (4.0, 0.24),     # 保守
}


def params_for(preset: float) -> tuple[float, float]:
    """把界面上的档位（0.20/0.30/0.45）换成 ``(sigma, 绝对下限)``。"""
    value = float(preset or 0.30)
    key = min(PRESETS, key=lambda k: abs(k - value))
    return PRESETS[key]


def detect_from_histograms(histograms, fps: float, preset: float = 0.30):
    """按档位调用 ``detect_cuts``，返回切点列表（含第 0 帧）。"""
    sigma, floor = params_for(preset)
    return detect_cuts(histograms, fps, sigma=sigma, floor=floor)


def cut_frames(cuts) -> list[int]:
    frames = []
    for item in cuts or []:
        frame = int(item.get("frame", 0))
        if not frames or frame > frames[-1]:
            frames.append(frame)
    return frames or [0]


# --------------------------------------------------------------------------- #
# 采样器（QProcess，把整片读成直方图序列）
# --------------------------------------------------------------------------- #
class SceneSampler(QObject):
    """跑一遍 ``ffmpeg → rawvideo``，边读边算直方图。"""

    progress = Signal(int)                 # 0~100
    finished = Signal(object, bool, str)   # histograms, ok, error

    def __init__(self, parent=None):
        super().__init__(parent)
        self._proc: QProcess | None = None
        self._buf = b""
        self._hists: list[list[int]] = []
        self._frames_seen = 0
        self._expected = 0
        self._err = b""
        self._alive = True

    def busy(self) -> bool:
        return self._proc is not None

    def start(self, path, total_frames: int = 0):
        if self.busy():
            return False
        self._hists = []
        self._buf = b""
        self._frames_seen = 0
        self._err = b""
        self._expected = max(0, int(total_frames or 0))
        if not _ffmpeg_ready():
            self.finished.emit([], False, "找不到 ffmpeg")
            return False
        proc = QProcess(self)
        proc.setProgram(str(_ffmpeg_path()))
        proc.setArguments(build_sample_args(path))
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(lambda: self._read_out(proc))
        proc.readyReadStandardError.connect(lambda: self._read_err(proc))
        proc.finished.connect(lambda code, status, p=proc: self._done(p, code))
        proc.errorOccurred.connect(lambda err, p=proc: self._on_error(p, err))
        self._proc = proc
        proc.start()
        return True

    def cancel(self):
        self._alive = False
        self._kill()

    # ---------------- 内部 ----------------
    def _read_out(self, proc):
        if proc is not self._proc:
            return
        try:
            self._buf += bytes(proc.readAllStandardOutput())
        except RuntimeError:
            return
        self._consume()

    def _consume(self):
        take = len(self._buf) // SAMPLE_BYTES
        if not take:
            return
        for n in range(take):
            chunk = self._buf[n * SAMPLE_BYTES:(n + 1) * SAMPLE_BYTES]
            self._hists.append(hist_of(chunk))
        self._buf = self._buf[take * SAMPLE_BYTES:]
        self._frames_seen += take
        if self._expected > 0:
            pct = int(min(100.0, self._frames_seen * 100.0 / self._expected))
            self.progress.emit(pct)
        else:
            self.progress.emit(min(99, self._frames_seen // 50))

    def _read_err(self, proc):
        if proc is not self._proc:
            return
        try:
            self._err = (self._err + bytes(proc.readAllStandardError()))[-2000:]
        except RuntimeError:
            pass

    def _on_error(self, proc, error):
        if proc is not self._proc:
            return
        if error == QProcess.FailedToStart:
            self._kill()
            self.finished.emit([], False, "ffmpeg 起不来")

    def _done(self, proc, code):
        if proc is not self._proc:
            return
        self._proc = None
        try:
            self._read_out(proc)
        except RuntimeError:
            pass
        proc.deleteLater()
        if not self._alive:
            return
        self._consume()
        if code != 0 and not self._hists:
            tail = self._err.decode("utf-8", "replace").strip().splitlines()
            self.finished.emit([], False, tail[-1][:120] if tail else f"ffmpeg rc={code}")
            return
        self.progress.emit(100)
        self.finished.emit(self._hists, True, "")

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.waitForFinished(800)
            except (RuntimeError, AttributeError):
                pass
            try:
                proc.deleteLater()
            except RuntimeError:
                pass


def _ffmpeg_path():
    from . import ffmpeg as _ff

    return _ff.Tools.ffmpeg


def _ffmpeg_ready() -> bool:
    return _ffmpeg_path() is not None
