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

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vff import ffmpeg  # noqa: E402
from vff.ffmpeg import Tools  # noqa: E402
from vff.ui.main_window import MainWindow  # noqa: E402
from vff.ui.widgets import LANE_H  # noqa: E402

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

        # ---- B2: 时间轴手柄「垂直拖动」+ 预览跟随被拖的帧 ----
        total = max(1, int(page.info.frames or 0))
        page.set_display_side(320)          # 放大缩略图 → 墙折成多行，才能测纵向拖动
        win.resize(760, 760)
        app.processEvents()
        page.in_frame, page.out_frame, page.cursor_frame = None, None, None
        page.set_in_point(24)
        page.set_out_point(total - 1)
        app.processEvents()
        view = page.view
        rows = view._visible_rows()
        check("wall wraps into rows", len(rows) >= 2, f"rows={len(rows)}")

        def lane_of(row):
            rects = [view._row_rect(i) for i in row[1]]
            rects = [r for r in rects if r is not None]
            return max(r.bottom() for r in rects) - LANE_H // 2, rects

        if len(rows) >= 2:
            target_row = rows[-1]
            lane_y, target_rects = lane_of(target_row)
            in_row = None
            for row in rows:
                if int(page.in_frame / page.step) in row[1]:
                    in_row = row
                    break
            start_lane, rects0 = lane_of(in_row or rows[0])
            x_in = view._x_for_frame((in_row or rows[0])[1], rects0, page.in_frame)
        else:
            x_in = None
        if x_in is not None and len(rows) >= 2:
            # 按在柄上 → 一路拖到最后一行 → 松手
            QTest.mousePress(view.viewport(), Qt.LeftButton,
                             pos=QPoint(int(x_in + 1), start_lane))
            QTest.mouseMove(view.viewport(),
                            QPoint(int(target_rects[0].center().x()), lane_y))
            app.processEvents()
            dragged = page.in_frame
            release_cursor = page.cursor_frame
            shown = wait(app, lambda: f"第 {dragged} 帧" in page.side.lbl_frame.text(), 2)
            check("vertical drag moves in point",
                  dragged is not None and dragged != 24, f"in={dragged}")
            check("vertical drag follows dragged frame",
                  release_cursor == dragged and shown,
                  f"cursor={release_cursor} side={page.side.lbl_frame.text()}")
            check("vertical drag lands on target row",
                  target_row[1][0] * page.step <= dragged
                  <= (target_row[1][-1] + 1) * page.step - 1,
                  f"in={dragged} row={target_row[1][0]}..{target_row[1][-1]}")
            QTest.mouseRelease(view.viewport(), Qt.LeftButton,
                               pos=QPoint(int(target_rects[0].center().x()), lane_y))
            app.processEvents()
            check("in point kept after release", page.in_frame == dragged,
                  f"{page.in_frame} vs {dragged}")
        else:
            check("vertical drag moves in point", False)
            check("vertical drag follows dragged frame", False)
            check("vertical drag lands on target row", False)
            check("in point kept after release", False)

        # ---- B3: 出点手柄同样能垂直拖；纯横向拖动仍然正常 ----
        page.set_in_point(0)
        page.set_out_point(30)
        app.processEvents()
        rows = view._visible_rows()
        if len(rows) >= 2:
            out_row = next((r for r in rows
                            if int(page.out_frame / page.step) in r[1]), None)
            last_row = rows[-1]
            moved_out = page.out_frame
            if out_row is not None and last_row[1] != out_row[1]:
                o_lane, o_rects = lane_of(out_row)
                t_lane, t_rects = lane_of(last_row)
                ox = view._x_for_frame(out_row[1], o_rects, page.out_frame)
                if ox is not None:
                    QTest.mousePress(view.viewport(), Qt.LeftButton,
                                     pos=QPoint(int(ox - 1), o_lane))
                    QTest.mouseMove(view.viewport(),
                                    QPoint(int(t_rects[0].center().x()), t_lane))
                    app.processEvents()
                    moved_out = page.out_frame
                    QTest.mouseRelease(view.viewport(), Qt.LeftButton,
                                       pos=QPoint(int(t_rects[0].center().x()), t_lane))
                    app.processEvents()
            check("vertical drag moves out point",
                  moved_out > 30 and page.cursor_frame == moved_out, f"out={moved_out}")

            # 横向：同一行里按到下一个格子，帧号跟着变（老的微调行为没坏）
            page.set_display_side(200)
            win.resize(1100, 760)
            app.processEvents()
            rows = view._visible_rows()
            two = next((r for r in rows if len(r[1]) >= 2), None)
            if two is not None:
                lane2, rects2 = lane_of(two)
                page.set_in_point(two[1][0] * page.step)
                app.processEvents()
                x0 = view._x_for_frame(two[1], rects2, page.in_frame)
                if x0 is not None:
                    QTest.mousePress(view.viewport(), Qt.LeftButton,
                                     pos=QPoint(int(x0 + 1), lane2))
                    QTest.mouseMove(view.viewport(),
                                    QPoint(int(rects2[1].center().x()), lane2))
                    app.processEvents()
                    horiz = page.in_frame
                    QTest.mouseRelease(view.viewport(), Qt.LeftButton,
                                       pos=QPoint(int(rects2[1].center().x()), lane2))
                    app.processEvents()
                    check("horizontal drag still works",
                          horiz == two[1][1] * page.step, f"in={horiz}")
                else:
                    check("horizontal drag still works", False)
            else:
                check("horizontal drag still works", False)
        else:
            check("vertical drag moves out point", False)
            check("horizontal drag still works", False)
        page.set_display_side(160)
        win.resize(1200, 760)
        app.processEvents()

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

        # ffmpeg 缺失引导窗（0.2：官网/镜像/自动搜索/选目录；1.1.2：加国内镜像）
        from PySide6.QtWidgets import QLabel, QPushButton
        from vff.ui.ffmpeg_missing import FFmpegMissingDialog, MIRROR_ZIP
        guide = FFmpegMissingDialog()
        guide.setAttribute(Qt.WA_DontShowOnScreen, True)
        guide.show()
        app.processEvents()
        texts = " ".join(w.text() for w in guide.findChildren(QLabel))
        buttons = [b.text() for b in guide.findChildren(QPushButton)]
        check("guide dialog links",
              "ffmpeg.org" in texts and "gyan.dev" in texts and "BtbN" in texts)
        check("guide dialog domestic mirror",
              "npmmirror" in texts and MIRROR_ZIP in texts
              and "registry.npmmirror.com" in texts, MIRROR_ZIP)
        check("guide dialog buttons",
              any(t.startswith("选择 ffmpeg") for t in buttons)
              and any("自动搜索" in t for t in buttons)
              and any("重新检测" in t for t in buttons)
              and any("复制镜像链接" in t for t in buttons), " | ".join(buttons))
        check("guide dialog not resolved", guide.resolved is False)
        guide._copy_mirror()
        check("copy mirror link", QApplication.clipboard().text() == MIRROR_ZIP)
        guide.close()
        app.processEvents()

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
