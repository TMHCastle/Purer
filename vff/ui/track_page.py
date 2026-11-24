# -*- coding: utf-8 -*-
"""单个视频页：图墙（自带时间轴）+ 浮窗预览卡 + 区间导出。

交互模型（B 阶段定稿）：
  * 图墙本身就是时间轴：每行下方那条线是时间轴，刻度朝上，选中区间高亮；
  * 点任意一帧 -> 旁边浮出预览卡（不挤压布局），可用 ◀ ▶ 逐帧、I/O 设入出点；
  * 导出用「入点/出点」；Shift 框两帧仍然等价。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from PySide6.QtCore import (QElapsedTimer, QItemSelectionModel, QPoint, QSize, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QMenu,
                               QMessageBox, QSplitter, QVBoxLayout, QWidget)

from .. import ffmpeg, scenedetect, smartcut
from ..cache import cache_dir_for, cache_digest
from ..cache_store import STATE_NAME
from ..config import (DEFAULT_DISPLAY_SIDE, DISPLAY_MAX, DISPLAY_MIN, DISPLAY_STEP,
                      cache_root, preset_label)
from ..export_names import existing_names, plan_filenames
from ..jobs import Job, JobQueue
from ..media import MediaInfo, thumb_size_for
from ..scenedetect import SceneSampler
from ..timeline import SceneTimeline, StepTimeline
from ..util import (fmt_tc, fmt_tc_compact, human_size, next_play_index,
                    safe_filename)
from .dialogs import ask_batch_export, ask_export, show_log
from .filmstrip import PLAY_FPS, FilmStrip, PlayClip
from .images import jpeg_ready
from .overview_bar import OverviewBar
from .sidebar import SIDEBAR_MAX, SIDEBAR_MIN, EditSidebar
from .wall_view import WallView
from .widgets import LANE_H, FrameDelegate, FrameModel, FrameRef, PreviewDialog


class TrackPage(QWidget):
    reload_requested = Signal(object)
    display_side_changed = Signal(int)

    def __init__(self, path: Path, step: int, tw: int, th: int, info: MediaInfo,
                 queue: JobQueue, parent=None, display_side: int | None = None,
                 scene_threshold: float | None = None, store=None,
                 restore: dict | None = None):
        super().__init__(parent)
        self.path = Path(path)
        self.step = float(step)
        # 抽取方式：scene_threshold 有值 = 按镜头（阈值），否则 = 每 step 帧一张
        self.threshold = float(scene_threshold) if scene_threshold else 0.0
        self.mode = "scene" if scene_threshold else "step"
        self.scene_frames: list[int] = []       # 每个镜头的首帧
        self.scene_times: list[float] = []      # 同上，秒
        self.scene_scores: list[float] = []     # 检测分数（诊断用）
        self._analyzing = False
        self.sampler = SceneSampler(self)
        self.sampler.progress.connect(self._on_analyze_progress)
        self.sampler.finished.connect(self._on_analyze_done)
        self.timeline = StepTimeline(self.step, max(0, int(info.frames or 0)))
        self.tw, self.th = int(tw), int(th)          # 抽取尺寸（磁盘上的 jpg）
        self.display_side = int(display_side or DEFAULT_DISPLAY_SIDE)
        self.disp_w, self.disp_h = thumb_size_for(info.width, info.height,
                                                  self.display_side)
        self.info = info
        self.queue = queue
        self.store = store                      # 持久缓存库（MainWindow 传进来）
        self.cache_source = ""                  # "temp" / "persistent" / "new"
        self.used_seconds = 0.0                 # 本次会话里这个页签被使用的时间
        self.export_count = 0                   # 本次会话成功导出的次数

        self.out_dir: Path | None = None
        self.total = 0
        self.scan_pos = 0
        self.job: Job | None = None
        self.exp_job: Job | None = None
        self.exp_out: Path | None = None

        self.cursor_frame: int | None = None
        self.in_frame: int | None = None
        self.out_frame: int | None = None
        self.markers: list[int] = []       # M 键标记（单点，和入点/出点无关）
        self.segments: list[dict] = []
        self.exports: list[dict] = []      # 已导出的片段文件（打开 / 打开文件夹用）
        self._restore_pending: dict | None = dict(restore) if restore else None
        self._batch: dict | None = None
        self.batch_job: Job | None = None

        self.strip: FilmStrip | None = None
        self.playclip: PlayClip | None = None
        self._playing = False
        self._play_pending = False
        self._player_open = False
        self._play_index = 0
        self._play_clock = QElapsedTimer()
        self._play_base = 0
        self._syncing = False

        self._alive = True
        self._speed = ""
        self._finished = False
        self._final_total = 0
        self._result_ok = False
        self.quiet = False  # 自检模式：不弹模态框
        self._export_text = ""
        self._export_ctx: dict | None = None
        self._prepare_ctx: dict | None = None

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(260)
        self._timer.timeout.connect(self._scan)

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(int(1000 / PLAY_FPS))
        self._play_timer.timeout.connect(self._play_tick)

        # 拖动入点/出点时的预览节流：先给近似帧（墙上的缩略图，毫秒级），
        # 停手 140ms 后再补一张精确帧（走 FilmStrip 异步解码，不卡）。
        self._drag_frame: int | None = None
        self._drag_tick = QTimer(self)
        self._drag_tick.setSingleShot(True)
        self._drag_tick.setInterval(50)
        self._drag_tick.timeout.connect(self._on_drag_preview)
        self._drag_settle = QTimer(self)
        self._drag_settle.setSingleShot(True)
        self._drag_settle.setInterval(140)
        self._drag_settle.timeout.connect(self._on_drag_settle)

        self.queue.jobStarted.connect(self._on_job_started)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ---- 图墙（左，占满）+ 右侧编辑栏 ----
        self.view = WallView()
        self.view.setViewMode(WallView.IconMode)
        self.view.setFlow(WallView.LeftToRight)
        self.view.setWrapping(True)
        self.view.setResizeMode(WallView.Adjust)
        self.view.setMovement(WallView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setLayoutMode(WallView.Batched)
        self.view.setBatchSize(120)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setWordWrap(False)
        self.view.setSpacing(0)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.set_timeline(self.timeline)
        self.model = FrameModel(self)
        self.view.setModel(self.model)
        self.view.setItemDelegate(FrameDelegate(self.disp_w, self.disp_h, self.view))
        self.view.setGridSize(QSize(self.disp_w + 10, self.disp_h + 10 + 22 + LANE_H))
        self.view.selectionModel().currentChanged.connect(self._on_current)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.view.doubleClicked.connect(self._open_big)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.zoomRequested.connect(self._on_wheel_zoom)
        self.view.rangeMoved.connect(self._on_range_drag)
        self.view.rangeEdited.connect(self._end_range_drag)
        self.view.rangeDragged.connect(self._on_wall_range_drag)
        self.view.rangeSelected.connect(self._on_wall_range_selected)
        self.view.markerActivated.connect(self._goto_marker_frame)

        self.side = EditSidebar()
        self.side.stopRequested.connect(self.stop_extract)
        self.side.reloadRequested.connect(lambda: self.reload_requested.emit(self))
        self.side.commandRequested.connect(self._show_extract_command)
        self.side.stepRequested.connect(self._on_sidebar_step)
        self.side.setInRequested.connect(lambda: self.set_in_point())
        self.side.setOutRequested.connect(lambda: self.set_out_point())
        self.side.markerAddRequested.connect(lambda: self.add_marker())
        self.side.copyTimecodeRequested.connect(self._copy_tc)
        self.side.copyFrameRequested.connect(self._copy_img)
        self.side.playRequested.connect(self.toggle_playback)
        self.side.exportRequested.connect(self.export_segment)
        self.side.addSegmentRequested.connect(self.add_segment)
        self.side.clearSegmentsRequested.connect(self.clear_segments)
        self.side.removeSegmentRequested.connect(self.remove_segment)
        self.side.selectSegmentRequested.connect(self.select_segment)
        self.side.exportAllSegmentsRequested.connect(self.export_all_segments)
        self.side.openExportRequested.connect(self.open_export)
        self.side.folderExportRequested.connect(self.reveal_export)

        # 图墙 | 右侧面板 之间可拖拽（宽度由 MainWindow 记住）
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.setHandleWidth(8)
        self.split.addWidget(self.view)
        self.split.addWidget(self.side)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([900, 372])
        root.addWidget(self.split, 1)

        # 底部总进度条：整段视频一条，和墙上的时间轴共用同一份入点/出点/光标/片段
        self.overview = OverviewBar()
        self.overview.set_media(self.info.frames or 0, self.info.fps or 25.0)
        root.addWidget(self.overview)
        self.overview.seekRequested.connect(self._seek_overview)
        self.overview.inMoved.connect(lambda f: self._on_range_drag("in", f))
        self.overview.outMoved.connect(lambda f: self._on_range_drag("out", f))
        self.overview.editFinished.connect(self._end_range_drag)
        self.overview.setInRequested.connect(self.set_in_point)
        self.overview.setOutRequested.connect(self.set_out_point)
        self.overview.clearRangeRequested.connect(self.clear_range)
        self.overview.addSegmentRequested.connect(self.add_segment)
        self.overview.segmentActivated.connect(self.select_segment)
        self.overview.markerActivated.connect(self._goto_marker_frame)
        self.overview.addMarkerRequested.connect(self.add_marker)
        self.overview.removeMarkerRequested.connect(self.remove_marker)
        self._refresh_markers()

        # 兼容别名：导出/批处理等既有逻辑继续用原来的控件名
        self.lbl_meta = self.side.lbl_info
        self.lbl_count = self.side.lbl_count
        self.bar = self.side.bar
        self.btn_stop = self.side.btn_stop
        self.btn_reload = self.side.btn_reload
        self.btn_cmd = self.side.btn_cmd
        self.exp_bar = self.side.exp_bar
        self.lbl_export = self.side.lbl_status
        self.btn_export = self.side.btn_export
        self.btn_play = self.side.btn_play
        self.lbl_range = self.side.lbl_range
        self.seg_bar = self.side

        self.side.set_extract_progress(0, False, False)
        self.side.set_aspect(self.info.width, self.info.height)
        self._install_shortcuts()
        self._refresh_range()

    def _install_shortcuts(self):
        def bind(seq, fn):
            sc = QShortcut(QKeySequence(seq), self.view)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
            return sc

        self._sc = [
            bind("Left", lambda: self.step_frames(-1)),
            bind("Right", lambda: self.step_frames(1)),
            bind("Shift+Left", lambda: self.step_seconds(-1)),
            bind("Shift+Right", lambda: self.step_seconds(1)),
            bind("PageUp", lambda: self.step_frames(-self._step_int())),
            bind("PageDown", lambda: self.step_frames(self._step_int())),
            bind("Home", self._go_first),
            bind("End", self._go_last),
            bind("I", lambda: self.set_in_point()),
            bind("O", lambda: self.set_out_point()),
            bind("A", self.add_segment),
            bind("M", self.add_marker),
            bind("Shift+M", lambda: self.goto_marker(1)),
            bind("Ctrl+Shift+M", lambda: self.goto_marker(-1)),
            bind("=", lambda: self._on_wheel_zoom(1)),
            bind("+", lambda: self._on_wheel_zoom(1)),
            bind("-", lambda: self._on_wheel_zoom(-1)),
            bind("Space", self.toggle_playback),
            bind("Escape", self._on_escape),
        ]

    # ------------------------------------------------------------- 抽帧
    def _step_int(self) -> int:
        """PageUp/PageDown 用：跳一个抽取格（按镜头时用平均镜头长度）。"""
        return max(1, int(round(self.timeline.guess_step())))

    def _fps(self) -> float:
        return float(self.info.fps or 25.0)

    def _frame_for_index(self, index: int) -> int | None:
        """第 index 张缩略图（0 起）对应源视频的哪一帧。"""
        if self.mode != "scene":
            return int(round(index * self.step))
        if 0 <= index < len(self.scene_frames):
            return int(self.scene_frames[index])
        return None

    def _sync_timeline(self):
        """把「第几张 = 哪一帧」同步给图墙（按镜头时帧号不规则）。"""
        if self.mode != "scene":
            self.timeline = StepTimeline(self.step, max(0, int(self.info.frames or 0)))
        else:
            frames = [self.model.item(i).frame for i in range(self.model.rowCount())
                      if self.model.item(i) is not None]
            self.timeline = SceneTimeline(frames, max(0, int(self.info.frames or 0)))
        self.view.set_timeline(self.timeline)
        self._sync_overview()

    def start(self):
        i = self.info
        self.scene_frames = []
        self.scene_times = []
        self.scene_scores = []
        if self.mode == "scene":
            self.total = 0                     # 镜头数要检测完才知道
            self.side.set_video_info(
                f"{i.resolution} · {i.fps:.4g} FPS · {fmt_tc(i.duration)}",
                f"共 {i.frames} 帧 · 按镜头（{preset_label(self.threshold)}）")
        else:
            est = max(1, int(round(i.frames / self.step))) if i.frames else 1
            self.total = est
            self.side.set_video_info(
                f"{i.resolution} · {i.fps:.4g} FPS · {fmt_tc(i.duration)}",
                f"共 {i.frames} 帧 · 每 {self.step:g} 帧 1 张 · "
                f"≈{self.step / max(i.fps, 1e-6):.2f} 秒")
        self.side.set_aspect(i.width, i.height)
        self.out_dir = cache_dir_for(self.path, self.step, self.tw, self.th,
                                     mode=self.mode, threshold=self.threshold)
        try:
            cache_root().mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.strip = FilmStrip(self.path, self.info, self.out_dir, self)
        self.strip.windowReady.connect(self._on_strip_ready)
        self.playclip = PlayClip(self.path, self.info, self.out_dir, self)
        self.playclip.ready.connect(self._on_play_ready)
        self.playclip.failed.connect(self._on_play_failed)
        if (self.out_dir / ".done").exists() and self._load_cache():
            self.cache_source = "temp"
            self._touch_persistent_hit()
            return
        if self._load_persistent_cache():
            return
        self.cache_source = "new"
        shutil.rmtree(self.out_dir, ignore_errors=True)
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.lbl_meta.setText(f"⚠ 无法创建缓存目录：{e}")
            self._finished = True
            return
        self._launch()

    def _launch(self):
        if self.mode == "scene":
            self._start_analysis()
            return
        variants = ffmpeg.build_extract_variants(
            self.path, self.step, self.tw, self.th, self.out_dir,
            src_fps=self.info.fps)
        self._submit_extract(variants, f"抽帧 {self.path.name}")

    # ------------------------------------------------- 按镜头：先分析再抽帧
    def _start_analysis(self):
        """第一遍：把整片缩成 32×18 灰度、算直方图，找镜头切点。"""
        self.lbl_count.setText("分析镜头…")
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_stop.setVisible(True)
        self.sampler.start(self.path, self.info.frames)
        self._analyzing = True

    def _on_analyze_progress(self, pct: int):
        if not self._analyzing:
            return
        self.bar.setValue(int(max(0, min(100, pct)) * 0.55))
        self.lbl_count.setText(f"分析镜头… {pct}%")

    def _on_analyze_done(self, histograms, ok: bool, error: str):
        self._analyzing = False
        if not self._alive:
            return
        if not ok or not histograms:
            self.bar.setVisible(False)
            self.btn_stop.setVisible(False)
            self._finished = True
            self._result_ok = False
            self.lbl_count.setText("⚠ 镜头分析失败")
            if not self.quiet:
                show_log(self, "镜头分析失败",
                         f"《{self.path.name}》按镜头分析失败。\n{error}", "", "")
            return
        cuts = scenedetect.detect_from_histograms(histograms, self._fps(),
                                                  self.threshold)
        frames = scenedetect.cut_frames(cuts)
        self.scene_frames = frames
        self.scene_scores = [float(c.get("score", 0.0)) for c in cuts]
        self.scene_times = [f / self._fps() for f in frames]
        self.lbl_count.setText(f"共 {len(frames)} 个镜头，抽帧中…")
        variants = ffmpeg.build_frames_extract_variants(
            self.path, frames, self.tw, self.th, self.out_dir)
        self._submit_extract(variants, f"按镜头抽帧 {self.path.name}")

    def _submit_extract(self, variants, label: str):
        job = Job(variants[0], duration=self.info.duration, label=label,
                  alts=variants[1:], stderr_limit=200000)
        self.job = job
        job.progress.connect(self._on_extract_progress)
        job.status.connect(self._on_extract_status)
        job.finished.connect(self._on_extract_finished)
        self.bar.setVisible(True)
        self.btn_stop.setVisible(True)
        self.lbl_count.setText("排队中…")
        self.queue.submit(job)
        if job.state == "idle":
            pos = self.queue.position(job)
            if pos:
                self.lbl_count.setText(f"排队中…（第 {pos} 位）")

    def _on_job_started(self, job):
        if job is self.job:
            self.bar.setVisible(True)
            self.bar.setValue(0)
            self.btn_stop.setVisible(True)
            self.lbl_count.setText("抽帧中…")
            self._timer.start()
        elif job is self.exp_job:
            self.side.set_export_text(self._export_text or "导出中…")

    def _on_extract_progress(self, pct: int):
        # 按镜头时前 55% 是分析阶段，抽帧阶段接在后面
        value = int(55 + max(0, min(100, pct)) * 0.45) if self.mode == "scene" else pct
        if value > self.bar.value():
            self.bar.setValue(value)

    def _on_extract_status(self, text: str):
        self._speed = text
        if self.job is not None and self.job.state == "running":
            if self.mode == "scene":
                self.lbl_count.setText(
                    f"已抽 {self.model.rowCount()} / {len(self.scene_frames)} 个镜头 · {text}")
            else:
                self.lbl_count.setText(
                    f"已抽 {self.model.rowCount()} 张 / 约 {self.total} · {text}")

    def _on_extract_finished(self, ok: bool):
        job = self.job
        self._finished = True
        self._result_ok = ok
        self.bar.setVisible(False)
        self.btn_stop.setVisible(False)
        self._timer.setInterval(60)
        self._timer.start()
        if ok:
            if self.mode == "scene":
                self._save_scene_meta()
            try:
                self._final_total = len(list(self.out_dir.glob("f_*.jpg")))
            except OSError:
                self._final_total = 0
            if self.mode == "scene" and self.scene_frames:
                self._final_total = min(self._final_total, len(self.scene_frames))
            if self._final_total > 0:
                try:
                    (self.out_dir / ".done").write_text(str(self._final_total),
                                                        encoding="utf-8")
                except OSError:
                    pass
                self.bar.setValue(100)
        elif job is not None and job.cancelled:
            self.lbl_count.setText(
                f"已停止 · {self.model.rowCount()} 张（未完成，下次会重新抽）")
        else:
            self.lbl_count.setText("⚠ 抽帧失败")
            if job is not None and not self.quiet:
                show_log(self, "抽帧失败",
                         f"《{self.path.name}》抽帧失败。\n{job.error}",
                         job.command_line, job.stderr_text)
        self._scan(force=True)
        self.job = None
        self._refresh_range()
        self._apply_pending_restore()            # 抽完才恢复，否则行数还没齐

    def stop_extract(self):
        if self.job is not None:
            self.job.cancel()
        if self._analyzing:
            self._analyzing = False
            self.sampler.cancel()
            self.bar.setVisible(False)
            self.btn_stop.setVisible(False)
            self._finished = True
            self._result_ok = False
            self.lbl_count.setText("已停止（镜头分析未完成，下次会重来）")

    @property
    def busy(self) -> bool:
        if self._analyzing:
            return True
        return self.job is not None and self.job.state != "done"

    @property
    def exporting(self) -> bool:
        return self.exp_job is not None

    # ------------------------------------------------- 持久缓存（保存下来的图墙）
    def cache_digest(self) -> str:
        """这个视频 + 这组设置的缓存 id（临时目录名 / 持久库条目 id）。"""
        return cache_digest(self.path, self.step, self.tw, self.th,
                            mode=self.mode, threshold=self.threshold)

    def cache_ready(self) -> bool:
        """本次抽帧已经完成（图墙完整），可以存进持久库。"""
        if not self.out_dir or not self._finished or not self._result_ok:
            return False
        if self.model.rowCount() <= 0:
            return False
        try:
            return (self.out_dir / ".done").is_file()
        except OSError:
            return False

    def cache_entry(self) -> dict:
        """给持久库的元信息。"""
        try:
            stat = self.path.stat()
            size, mtime = stat.st_size, int(stat.st_mtime)
        except OSError:
            size, mtime = 0, 0
        return {
            "video": str(self.path),
            "video_size": size,
            "video_mtime": mtime,
            "mode": self.mode,
            "step": self.step if self.mode != "scene" else None,
            "threshold": self.threshold if self.mode == "scene" else None,
            "tw": self.tw,
            "th": self.th,
            "frames": int(self.info.frames or 0),
        }

    # ------------------------------------------------- 片段状态（跟着缓存放）
    def state_payload(self) -> dict:
        """这一页的「片段列表 / 入点出点 / 导出记录」，会写进缓存目录。"""
        return {
            "version": 1,
            "segments": [
                {"t_in": round(float(s["t_in"]), 4),
                 "t_end": round(float(s["t_end"]), 4),
                 "name": str(s.get("name") or "")}
                for s in self.segments],
            "range": {"in": self.in_frame, "out": self.out_frame},
            "markers": sorted(int(m) for m in self.markers),
            "exports": [dict(item) for item in self.exports[-200:]],
            "updated_at": time.time(),
        }

    def save_state(self) -> bool:
        """把片段状态写进临时缓存目录；持久库里有同一条缓存就一起更新。"""
        if not self.out_dir:
            return False
        payload = self.state_payload()
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / STATE_NAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            return False
        if self.store is not None:
            try:
                self.store.write_state(self.cache_digest(), payload)
            except Exception:      # noqa: BLE001  状态写失败不能影响导出
                pass
        return True

    def _read_state(self, folder: Path | None = None) -> dict:
        path = (folder or self.out_dir)
        if path is None:
            return {}
        try:
            data = json.loads((path / STATE_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def apply_state(self, payload: dict) -> bool:
        """把读到的片段状态套回界面（片段列表 + 入点出点 + 导出记录）。"""
        if not isinstance(payload, dict):
            return False
        fps = self._fps()
        segments = []
        for item in payload.get("segments") or []:
            if not isinstance(item, dict):
                continue
            try:
                t_in = float(item.get("t_in"))
                t_end = float(item.get("t_end"))
            except (TypeError, ValueError):
                continue
            if t_end <= t_in:
                continue
            segments.append({"t_in": t_in, "t_end": t_end,
                             "name": str(item.get("name") or "")})
        self.segments = segments
        rng = payload.get("range") or {}
        limit = max(0, int(self.info.frames or 0) - 1)
        try:
            in_frame = rng.get("in")
            out_frame = rng.get("out")
            if in_frame is not None:
                self.in_frame = max(0, min(limit, int(in_frame)))
            if out_frame is not None:
                self.out_frame = max(0, min(limit, int(out_frame)))
        except (TypeError, ValueError):
            pass
        markers = []
        for value in payload.get("markers") or []:
            try:
                frame = int(value)
            except (TypeError, ValueError):
                continue
            frame = max(0, min(limit, frame))
            if frame not in markers:
                markers.append(frame)
        self.markers = sorted(markers)
        exports = []
        for item in payload.get("exports") or []:
            if not isinstance(item, dict):
                continue
            target = str(item.get("path") or "")
            if not target:
                continue
            exports.append({"path": target,
                            "at": float(item.get("at") or 0),
                            "size": int(item.get("size") or 0),
                            "kind": str(item.get("kind") or "")})
        self.exports = exports
        self._refresh_segments()
        self._refresh_markers()
        self.side.set_exports(self.exports)
        self._refresh_range()
        return bool(segments or exports or rng or self.markers)

    # ------------------------------------------------- 会话快照（跨重启恢复）
    def session_snapshot(self) -> dict:
        """这一页需要跨重启记住的东西（session.py 的 tab 结构）。

        还没套上「待恢复状态」的页面（刚建好、图墙还在抽）：用待恢复值兜底，
        否则中途存盘会把片段/入出点抹掉。
        """
        pending = self._restore_pending or {}
        row = 0
        try:
            index = self.view.indexAt(QPoint(4, 4))
            if index.isValid():
                row = index.row()
        except RuntimeError:
            row = 0

        segments = [{"t_in": float(s["t_in"]),
                     "t_end": float(s["t_end"]),
                     "name": str(s.get("name") or "")}
                    for s in self.segments]
        if not segments:
            segments = list(pending.get("segments") or [])
        exports = [dict(item) for item in self.exports[-100:]]
        if not exports:
            exports = list(pending.get("exports") or [])
        markers = sorted({int(m) for m in self.markers})
        if not markers:
            markers = list(pending.get("markers") or [])

        def _pick(key, value):
            return value if value is not None else pending.get(key)

        return {
            "path": str(self.path),
            "step": self.step,
            "mode": self.mode,
            "threshold": self.threshold if self.mode == "scene" else None,
            "display_side": self.display_side,
            "sidebar_width": self.sidebar_width(),
            "cursor_frame": _pick("cursor_frame", self.cursor_frame),
            "in_frame": _pick("in_frame", self.in_frame),
            "out_frame": _pick("out_frame", self.out_frame),
            "scroll_row": row or int(pending.get("scroll_row") or 0),
            "segments": segments,
            "exports": exports,
            "markers": markers,
        }

    def session_restore(self, data: dict) -> bool:
        """把会话里的片段/入出点/光标/滚动套回界面（图墙就绪之后调用）。"""
        if not isinstance(data, dict):
            return False
        limit = max(0, int(self.info.frames or 0) - 1)

        def _frame(key):
            value = data.get(key)
            if value is None:
                return None
            try:
                return max(0, min(limit, int(value)))
            except (TypeError, ValueError):
                return None

        in_frame, out_frame = _frame("in_frame"), _frame("out_frame")
        if in_frame is not None and out_frame is not None and out_frame <= in_frame:
            out_frame = None
        self.in_frame, self.out_frame = in_frame, out_frame

        segments = data.get("segments") or []
        exports = data.get("exports") or []
        markers = data.get("markers") or []
        if segments or exports or markers:
            self.apply_state({"segments": segments,
                              "range": {"in": in_frame, "out": out_frame},
                              "exports": exports,
                              "markers": markers})
        else:
            self._refresh_markers()
            self._refresh_range()

        cursor = _frame("cursor_frame")
        if cursor is None:
            cursor = in_frame if in_frame is not None else 0
        if self.model.rowCount():
            self._set_cursor(cursor)

        try:
            width = int(data.get("sidebar_width") or 0)
        except (TypeError, ValueError):
            width = 0
        if width > 0:
            self.set_sidebar_width(width)

        try:
            row = int(data.get("scroll_row") or 0)
        except (TypeError, ValueError):
            row = 0
        if 0 < row < self.model.rowCount():
            self.view.scrollTo(self.model.index(row),
                               QAbstractItemView.PositionAtTop)
        return True

    def _apply_pending_restore(self) -> bool:
        """图墙一就绪就把待恢复的状态套上去（只做一次）。"""
        data, self._restore_pending = self._restore_pending, None
        if not data or not self.model.rowCount():
            return False
        try:
            return self.session_restore(data)
        except Exception:      # noqa: BLE001  恢复失败不能影响看图
            return False

    def _record_export(self, out: Path, kind: str = ""):
        """记一条「已导出的片段文件」，顺手把片段状态存进缓存。"""
        try:
            size = out.stat().st_size
        except OSError:
            size = 0
        entry = {"path": str(out), "at": time.time(), "size": size,
                 "kind": kind}
        self.exports = [e for e in self.exports
                        if str(e.get("path")) != str(out)] + [entry]
        self.side.set_exports(self.exports)
        self.save_state()

    def open_export(self, path: str):
        """用系统默认播放器打开一个已导出的片段。"""
        target = Path(str(path or ""))
        if not target.is_file():
            self.side.set_export_text(f"⚠ 文件不在了：{target.name}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def reveal_export(self, path: str):
        """打开已导出片段所在的文件夹（没给路径就用最近一条）。"""
        target = Path(str(path or ""))
        folder = target.parent if target.suffix else target
        if not folder.is_dir():
            self.side.set_export_text("⚠ 文件夹不在了")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _load_persistent_cache(self) -> bool:
        """临时目录没有、但持久库里有 -> 恢复一份进来（硬链接，几乎瞬间）。"""
        if self.store is None or self.out_dir is None:
            return False
        digest = self.cache_digest()
        if not self.store.has(digest):
            return False
        shutil.rmtree(self.out_dir, ignore_errors=True)
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        if not self.store.materialize(digest, self.out_dir):
            shutil.rmtree(self.out_dir, ignore_errors=True)
            return False
        if not self._load_cache():
            shutil.rmtree(self.out_dir, ignore_errors=True)
            return False
        self.cache_source = "persistent"
        self.lbl_count.setToolTip("来自已保存的缓存（下次打开同样能直接加载）")
        return True

    def _touch_persistent_hit(self):
        """临时缓存命中也算「用过这个保存的缓存」，让 LRU 更准确。"""
        if self.store is None:
            return
        try:
            self.store.touch(self.cache_digest())
        except Exception:      # noqa: BLE001  统计失败不能影响看片
            pass

    def _load_cache(self) -> bool:
        files = sorted(self.out_dir.glob("f_*.jpg"))
        if not files:
            return False
        if self.mode == "scene":
            meta = self.out_dir / "scene.json"
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                frames = [int(x) for x in data.get("frames") or []]
                times = [float(x) for x in data.get("times") or []]
                scores = [float(x) for x in data.get("scores") or []]
            except (OSError, ValueError, TypeError):
                return False
            if len(frames) < len(files):
                return False
            self.scene_frames = frames[:len(files)]
            self.scene_times = times[:len(files)] if len(times) >= len(files) else []
            self.scene_scores = scores[:len(files)]
        refs = []
        for n, f in enumerate(files):
            frame = self._frame_for_index(n)
            if frame is None:
                return False
            refs.append(FrameRef(str(f), frame / max(self.info.fps, 1e-6), frame))
        self.model.append(refs)
        self.scan_pos = len(files)
        self._sync_timeline()
        self.bar.setValue(100)
        self.bar.setVisible(False)
        self.btn_stop.setVisible(False)
        self._finished = True
        self._result_ok = True
        self._final_total = len(files)
        self._update_count_label("已抽")
        self.lbl_count.setToolTip("来自缓存（同一视频同设置，秒开）")
        if self.model.rowCount():
            self.view.setCurrentIndex(self.model.index(0))
        self.apply_state(self._read_state())     # 片段 / 入点出点 / 导出记录
        self._apply_pending_restore()            # 会话快照优先于缓存里的旧状态
        return True

    def _save_scene_meta(self):
        """把「第几张 = 哪一帧」写进缓存目录，下次秒开不用重新分析。"""
        if not self.out_dir:
            return
        try:
            (self.out_dir / "scene.json").write_text(
                json.dumps({"mode": "scene", "threshold": self.threshold,
                            "frames": self.scene_frames,
                            "times": [round(t, 4) for t in self.scene_times],
                            "scores": [round(s, 4) for s in self.scene_scores]},
                           ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    def _update_count_label(self, prefix: str):
        rows = self.model.rowCount()
        if self.mode == "scene":
            self.lbl_count.setText(f"{prefix} {rows} 个镜头")
        else:
            self.lbl_count.setText(f"{prefix} {rows} 张")

    def _scan(self, force=False):
        if not self._alive or not self.out_dir:
            return
        extracting = self.job is not None and self.job.state == "running"
        refs = []
        budget = 800 if force else 300
        while len(refs) < budget:
            n = self.scan_pos + 1
            f = self.out_dir / f"f_{n:06d}.jpg"
            try:
                if not f.exists():
                    break
                if extracting and not force:
                    # 正在写最后一帧时先不读：下一帧出现 = 这一帧已写完
                    if not (self.out_dir / f"f_{n + 1:06d}.jpg").exists():
                        break
            except OSError:
                break
            if not jpeg_ready(f):
                if force:
                    self.scan_pos = n  # 坏帧跳过，避免永远卡在同一张
                    continue
                break
            frame = self._frame_for_index(n - 1)
            if frame is None:
                if force:              # 按镜头：切点还没到/被合并掉了，跳过这张
                    self.scan_pos = n
                    continue
                break
            refs.append(FrameRef(str(f), frame / max(self.info.fps, 1e-6), frame))
            self.scan_pos = n
        if refs:
            first = self.model.rowCount() == 0
            self.model.append(refs)
            self._sync_timeline()
            if first:
                self.view.setCurrentIndex(self.model.index(0))

        rows = self.model.rowCount()
        if extracting:
            extra = f" · {self._speed}" if self._speed else ""
            if self.mode == "scene":
                self.lbl_count.setText(f"已抽 {rows} 个镜头{extra}")
            else:
                self.lbl_count.setText(f"已抽 {rows} 张 / 约 {self.total}{extra}")
        elif self._finished:
            if self._final_total and rows < self._final_total:
                self.lbl_count.setText(f"载入中 {rows}/{self._final_total} 张")
            elif self._result_ok:
                self.lbl_count.setText(
                    f"已抽 {rows} 个镜头" if self.mode == "scene" else f"已抽 {rows} 张")
            if not refs:
                self._timer.stop()
        self.view.set_markers(self.in_frame, self.out_frame, self.cursor_frame)
        self._sync_overview()

    # ------------------------------------------------------- 光标 / 预览卡
    def _on_current(self, cur, _prev):
        if self._syncing or not (cur and cur.isValid()):
            return
        if self.view.box_selecting():
            # 随意框选过程中，原生多选会把 current 挪来挪去；以按下点/当前点为准
            return
        ref = self.model.item(cur.row())
        if ref is None:
            return
        self._set_cursor(ref.frame, sync_wall=False)

    def _set_cursor(self, frame: int, sync_wall: bool = True):
        total = max(1, int(self.info.frames or 0))
        frame = max(0, min(total - 1, int(frame)))
        self.cursor_frame = frame
        if sync_wall:
            self._sync_wall_selection(frame)
        self.view.set_markers(self.in_frame, self.out_frame, frame)
        self._sync_overview()
        if self._playing or self._player_open:
            # 回放/暂停时只刷新播放窗口，不重定位浮窗，否则会一直抖
            self._update_player()
        else:
            self._refresh_range()
            self._show_card()

    def _sync_wall_selection(self, frame: int):
        row = self.timeline.index_of(frame)[0]
        row = max(0, min(self.model.rowCount() - 1, row)) if self.model.rowCount() else -1
        if row < 0:
            return
        index = self.model.index(row)
        # 光标落在已框选区间内时，保留 Shift 多选（不要被 setCurrentIndex 清掉）
        keep = (self.in_frame is not None and self.out_frame is not None
                and self.in_frame <= frame <= self.out_frame
                and self.view.selectionModel().hasSelection())
        if keep and self.view.currentIndex() == index:
            self.view.scrollTo(index, QAbstractItemView.EnsureVisible)
            return
        self._syncing = True
        try:
            if keep:
                self.view.selectionModel().setCurrentIndex(
                    index, QItemSelectionModel.NoUpdate)
            else:
                self.view.setCurrentIndex(index)
            self.view.scrollTo(index, QAbstractItemView.EnsureVisible)
        finally:
            self._syncing = False

    def step_frames(self, delta: int):
        base = self.cursor_frame if self.cursor_frame is not None else 0
        self._set_cursor(base + int(delta))

    def step_seconds(self, seconds: float):
        fps = self.info.fps or 25.0
        self.step_frames(int(round(seconds * fps)))

    def _go_first(self):
        self._set_cursor(0)

    def _go_last(self):
        total = max(1, int(self.info.frames or 0))
        self._set_cursor(total - 1)

    def _item_rect(self, frame: int):
        """某一帧关联的缩略图矩形（用于取占位图）。"""
        if not self.model.rowCount():
            return None
        row = self.timeline.index_of(frame)[0]
        row = max(0, min(self.model.rowCount() - 1, row))
        return self.view.visualRect(self.model.index(row))

    def _show_card(self):
        """右侧编辑栏常驻，这里只负责刷新预览（保留旧名字给调用点用）。"""
        if self._player_open:
            return
        self._update_card()

    def _reposition_card(self, *_args):
        return

    def _hide_card(self):
        return

    def _update_card(self, approx: bool = False):
        frame = self.cursor_frame
        if frame is None:
            return
        fps = self.info.fps or 25.0
        t = frame / max(fps, 1e-6)
        marks = []
        if self.in_frame == frame:
            marks.append("入点")
        if self.out_frame == frame:
            marks.append("出点")
        suffix = f" · {'/'.join(marks)}" if marks else ""
        title = f"第 {frame} 帧 · {fmt_tc(t)}{suffix}"
        self.side.set_frame_marks(marks)

        pixmap = None
        if self.strip is not None:
            path = self.strip.path_for(frame)
            if path is not None:
                pixmap = QPixmap(str(path))
            elif not approx:
                # approx=True（拖动中）不在这里取帧：每移动一次就杀一次 ffmpeg 会卡
                self.strip.ensure(frame)
        if pixmap is None or pixmap.isNull():
            # 先用墙上的缩略图占位，避免空白
            ref_rect = self._item_rect(frame)
            if ref_rect is not None:
                ref = self.model.item(self.timeline.index_of(frame)[0])
                if ref is not None:
                    pixmap = QPixmap(ref.path)
        self.side.set_frame(pixmap, title)

    # ------------------------------------------------- 拖动中的预览跟随
    def _on_drag_preview(self):
        """拖动过程中刷新预览（近似帧：墙上的缩略图，立刻出来）。"""
        if not self._alive or self._player_open or self._drag_frame is None:
            return
        self._update_card(approx=True)

    def _on_drag_settle(self):
        """停在某处不动了：补一张精确帧。"""
        if not self._alive or self._player_open or self._drag_frame is None:
            return
        self._update_card()

    def _update_player(self):
        if self.playclip is None or self.cursor_frame is None:
            return
        pixmap = self.playclip.pixmap(self._play_index)
        fps = self.info.fps or 25.0
        cur = fmt_tc(self.cursor_frame / max(fps, 1e-6))
        total = fmt_tc(self.out_frame / fps) if self.out_frame is not None else "--:--:--"
        count = max(1, self.playclip.count)
        self.side.set_frame(pixmap, f"第 {self.cursor_frame} 帧 · {cur} / {total}")
        self.side.set_play_state(self._playing, self._play_index / max(1, count - 1))

    def _on_strip_ready(self, _start: int):
        if self.cursor_frame is not None and not self._playing:
            self._update_card()

    def _on_sidebar_step(self, delta: int):
        if abs(delta) >= 1000:
            self.step_seconds(delta / 1000.0)
        else:
            self.step_frames(delta)

    # ------------------------------------------------------------- 回放
    def toggle_playback(self):
        if self._playing:
            self._pause_playback()
            return
        if self._player_open:          # 暂停状态 -> 继续
            self._resume_playback()
            return
        if self.in_frame is None or self.out_frame is None or self.out_frame <= self.in_frame:
            self.side.set_export_text("先设入点和出点，再回放")
            return
        if self.playclip is None:
            return
        self._play_pending = True
        self.btn_play.setText("▶ 播放中…")
        self.side.set_export_text("准备回放…")
        self.playclip.prepare(self.in_frame, self.out_frame)
        # 已经缓存过时会同步发 ready，所以真正的启动在信号里

    def _on_play_ready(self):
        if not self._play_pending:
            return
        self._play_pending = False
        self._playing = True
        self._play_index = 0
        self._player_open = True
        self._apply_play_speed()
        self.side.set_play_state(True, 0.0)
        self.side.set_export_text(self._play_hint())
        self._set_cursor(self.in_frame, sync_wall=True)
        self._play_timer.start()

    def _apply_play_speed(self):
        """回放按**真实时长**走：用挂钟推进，UI 慢了也不会跑快/跑慢。

        以前固定 12fps tick：短选段差不多实时，长选段（stride 8）会快 4 倍，
        看着就是「预览跑得飞快、跟时间轴对不上」。现在 tick 只负责刷新，
        当前该显示第几张由 ``elapsed / 1000 * (fps / stride)`` 算出来。
        """
        self._play_clock.restart()
        self._play_base = int(self._play_index)
        stride = max(1, int(getattr(self.playclip, "stride", 1) or 1))
        fps = max(1.0, float(self.info.fps or 25.0))
        rate = max(0.5, fps / stride)              # 每秒推进多少张
        self._play_timer.setInterval(max(16, min(120, int(500.0 / rate))))

    def _play_hint(self) -> str:
        stride = max(1, int(getattr(self.playclip, "stride", 1) or 1))
        fps = max(1.0, float(self.info.fps or 25.0))
        rate = fps / stride
        tail = f"（≈{rate:.1f} fps 抽稀，时间与素材一致）" if stride > 1 else ""
        # 片尾那一两张常常落在「估计出来的末帧」之外，能播多少就播多少
        short = int(getattr(self.playclip, "count", 0) or 0)
        note = ""
        if getattr(self.playclip, "partial", 0) and short:
            note = f" · 可播 {short} 张（片尾不足）"
        return f"回放中…{tail}{note}（空格暂停 · Esc 停止）"

    def _pause_playback(self):
        if not self._playing:
            return
        self._playing = False
        self._play_timer.stop()
        self.side.set_play_state(False,
                                 self._play_index / max(1, (self.playclip.count - 1)
                                                        if self.playclip else 1))
        self.side.set_export_text("已暂停（空格继续）")

    def _resume_playback(self):
        if self._playing or self.playclip is None:
            return
        self._playing = True
        self._apply_play_speed()
        self.side.set_play_state(True,
                                 self._play_index / max(1, self.playclip.count - 1))
        self.side.set_export_text(self._play_hint())
        self._play_timer.start()

    def _on_play_failed(self, why: str):
        self._play_pending = False
        self._playing = False
        self._player_open = False
        self._play_timer.stop()
        self.side.set_play_state(None)
        self.side.set_export_text(f"回放准备失败：{why}")

    def _play_tick(self):
        if not self._playing or self.playclip is None:
            return
        count = max(0, int(getattr(self.playclip, "count", 0) or 0))
        stride = max(1, int(getattr(self.playclip, "stride", 1) or 1))
        fps = max(1.0, float(self.info.fps or 25.0))
        rate = max(0.5, fps / stride)                    # 每秒推进多少张
        want = self._play_base + int(self._play_clock.elapsed() * rate / 1000.0)
        index, finished = next_play_index(want, count, self._play_index)
        if finished:
            # 最后一张已经显示过（或本来就空）：收尾。
            # 选段帧数不能被 stride 整除时，最后一张会小于出点；
            # 以前只靠 frame >= out_frame 判断，会永远停不下来。
            self.stop_playback(at_end=True)
            return
        if index <= self._play_index:
            return                                       # 还没到下一张（省一次刷新）
        if self.playclip.path_for(index) is None:
            self.stop_playback(at_end=True)
            return
        self._play_index = index
        frame = self.playclip.frame_at(index)
        if self.out_frame is not None and frame >= self.out_frame:
            self.stop_playback(at_end=True)
            return
        self._set_cursor(frame, sync_wall=True)

    def stop_playback(self, at_end: bool = False):
        was = self._playing or self._play_pending or self._player_open
        self._playing = False
        self._play_pending = False
        self._player_open = False
        self._play_timer.stop()
        self.side.set_play_state(None)
        if not was:
            return
        if at_end:
            if self.out_frame is not None:
                self._set_cursor(self.out_frame, sync_wall=True)
            self.side.set_export_text("回放到出点")
        else:
            self.side.set_export_text("")
        self._update_card()

    def _on_escape(self):
        if self._playing or self._play_pending or self._player_open:
            self.stop_playback()

    def _stop_playback_if_playing(self):
        if self._playing or self._play_pending or self._player_open:
            self.stop_playback()

    # ------------------------------------------------------- 入点 / 出点
    def _on_selection_changed(self, *_args):
        if self._syncing or self.view.box_selecting():
            return
        rows = self.selected_rows()
        if len(rows) >= 2:
            self._stop_playback_if_playing()
            first = self.model.item(rows[0])
            last = self.model.item(rows[-1])
            if first is not None and last is not None:
                self.in_frame = min(first.frame, last.frame)
                self.out_frame = max(first.frame, last.frame)
                # 选区一变，预览就跳到入点（用户刚定的起点），不要停在旧光标上
                self.cursor_frame = self.in_frame
                self.view.set_markers(self.in_frame, self.out_frame, self.cursor_frame)
                self._refresh_range()
                self._update_card()
                self._save_range_state()

    def _seek_overview(self, frame: int):
        """底部总进度条：点/拖只移动光标，不碰入点出点。"""
        self._stop_playback_if_playing()
        self._set_cursor(frame, sync_wall=True)

    def _on_wall_range_drag(self, start_frame: int, end_frame: int):
        """随意框选进行中：严格按「按下点 → 当前点」的精确帧，不用格子边界。"""
        if self._syncing:
            return
        self._stop_playback_if_playing()
        lo, hi = sorted((int(start_frame), int(end_frame)))
        self.in_frame, self.out_frame = lo, hi
        self.cursor_frame = lo
        self._drag_frame = lo
        self.view.set_markers(self.in_frame, self.out_frame, self.cursor_frame)
        self._refresh_range()
        if not self._drag_tick.isActive():
            self._on_drag_preview()
            self._drag_tick.start()
        self._drag_settle.start()

    def _on_wall_range_selected(self, start_frame: int, end_frame: int):
        """随意框选松手：用两端精确帧定稿，覆盖原生「整格多选」的结果。"""
        self._drag_tick.stop()
        self._drag_settle.stop()
        self._drag_frame = None
        lo, hi = sorted((int(start_frame), int(end_frame)))
        self.in_frame, self.out_frame = lo, hi
        self._set_cursor(lo, sync_wall=True)
        self._refresh_range()
        count = hi - lo + 1
        self.side.set_export_text(
            f"框选区间：{count} 帧（{count / max(self._fps(), 1e-6):.2f} 秒）")
        self._save_range_state()

    def _sync_overview(self):
        """把光标/入点/出点/片段推给底部总进度条（唯一的同步出口）。"""
        bar = getattr(self, "overview", None)
        if bar is None:
            return
        try:
            bar.set_media(int(self.info.frames or 0),
                          float(self.info.fps or 25.0))
            bar.set_state(self.cursor_frame, self.in_frame, self.out_frame)
            bar.set_segments([(float(s["t_in"]), float(s["t_end"]),
                               str(s.get("name") or ""))
                              for s in self.segments])
        except (RuntimeError, KeyError, TypeError, ValueError):
            pass

    # ------------------------------------------------------- 标记（PR 的 M）
    def _refresh_markers(self):
        """标记只存帧号；墙上时间轴和底部总进度条各画一份。"""
        marks = sorted({int(m) for m in self.markers})
        self.markers = marks
        bar = getattr(self, "overview", None)
        if bar is not None:
            try:
                bar.set_markers(marks)
            except RuntimeError:
                pass
        try:
            self.view.set_range_marks(marks)
        except RuntimeError:
            pass
        side = getattr(self, "side", None)
        if side is not None:
            side.set_marker_info(len(marks), marks)

    def add_marker(self, frame: int | None = None):
        frame = self.cursor_frame if frame is None else frame
        if frame is None:
            self.side.set_export_text("先点一帧，再按 M 加标记")
            return
        total = max(1, int(self.info.frames or 0))
        frame = max(0, min(total - 1, int(frame)))
        if frame in self.markers:
            self.side.set_export_text(f"第 {frame} 帧已经有标记了")
            return
        self.markers.append(frame)
        self._refresh_markers()
        index = self.markers.index(frame) + 1
        self.side.set_export_text(
            f"已添加标记 #{index}（第 {frame} 帧 · {fmt_tc(frame / self._fps())}）")
        self._save_range_state()

    def remove_marker(self, frame: int | None = None):
        frame = self.cursor_frame if frame is None else frame
        if frame is None:
            return
        frame = int(frame)
        if frame not in self.markers:
            self.side.set_export_text(f"第 {frame} 帧没有标记")
            return
        self.markers.remove(frame)
        self._refresh_markers()
        self.side.set_export_text(f"已删除第 {frame} 帧的标记")
        self._save_range_state()

    def clear_markers(self):
        if not self.markers:
            return
        self.markers.clear()
        self._refresh_markers()
        self.side.set_export_text("已清除全部标记")
        self._save_range_state()

    def goto_marker(self, direction: int = 1):
        """Shift+M 下一个；Ctrl+Shift+M 上一个（和 Premiere 一致）。"""
        if not self.markers:
            self.side.set_export_text("还没有标记（M 添加）")
            return
        current = self.cursor_frame
        if direction >= 0:
            target = next((m for m in self.markers
                           if current is None or m > current), None)
            empty = "后面没有标记了"
        else:
            target = next((m for m in reversed(self.markers)
                           if current is not None and m < current), None)
            empty = "前面没有标记了"
        if target is None:
            self.side.set_export_text(empty)
            return
        self._goto_marker_frame(target)

    def _goto_marker_frame(self, frame: int):
        self._stop_playback_if_playing()
        self._set_cursor(int(frame), sync_wall=True)
        self.side.set_export_text(
            f"标记：第 {int(frame)} 帧 · {fmt_tc(int(frame) / self._fps())}")

    def set_in_point(self, frame: int | None = None):
        self._stop_playback_if_playing()
        frame = self.cursor_frame if frame is None else frame
        if frame is None:
            return
        self.in_frame = frame
        if self.out_frame is not None and self.out_frame <= frame:
            self.out_frame = None
        self.cursor_frame = frame          # 预览跟着刚设的入点
        self._clear_drag_hint()
        self._refresh_range()
        self._update_card()
        self._save_range_state()

    def set_out_point(self, frame: int | None = None):
        self._stop_playback_if_playing()
        frame = self.cursor_frame if frame is None else frame
        if frame is None:
            return
        self.out_frame = frame
        if self.in_frame is not None and self.in_frame >= frame:
            self.in_frame = None
        self.cursor_frame = frame          # 预览跟着刚设的出点
        self._clear_drag_hint()
        self._refresh_range()
        self._update_card()
        self._save_range_state()

    def _save_range_state(self):
        """入点/出点也属于「片段状态」，跟着缓存一起存。"""
        if self._finished:
            self.save_state()

    def _clear_drag_hint(self):
        if self.lbl_export.text().startswith("区间已调整"):
            self.side.set_export_text("")

    def clear_range(self):
        self.in_frame = None
        self.out_frame = None
        self._refresh_range()
        self._save_range_state()

    # ------------------------------------------------- 拖动入点/出点手柄
    def _on_range_drag(self, kind: str, frame: int):
        """拖动过程中：标记、光标三角、右侧预览都跟着「正在拖的那一帧」。

        预览策略见 `_on_drag_preview` / `_on_drag_settle`：
        移动时先出墙上的缩略图（毫秒级），停手 140ms 再补精确帧。
        """
        self._stop_playback_if_playing()
        total = max(1, int(self.info.frames or 0))
        frame = max(0, min(total - 1, int(frame)))
        if kind == "in":
            if self.out_frame is not None:
                frame = min(frame, max(0, self.out_frame - 1))
            self.in_frame = frame
        else:
            if self.in_frame is not None:
                frame = max(frame, self.in_frame + 1)
            self.out_frame = min(total - 1, frame)
        self.cursor_frame = frame        # 预览/三角跟着被拖的那一端，而不是停在旧光标
        self._drag_frame = frame
        self._refresh_range()
        if not self._drag_tick.isActive():
            # 第一次移动立刻出图（手感跟手），后续 50ms 合一次，拖快了也不卡
            self._on_drag_preview()
            self._drag_tick.start()
        self._drag_settle.start()

    def _end_range_drag(self, kind: str):
        """松手后把光标落到刚拖的那一端，让预览变成精确帧。"""
        self._drag_tick.stop()
        self._drag_settle.stop()
        self._drag_frame = None
        frame = self.in_frame if kind == "in" else self.out_frame
        if frame is not None:
            self._set_cursor(frame, sync_wall=True)
        self._refresh_range()
        if self.in_frame is not None and self.out_frame is not None:
            count = self.out_frame - self.in_frame + 1
            self.side.set_export_text(f"区间已调整：{count} 帧"
                                      f"（{count / max(self._fps(), 1e-6):.2f} 秒）")
        self._save_range_state()

    # ------------------------------------------------------- 片段列表
    def _fps(self) -> float:
        return self.info.fps or 25.0

    def _refresh_segments(self):
        self.side.set_segments([
            (s["t_in"], s["t_end"], self._thumb_for_time(s["t_in"]),
             str(s.get("name") or ""))
            for s in self.segments])
        self._sync_overview()

    def _thumb_for_time(self, t: float) -> str | None:
        """片段缩略图 = 已抽好的关键帧里最接近的一张（不额外解码）。"""
        if not self.model.rowCount():
            return None
        row = self.timeline.index_of(t * self._fps())[0]
        row = max(0, min(self.model.rowCount() - 1, row))
        ref = self.model.item(row)
        return ref.path if ref is not None else None

    def clear_segments(self):
        if not self.segments:
            return
        self.segments.clear()
        self._refresh_segments()
        self.side.set_export_text("已清空片段列表")
        self.save_state()

    # ------------------------------------------------------- 显示尺寸（不重抽）
    def set_display_side(self, side: int, notify: bool = True,
                         anchor_frame: int | None = None,
                         anchor_point=None,
                         anchor_row: int | None = None):
        """改缩略图尺寸。带锚点（Alt+滚轮）时把鼠标那一格放回原来的高度，
        图墙不会跳走；``anchor_row`` 是缩放前 ``indexAt`` 命中的格子。"""
        side = max(DISPLAY_MIN, min(DISPLAY_MAX, int(side)))
        w, h = thumb_size_for(self.info.width, self.info.height, side)
        if (side, w, h) == (self.display_side, self.disp_w, self.disp_h):
            return
        count = self.model.rowCount()
        keep_row = None
        keep_top = 0
        if anchor_row is None and anchor_frame is not None and count:
            try:
                anchor_row = self.view._item_for_frame(int(anchor_frame))
            except (AttributeError, RuntimeError):
                anchor_row = None
        if anchor_row is not None and 0 <= int(anchor_row) < count:
            rect = self.view.visualRect(self.model.index(int(anchor_row), 0))
            if rect.isValid() and not rect.isEmpty():
                keep_row = int(anchor_row)
                keep_top = rect.top()
        anchor = None if keep_row is not None else self._top_visible_index()
        self.display_side = side
        self.disp_w, self.disp_h = w, h
        delegate = self.view.itemDelegate()
        delegate.tw, delegate.th = w, h
        self.view.setGridSize(QSize(w + 10, h + 10 + 22 + LANE_H))
        self.view.doItemsLayout()
        if keep_row is not None:
            # 缩放后这一格可能已经滚出已布局范围（visualRect 变无效），
            # 按「均匀网格」直接算内容坐标；滚动条上限此时可能还是旧的，
            # 先按网格算好的内容高度把范围设对，再设滚动值，免得被夹住。
            grid = self.view.gridSize()
            gh = max(1, grid.height())
            gw = max(1, grid.width())
            per_row = max(1, self.view.viewport().width() // gw)
            content_h = ((count + per_row - 1) // per_row) * gh
            scroll = self.view.verticalScrollBar()
            scroll.setRange(0, max(0, content_h - self.view.viewport().height()))
            scroll.setValue(int(round((keep_row // per_row) * gh - keep_top)))
        elif anchor is not None:
            self.view.scrollTo(anchor, QAbstractItemView.PositionAtTop)
        self.view.viewport().update()
        if notify:
            self.display_side_changed.emit(side)

    def _top_visible_index(self):
        index = self.view.indexAt(QPoint(4, 4))
        return index if index.isValid() else None

    def _on_wheel_zoom(self, direction: int):
        anchor = None
        try:
            anchor = self.view.take_zoom_anchor()
        except AttributeError:
            anchor = None
        frame, point, row = anchor if anchor else (None, None, None)
        self.set_display_side(self.display_side + int(direction) * DISPLAY_STEP,
                              anchor_frame=frame, anchor_point=point,
                              anchor_row=row)

    # ------------------------------------------------------- 面板宽度
    def sidebar_width(self) -> int:
        return self.side.width()

    def set_sidebar_width(self, px: int):
        width = max(320, self.split.width())
        px = max(SIDEBAR_MIN, min(SIDEBAR_MAX, int(px)))
        self.split.setSizes([max(240, width - px), px])

    def add_segment(self):
        if self.in_frame is None or self.out_frame is None \
                or self.out_frame <= self.in_frame:
            self.side.set_export_text("先设好入点/出点（I/O 或 Shift 框两帧），再加入片段")
            return
        fps = self._fps()
        item = {"t_in": self.in_frame / fps, "t_end": self.out_frame / fps,
                "name": ""}
        for existing in self.segments:
            if (abs(existing["t_in"] - item["t_in"]) < 1e-6
                    and abs(existing["t_end"] - item["t_end"]) < 1e-6):
                self.side.set_export_text("这一段已经在列表里了")
                return
        self.segments.append(item)
        self._refresh_segments()
        self.side.set_active_segment(len(self.segments) - 1)
        self.side.set_export_text(f"已加入片段 #{len(self.segments)}"
                                f"（共 {len(self.segments)} 段）")
        self.save_state()

    def remove_segment(self, index: int):
        if 0 <= index < len(self.segments):
            self.segments.pop(index)
            self._refresh_segments()
            self.side.set_export_text(f"已删除片段，剩 {len(self.segments)} 段")
            self.save_state()

    def select_segment(self, index: int):
        if not (0 <= index < len(self.segments)):
            return
        segment = self.segments[index]
        fps = self._fps()
        self.in_frame = int(round(segment["t_in"] * fps))
        self.out_frame = int(round(segment["t_end"] * fps))
        self.side.set_active_segment(index)
        self._set_cursor(self.in_frame)
        self._refresh_range()
        self._save_range_state()

    def _refresh_range(self):
        fps = self.info.fps or 25.0
        self.view.set_markers(self.in_frame, self.out_frame, self.cursor_frame)
        if self.in_frame is not None and self.out_frame is not None:
            t1 = self.in_frame / max(fps, 1e-6)
            t2 = self.out_frame / max(fps, 1e-6)
            count = max(1, self.out_frame - self.in_frame + 1)
            main = f"入点 {fmt_tc(t1)} → 出点 {fmt_tc(t2)}"
            sub = f"{count} 帧 / {count / max(fps, 1e-6):.2f} 秒"
            tip = (f"入点 #{self.in_frame} {fmt_tc(t1)}\n"
                   f"出点 #{self.out_frame} {fmt_tc(t2)}\n"
                   f"{count} 帧 · {count / max(fps, 1e-6):.2f} 秒（含出点那一帧）\n"
                   f"提示：拖时间轴上的蓝色旗子 —— 左右微调帧号，上下可跳到别的行（自动吸附）")
        elif self.in_frame is not None:
            main = f"入点 {fmt_tc(self.in_frame / max(fps, 1e-6))} · 再按 O 设出点"
            sub, tip = "", ""
        elif self.out_frame is not None:
            main = f"出点 {fmt_tc(self.out_frame / max(fps, 1e-6))} · 再按 I 设入点"
            sub, tip = "", ""
        else:
            main = "←/→ 逐帧 · I/O 设点 · 拖蓝旗：左右微调 / 上下跳行"
            sub, tip = "", ""
        self.side.set_range(main, sub)
        self.side.lbl_range.setToolTip(tip)
        range_ok = (self.in_frame is not None and self.out_frame is not None
                    and self.out_frame > self.in_frame)
        self.btn_export.setEnabled(range_ok and not self.busy and not self.exporting
                                   and self._batch is None)
        self.btn_play.setEnabled(range_ok and self._batch is None)
        self._sync_overview()

    def selected_rows(self) -> list[int]:
        model = self.view.selectionModel()
        if model is None:
            return []
        return sorted({i.row() for i in model.selectedIndexes()})

    # ------------------------------------------------------------- 导出
    def plan_export(self, a: FrameRef, b: FrameRef) -> dict:
        return self.plan_export_times(a.t, b.t)

    def plan_export_times(self, start_t: float, end_t: float) -> dict:
        """算出两种切法的实际区间（相对视频起点的秒数）。"""
        fd = self.info.frame_duration
        start_abs = self.info.absolute_time(start_t)
        end_abs = self.info.absolute_time(end_t) + fd

        keyframes = ffmpeg.read_keyframes(self.path, start_abs - 90.0, start_abs + 5.0)
        if not keyframes:
            keyframes = ffmpeg.read_keyframes(self.path, max(0.0, start_abs - 600.0),
                                              start_abs + 5.0)
        snapped = ffmpeg.snap_before(keyframes, start_abs)
        keyframe_ok = snapped is not None
        lossless_start = self.info.relative_time(snapped) if snapped is not None else start_t

        packets = ffmpeg.read_packets(self.path, max(0.0, end_abs - 2.0), end_abs + 2.0)
        best = ffmpeg.closest([p for p in packets if p["t"] >= start_abs], end_abs)
        # 出点是"包含这一帧"：区间是 [in, out+1)，所以结尾取 out+1 帧的时间。
        # 注意不要再额外 +1 帧（之前那样会切成 out+2 帧，多出一帧）。
        if best is not None:
            end = max(self.info.relative_time(best["t"]), end_t + fd)
        else:
            end = end_t + fd
        end = max(end, start_t + fd, lossless_start + fd)

        warn = ""
        if not keyframe_ok:
            warn = "没找到起点之前的关键帧，只能按原时间点试切（可能不准甚至切不动）。"
        return {
            "start": start_t,
            "end": end,
            "lossless_start": lossless_start,
            "duration": end - start_t,
            "early": max(0.0, start_t - lossless_start),
            "keyframe_ok": keyframe_ok,
            "warn": warn,
        }

    def _smart_preview(self, plan: dict, suffix: str | None = None):
        probe = self.path.with_suffix(suffix or (self.path.suffix or ".mp4"))
        return smartcut.plan(self.path, self.info, plan["start"], plan["end"],
                             probe, exact=True, lossless_start=plan["lossless_start"])

    def export_segment(self):
        self._stop_playback_if_playing()
        if self.in_frame is None or self.out_frame is None:
            QMessageBox.information(
                self, "导出片段",
                "先设入点和出点：\n"
                "  · 点一帧后按 I / O；或\n"
                "  · Shift 点首尾两帧。")
            return
        fps = self.info.fps or 25.0
        start_t = self.in_frame / max(fps, 1e-6)
        end_t = self.out_frame / max(fps, 1e-6)

        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.side.set_export_text("正在定位关键帧…")
        QApplication.processEvents()
        error = None
        plan = None
        smart = None
        try:
            plan = self.plan_export_times(start_t, end_t)
            smart = self._smart_preview(plan)
        except Exception as e:  # noqa: BLE001
            error = e
        finally:
            QApplication.restoreOverrideCursor()
            self.side.set_export_text("")
        if error is not None:
            QMessageBox.warning(self, "导出片段", f"定位关键帧失败：{error}")
            return
        if plan is None:
            return

        detail = [f"选区：{fmt_tc(start_t)} → {fmt_tc(end_t)}"
                  f"（{self.out_frame - self.in_frame + 1} 帧 / "
                  f"{(self.out_frame - self.in_frame + 1) / (self.info.fps or 25.0):.2f} 秒，"
                  f"含出点那一帧）"]
        if smart.mode == "smart":
            detail.append(
                f"精确模式：{fmt_tc(plan['start'])} → {fmt_tc(plan['end'])}"
                f"（{plan['duration']:.2f} 秒）· 只重编码头部 {smart.head_duration:.2f}s，"
                f"其余原始，约 {smart.estimate_seconds:.1f} 秒完成")
        detail.append(
            f"无损模式：{fmt_tc(plan['lossless_start'])} → {fmt_tc(plan['end'])}"
            f"（{plan['end'] - plan['lossless_start']:.2f} 秒）"
            + (f" · 起点早 {plan['early']:.2f} 秒" if plan["early"] >= 0.05 else ""))
        if plan["warn"]:
            detail.append("⚠ " + plan["warn"])
        detail.append(
            f"容器 {self.path.suffix or '原格式'} ｜ 视频 {self.info.codec or '?'}"
            f" ｜ 音频 {self.info.audio or '无'}")

        ok, exact = ask_export(
            self, f"导出《{self.path.name}》的选中区间？", "\n".join(detail),
            exact_available=smart.mode == "smart",
            exact_note=(smart.reason if smart.mode != "smart" else
                        f"只重编码切点附近 {smart.head_duration:.2f} 秒，中段保持原始码流"))
        if not ok:
            self.side.set_export_text("")
            return

        start_for_name = plan["start"] if exact else plan["lossless_start"]
        default = (self.path.parent /
                   f"{safe_filename(self.path.stem)}__"
                   f"{fmt_tc_compact(start_for_name)}_to_"
                   f"{fmt_tc_compact(plan['end'])}{self.path.suffix}")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出片段到…", str(default),
            f"视频 (*{self.path.suffix});;所有文件 (*.*)")
        if not path:
            self.side.set_export_text("")
            return
        self._export_to_path(Path(path), plan, exact=exact)

    def _lossless_start_for(self, src: Path, info: MediaInfo, start_rel: float) -> float:
        start_abs = info.absolute_time(start_rel)
        keyframes = ffmpeg.read_keyframes(src, max(0.0, start_abs - 90.0), start_abs + 5.0)
        if not keyframes:
            keyframes = ffmpeg.read_keyframes(src, max(0.0, start_abs - 600.0),
                                              start_abs + 5.0)
        snapped = ffmpeg.snap_before(keyframes, start_abs)
        return info.relative_time(snapped) if snapped is not None else start_rel

    def _export_to_path(self, out: Path, plan: dict | None = None, exact: bool = True):
        """真正开始导出（对话框与自检共用）。"""
        if plan is None:
            if self.in_frame is None or self.out_frame is None:
                return
            fps = self.info.fps or 25.0
            plan = self.plan_export_times(self.in_frame / max(fps, 1e-6),
                                          self.out_frame / max(fps, 1e-6))
        start_rel, end_rel = plan["start"], plan["end"]
        if smartcut.needs_normalize(self.info, self.path):
            self._start_prepare(out, start_rel, end_rel, exact)
        else:
            self._start_export_job(self.path, self.info, out, start_rel, end_rel, exact)

    # ---------------- 预处理：无损转封装 ----------------
    def _start_prepare(self, out: Path, start_rel: float, end_rel: float, exact: bool):
        import tempfile
        temp_dir = Path(tempfile.mkdtemp(prefix="purer_norm_"))
        norm = temp_dir / "norm.mkv"
        args = ffmpeg.build_normalize_args(self.path, norm)
        job = Job(args, duration=self.info.duration, label="转封装")
        self.exp_job = job
        self._prepare_ctx = {"out": out, "start": start_rel, "end": end_rel,
                             "exact": exact, "dir": temp_dir, "norm": norm}
        self.btn_export.setEnabled(False)
        self.exp_bar.setVisible(True)
        self.exp_bar.setValue(0)
        self._export_text = f"源时间戳不规范，先无损转封装…（{self.path.suffix}）"
        self.side.set_export_text(self._export_text)
        job.progress.connect(self.exp_bar.setValue)
        job.finished.connect(self._on_prepare_finished)
        self.queue.submit(job)

    def _on_prepare_finished(self, ok: bool):
        job = self.exp_job
        ctx = self._prepare_ctx
        self.exp_job = None
        self.exp_bar.setVisible(False)
        if job is None or ctx is None:
            return
        if job.cancelled:
            smartcut.cleanup([ctx["dir"]])
            self._prepare_ctx = None
            self.side.set_export_text("已取消导出")
            self._refresh_range()
            return
        if not ok:
            smartcut.cleanup([ctx["dir"]])
            self._prepare_ctx = None
            self.side.set_export_text("❌ 转封装失败")
            self._refresh_range()
            if not self.quiet:
                show_log(self, "转封装失败",
                         f"《{self.path.name}》时间戳不规范，需要先无损转封装，但失败了。\n{job.error}",
                         job.failed_command, job.stderr_text)
            return
        try:
            norm_info = ffmpeg.probe(ctx["norm"])
        except Exception as e:  # noqa: BLE001
            smartcut.cleanup([ctx["dir"]])
            self._prepare_ctx = None
            self.side.set_export_text(f"❌ 转封装结果无法读取：{e}")
            self._refresh_range()
            return
        self._prepare_ctx = None
        self._start_export_job(ctx["norm"], norm_info, ctx["out"], ctx["start"],
                               ctx["end"], ctx["exact"], cleanup=[ctx["dir"]])

    # ---------------- 真正导出 ----------------
    def _start_export_job(self, src: Path, info: MediaInfo, out: Path,
                          start_rel: float, end_rel: float, exact: bool,
                          cleanup: list | None = None):
        lossless_start = self._lossless_start_for(src, info, start_rel)
        cut = smartcut.plan(src, info, start_rel, end_rel, out, exact=exact,
                            lossless_start=lossless_start)
        if cut.mode != "smart":
            cut = smartcut.Plan(mode="copy", start=lossless_start, end=end_rel,
                                reason=cut.reason)
        if exact and cut.mode != "smart" and not self.quiet:
            self.side.set_export_text(f"精确模式不可用（{cut.reason}），已改用无损…")

        stages, temp = smartcut.build_stages(src, info, out, cut)
        job = Job(stages=stages, label=f"导出 {out.name}")
        self.exp_job = job
        self.exp_out = out
        self._export_ctx = {
            "cut": cut,
            "temp": list(temp) + list(cleanup or []),
            "expected": cut.duration,
            "junction": cut.head_duration if cut.mode == "smart" else None,
            "lossless_start": lossless_start,
            "end": end_rel,
            "src": src,
            "info": info,
            "fallback": False,
            "reason": "",
        }
        self.btn_export.setEnabled(False)
        self.exp_bar.setVisible(True)
        self.exp_bar.setRange(0, 100)
        self.exp_bar.setValue(0)
        kind = "精确" if cut.mode == "smart" else "无损"
        self._export_text = (
            f"导出中（{kind}）… {fmt_tc(cut.start)} → {fmt_tc(cut.end)} → {out.name}")
        self.side.set_export_text(self._export_text)
        job.progress.connect(self.exp_bar.setValue)
        job.finished.connect(self._on_export_finished)
        self.queue.submit(job)
        if job.state == "idle":
            pos = self.queue.position(job)
            if pos:
                self.side.set_export_text(f"排队中…（第 {pos} 位）→ {out.name}")

    def _fallback_to_lossless(self, ctx: dict, reason: str):
        smartcut.cleanup(ctx["temp"])
        try:
            self.exp_out.unlink(missing_ok=True)
        except OSError:
            pass
        copy = smartcut.Plan(mode="copy", start=ctx["lossless_start"], end=ctx["end"])
        stages, temp = smartcut.build_stages(ctx["src"], ctx["info"], self.exp_out, copy)
        ctx.update({"cut": copy, "temp": temp, "expected": copy.duration,
                    "junction": None, "fallback": True, "reason": reason})
        job = Job(stages=stages, label=f"退回无损 {self.exp_out.name}")
        self.exp_job = job
        self._export_text = f"精确切割失败（{reason}），已自动退回无损…"
        self.side.set_export_text(self._export_text)
        self.exp_bar.setVisible(True)
        self.exp_bar.setValue(0)
        job.progress.connect(self.exp_bar.setValue)
        job.finished.connect(self._on_export_finished)
        self.queue.submit(job)

    def _on_export_finished(self, ok: bool):
        job = self.exp_job
        ctx = self._export_ctx
        self.exp_job = None
        self.exp_bar.setVisible(False)
        self._refresh_range()
        if job is None or ctx is None or self.exp_out is None:
            return
        if job.cancelled:
            smartcut.cleanup(ctx["temp"])
            self.side.set_export_text("已取消导出")
            return

        if not ok:
            if ctx["cut"].mode == "smart" and not ctx["fallback"]:
                self._fallback_to_lossless(ctx, job.error or "编码/拼接失败")
                return
            smartcut.cleanup(ctx["temp"])
            self.side.set_export_text("❌ 导出失败")
            if not self.quiet:
                show_log(self, "导出失败",
                         f"导出《{self.exp_out.name}》失败。\n{job.error}",
                         job.failed_command, job.stderr_text)
            return

        if ctx["cut"].mode == "smart" and not ctx["fallback"]:
            good, why = smartcut.verify(self.exp_out, ctx["expected"],
                                        ctx["info"].frame_duration, ctx["junction"])
            if not good:
                self._fallback_to_lossless(ctx, why)
                return

        smartcut.cleanup(ctx["temp"])
        out = self.exp_out
        info_text = ""
        try:
            out_info = ffmpeg.probe(out)
            size = out.stat().st_size
            info_text = (f"{out_info.duration:.2f} 秒 · {human_size(size)} · "
                         f"{out_info.codec or '?'}"
                         + (f" + {out_info.audio}" if out_info.audio else ""))
        except Exception:  # noqa: BLE001
            try:
                info_text = human_size(out.stat().st_size)
            except OSError:
                info_text = ""
        kind = "帧精确" if ctx["cut"].mode == "smart" else "无损"
        if ctx["fallback"]:
            kind = f"无损（精确失败已回退：{ctx['reason']}）"
        self.side.set_export_text(
            f"✅ 已导出 {out.name}（{kind}）"
            + (f" · {info_text}" if info_text else ""))
        self._export_text = self.lbl_export.text()
        self.export_count += 1
        self._record_export(out, kind)

    # ------------------------------------------------------- 批量导出
    def export_all_segments(self):
        self._stop_playback_if_playing()
        if not self.segments:
            self.side.set_export_text("列表里还没有片段")
            return
        out_dir = QFileDialog.getExistingDirectory(
            self, "导出全部片段到…", str(self.path.parent))
        if not out_dir:
            return
        smart = None
        try:
            first = self.segments[0]
            smart = self._smart_preview(
                self.plan_export_times(first["t_in"], first["t_end"]))
        except Exception:  # noqa: BLE001
            smart = None

        rows = []
        for segment in self.segments:
            count = max(1, round((segment['t_end'] - segment['t_in'])
                                 * (self.info.fps or 25.0)))
            rows.append(
                f"{fmt_tc(segment['t_in'])} → {fmt_tc(segment['t_end'])}"
                f"（{count} 帧 / {count / (self.info.fps or 25.0):.2f} 秒）")
        defaults = plan_filenames([""] * len(self.segments), self.path.stem,
                                  self.path.suffix, existing_names(Path(out_dir)))
        ok, exact, texts = ask_batch_export(
            self, f"导出全部 {len(self.segments)} 段",
            rows,
            [str(segment.get("name") or "") for segment in self.segments],
            defaults,
            exact_available=bool(smart and smart.mode == "smart"),
            exact_note=(smart.reason if smart and smart.mode != "smart"
                        else "只重编码每段的切点，中段保持原始码流"),
            folder=out_dir)
        if not ok:
            return
        for i, text in enumerate(texts):
            if i < len(self.segments):
                self.segments[i]["name"] = str(text or "").strip()
        self._batch_start(Path(out_dir), exact, names=texts)

    def _batch_start(self, out_dir: Path, exact: bool, names=None):
        """``names`` = 用户手填的素材名（可以是空串 / 可以不带扩展名）。"""
        texts = [str(x or "") for x in names] if names else []
        if len(texts) < len(self.segments):
            texts.extend([""] * (len(self.segments) - len(texts)))
        filenames = plan_filenames(texts, self.path.stem, self.path.suffix,
                                   existing_names(out_dir))
        items = []
        for i, segment in enumerate(self.segments):
            name = Path(filenames[i]).name
            out = out_dir / name
            items.append({"t_in": segment["t_in"], "t_end": segment["t_end"],
                          "out": out, "label": f"#{i + 1}", "name": name,
                          "ok": None, "error": "", "cut": None, "temp": [],
                          "fallback": False})
        self._batch = {"items": items, "index": 0, "exact": exact,
                       "src": self.path, "info": self.info, "dir": out_dir,
                       "norm_dir": None}
        self.seg_bar.set_busy(True)
        self.btn_export.setEnabled(False)
        self.btn_play.setEnabled(False)
        self.exp_bar.setVisible(True)
        self.exp_bar.setRange(0, 100)
        self.exp_bar.setValue(0)
        if smartcut.needs_normalize(self.info, self.path):
            self._batch_prepare()
        else:
            QTimer.singleShot(0, self._batch_next)

    def _batch_prepare(self):
        import tempfile
        batch = self._batch
        if not batch:
            return
        temp_dir = Path(tempfile.mkdtemp(prefix="purer_bnorm_"))
        norm = temp_dir / "norm.mkv"
        job = Job(ffmpeg.build_normalize_args(self.path, norm),
                  duration=self.info.duration, label="转封装")
        batch["norm_dir"] = temp_dir
        batch["norm"] = norm
        self.batch_job = job
        self.side.set_export_text(f"源时间戳不规范，先无损转封装一次…（{len(batch['items'])} 段）")
        job.progress.connect(self.exp_bar.setValue)
        job.finished.connect(self._on_batch_prepare)
        self.queue.submit(job)

    def _on_batch_prepare(self, ok: bool):
        batch = self._batch
        job = self.batch_job
        self.batch_job = None
        if not batch:
            return
        if job is None or job.cancelled:
            self._batch_abort("已取消")
            return
        if not ok:
            self._batch_abort(f"转封装失败：{job.error}")
            return
        try:
            batch["info"] = ffmpeg.probe(batch["norm"])
            batch["src"] = batch["norm"]
        except Exception as e:  # noqa: BLE001
            self._batch_abort(f"转封装结果无法读取：{e}")
            return
        self._batch_next()

    def _batch_next(self):
        batch = self._batch
        if not batch:
            return
        if batch["index"] >= len(batch["items"]):
            self._batch_finish()
            return
        item = batch["items"][batch["index"]]
        try:
            lossless = self._lossless_start_for(batch["src"], batch["info"], item["t_in"])
            cut = smartcut.plan(batch["src"], batch["info"], item["t_in"], item["t_end"],
                                item["out"], exact=batch["exact"],
                                lossless_start=lossless)
        except Exception as e:  # noqa: BLE001
            item["ok"] = False
            item["error"] = str(e)
            self._batch_advance()
            return
        if cut.mode != "smart":
            cut = smartcut.Plan(mode="copy", start=lossless, end=item["t_end"],
                                reason=cut.reason)
        item["lossless"] = lossless
        item["cut"] = cut
        item["fallback"] = False
        self._batch_submit(item)

    def _batch_submit(self, item: dict):
        batch = self._batch
        if not batch:
            return
        stages, temp = smartcut.build_stages(batch["src"], batch["info"],
                                             item["out"], item["cut"])
        item["temp"] = temp
        job = Job(stages=stages, label=item["label"])
        self.batch_job = job
        self.exp_bar.setRange(0, 100)
        self.exp_bar.setValue(0)
        kind = "" if item["cut"].mode == "smart" else "（无损）"
        self.side.set_export_text(
            f"导出 {batch['index'] + 1}/{len(batch['items'])}{kind}：{item['out'].name}")
        job.progress.connect(self.exp_bar.setValue)
        job.finished.connect(self._on_batch_item_finished)
        self.queue.submit(job)

    def _on_batch_item_finished(self, ok: bool):
        batch = self._batch
        job = self.batch_job
        self.batch_job = None
        if not batch or job is None:
            return
        item = batch["items"][batch["index"]]
        if job.cancelled:
            item["ok"] = False
            item["error"] = "已取消"
            self._batch_abort("已取消")
            return
        if not ok:
            if item["cut"] is not None and item["cut"].mode == "smart" \
                    and not item["fallback"]:
                self._batch_fallback(item, job.error or "编码/拼接失败")
                return
            item["ok"] = False
            item["error"] = job.error
            smartcut.cleanup(item["temp"])
            self._batch_advance()
            return
        if item["cut"].mode == "smart" and not item["fallback"]:
            good, why = smartcut.verify(item["out"], item["cut"].duration,
                                        batch["info"].frame_duration,
                                        item["cut"].head_duration)
            if not good:
                self._batch_fallback(item, why)
                return
        item["ok"] = True
        self.export_count += 1
        smartcut.cleanup(item["temp"])
        self._batch_advance()

    def _batch_fallback(self, item: dict, reason: str):
        smartcut.cleanup(item["temp"])
        try:
            item["out"].unlink(missing_ok=True)
        except OSError:
            pass
        item["cut"] = smartcut.Plan(mode="copy", start=item["lossless"],
                                    end=item["t_end"])
        item["fallback"] = True
        item["reason"] = reason
        self._batch_submit(item)

    def _batch_advance(self):
        batch = self._batch
        if not batch:
            return
        batch["index"] += 1
        QTimer.singleShot(0, self._batch_next)

    def _batch_finish(self):
        batch = self._batch
        self._batch = None
        if not batch:
            return
        if batch.get("norm_dir"):
            smartcut.cleanup([batch["norm_dir"]])
        ok_items = [i for i in batch["items"] if i["ok"]]
        fail = [i for i in batch["items"] if i["ok"] is False]
        precise = sum(1 for i in ok_items
                      if i["cut"] is not None and i["cut"].mode == "smart"
                      and not i["fallback"])
        fallback = sum(1 for i in ok_items if i["fallback"])
        text = f"✅ 已导出 {len(ok_items)}/{len(batch['items'])} 段"
        if ok_items:
            text += f"（{precise} 帧精确"
            text += f" · {fallback} 无损回退" if fallback else ""
            text += "）"
        if fail:
            text += "　失败：" + "、".join(
                f"{i['label']}（{i['error'] or '未知'}）" for i in fail[:3])
            if len(fail) > 3:
                text += " …"
        self.side.set_export_text(text)
        self.exp_bar.setVisible(False)
        self.seg_bar.set_busy(False)
        self._refresh_range()
        if ok_items:
            self.exp_out = batch["dir"]
            for item in ok_items:
                kind = ("无损" if (item["fallback"] or item["cut"] is None
                                  or item["cut"].mode != "smart") else "帧精确")
                self._record_export(item["out"], kind)

    def _batch_abort(self, reason: str = ""):
        batch = self._batch
        if not batch:
            return
        job = self.batch_job
        self.batch_job = None
        if job is not None:
            job.cancel()
        for item in batch["items"]:
            smartcut.cleanup(item.get("temp") or [])
        if batch.get("norm_dir"):
            smartcut.cleanup([batch["norm_dir"]])
        self._batch = None
        self.exp_bar.setVisible(False)
        self.seg_bar.set_busy(False)
        self._refresh_range()
        self.side.set_export_text("已取消批量导出" if "取消" in reason
                                else f"批量导出中止：{reason}")

    # ------------------------------------------------------------- 操作
    def _show_extract_command(self):
        out_dir = self.out_dir or cache_dir_for(
            self.path, self.step, self.tw, self.th,
            mode=self.mode, threshold=self.threshold)
        if self.mode == "scene":
            frames = self.scene_frames or [0]
            variants = ffmpeg.build_frames_extract_variants(
                self.path, frames, self.tw, self.th, out_dir)
            analyze = scenedetect.build_sample_args(self.path)
            headline = (f"按镜头：先分析（{len(frames)} 个镜头，灵敏度 "
                        f"{preset_label(self.threshold)}），再按帧号清单抽图 "
                        f"{self.tw}×{self.th}")
            cmd = (ffmpeg.format_command(analyze, program=str(ffmpeg.Tools.ffmpeg))
                   + "\n\n" + ffmpeg.format_command(variants[0]))
        else:
            variants = ffmpeg.build_extract_variants(
                self.path, self.step, self.tw, self.th, out_dir,
                src_fps=self.info.fps)
            headline = f"每 {self.step} 帧取 1 帧，{self.tw}×{self.th}"
            cmd = ffmpeg.format_command(variants[0])
        detail = (f"视频：{self.path}\n"
                  f"缓存目录：{self.out_dir}\n\n"
                  f"同样的命令可以在命令行里手动重跑，用来排查问题。")
        show_log(self, "抽帧命令", headline, cmd, detail)

    def _cursor_pixmap(self):
        frame = self.cursor_frame
        if frame is None:
            return None
        if self.strip is not None:
            path = self.strip.path_for(frame)
            if path is not None:
                pm = QPixmap(str(path))
                if not pm.isNull():
                    return pm
        rect = self._item_rect(frame)
        if rect is not None:
            ref = self.model.item(self.timeline.index_of(frame)[0])
            if ref is not None:
                return QPixmap(ref.path)
        return None

    def _copy_tc(self):
        if self.cursor_frame is None:
            return
        fps = self.info.fps or 25.0
        text = fmt_tc(self.cursor_frame / max(fps, 1e-6))
        QApplication.clipboard().setText(text)
        self.side.set_export_text(f"已复制时间码 {text}（第 {self.cursor_frame} 帧）")

    def _copy_img(self):
        pm = self._cursor_pixmap()
        if pm is not None and not pm.isNull():
            QApplication.clipboard().setPixmap(pm)
            self.side.set_export_text("已复制画面到剪贴板")

    def _open_player(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))

    def _open_big(self, index):
        ref = index.data(Qt.UserRole) if index.isValid() else None
        if ref is None:
            return
        pm = QPixmap(ref.path)
        dlg = PreviewDialog(pm, f"{self.path.name} · {fmt_tc(ref.t)} · 第 {ref.frame} 帧", self)
        dlg.exec()

    def _context_menu(self, pos):
        index = self.view.indexAt(pos)
        if not index.isValid():
            self._hide_card()
            return
        rows = self.selected_rows()
        if index.row() not in rows:
            self.view.setCurrentIndex(index)
            rows = [index.row()]
        menu = QMenu(self)
        act_in = menu.addAction("设入点（I）")
        act_out = menu.addAction("设出点（O）")
        marker = self.view.marker_at(pos)
        act_unmark = None
        if marker is not None:
            act_unmark = menu.addAction(f"删除标记（第 {marker} 帧）")
        menu.addSeparator()
        act_tc = menu.addAction("复制时间码")
        act_img = menu.addAction("复制画面")
        act_big = menu.addAction("查看大图…")
        act_path = menu.addAction("复制视频路径")
        act_play = menu.addAction("用播放器打开")
        menu.addSeparator()
        act_export = menu.addAction("✂ 导出选中区间…")
        act_export.setEnabled(self.in_frame is not None and self.out_frame is not None)
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if chosen is act_in:
            self.set_in_point()
        elif chosen is act_out:
            self.set_out_point()
        elif act_unmark is not None and chosen is act_unmark:
            self.remove_marker(marker)
        elif chosen is act_tc:
            self._copy_tc()
        elif chosen is act_img:
            self._copy_img()
        elif chosen is act_big:
            self._open_big(index)
        elif chosen is act_path:
            QApplication.clipboard().setText(str(self.path))
        elif chosen is act_play:
            self._open_player()
        elif chosen is act_export:
            self.export_segment()

    # ------------------------------------------------------------- 杂项
    def resizeEvent(self, e):
        super().resizeEvent(e)

    def drop_preview(self):
        """切到别的标签页时停掉回放（预览栏保留光标）。"""
        self.stop_playback()

    def showEvent(self, e):
        super().showEvent(e)
        if self.cursor_frame is not None and self.model.rowCount():
            QTimer.singleShot(0, self._show_card)

    def shutdown(self):
        self._alive = False
        self.stop_playback()
        if self._batch is not None:
            self._batch_abort("shutdown")
        try:
            self._timer.stop()
            self._drag_tick.stop()
            self._drag_settle.stop()
        except RuntimeError:
            pass
        if self.job is not None:
            self.job.cancel()
        self._analyzing = False
        try:
            self.sampler.cancel()
        except (RuntimeError, AttributeError):
            pass
        if self.exp_job is not None:
            self.exp_job.cancel()
        if self.strip is not None:
            self.strip.stop()
        if self.playclip is not None:
            self.playclip.stop()
