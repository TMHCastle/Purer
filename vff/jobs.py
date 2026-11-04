# -*- coding: utf-8 -*-
"""长任务（抽帧 / 导出）与串行队列。

设计要点：
  * 一个 Job = 一个逻辑任务，可以由**多个 ffmpeg 阶段**组成
    （智能切割：编码头段 -> 复制尾段 -> 拼接），进度按阶段时长加权合成一条。
  * JobQueue 保证同一时刻只有 1 个重任务（README 的承诺），
    抽帧与导出天然互斥又都能排队。
  * 预览用的单帧抓取是短命进程（<1s），不占这个队列，避免点缩略图要等整段抽帧。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from .ffmpeg import Tools, format_command, parse_progress_line

STDERR_MAX_LINES = 5000


class Job(QObject):
    """一个逻辑任务；内部可串行跑多个 ffmpeg 阶段。"""

    progress = Signal(int)          # 0~100；-1 表示未知
    status = Signal(str)            # 当前阶段说明 / speed
    logLine = Signal(str)           # 原始 stderr 行（诊断用）
    started = Signal()
    finished = Signal(bool)         # True = 正常结束

    def __init__(self, args=None, duration: float | None = None, label: str = "",
                 stages: list[dict] | None = None, parent=None):
        super().__init__(parent)
        if stages:
            self.stages = [dict(stage) for stage in stages]
        else:
            self.stages = [{"args": list(args or []), "duration": duration,
                            "label": label}]
        for stage in self.stages:
            stage["args"] = [str(a) for a in stage.get("args") or []]
            stage["duration"] = float(stage.get("duration") or 0.0)
        self.args = self.stages[0]["args"]
        self.label = label or self.stages[0].get("label", "")
        self.duration = sum(s["duration"] for s in self.stages) or duration
        self.command_line = format_command(self.args, program=self._program())
        self.failed_command = self.command_line
        self.ok = False
        self.cancelled = False
        self.error = ""
        self.state = "idle"          # idle / running / done
        self._index = 0
        self._done_duration = 0.0
        self._proc: QProcess | None = None
        self._out_buf = ""
        self._err_buf = ""
        self._stderr_lines: list[str] = []
        self._pct = -1

    # ---------------- 生命周期 ----------------
    @property
    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    @property
    def stage_label(self) -> str:
        if 0 <= self._index < len(self.stages):
            return self.stages[self._index].get("label") or ""
        return ""

    def _program(self) -> str:
        return str(Tools.ffmpeg) if Tools.ffmpeg else "ffmpeg"

    def start(self):
        if self.state != "idle":
            return
        if Tools.ffmpeg is None:
            self.error = "找不到 ffmpeg"
            self.finalize(False)
            return
        self.state = "running"
        self.started.emit()
        self._start_stage()

    def cancel(self):
        if self.state == "idle":          # 还在排队：直接作废
            self.cancelled = True
            self.finalize(False)
            return
        if self._proc is None or self.state != "running":
            return
        self.cancelled = True
        try:
            self._proc.kill()
            self._proc.waitForFinished(2000)
        except Exception:  # noqa: BLE001
            pass

    @property
    def running(self) -> bool:
        return self.state == "running"

    # ---------------- 阶段推进 ----------------
    def _start_stage(self):
        stage = self.stages[self._index]
        self.args = stage["args"]
        self.command_line = format_command(self.args, program=self._program())
        self._out_buf = ""
        self._err_buf = ""
        self._pct = -1
        self.status.emit(stage.get("label") or "")
        self.progress.emit(self._overall_pct(0.0))

        proc = QProcess(self)
        proc.setProgram(self._program())
        proc.setArguments(self.args)
        proc.setProcessChannelMode(QProcess.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._read_out)
        proc.readyReadStandardError.connect(self._read_err)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        self._proc = proc
        proc.start()

    def _overall_pct(self, stage_pct: float) -> int:
        total = self.duration or 0.0
        if total <= 0:
            return -1
        current = self.stages[self._index]["duration"]
        done = self._done_duration + stage_pct * current
        return int(max(0.0, min(99.0, done / total * 100)))

    # ---------------- 读取 ----------------
    def _read_out(self):
        if self._proc is None:
            return
        self._out_buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._out_buf = self._out_buf.split("\n")
        for line in lines:
            parsed = parse_progress_line(line)
            if not parsed:
                continue
            if parsed["type"] == "time":
                current = self.stages[self._index]["duration"]
                if current <= 0:
                    continue
                stage_pct = min(1.0, max(0.0, parsed["t"] / current))
                value = self._overall_pct(stage_pct)
                if value > self._pct:
                    self._pct = value
                    self.progress.emit(value)
            elif parsed["type"] == "speed":
                label = self.stage_label
                self.status.emit(f"{label} · {parsed['speed']}" if label else parsed["speed"])
            elif parsed["type"] == "end":
                self.progress.emit(self._overall_pct(1.0))

    def _read_err(self):
        if self._proc is None:
            return
        self._err_buf += bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        *lines, self._err_buf = self._err_buf.split("\n")
        for line in lines:
            if not line.strip():
                continue
            self._stderr_lines.append(line)
            if len(self._stderr_lines) > STDERR_MAX_LINES:
                del self._stderr_lines[:1000]
            self.logLine.emit(line)

    # ---------------- 结束 ----------------
    def _on_error(self, err):
        if err == QProcess.FailedToStart:
            self.error = "ffmpeg 启动失败（检查 ffmpeg 路径/权限）"
            self.status.emit(self.error)

    def _on_finished(self, code, status):
        proc = self._proc
        if proc is not None and proc.bytesAvailable():
            self._read_err()
        self._proc = None
        if proc is not None:
            proc.deleteLater()

        ok = (code == 0 and status == QProcess.NormalExit and not self.cancelled)
        if not ok:
            if not self.cancelled and not self.error:
                tail = [ln for ln in self._stderr_lines if ln.strip()]
                self.error = tail[-1] if tail else f"ffmpeg 退出码 {code}"
            self.failed_command = self.command_line
            self.finalize(False)
            return

        self._done_duration += self.stages[self._index]["duration"]
        if self._index + 1 < len(self.stages):
            self._index += 1
            self._start_stage()
            return
        self.progress.emit(100)
        self.finalize(True)

    def finalize(self, ok: bool):
        if self.state == "done":
            return
        self.ok = ok
        self.state = "done"
        self.finished.emit(ok)


class JobQueue(QObject):
    """FIFO 串行队列：同一时刻最多 max_concurrent 个重任务。"""

    jobStarted = Signal(object)
    jobFinished = Signal(object)

    def __init__(self, max_concurrent: int = 1, parent=None):
        super().__init__(parent)
        self.max_concurrent = max(1, int(max_concurrent))
        self._pending: list[Job] = []
        self._running: list[Job] = []

    # ---------------- 查询 ----------------
    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def busy(self) -> bool:
        return bool(self._pending or self._running)

    def position(self, job: Job) -> int:
        """排队位置（1 起）；正在跑或已完成返回 0。"""
        try:
            return self._pending.index(job) + 1
        except ValueError:
            return 0

    # ---------------- 提交 ----------------
    def submit(self, job: Job):
        if job.state != "idle":
            return
        job.setParent(self)
        self._pending.append(job)
        self._pump()

    def cancel_all(self):
        pending, self._pending = self._pending, []
        for job in pending:
            job.cancel()
        for job in list(self._running):
            job.cancel()

    # ---------------- 调度 ----------------
    def _pump(self):
        while self._pending and len(self._running) < self.max_concurrent:
            job = self._pending.pop(0)
            if job.state != "idle" or job.cancelled:
                continue
            job.finished.connect(lambda ok, j=job: self._on_done(j, ok))
            self._running.append(job)
            job.start()
            if job.state == "running":
                self.jobStarted.emit(job)

    def _on_done(self, job: Job, ok: bool):
        if job in self._running:
            self._running.remove(job)
        self.jobFinished.emit(job)
        QTimer.singleShot(0, self._pump)
