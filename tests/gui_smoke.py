# -*- coding: utf-8 -*-
"""界面级冒烟（离线渲染）：多视频排队 + 区间导出 + 排队中取消。

    python tests\\gui_smoke.py

用 QT_QPA_PLATFORM=offscreen，不弹窗口；需要 ffmpeg。
输出 PASS/FAIL 到 stdout，退出码 0/2。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QItemSelection, QItemSelectionModel, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vff import ffmpeg  # noqa: E402
from vff.ffmpeg import Tools  # noqa: E402
from vff.ui.main_window import MainWindow  # noqa: E402

LOG: list[str] = []
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    LOG.append(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        FAILS.append(name)


def wait(app, cond, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    app.processEvents()
    return cond()


def make_clip(path: Path, seconds: int, gop: int, size: str = "640x360",
              fps: int = 30) -> None:
    subprocess.run(
        [str(Tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", str(seconds), "-c:v", "libx264", "-g", str(gop),
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True)


def main() -> int:
    if not Tools.locate():
        print("ffmpeg not found")
        return 2
    app = QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="purer_gui_"))
    try:
        clips = [tmp / f"c{i}.mp4" for i in range(3)]
        for c in clips:
            make_clip(c, 4, 15)

        win = MainWindow()
        win.resize(1200, 760)
        win.show()

        # ---- 1. 一次拖 3 个：必须串行 ----
        win.add_videos(clips)
        check("tabs==3", win.tabs.count() == 3, str(win.tabs.count()))

        peak = 0
        deadline = time.time() + 180
        while time.time() < deadline:
            app.processEvents()
            peak = max(peak, win.jobs.running_count)
            pages = [win.tabs.widget(i) for i in range(win.tabs.count())]
            if all(p is not None and not p.busy and p.model.rowCount() > 0 for p in pages):
                break
            time.sleep(0.02)
        check("queue never exceeded 1", peak <= 1, f"peak={peak}")
        rows = [win.tabs.widget(i).model.rowCount() for i in range(win.tabs.count())]
        check("all pages extracted", rows == [5, 5, 5], str(rows))

        # ---- 2. 区间导出（行 1..3） ----
        win.tabs.setCurrentIndex(0)
        app.processEvents()
        page = win.tabs.widget(0)
        page.quiet = True
        sm = page.view.selectionModel()
        sm.select(QItemSelection(page.model.index(1), page.model.index(3)),
                  QItemSelectionModel.SelectionFlag.ClearAndSelect)
        app.processEvents()
        check("selection label", "入点" in page.lbl_range.text()
              and "出点" in page.lbl_range.text(), page.lbl_range.text())
        check("export enabled", page.btn_export.isEnabled())

        # ---- B: 逐帧步进 / 浮窗卡 / 入出点 ----
        check("in/out from selection",
              page.in_frame == 24 and page.out_frame == 72,
              f"{page.in_frame},{page.out_frame}")
        page._set_cursor(26, sync_wall=True)
        app.processEvents()
        check("step 1 frame cursor", page.cursor_frame == 26, str(page.cursor_frame))
        check("sidebar shows frame", "第 26 帧" in page.side.lbl_frame.text(),
              page.side.lbl_frame.text())
        before = page.cursor_frame
        QTest.mouseClick(page.side.image, Qt.LeftButton,
                         pos=page.side.image.rect().center())
        app.processEvents()
        check("sidebar swallows clicks", page.cursor_frame == before,
              str(page.cursor_frame))
        strip_ok = wait(app, lambda: page.strip is not None
                        and page.strip.path_for(26) is not None, 30)
        check("filmstrip extracted", strip_ok,
              str(page.strip.path_for(26) if page.strip else None))
        page.set_in_point(25)
        page.set_out_point(30)
        app.processEvents()
        check("set in/out by frame",
              page.in_frame == 25 and page.out_frame == 30,
              f"{page.in_frame},{page.out_frame}")
        check("wall markers kept", page.view.in_frame == 25 and page.view.out_frame == 30)

        # ---- C: 选区回放（8fps，不解码） ----
        page.set_in_point(24)
        page.set_out_point(72)
        app.processEvents()
        page.toggle_playback()
        started = wait(app, lambda: page._playing, 30)
        check("playback starts", started, f"idx={page._play_index}")
        check("playback in sidebar", page.side.play_bar.isVisible() and page._playing,
              f"bar={page.side.play_bar.isVisible()}")
        if started:
            advanced = wait(app, lambda: (page.cursor_frame or 0) >= 40, 30)
            check("playback advances", advanced, str(page.cursor_frame))
            ended = wait(app, lambda: not page._playing, 40)
            check("playback stops at out",
                  ended and page.cursor_frame == 72, str(page.cursor_frame))
            check("play bar hidden", not page.side.play_bar.isVisible())
        else:
            check("playback advances", False)
            check("playback stops at out", False)
        # 还原成 1..3 行，给下面的导出用
        sm.select(QItemSelection(page.model.index(1), page.model.index(3)),
                  QItemSelectionModel.SelectionFlag.ClearAndSelect)
        app.processEvents()

        plan = page.plan_export(page.model.item(1), page.model.item(3))
        # 你点的帧 = 0.8s；无损只能从 0.5s 的关键帧开始（早 0.3s）
        check("requested start kept", abs(plan["start"] - 0.8) < 0.02,
              f"start={plan['start']:.3f}")
        check("lossless snaps to keyframe",
              abs(plan["lossless_start"] - 0.5) < 0.02
              and abs(plan["early"] - 0.3) < 0.02,
              f"lossless={plan['lossless_start']:.3f} early={plan['early']:.3f}")
        check("end covers selection", plan["end"] >= 2.4, f"end={plan['end']:.3f}")

        out = tmp / "clip.mp4"
        page._export_to_path(out, plan)
        done = wait(app, lambda: page.exp_job is None and out.exists()
                    and out.stat().st_size > 1000, 120)
        check("export finished", done, page.lbl_export.text())
        if done:
            info = ffmpeg.probe(out)
            check("export duration", 1.6 < info.duration < 1.9, f"{info.duration:.3f}")
            check("streams copied", info.codec == "h264" and info.audio == "aac",
                  f"{info.codec}+{info.audio}")
            check("exact mode", "帧精确" in page.lbl_export.text(),
                  page.lbl_export.text()[:40])
            check("label ok", page.lbl_export.text().startswith("✅"), page.lbl_export.text()[:40])

        # ---- D: 多片段列表 + 一次导出 ----
        page.set_in_point(24)
        page.set_out_point(36)
        page.add_segment()
        page.set_in_point(48)
        page.set_out_point(60)
        page.add_segment()
        page.set_in_point(72)
        page.set_out_point(90)
        page.add_segment()
        check("segments collected", len(page.segments) == 3, str(len(page.segments)))
        batch_dir = tmp / "batch"
        batch_dir.mkdir()
        page._batch_start(batch_dir, exact=True)
        batch_done = wait(app, lambda: page._batch is None
                          and page.lbl_export.text().startswith("✅"), 240)
        batch_files = sorted(batch_dir.glob("*.mp4"))
        check("batch exported 3", batch_done and len(batch_files) == 3,
              f"{len(batch_files)} 个 · {page.lbl_export.text()[:40]}")
        if len(batch_files) == 3:
            fps = page.info.fps or 30.0
            expected = [(36 - 24) / fps, (60 - 48) / fps, (90 - 72) / fps]
            durs = [ffmpeg.probe(f).duration for f in batch_files]
            check("batch durations",
                  all(abs(d - e) < 0.15 for d, e in zip(durs, expected)),
                  str([round(d, 2) for d in durs]))
            check("batch exact mode", "帧精确" in page.lbl_export.text(),
                  page.lbl_export.text()[:40])

        # ---- 3. 校验失败 -> 自动回退无损（稳定性关键路径） ----
        import vff.smartcut as smartcut_mod
        original_verify = smartcut_mod.verify
        smartcut_mod.verify = lambda *a, **k: (False, "测试用：模拟拼缝损坏")
        fallback_out = tmp / "fallback.mp4"
        try:
            page.lbl_export.setText("")
            page._export_to_path(fallback_out, plan, exact=True)
            fb_done = wait(app, lambda: page.exp_job is None and fallback_out.exists()
                           and "已导出" in page.lbl_export.text(), 120)
            check("fallback on verify fail", fb_done and "回退" in page.lbl_export.text(),
                  page.lbl_export.text()[:60])
            if fb_done:
                fb_info = ffmpeg.probe(fallback_out)
                # 回退后是无损：从 0.5s 开始 -> 约 1.97s
                check("fallback is lossless", 1.85 < fb_info.duration < 2.15,
                      f"{fb_info.duration:.3f}")
        finally:
            smartcut_mod.verify = original_verify

        # ---- 4. 排队中取消：新页面在排队时点停止 ----
        fourth = tmp / "c3.mp4"
        make_clip(fourth, 8, 30)
        page._export_to_path(tmp / "clip2.mp4", plan)   # 先占住队列
        win.add_videos([fourth])
        queued = win.tabs.widget(win.tabs.count() - 1)
        app.processEvents()
        check("fourth is queued", queued.job is not None and queued.job.state == "idle",
              f"state={queued.job.state if queued.job else None}")
        queued.stop_extract()
        app.processEvents()
        check("cancel while queued", queued.job is None or queued.job.state == "done")
        check("queue recovered", wait(app, lambda: win.jobs.running_count <= 1, 30))

        # 四个页面最终都不应卡死
        wait(app, lambda: all(not win.tabs.widget(i).busy
                              for i in range(win.tabs.count())), 120)

        # ---- 5. .ts 源：先无损转封装再切（真实相机素材路径） ----
        ts_clip = tmp / "c_ts.ts"
        subprocess.run([str(Tools.ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-y", "-i", str(clips[0]), "-c", "copy", "-f", "mpegts",
                        str(ts_clip)], check=True)
        win.add_videos([ts_clip])
        tpage = win.tabs.widget(win.tabs.count() - 1)
        tpage.quiet = True
        ok_extract = wait(app, lambda: tpage is not None and not tpage.busy
                          and tpage.model.rowCount() > 2, 120)
        check("ts extracted", ok_extract, f"rows={tpage.model.rowCount() if tpage else 0}")
        if ok_extract:
            tsm = tpage.view.selectionModel()
            tsm.select(QItemSelection(tpage.model.index(1), tpage.model.index(2)),
                       QItemSelectionModel.SelectionFlag.ClearAndSelect)
            app.processEvents()
            ts_plan = tpage.plan_export(tpage.model.item(1), tpage.model.item(2))
            ts_out = tmp / "ts_out.mp4"
            tpage._export_to_path(ts_out, ts_plan, exact=True)
            ts_done = wait(app, lambda: tpage.exp_job is None and ts_out.exists()
                           and ts_out.stat().st_size > 1000
                           and "已导出" in tpage.lbl_export.text(), 180)
            check("ts export finished", ts_done, tpage.lbl_export.text()[:60])
            if ts_done:
                ts_info = ffmpeg.probe(ts_out)
                check("ts exact mode", "帧精确" in tpage.lbl_export.text(),
                      tpage.lbl_export.text()[:40])
                check("ts duration ok",
                      abs(ts_info.duration - ts_plan["duration"]) < 0.5,
                      f"{ts_info.duration:.2f} vs {ts_plan['duration']:.2f}")

        # ---- 6. 导出确认对话框能正常构造 ----
        from PySide6.QtWidgets import QDialog
        from vff.ui import dialogs as dialogs_mod
        original_exec = QDialog.exec
        QDialog.exec = lambda self: QDialog.DialogCode.Accepted
        try:
            dlg_ok, dlg_exact = dialogs_mod.ask_export(
                None, "标题", "细节", exact_available=True, exact_note="说明")
            check("confirm dialog builds", dlg_ok and dlg_exact, f"{dlg_ok},{dlg_exact}")
            dlg_ok2, dlg_exact2 = dialogs_mod.ask_export(
                None, "标题", "细节", exact_available=False, exact_note="不支持")
            check("confirm dialog disables exact", dlg_ok2 and not dlg_exact2,
                  f"{dlg_ok2},{dlg_exact2}")
        finally:
            QDialog.exec = original_exec

        win.close()
        app.processEvents()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n".join(LOG))
    print("RESULT:", "PASS" if not FAILS else "FAIL " + ", ".join(FAILS))
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
