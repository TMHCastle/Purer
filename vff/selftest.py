# -*- coding: utf-8 -*-
"""打包后自检（--selftest）：ffmpeg 链路 + 界面渲染 + 区间导出。

不显示窗口，结果写到 %TEMP%\\Purer_selftest.log（附一张截图）。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from . import ffmpeg
from .config import APP_ID
from .ffmpeg import Tools, run_hidden


def _wait(app, cond, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


def run(app) -> int:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    tmp = Path(tempfile.mkdtemp(prefix="purer_selftest_"))
    log: list[str] = []
    ok = True
    try:
        found = Tools.locate(deep=True)
        log.append(Tools.describe())
        if not found:
            raise RuntimeError("ffmpeg/ffprobe 未找到")

        src = tmp / "t.mp4"
        result = run_hidden([
            str(Tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "4", "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(src)], timeout=120)
        log.append(f"gen rc={result.returncode} size={src.stat().st_size}")
        ok = ok and result.returncode == 0

        info = ffmpeg.probe(src)
        log.append(f"probe: {info.resolution} {info.fps:g}fps "
                   f"{info.frames}帧 {info.duration:.2f}s "
                   f"v={info.codec} a={info.audio} start={info.start_time}")
        ok = ok and info.width == 640 and info.frames >= 100

        # ---- 抽帧 ----
        out = tmp / "thumbs"
        out.mkdir()
        args = ffmpeg.build_extract_args(src, 24, 320, 180, out)
        result = run_hidden([str(Tools.ffmpeg)] + args, timeout=120)
        files = sorted(out.glob("f_*.jpg"))
        log.append(f"extract rc={result.returncode} thumbs={len(files)} "
                   f"err={result.stderr.decode('utf-8', 'replace')[:200]}")
        from PySide6.QtGui import QPixmap
        pm = QPixmap(str(files[0])) if files else QPixmap()
        log.append(f"thumb size={pm.width()}x{pm.height()}")
        ok = ok and result.returncode == 0 and len(files) == 5 and pm.width() == 320

        # ---- 单帧预览（绝对时间） ----
        pv = tmp / "pv.jpg"
        result = run_hidden([str(Tools.ffmpeg)] + ffmpeg.build_preview_args(
            src, info.absolute_time(1.0), pv), timeout=120)
        pm2 = QPixmap(str(pv))
        log.append(f"preview rc={result.returncode} size={pm2.width()}x{pm2.height()}")
        ok = ok and result.returncode == 0 and pm2.width() == 640

        # ---- 界面渲染 ----
        win = MainWindow()
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.resize(1280, 800)
        win.show()
        win.add_videos([src])
        page = win.tabs.widget(0)
        page.quiet = True
        drained = _wait(app, lambda: page is not None and not page.busy
                        and page.model.rowCount() > 0, 120)
        rows = page.model.rowCount() if page is not None else 0
        log.append(f"ui rows={rows} tabs={win.tabs.count()} drained={drained}")

        shot = Path(tempfile.gettempdir()) / f"{APP_ID}_selftest.png"
        win.grab().save(str(shot))
        log.append(f"shot={shot} exists={shot.exists()}")
        ok = ok and rows == 5 and shot.exists() and shot.stat().st_size > 5000

        # ---- 缺 ffmpeg 的引导窗（打包后也要能构建） ----
        from .ui.ffmpeg_missing import FFmpegMissingDialog
        guide = FFmpegMissingDialog()
        guide.setAttribute(Qt.WA_DontShowOnScreen, True)
        guide.show()
        app.processEvents()
        from PySide6.QtWidgets import QLabel
        guide_text = " ".join(w.text() for w in guide.findChildren(QLabel))
        guide_ok = ("ffmpeg.org" in guide_text and "gyan.dev" in guide_text
                    and "BtbN" in guide_text and guide.resolved is False)
        log.append(f"guide window ok={guide_ok}")
        guide.close()
        app.processEvents()
        ok = ok and guide_ok

        # ---- 区间导出（关键帧吸附 + 无损复制） ----
        exported = tmp / "clip.mp4"
        if page is not None and rows >= 4:
            sm = page.view.selectionModel()
            from PySide6.QtCore import QItemSelection, QItemSelectionModel
            first = page.model.index(1)
            last = page.model.index(3)
            sm.select(QItemSelection(first, last),
                      QItemSelectionModel.SelectionFlag.ClearAndSelect)
            app.processEvents()

            # B：图墙即时间轴 + 浮窗卡 + 逐帧
            page._set_cursor(30)
            app.processEvents()
            strip_ok = _wait(app, lambda: page.strip is not None
                             and page.strip.path_for(30) is not None, 30)
            log.append(f"b: in={page.in_frame} out={page.out_frame} "
                       f"cursor={page.cursor_frame} "
                       f"side={page.side.lbl_frame.text()[:18]} strip={strip_ok}")
            ok = ok and page.in_frame == 24 and page.out_frame == 72
            ok = ok and page.cursor_frame == 30 and strip_ok
            ok = ok and page.side.lbl_frame.text().startswith("第 30 帧")

            # C：选区回放缓存（8fps 抽图，播放时只翻图）
            page.playclip.prepare(24, 72)
            play_ok = _wait(app, lambda: page.playclip is not None
                            and page.playclip.path_for(0) is not None
                            and page.playclip.path_for(6) is not None, 30)
            log.append(f"c: play_frames={page.playclip.count if page.playclip else 0} "
                       f"stride={page.playclip.stride if page.playclip else 0} ok={play_ok}")
            ok = ok and play_ok

            # D：多片段批量导出
            batch_dir = tmp / "batch"
            batch_dir.mkdir()
            page.set_in_point(24)
            page.set_out_point(36)
            page.add_segment()
            page.set_in_point(48)
            page.set_out_point(60)
            page.add_segment()
            page._batch_start(batch_dir, exact=True)
            batch_ok = _wait(app, lambda: page._batch is None
                             and page.lbl_export.text().startswith("✅"), 180)
            batch_files = sorted(batch_dir.glob("*.mp4"))
            log.append(f"d: segments={len(page.segments)} files={len(batch_files)} "
                       f"ok={batch_ok} label={page.lbl_export.text()[:40]}")
            ok = ok and batch_ok and len(batch_files) == 2
            plan = page.plan_export(page.model.item(1), page.model.item(3))
            log.append("plan: start=%.3f end=%.3f dur=%.3f early=%.3f kf=%s warn=%s"
                       % (plan["start"], plan["end"], plan["duration"],
                          plan["early"], plan["keyframe_ok"], plan["warn"] or "-"))
            # 行 1 = 0.8s，GOP 0.5s -> 应吸附到 0.5s（早 0.3s）
            ok = ok and plan["keyframe_ok"] and 0.2 < plan["early"] < 0.45

            page._export_to_path(exported, plan)
            done = _wait(app, lambda: page.exp_job is None and exported.exists()
                         and exported.stat().st_size > 1000, 120)
            label = page.lbl_export.text()
            log.append(f"export done={done} label={label}")
            if done:
                out_info = ffmpeg.probe(exported)
                log.append("export: %.2fs v=%s a=%s size=%d"
                           % (out_info.duration, out_info.codec, out_info.audio,
                              exported.stat().st_size))
                ok = ok and 1.55 < out_info.duration < 1.9
                ok = ok and out_info.codec == info.codec
                ok = ok and "帧精确" in label
            else:
                ok = False
            ok = ok and label.startswith("✅")

        win.close()
        QApplication.processEvents()
    except Exception as e:  # noqa: BLE001
        ok = False
        log.append(f"EXCEPTION: {e!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    log.append("RESULT: " + ("OK" if ok else "FAIL"))
    text = "\n".join(log)
    try:
        (Path(tempfile.gettempdir()) / f"{APP_ID}_selftest.log").write_text(
            text, encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 2
