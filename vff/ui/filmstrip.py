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
        # 拖动时上游可能给小数帧号（时间轴插值），这里统一收敛成整数
        frame = int(frame)
        return self.start <= frame < self.start + self.count

    def path_for(self, frame: int) -> Path | None:
        frame = int(frame)
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
        # 这里**不能**学 PlayClip 加 setpts=N：本命令没带 -fps_mode，默认 vsync=cfr
        # 会按时间戳丢帧，时间戳被 setpts 压成 0,1,2… 之后只会出来 2 张
        # （2026-11-12 实测）。回放缓存那条命令带了 passthrough，所以可以加。
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

    def _window_on_disk(self, start: int, count: int) -> bool:
        for i in range(max(1, int(count))):
            path = self.dir / f"f_{int(start) + i:06d}.jpg"
            try:
                if path.is_file() and path.stat().st_size > 512:
                    return True
            except OSError:
                continue
        return False

    def _done(self, code, status, start: int, count: int):
        self._proc = None
        if not self._alive:
            return
        try:
            if code == 0 and status == QProcess.NormalExit:
                self.windowReady.emit(start)
                return
            # 片尾那一段 ffmpeg 常会「写完图再报错退出」（时间戳不单调），
            # 实测最后一帧的窗口 rc=-1 但其实张张都在 —— 有图就用，
            # 不要整段退回「取相邻帧失败」，那样右侧预览会掉成墙上的缩略图。
            if self._window_on_disk(start, count):
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
        self._requested: tuple[int, int, int] | None = None   # (start, count, stride)
        self.partial = 0        # >0 = 只拿到这么多张之前就没了（片尾不足）

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
        want = (start, count, stride)
        # 用「请求过的区间」判断命中，而不是用实际张数：片尾少一两张时
        # 会走「部分可用」分支把 count 收窄，拿 count 比就永远命不中。
        if (want == self._requested and self.count > 0
                and self._frames_on_disk() >= self.count):
            self.ready.emit()
            return

        self._kill()
        self._cache.clear()
        self.partial = 0
        self._requested = want
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

    def _frames_on_disk(self) -> int:
        """从 p_00000.jpg 开始连着数，遇到缺号/坏图就停（= 能播的张数）。"""
        n = 0
        limit = PLAY_MAX_FRAMES + 8
        while n < limit:
            path = self.dir / f"p_{n:05d}.jpg"
            try:
                if not path.is_file() or path.stat().st_size <= 256:
                    break
            except OSError:
                break
            n += 1
        return n

    def _files_ok(self, count: int) -> bool:
        return count > 0 and self._frames_on_disk() >= count

    def _why(self, code) -> str:
        tail = " ".join(line.strip() for line in (self._err or "").splitlines()[-3:])
        if tail:
            return f"ffmpeg 抽图中断（退出码 {code}）：{tail[:160]}"
        return f"ffmpeg 一张回放图也没抽出来（退出码 {code}）"

    def _done(self, proc, code, status):
        if proc is not self._proc:        # 已被新的启动 / 取消顶掉
            return
        self._proc = None
        if not self._alive:
            return
        try:
            normal = code == 0 and status == QProcess.NormalExit
            have = self._frames_on_disk() if normal else 0
            if have >= self.count:
                self.partial = 0
                self.ready.emit()
                return
            if have > 0:
                # 片尾差几张是**正常现象**：nb_frames 常缺失，程序按
                # 「时长 × 帧率」估出来的总帧数会比真实末帧多 1 帧，
                # 于是选区一贴到片尾就差最后一张。能播多少播多少，
                # 别因为少一张就把整段回放报废。
                self.partial = self.count - have
                self.count = have
                self.ready.emit()
                return
            if self._variants and ffmpeg.command_rejected(self._err):
                self._launch()            # 换一种写法再试（第一次是首选）
                return
            self.start, self.count = -1, 0
            self.failed.emit(self._why(code))
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
