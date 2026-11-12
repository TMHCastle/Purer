# -*- coding: utf-8 -*-
"""按需取「相邻几十帧」的小窗口（逐帧微调用）。

为什么不每次单独抽一帧：步进 30 次就会起 30 个 ffmpeg，每个都要寻址 + 解码，
在低配机上会一顿一顿的。这里一次取 24 帧（≈0.8 秒素材），步进时基本是读缓存。
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

from .. import ffmpeg
from ..media import MediaInfo

WINDOW_SIZE = 24      # 一次取多少帧
WINDOW_BEFORE = 8     # 光标前面留几帧
MAX_WIDTH = 1280


class FilmStrip(QObject):
    """在视频任意位置取一小段连续帧，按绝对帧号命名。"""

    windowReady = Signal(int)     # 新窗口起始帧
    failed = Signal(str)

    def __init__(self, src: Path, info: MediaInfo, base_dir: Path, parent=None):
        super().__init__(parent)
        self.src = Path(src)
        self.info = info
        self.dir = Path(base_dir) / "strip"
        self.start = -1
        self.count = 0
        self.total_frames = max(1, int(info.frames or 0))
        self._proc: QProcess | None = None
        self._alive = True

    # ---------------- 查询 ----------------
    def contains(self, frame: int) -> bool:
        return self.start <= frame < self.start + self.count

    def path_for(self, frame: int) -> Path | None:
        if not self.contains(frame):
            return None
        path = self.dir / f"f_{frame:06d}.jpg"
        try:
            if path.is_file() and path.stat().st_size > 512:
                return path
        except OSError:
            pass
        return None

    # ---------------- 取帧 ----------------
    def ensure(self, frame: int):
        """保证 frame 落在当前窗口里；不在就（重新）取一段。"""
        if not self._alive or self.info.fps <= 0:
            return
        frame = max(0, min(self.total_frames - 1, int(frame)))
        if self.contains(frame):
            return
        if self._running():
            self._proc.kill()
            self._proc = None
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.failed.emit(f"无法创建缓存目录：{e}")
            return

        start = max(0, frame - WINDOW_BEFORE)
        count = min(WINDOW_SIZE, self.total_frames - start)
        if count <= 0:
            return
        # 往回多半帧，保证第一帧就是 start（浮点边界不会多跳一帧）
        t = max(0.0, (start - 0.5) * self.info.frame_duration)
        args = ["-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.6f}", "-i", str(self.src),
                "-frames:v", str(count),
                "-vf", f"scale='min({MAX_WIDTH},iw)':-2",
                "-q:v", "3", "-start_number", str(start), "-y",
                str(self.dir / "f_%06d.jpg")]
        proc = QProcess(self)
        proc.setProgram(str(ffmpeg.Tools.ffmpeg))
        proc.setArguments(args)
        proc.finished.connect(lambda code, status, s=start, c=count: self._done(code, status, s, c))
        self._proc = proc
        self.start = start
        self.count = count
        proc.start()

    def _running(self) -> bool:
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def _done(self, code, status, start: int, count: int):
        self._proc = None
        if not self._alive:
            return
        try:
            if code == 0 and status == QProcess.NormalExit:
                self.windowReady.emit(start)
            else:
                self.start, self.count = -1, 0
                self.failed.emit("取相邻帧失败")
        except RuntimeError:      # 页面已销毁（关标签页时进程才结束）
            pass

    def stop(self):
        self._alive = False
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.waitForFinished(500)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None


PLAY_FPS = 12           # 兼容旧名（实际速率由 stride 与素材 fps 决定）
PLAY_MAX_FRAMES = 300   # 单次回放最多抽这么多图（覆盖整段，不缩短）


class PlayClip(QObject):
    """选区回放：一次性把整段抽成 12~30fps 的图，播放时只翻图、不解码。"""

    ready = Signal()
    failed = Signal(str)

    def __init__(self, src: Path, info: MediaInfo, base_dir: Path, parent=None):
        super().__init__(parent)
        self.src = Path(src)
        self.info = info
        self.dir = Path(base_dir) / "play"
        self.start = 0
        self.count = 0
        self.stride = 1
        self._proc: QProcess | None = None
        self._alive = True
        self._variants: list[list[str]] = []     # 备选命令（ffmpeg 构建差异兜底）
        self._err = ""
        self._cache: dict[int, object] = {}      # index -> QPixmap（回放时反复读同一批图）

    # ---------------- 准备 ----------------
    def prepare(self, start_frame: int, end_frame: int):
        if not self._alive or self.info.fps <= 0:
            return
        span = max(1, int(end_frame) - int(start_frame))
        # 抽稀比例：只受「最多抽多少张」限制，**必须覆盖整段**。
        # 以前按 PLAY_FPS 反推 stride 并且把张数截断，长选段会少播一截，
        # 看上去就是「预览跟时间轴对不上 / 提前结束」。
        stride = max(1, int(math.ceil(span / float(PLAY_MAX_FRAMES))))
        count = int(span // stride) + 1
        start = max(0, int(start_frame))
        if (start == self.start and count == self.count and stride == self.stride
                and self._files_ok(count)):
            self.ready.emit()
            return

        self._kill()
        self._cache.clear()
        shutil.rmtree(self.dir, ignore_errors=True)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.failed.emit(f"无法创建回放缓存：{e}")
            return
        t = max(0.0, (start - 0.5) * self.info.frame_duration)
        self._variants = ffmpeg.build_play_cache_variants(self.src, t, count, stride,
                                                          self.dir)
        self.start, self.count, self.stride = start, count, stride
        self._launch()

    def _launch(self):
        args = self._variants.pop(0)
        self._err = ""
        proc = QProcess(self)
        proc.setProgram(str(ffmpeg.Tools.ffmpeg))
        proc.setArguments(args)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardError.connect(lambda p=proc: self._read_err(p))
        proc.finished.connect(lambda code, status, p=proc: self._done(p, code, status))
        self._proc = proc
        proc.start()

    def _read_err(self, proc):
        try:
            self._err += bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        except RuntimeError:          # 进程已销毁
            pass

    def _files_ok(self, count: int) -> bool:
        if count <= 0:
            return False
        first = self.dir / "p_00000.jpg"
        last = self.dir / f"p_{count - 1:05d}.jpg"
        try:
            return (first.is_file() and first.stat().st_size > 512
                    and last.is_file() and last.stat().st_size > 512)
        except OSError:
            return False

    def _done(self, proc, code, status):
        if proc is not self._proc:        # 已被新的启动 / 取消顶掉
            return
        self._proc = None
        if not self._alive:
            return
        try:
            ok = (code == 0 and status == QProcess.NormalExit
                  and self._files_ok(self.count))
            if not ok and self._variants and ffmpeg.command_rejected(self._err):
                self._launch()            # 换一种写法再试（第一次是首选）
                return
            if ok:
                self.ready.emit()
            else:
                self.failed.emit("回放缓存生成失败")
        except RuntimeError:      # 页面已销毁
            pass

    # ---------------- 查询 ----------------
    def path_for(self, index: int) -> Path | None:
        if index < 0 or index >= self.count:
            return None
        path = self.dir / f"p_{index:05d}.jpg"
        try:
            if path.is_file() and path.stat().st_size > 256:
                return path
        except OSError:
            pass
        return None

    def pixmap(self, index: int):
        """回放用的图（带缓存：长选段时 tick 可能只有 40–300ms，别每次都读盘解码）。"""
        from PySide6.QtGui import QPixmap

        cached = self._cache.get(index)
        if cached is not None:
            return cached
        path = self.path_for(index)
        if path is None:
            return None
        pix = QPixmap(str(path))
        if pix.isNull():
            return None
        if len(self._cache) > 300:
            self._cache.clear()
        self._cache[index] = pix
        return pix

    def frame_at(self, index: int) -> int:
        return self.start + index * self.stride

    # ---------------- 收尾 ----------------
    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.waitForFinished(800)
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        self._alive = False
        self._kill()
