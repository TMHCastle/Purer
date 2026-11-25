# -*- coding: utf-8 -*-
"""界面级冒烟（离线渲染）：多视频排队 + 区间导出 + 排队中取消。

    python tests\\gui_smoke.py

用 QT_QPA_PLATFORM=offscreen，不弹窗口；需要 ffmpeg。
输出 PASS/FAIL 到 stdout，退出码 0/2。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PURER_NO_UPDATE"] = "1"        # 冒烟测试不联网
os.environ["PURER_DOWNLOAD_MIRRORS"] = ("https://m1.example/,"
                                        "https://m2.example/")
# 用独立缓存目录：别读到你自己 ui.ini 里的 抽帧方式/显示尺寸（否则结果不可复现）
os.environ["PURER_CACHE_DIR"] = tempfile.mkdtemp(prefix="purer_smoke_cache_")
# 设置 / 已保存缓存也放临时目录：冒烟测试不能碰真实用户数据
os.environ["PURER_DATA_DIR"] = tempfile.mkdtemp(prefix="purer_smoke_data_")
os.environ["PURER_PERSIST_CACHE_DIR"] = tempfile.mkdtemp(
    prefix="purer_smoke_pcache_")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (QApplication, QMessageBox, QSplitter,  # noqa: E402
                               QTabBar)

from vff import ffmpeg  # noqa: E402
from vff.config import APP_VERSION  # noqa: E402
from vff.ffmpeg import Tools  # noqa: E402
from vff.ui.main_window import MainWindow  # noqa: E402
from vff.ui.style import OK, TAB_GUIDE, WARN, apply_theme  # noqa: E402
from vff.ui.widgets import LANE_H  # noqa: E402
from vff.update import CheckResult  # noqa: E402

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
    apply_theme(app)          # 真实运行一定会套主题：冒烟也要用同一套样式
    tmp = Path(tempfile.mkdtemp(prefix="purer_gui_"))
    try:
        clips = [tmp / f"c{i}.mp4" for i in range(3)]
        for c in clips:
            make_clip(c, 4, 15)

        win = MainWindow()
        win.resize(1200, 760)
        win.show()
        app.processEvents()
        screen = win.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry()
        check("minimum window fits current screen",
              win.minimumWidth() <= area.width()
              and win.minimumHeight() <= area.height(),
              f"min={win.minimumWidth()}x{win.minimumHeight()} "
              f"screen={area.width()}x{area.height()}")

        # 冒烟测试固定用「每 N 帧」，不读机器上的 ui.ini
        for i in range(win.combo_mode.count()):
            if win.combo_mode.itemData(i) == "step":
                win.combo_mode.setCurrentIndex(i)
                break
        check("extract mode is step", win._extract_mode == "step", win._extract_mode)

        # ---- 间隔默认「跟随视频帧率（1 秒 1 帧）」 ----
        class _FakeInfo:
            fps = 60.0

        preset_values = [win.combo_step.itemData(i)
                         for i in range(win.combo_step.count())]
        check("step presets include common frame rates",
              all(v in preset_values for v in (24, 25, 30, 50, 60, 120)),
              str(preset_values))
        check("auto step follows source fps",
              win._step_auto and abs(win._step_for(_FakeInfo()) - 60.0) < 1e-6,
              f"auto={win._step_auto} step={win._step_for(_FakeInfo())}")
        # 后面的帧号 / 导出断言都按「每 24 帧」写，这里固定成 24
        win.combo_step.set_value(24)
        app.processEvents()
        check("fixed step turns auto off", win._step_auto is False)

        # ---- A: 自绘标题栏 + 顶部卡片正中的「检查更新」药丸 ----
        check("window is frameless", bool(win.windowFlags() & Qt.FramelessWindowHint))
        titlebar, badge = win.titlebar, win.badge
        check("titlebar visible on top",
              titlebar.isVisible() and titlebar.height() > 0
              and titlebar.y() < win.card.height(),
              f"h={titlebar.height()} y={titlebar.y()}")
        check("titlebar shows app + settings",
              "Purer" in titlebar.brand.text()
              and titlebar.btn_settings.text() == "设置"
              and APP_VERSION in titlebar.btn_settings.toolTip(),
              f"{titlebar.brand.text()} {titlebar.btn_settings.text()} "
              f"{titlebar.btn_settings.toolTip()}")
        offset = badge.geometry().center().x() - titlebar.width() // 2
        check("badge centered in top card", abs(offset) <= 2,
              f"dx={offset} badge={badge.geometry().getRect()} tb={titlebar.width()}")
        check("badge inside titlebar",
              titlebar.rect().contains(badge.geometry().center()),
              str(badge.geometry().getRect()))

        win._on_update_result(CheckResult(True, "v999.0.0", "https://example.invalid/r"))
        check("badge orange when newer",
              badge.state_name() == "stale" and "999" in badge.label_text()
              and badge.dot_color() == WARN,
              f"{badge.state_name()} {badge.label_text()} {badge.dot_color()}")
        win._on_update_result(CheckResult(True, f"v{APP_VERSION}", ""))
        check("badge green when latest",
              badge.state_name() == "latest" and badge.dot_color() == OK,
              f"{badge.state_name()} {badge.label_text()} {badge.dot_color()}")
        win._on_update_result(CheckResult(False, "", "", "离线"))
        check("badge gray when check failed",
              badge.state_name() == "error"
              and badge.dot_color() not in (OK, WARN)
              and "失败" in badge.label_text(),
              f"{badge.state_name()} {badge.label_text()} {badge.dot_color()}")
        check("badge stays centered after text change",
              abs(badge.geometry().center().x() - titlebar.width() // 2) <= 2,
              str(badge.geometry().getRect()))

        # ---- 标签页：圆角卡片 + 自绘关闭按钮 ----
        from vff.ui.style import DARK_QSS
        check("modern tab style in stylesheet",
              "QTabBar::tab" in DARK_QSS and "border-radius" in DARK_QSS
              and "QTabBar::tab:selected" in DARK_QSS)
        check("custom tab close style in stylesheet",
              "QPushButton#tabClose" in DARK_QSS
              and "QPushButton#tabClose:hover" in DARK_QSS)

        # ---- 自动更新用的两个新状态（下载中 / 已下载待装） ----
        badge.set_state("downloading", tag="v1.1.5", detail="42%")
        check("badge downloading shows percent",
              badge.state_name() == "downloading" and "42%" in badge.label_text(),
              badge.label_text())
        badge.set_progress(77)
        check("badge progress updates text", "77%" in badge.label_text(),
              badge.label_text())
        badge.set_state("ready", tag="v1.1.5")
        check("badge ready asks to restart",
              badge.state_name() == "ready" and "重启安装" in badge.label_text()
              and badge.label_text().find("v1.1.5") >= 0,
              badge.label_text())
        from vff.selfupdate import can_self_update
        allowed, _why = can_self_update()
        check("self-update refused in source run", not allowed, _why)

        titlebar.btn_max.click()
        app.processEvents()
        maximized = win.isMaximized()
        titlebar.btn_max.click()
        app.processEvents()
        check("maximize / restore buttons", maximized and not win.isMaximized(),
              f"max={maximized} back={win.isMaximized()}")

        # ---- 设置窗：关于 / 缓存 / 更新 ----
        from vff.ui.settings_dialog import CloseCacheDialog, SettingsDialog
        from PySide6.QtWidgets import QLabel
        settings_dlg = SettingsDialog(win, settings=win.app_settings,
                                      store=win.cache_store)
        settings_dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
        settings_dlg.show()
        app.processEvents()
        tabs = [settings_dlg.tabs.tabText(i)
                for i in range(settings_dlg.tabs.count())]
        check("settings tabs (cache left, update, about right)",
              tabs == ["缓存", "更新", "关于"], str(tabs))
        check("settings opens the leftmost tab",
              settings_dlg.tabs.currentIndex() == 0,
              str(settings_dlg.tabs.currentIndex()))
        check("update page has both toggles",
              settings_dlg.check_auto_update.isChecked()
              and not settings_dlg.check_mute_update.isChecked(),
              f"auto={settings_dlg.check_auto_update.isChecked()} "
              f"mute={settings_dlg.check_mute_update.isChecked()}")
        check("update page shows state",
              "没有忽略" in settings_dlg.lbl_update_state.text(),
              settings_dlg.lbl_update_state.text())
        about_text = " ".join(w.text() for w in settings_dlg.findChildren(QLabel))
        check("about shows version", f"v{APP_VERSION}" in about_text,
              about_text[:80])
        check("about shows library info",
              "ffmpeg" in about_text and "PySide6" in about_text
              and "Python" in about_text)
        check("cache page default policy",
              settings_dlg.radio_never.isChecked()
              and not settings_dlg.check_skip.isEnabled()
              and settings_dlg.spin_videos.value() == 20,
              f"never={settings_dlg.radio_never.isChecked()} "
              f"skip_enabled={settings_dlg.check_skip.isEnabled()} "
              f"videos={settings_dlg.spin_videos.value()}")
        settings_dlg.radio_usage.setChecked(True)
        app.processEvents()
        check("usage mode enables its spinboxes",
              settings_dlg.spin_minutes.isEnabled()
              and settings_dlg.spin_exports.isEnabled()
              and not settings_dlg.spin_videos.isEnabled()
              and settings_dlg.check_skip.isEnabled())
        settings_dlg.spin_minutes.setValue(7)
        settings_dlg.check_skip.setChecked(True)
        values = settings_dlg.values()
        check("settings values collected",
              values["mode"] == "usage" and values["usage_minutes"] == 7
              and values["skip_close_prompt"] is True, str(values))
        settings_dlg.radio_never.setChecked(True)
        app.processEvents()
        self_never = settings_dlg.values()
        check("never mode disables skip prompt",
              self_never["mode"] == "never"
              and not settings_dlg.check_skip.isEnabled()
              and self_never["skip_close_prompt"] is False, str(self_never))
        settings_dlg.radio_usage.setChecked(True)
        settings_dlg.check_skip.setChecked(True)
        app.processEvents()
        settings_dlg.accept()
        app.processEvents()
        saved_cfg = win.app_settings.cache()
        check("settings saved to disk",
              saved_cfg["mode"] == "usage" and saved_cfg["usage_minutes"] == 7
              and saved_cfg["skip_close_prompt"] is True, str(saved_cfg))
        settings_dlg.deleteLater()
        app.processEvents()

        # ---- 设置改动即时落盘（用 Esc / 叉关掉也不丢） ----
        auto_dlg = SettingsDialog(win, settings=win.app_settings,
                                  store=win.cache_store)
        auto_dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
        auto_dlg.show()
        app.processEvents()
        auto_dlg.radio_count.setChecked(True)
        auto_dlg.spin_videos.setValue(5)
        wait(app, lambda: win.app_settings.cache()["mode"] == "count"
             and win.app_settings.cache()["max_videos"] == 5, 3.0)
        check("settings autosave on change",
              win.app_settings.cache()["mode"] == "count"
              and win.app_settings.cache()["max_videos"] == 5,
              str(win.app_settings.cache()))
        disk_cfg = json.loads(
            win.app_settings.path.read_text(encoding="utf-8"))["cache"]
        check("settings autosave writes file",
              disk_cfg["mode"] == "count" and disk_cfg["max_videos"] == 5,
              str(disk_cfg))
        auto_dlg.reject()                   # 用 Esc / 标题栏的叉关掉
        app.processEvents()
        after_cfg = json.loads(
            win.app_settings.path.read_text(encoding="utf-8"))["cache"]
        check("settings survive esc / close",
              after_cfg["mode"] == "count" and after_cfg["max_videos"] == 5
              and win.app_settings.cache()["mode"] == "count",
              str(after_cfg))
        auto_dlg.deleteLater()
        app.processEvents()
        # 还原成后面用例需要的策略
        win.app_settings.update_cache(mode="usage", usage_minutes=7,
                                      skip_close_prompt=True)
        win.app_settings.save()
        app.processEvents()

        check("cache store follows settings dir",
              win.cache_store.root == win.app_settings.effective_cache_dir(),
              f"{win.cache_store.root} vs "
              f"{win.app_settings.effective_cache_dir()}")

        # ---- 1. 一次拖 3 个：必须串行 ----
        win.add_videos(clips)
        check("tabs==3", win.tabs.count() == 3, str(win.tabs.count()))
        bar = win.tabs.tabBar()
        check("custom close button on every tab",
              bar.count() == 3 and all(
                  bar.tabButton(i, QTabBar.RightSide) is not None
                  for i in range(bar.count())))

        # ---- 标签栏横条：上下浅灰引导线（状态色画在标签卡片上） ----
        win._refresh_tab_states()
        check("tab shows loading while extracting",
              win.page_cache_state(win.tabs.widget(0)) == "loading"
              and bar.state_of(0) == "loading",
              f"{win.page_cache_state(win.tabs.widget(0))} / {bar.state_of(0)}")
        app.processEvents()
        shot = win.grab().toImage()

        def _hex(x: int, y: int) -> str:
            c = shot.pixelColor(x, y)
            return "#%02X%02X%02X" % (c.red(), c.green(), c.blue())

        tbar_top = bar.mapTo(win, bar.rect().topLeft()).y()
        mid_x = bar.mapTo(win, bar.rect().center()).x()    # 标签栏中段（避开滚动按钮）
        left_x = bar.mapTo(win, bar.rect().topLeft()).x() + 2
        top_row = [_hex(left_x, tbar_top + 1), _hex(mid_x, tbar_top + 1)]
        bottom_row = [_hex(left_x, tbar_top + bar.height() - 1),
                      _hex(mid_x, tbar_top + bar.height() - 1)]
        check("tab strip top guide line (full width)",
              top_row == [TAB_GUIDE, TAB_GUIDE], str(top_row))
        check("tab strip bottom guide line (full width)",
              bottom_row == [TAB_GUIDE, TAB_GUIDE], str(bottom_row))

        last_dir = str(win.setting.value("paths/last_file_dir", ""))
        check("remembers last opened directory",
              Path(last_dir).resolve() == clips[0].parent.resolve(), last_dir)

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

        def tab_fill(index: int):
            """标签卡片里面（避开文字）的底色，用来判断「有没有按状态染色」。"""
            rect = bar.tabRect(index)
            img = bar.grab().toImage()
            return img.pixelColor(rect.left() + 5, rect.bottom() - 7)

        win.tabs.setCurrentIndex(2)
        app.processEvents()
        win._refresh_tab_states()
        page2 = win.tabs.widget(2)
        check("tab state is ready (blue) after load",
              win.page_cache_state(page2) == "ready" and bar.state_of(2) == "ready",
              f"{win.page_cache_state(page2)} / {bar.state_of(2)}")
        check("cache badge shows ready", win.cache_badge.state_name() == "ready",
              win.cache_badge.state_name())
        blue = tab_fill(2)
        check("uncached tab is tinted blue",
              blue.blue() - blue.red() >= 12,
              f"rgb={blue.red(),blue.green(),blue.blue()}")
        QTest.mouseClick(win.cache_badge, Qt.LeftButton)
        app.processEvents()
        win._refresh_tab_states()
        check("manual cache badge click saves",
              win.page_cache_state(page2) == "saved"
              and win.cache_store.has(page2.cache_digest()),
              win.page_cache_state(page2))
        check("cache badge turns saved",
              win.cache_badge.state_name() == "saved",
              win.cache_badge.state_name())
        green = tab_fill(2)
        check("cached tab is tinted green",
              green.green() - green.red() >= 10
              and green.green() > blue.green(),
              f"rgb={green.red(),green.green(),green.blue()} "
              f"blue={blue.red(),blue.green(),blue.blue()}")
        check("other tabs keep their own state",
              win.page_cache_state(win.tabs.widget(0)) == "ready")

        # 右键标签菜单：手动保存 / 关闭的入口还在（不真的弹出来）
        menu_items = [a.text() for a in
                      win._build_tab_menu(win.tabs.widget(0), 0).actions()]
        check("tab context menu offers save/close",
              "保存这一页的缓存" in menu_items and "关闭全部" in menu_items,
              str(menu_items))

        # 自动缓存策略下：渲染完就该自己变成「已缓存」（不用手动点）
        win.app_settings.update_cache(mode="count", max_videos=20,
                                      skip_close_prompt=True)
        win.app_settings.save()
        for _ in range(6):                 # 每次只存一页，多跑几拍
            if not win._auto_save_pass():
                break
            app.processEvents()
        win._refresh_tab_states()
        check("auto cache marks tabs as cached",
              all(win.page_cache_state(win.tabs.widget(i)) == "saved"
                  for i in range(win.tabs.count())),
              str([win.page_cache_state(win.tabs.widget(i))
                   for i in range(win.tabs.count())]))
        # 还原：后面「保存缓存 / 关闭前提示」用例需要「还没保存」的页面
        win.cache_store.clear()
        win.app_settings.update_cache(mode="never", skip_close_prompt=False)
        win.app_settings.save()
        app.processEvents()
        win._refresh_tab_states()
        check("clearing cache returns tabs to uncached",
              all(win.page_cache_state(win.tabs.widget(i)) == "ready"
                  for i in range(win.tabs.count())),
              str([win.page_cache_state(win.tabs.widget(i))
                   for i in range(win.tabs.count())]))

        # 记忆上次设置：间隔（测完恢复「跟随视频帧率」）
        win.combo_step.set_value(12)
        app.processEvents()
        check("remembers extract step",
              abs(float(win.setting.value("ui/step", 0)) - 12.0) < 1e-6,
              str(win.setting.value("ui/step", 0)))
        check("manual step turns auto off",
              win._step_auto is False
              and str(win.setting.value("ui/step_auto")).lower() == "false",
              f"auto={win._step_auto} "
              f"{win.setting.value('ui/step_auto')}")
        win.combo_step.set_auto(True, notify=True)
        app.processEvents()
        check("auto step can be turned back on", win._step_auto is True)
        win.combo_step.set_auto(True, notify=False)
        win._step_auto = True              # 保持一致：之后新开的视频按它自己的帧率
        app.processEvents()

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

        # ---- B1.5: 底部总进度条（和墙上时间轴共用同一份状态） ----
        bar = page.overview
        total = max(1, int(page.info.frames or 0))
        check("overview bar exists", bar is not None and not bar.isHidden()
              and bar.height() >= 60, f"h={bar.height() if bar else '?'}")
        check("overview bar has a draggable splitter",
              isinstance(page.vsplit, QSplitter)
              and page.vsplit.orientation() == Qt.Vertical,
              type(page.vsplit).__name__)
        app.processEvents()
        geo_bar, geo_side = bar.geometry(), page.side.geometry()
        check("bottom bar stops at the side card",
              geo_bar.right() <= geo_side.left(), f"{geo_bar} vs {geo_side}")
        check("side card spans the full page height",
              abs(geo_side.height() - page.split.height()) <= 2,
              f"card={geo_side.height()} split={page.split.height()}")
        img = page.side.image
        inner = (page.side.p_preview.width()
                 - page.side.p_preview.layout().contentsMargins().left()
                 - page.side.p_preview.layout().contentsMargins().right())
        check("preview fills the card inner width",
              abs(img.width() - inner) <= 6, f"{img.width()} vs {inner}")
        check("preview height follows the clip aspect",
              abs(img.width() / max(1, img.height()) - page.info.width
                  / max(1, page.info.height)) < 0.06,
              f"{img.width()}x{img.height()} clip="
              f"{page.info.width}x{page.info.height}")
        before_h = bar.height()
        page.set_overview_height(before_h + 40)
        app.processEvents()
        check("overview height can be dragged taller",
              bar.height() > before_h + 20, f"{before_h} -> {bar.height()}")
        page.set_overview_height(before_h)
        app.processEvents()
        check("overview mirrors in/out", bar.in_frame == 25 and bar.out_frame == 30,
              f"{bar.in_frame},{bar.out_frame}")
        check("overview roundtrips frame", all(
            abs(bar.frame_at(bar._x_for_frame(f)) - f) <= 1
            for f in (0, total // 2, total - 1)),
            f"total={total}")
        page.clear_range()
        midpoint = total // 2
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(int(bar._x_for_frame(midpoint)),
                                    bar.track_y()))
        app.processEvents()
        check("overview click seeks", abs((page.cursor_frame or -1) - midpoint) <= 1,
              f"cursor={page.cursor_frame} want={midpoint}")
        page.set_out_point(min(total - 1, midpoint + 80))
        page.set_in_point(midpoint)
        app.processEvents()
        x_in = int(bar._x_for_frame(page.in_frame))
        want_in = max(1, page.in_frame - 20)
        x_target = int(bar._x_for_frame(want_in))
        flag_y = bar.track_y() - 8
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(x_in, flag_y))
        QTest.mouseMove(bar, QPoint(x_target, flag_y))
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(x_target, flag_y))
        app.processEvents()
        check("overview drag moves in point",
              abs(page.in_frame - want_in) <= 1,
              f"in={page.in_frame} want={want_in}")
        check("overview mirrors cursor", bar.cursor_frame == page.cursor_frame,
              f"{bar.cursor_frame} vs {page.cursor_frame}")
        page.clear_range()
        page.set_in_point(midpoint)
        page.set_out_point(midpoint + 10)
        page.add_segment()
        app.processEvents()
        check("overview mirrors segments", len(bar._segments) == 1,
              str(bar._segments))
        page.clear_segments()
        page.clear_range()
        app.processEvents()

        # ---- B1.6: PR 式标记（M）+ Alt 缩放 ----
        page.clear_markers()
        page.view.setFocus()
        page._set_cursor(30, sync_wall=True)
        app.processEvents()
        QTest.keyClick(page.view, Qt.Key_M)
        app.processEvents()
        check("M adds marker at playhead", page.markers == [30], str(page.markers))
        check("wall shows marker", page.view.range_markers == [30],
              str(page.view.range_markers))
        check("overview shows marker", bar.markers == [30], str(bar.markers))
        page._set_cursor(90, sync_wall=True)
        page.add_marker()
        app.processEvents()
        check("second marker added", page.markers == [30, 90], str(page.markers))
        check("sidebar shows marker count",
              "标记 2 个" in page.side.lbl_markers.text(),
              page.side.lbl_markers.text())
        page._set_cursor(90, sync_wall=True)
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(int(bar._x_for_frame(30)), bar.track_y() - 5))
        app.processEvents()
        check("click overview marker jumps", page.cursor_frame == 30,
              str(page.cursor_frame))
        # M 再按一下 = 取消同一个点上的标记
        page._set_cursor(30, sync_wall=True)
        QTest.keyClick(page.view, Qt.Key_M)
        app.processEvents()
        check("M toggles marker off", page.markers == [90], str(page.markers))
        check("overview drops toggled marker", bar.markers == [90], str(bar.markers))
        QTest.keyClick(page.view, Qt.Key_M)
        app.processEvents()
        check("M adds it back", page.markers == [30, 90], str(page.markers))
        page._set_cursor(0, sync_wall=True)
        QTest.keyClick(page.view, Qt.Key_M, Qt.ShiftModifier)
        app.processEvents()
        check("shift+M next marker", page.cursor_frame == 30,
              str(page.cursor_frame))
        QTest.keyClick(page.view, Qt.Key_M, Qt.ShiftModifier)
        app.processEvents()
        check("shift+M last marker", page.cursor_frame == 90, str(page.cursor_frame))
        QTest.keyClick(page.view, Qt.Key_M, Qt.ShiftModifier | Qt.ControlModifier)
        app.processEvents()
        check("ctrl+shift+M previous marker", page.cursor_frame == 30,
              str(page.cursor_frame))
        before_side = page.display_side
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.AltModifier, Qt.NoScrollPhase, False)
        page.view.wheelEvent(event)
        app.processEvents()
        check("alt+wheel zooms", page.display_side > before_side,
              f"{before_side} -> {page.display_side}")
        before_ctrl = page.display_side
        event = QWheelEvent(QPointF(20, 20), QPointF(20, 20),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.ControlModifier, Qt.NoScrollPhase, False)
        page.view.wheelEvent(event)
        app.processEvents()
        check("ctrl+wheel no longer zooms", page.display_side == before_ctrl,
              f"{before_ctrl} -> {page.display_side}")
        page.set_display_side(before_side)
        check("bottom bar starts fit", bar.zoom == 1.0, str(bar.zoom))
        side_now = page.display_side
        event = QWheelEvent(QPointF(20, bar.track_y()),
                            QPointF(20, bar.track_y()),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.AltModifier, Qt.NoScrollPhase, False)
        bar.wheelEvent(event)
        app.processEvents()
        check("alt+wheel on the bar zooms the timeline", bar.zoom > 1.0,
              f"zoom={bar.zoom}")
        check("bottom bar zoom leaves thumbnails alone",
              page.display_side == side_now,
              f"{side_now} -> {page.display_side}")
        check("zoomed bar roundtrips frames", all(
            abs(bar.frame_at(bar._x_for_frame(f)) - f) <= 1
            for f in (int(bar._view_start) + 1,
                      bar._view_start + bar._visible() / 2)),
            f"zoom={bar.zoom} start={bar._view_start:.1f}")
        x_anchor = bar.width() * 0.25
        frame_before = bar.frame_at(x_anchor)
        for _ in range(2):
            event = QWheelEvent(QPointF(x_anchor, bar.track_y()),
                                QPointF(x_anchor, bar.track_y()),
                                QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                                Qt.AltModifier, Qt.NoScrollPhase, False)
            bar.wheelEvent(event)
            app.processEvents()
        check("bar zoom keeps the frame under the cursor",
              abs(bar.frame_at(x_anchor) - frame_before) <= 2,
              f"{frame_before} -> {bar.frame_at(x_anchor)} zoom={bar.zoom:.2f}")
        bar.reset_zoom()
        check("bar zoom resets to fit",
              bar.zoom == 1.0 and bar._view_start == 0.0,
              f"{bar.zoom} @ {bar._view_start}")
        page.set_display_side(before_side)

        # 叠在一起的片段：按层错开，Alt+滚轮翻层
        page.clear_segments()
        page.clear_range()
        for i in range(6):
            lo = midpoint + i
            page.set_in_point(lo)
            page.set_out_point(lo + 12)
            page.add_segment()
        app.processEvents()
        check("segments stack into lanes", bar.lane_count() >= 5,
              f"lanes={bar.lane_count()} n={len(bar._segments)}")
        check("stack starts at bottom lane", bar.band_scroll == 0,
              str(bar.band_scroll))
        check("bands have real height",
              bar.band_height() >= 8 and bar.band_gap() == 2,
              f"band={bar.band_height()} gap={bar.band_gap()}")
        h_before = bar.band_height()
        gap_before = bar.band_gap()
        panel_before = bar.height()
        # Shift+滚轮：所有条一起加高（缝隙固定 2px、面板高度不动）
        event = QWheelEvent(QPointF(bar.width() * 0.5, bar.track_y()),
                            QPointF(bar.width() * 0.5, bar.track_y()),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.ShiftModifier, Qt.NoScrollPhase, False)
        bar.wheelEvent(event)
        app.processEvents()
        check("shift+wheel makes all bars taller",
              bar.band_height() > h_before,
              f"{h_before} -> {bar.band_height()}")
        check("gap stays fixed at 2px", bar.band_gap() == gap_before == 2,
              f"{gap_before} -> {bar.band_gap()}")
        check("shift+wheel leaves the panel height alone",
              bar.height() == panel_before,
              f"{panel_before} -> {bar.height()}")
        check("shift+wheel does not zoom the bar", bar.zoom == 1.0, str(bar.zoom))
        bar.scale_bands(-1)
        check("lanes scale back", bar.band_height() == h_before,
              f"{h_before} -> {bar.band_height()}")

        # Alt+滚轮压在具体某一条上：只调那一条的高度（单轨垂直伸缩）
        first_band = next((b for b in bar._bands
                       if b.get("kind") == "segment"), None)
        if first_band is not None:
            rect = bar._band_rect(first_band)
            spot = QPointF(rect.center().x(), rect.center().y())
            others = [bar._band_h(b) for b in bar._bands
                      if b is not first_band]
            before_h = bar._band_h(first_band)
            alt_event = QWheelEvent(spot, spot, QPoint(0, 0), QPoint(0, 120),
                                    Qt.NoButton, Qt.AltModifier,
                                    Qt.NoScrollPhase, False)
            bar.wheelEvent(alt_event)
            app.processEvents()
            check("alt+wheel over one block resizes just that track",
                  bar._band_h(first_band) > before_h
                  and [bar._band_h(b) for b in bar._bands
                       if b is not first_band] == others,
                  f"{before_h} -> {bar._band_h(first_band)} "
                  f"rect={rect.getRect()} spot=({spot.x():.1f},{spot.y():.1f}) "
                  f"hit={bar._band_at(spot.toPoint()) is not None} "
                  f"lane={first_band.get('lane')} vis={bar._lane_visible(int(first_band.get('lane', 0)))} "
                  f"scroll={bar.band_scroll} width={bar.width()} h={bar.height()}")
            check("single-track resize does not zoom the timeline",
                  bar.zoom == 1.0, str(bar.zoom))
            bar.scale_band(first_band, -1)
        else:
            print("SKIP  single-track resize  (没有片段条)")

        check("plain wheel scrolls lanes", bar.scroll_bands(-1)
              and bar.band_scroll == 1, str(bar.band_scroll))
        bar.scroll_bands(1)
        check("lanes scroll back", bar.band_scroll == 0, str(bar.band_scroll))

        # 左侧竖滚动条（PR 同款小端点 + 圆滑块）
        geo = bar._scroll_geometry()
        check("left scrollbar appears when lanes overflow", geo is not None,
              f"lanes={bar.lane_count()} visible={bar.visible_lanes()} geo={geo}")
        if geo:
            sx, stop, sheight, sty, sth = geo
            cursor_before = page.cursor_frame
            before_scroll = bar.band_scroll
            QTest.mousePress(bar, Qt.LeftButton,
                             pos=QPoint(sx + 3, sty + sth // 2))
            QTest.mouseMove(bar, QPoint(sx + 3, stop + sheight - sth // 2))
            QTest.mouseRelease(bar, Qt.LeftButton,
                               pos=QPoint(sx + 3, stop + sheight - sth // 2))
            app.processEvents()
            check("dragging the left scrollbar scrolls the lanes",
                  bar.band_scroll > before_scroll,
                  f"{before_scroll} -> {bar.band_scroll}")
            check("dragging the left scrollbar keeps the playhead",
                  page.cursor_frame == cursor_before,
                  f"{cursor_before} -> {page.cursor_frame}")
            bar.band_scroll = 0
            bar.update()
            app.processEvents()

        # 普通滚轮 = 时间轴左右滚动；Ctrl+滚轮 = 上下翻层
        bar.set_zoom(4.0)
        start_before = bar._view_start
        stack_y = max(2, bar.stack_bottom() - bar.band_height() - 2)
        event = QWheelEvent(QPointF(bar.width() * 0.5, stack_y),
                            QPointF(bar.width() * 0.5, stack_y),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.NoModifier, Qt.NoScrollPhase, False)
        bar.wheelEvent(event)
        app.processEvents()
        check("plain wheel scrolls the timeline sideways",
              bar._view_start != start_before,
              f"{start_before:.1f} -> {bar._view_start:.1f}")
        start_before = bar._view_start
        ctrl_event = QWheelEvent(QPointF(bar.width() * 0.5, bar.track_y()),
                                 QPointF(bar.width() * 0.5, bar.track_y()),
                                 QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                                 Qt.ControlModifier, Qt.NoScrollPhase, False)
        bar.wheelEvent(ctrl_event)
        app.processEvents()
        check("ctrl+wheel scrolls lanes, not the timeline",
              bar._view_start == start_before,
              f"{start_before:.1f} -> {bar._view_start:.1f}")
        bar.reset_zoom()
        page.clear_segments()
        page.clear_range()
        app.processEvents()

        # 可滚动的大图墙：Alt+滚轮不要把位置甩走（锚点钉在鼠标那一行）
        page.set_display_side(320)
        app.processEvents()
        vbar = page.view.verticalScrollBar()
        if vbar.maximum() > 0:
            vbar.setValue(vbar.maximum() // 2)
            app.processEvents()
            top_before = page.view._first_visible()
            spot = QPoint(page.view.viewport().width() // 2,
                          page.view.viewport().height() // 2)
            event = QWheelEvent(QPointF(spot), QPointF(spot),
                                QPoint(0, 0), QPoint(0, -120), Qt.NoButton,
                                Qt.AltModifier, Qt.NoScrollPhase, False)
            page.view.wheelEvent(event)
            app.processEvents()
            top_after = page.view._first_visible()
            check("alt+wheel keeps the wall in place",
                  abs(top_after - top_before) <= 4,
                  f"top {top_before} -> {top_after}")
        else:
            print("SKIP  alt+wheel keeps the wall in place  (内容不足一屏)")
        page.set_display_side(before_side)

        # ---- B1.6b: 行内时间轴：点=移帧、拖=多段、导出全部收进片段表 ----
        page.clear_range()
        page.clear_segments()
        page.save_state()
        saved = page._read_state()
        check("markers saved with cache", saved.get("markers") == [30, 90],
              str(saved.get("markers")))
        page.clear_markers()
        page.set_display_side(160)
        app.processEvents()
        vis = page.view._visible_rows()
        if vis:
            bucket = vis[0][1]
            rects = [page.view.visualRect(page.model.index(i, 0))
                     for i in bucket]
            lane_y = max(r.bottom() for r in rects) - 6
            x = int((rects[0].left() + rects[-1].right()) / 2)
            before_in = page.in_frame
            QTest.mouseClick(page.view.viewport(), Qt.LeftButton,
                             pos=QPoint(x, lane_y))
            app.processEvents()
            check("click on the lane only moves the cursor",
                  page.in_frame is None and page.out_frame is None,
                  f"in={page.in_frame} out={page.out_frame}")
            check("lane click seeks to that frame",
                  page.cursor_frame is not None and page.cursor_frame > 0,
                  f"cursor={page.cursor_frame}")

            def drag(a, b, y=None):
                yy = lane_y if y is None else y
                QTest.mousePress(page.view.viewport(), Qt.LeftButton,
                                 pos=QPoint(a, yy))
                QTest.mouseMove(page.view.viewport(), QPoint((a + b) // 2, yy))
                QTest.mouseMove(page.view.viewport(), QPoint(b, yy))
                QTest.mouseRelease(page.view.viewport(), Qt.LeftButton,
                                   pos=QPoint(b, yy))
                app.processEvents()

            # 工具带上拖动 = 直接拖播放头（不再拉区间）
            before_cursor = page.cursor_frame
            drag(x - 60, x + 60)
            check("dragging the thumbnail ladder scrubs the playhead",
                  page.cursor_frame is not None
                  and page.cursor_frame != before_cursor
                  and page.in_frame is None and page.out_frame is None,
                  f"cursor {before_cursor} -> {page.cursor_frame} "
                  f"in={page.in_frame} out={page.out_frame}")

            # 点缩略图：第一下就落到点击那一帧（先跳锚帧 = 之前的「乱跳」）
            calls = []
            orig_cursor_fn = page._set_cursor

            def spy(frame, sync_wall=True):
                calls.append(int(frame))
                return orig_cursor_fn(frame, sync_wall)

            page._set_cursor = spy
            try:
                item_index = page.model.index(bucket[-1], 0)
                item_rect = page.view.visualRect(item_index)
                click_x = item_rect.left() + item_rect.width() // 4
                expect = page.view._frame_in_item(item_index, click_x)
                QTest.mouseClick(page.view.viewport(), Qt.LeftButton,
                                 pos=QPoint(click_x, item_rect.top() + 30))
                app.processEvents()
            finally:
                page._set_cursor = orig_cursor_fn
            check("clicking a thumbnail seeks straight to the clicked frame",
                  bool(calls) and calls[0] == int(expect),
                  f"calls={calls} expect={expect}")

            # 在缩略图上拖 = 框选区间
            thumb_y = rects[0].top() + 40
            drag(x - 60, x - 20, thumb_y)
            first_span = (page.in_frame, page.out_frame)
            drag(x + 20, x + 60, thumb_y)
            check("dragging the thumbnails adds a second range",
                  len(page.range_spans()) == 2,
                  f"ranges={page.range_spans()}")
            check("first range kept",
                  (first_span[0], first_span[1])
                  in [(lo, hi) for lo, hi, *_ in page.range_spans()],
                  f"{first_span} vs {page.range_spans()}")
            check("ranges get different colors",
                  len({c for _lo, _hi, c, _l, _a in page.range_spans()}) == 2,
                  str([(lo, hi, c) for lo, hi, c, _l, _a in page.range_spans()]))

            page.export_segment()
            app.processEvents()
            check("export moves the ranges into the queue",
                  len(page.segments) == 2 and page.range_spans() == [],
                  f"segments={len(page.segments)} ranges={page.range_spans()}")

            # 删掉当前段，另一段还在（重新造两段区间）
            page.ranges = [{"in": 10, "out": 20, "color": 0},
                           {"in": 30, "out": 40, "color": 1}]
            page.active_range = 0
            page.in_frame, page.out_frame = 10, 20
            page._refresh_range()
            app.processEvents()
            page.remove_active_range()
            app.processEvents()
            check("remove active range keeps the others",
                  len(page.range_spans()) == 1,
                  str(page.range_spans()))
        else:
            print("SKIP  lane drag tests  (没有可见行)")
        page.clear_range()
        page.clear_segments()
        app.processEvents()

        # ---- B1.6b2: 片段层选择（点选 / 框选 / 回车加入导出队列） ----
        page.clear_segments()
        page.clear_range()
        total_frames = max(1, int(page.info.frames or 0))
        for i in range(3):
            if i:
                page._begin_wall_range()
            lo = max(0, min(total_frames - 2, int(total_frames * (0.2 + i * 0.2))))
            page.set_in_point(lo)
            page.set_out_point(min(total_frames - 1, lo + max(2, total_frames // 10)))
        bar.band_scroll = 0
        bar.selection.clear()
        app.processEvents()
        check("three range bands on the bar",
              len([b for b in bar._bands if b["kind"] == "range"]) == 3,
              str([(b["kind"], b["lo"], b["hi"]) for b in bar._bands]))
        band = bar._bands[0]
        rect = bar._band_rect(band)
        cursor_before = page.cursor_frame
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(int(rect.center().x()),
                                    int(rect.center().y())))
        app.processEvents()
        check("clicking a band selects it", bar.is_selected(band),
              str(bar.selection))
        check("clicking a band does not move the playhead",
              page.cursor_frame == cursor_before,
              f"{cursor_before} -> {page.cursor_frame}")
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(int(rect.center().x()),
                                    int(rect.center().y())))
        app.processEvents()
        check("plain click keeps only that band selected",
              bar.selection == {bar._band_key(band)}, str(bar.selection))
        QTest.mouseClick(bar, Qt.LeftButton, Qt.ShiftModifier,
                         pos=QPoint(int(rect.center().x()),
                                    int(rect.center().y())))
        app.processEvents()
        check("shift+click toggles the band off",
              not bar.is_selected(band), str(bar.selection))

        # 空白处拖一个框，把三条都框进来
        start = QPoint(6, bar.track_y() - 20)
        end = QPoint(bar.width() - 6, 2)
        QTest.mousePress(bar, Qt.LeftButton, pos=start)
        QTest.mouseMove(bar, QPoint((start.x() + end.x()) // 2, 10))
        QTest.mouseMove(bar, end)
        QTest.mouseRelease(bar, Qt.LeftButton, pos=end)
        app.processEvents()
        check("rubber band selects every band in the box",
              len(bar.selection) == 3, str(bar.selection))

        win.activateWindow()
        page.view.setFocus(Qt.OtherFocusReason)
        app.processEvents()
        QTest.keyClick(page.view, Qt.Key_Return)
        app.processEvents()
        check("Enter adds the selected bands to the export queue",
              len(page.segments) == 3,
              f"segments={len(page.segments)} sel={bar.selection} "
              f"picked={bar.selected_ranges()} "
              f"status={page.side.lbl_status.text()!r} "
              f"focus={type(QApplication.focusWidget()).__name__}")
        page.clear_segments()
        page.clear_range()
        bar.clear_selection()
        app.processEvents()

        # ---- B1.6b3: 点空白不动光标；Ctrl+Z / Ctrl+Shift+Z ----
        page.clear_segments()
        page.clear_range()
        page.clear_markers()
        bar.clear_selection()
        page.set_in_point(10)
        page.set_out_point(20)
        app.processEvents()
        cursor_before = page.cursor_frame
        empty_y = max(2, bar.track_y() - 22)
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(bar.width() // 2, empty_y))
        app.processEvents()
        check("clicking empty segment area keeps the playhead",
              page.cursor_frame == cursor_before,
              f"{cursor_before} -> {page.cursor_frame} y={empty_y}")
        QTest.mouseClick(bar, Qt.LeftButton,
                         pos=QPoint(int(bar.width() * 0.6), bar.track_y()))
        app.processEvents()
        check("clicking the timeline moves the playhead",
              page.cursor_frame != cursor_before,
              f"{cursor_before} -> {page.cursor_frame}")

        # 撤销 / 重做：标记
        page.clear_markers()
        page.add_marker(30)
        app.processEvents()
        check("marker added before undo", page.markers == [30], str(page.markers))
        win.activateWindow()
        page.view.setFocus(Qt.OtherFocusReason)
        app.processEvents()
        QTest.keyClick(page.view, Qt.Key_Z, Qt.ControlModifier)
        app.processEvents()
        check("Ctrl+Z undoes the marker", page.markers == [], str(page.markers))
        QTest.keyClick(page.view, Qt.Key_Z,
                       Qt.ControlModifier | Qt.ShiftModifier)
        app.processEvents()
        check("Ctrl+Shift+Z redoes the marker", page.markers == [30],
              str(page.markers))

        # 撤销 / 重做：清空区间
        page.clear_markers()
        page.clear_range()
        page.set_in_point(5)
        page.set_out_point(15)
        app.processEvents()
        ranges_before = len(page.range_spans())
        page.clear_range()
        app.processEvents()
        check("range cleared before undo", page.range_spans() == [],
              str(page.range_spans()))
        page.undo()
        app.processEvents()
        check("undo restores the cleared range",
              len(page.range_spans()) == ranges_before,
              f"{ranges_before} -> {len(page.range_spans())}")
        page.redo()
        app.processEvents()
        check("redo clears it again", page.range_spans() == [],
              str(page.range_spans()))
        page.clear_range()
        page.clear_markers()
        app.processEvents()

        # ---- B1.6b4: 拖动片段（50px 死区 / 横向 / 纵向 / 虚影） ----
        page.clear_segments()
        page.clear_range()
        bar.band_scroll = 0
        page.segments = [{"t_in": 0.3, "t_end": 1.3, "name": "A"},
                         {"t_in": 2.0, "t_end": 3.0, "name": "B"}]
        page._refresh_segments()
        app.processEvents()
        band = next(b for b in bar._bands
                    if b["kind"] == "segment" and b["item"].get("name") == "A")
        rect = bar._band_rect(band)
        cx, cy = int(rect.center().x()), int(rect.center().y())
        t_in0 = page.segments[0]["t_in"]

        # 死区：拖 40px 不动
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(cx, cy))
        QTest.mouseMove(bar, QPoint(cx + 20, cy))
        QTest.mouseMove(bar, QPoint(cx + 40, cy))
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(cx + 40, cy))
        app.processEvents()
        check("dragging <50px keeps the clip in place",
              abs(page.segments[0]["t_in"] - t_in0) < 1e-6,
              f"{t_in0} -> {page.segments[0]['t_in']}")

        # 超过 50px：横向移动 + 落点压在 B 上 → 只能去上下新轨
        band = next(b for b in bar._bands
                    if b["kind"] == "segment" and b["item"].get("name") == "A")
        rect = bar._band_rect(band)
        cx, cy = int(rect.center().x()), int(rect.center().y())
        lane0 = int(band["lane"])
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(cx, cy))
        QTest.mouseMove(bar, QPoint(cx + 120, cy))
        QTest.mouseMove(bar, QPoint(cx + 200, cy))
        app.processEvents()
        check("drag shows a ghost preview", bool(bar.drag_preview()),
              str(bar.drag_preview()))
        snap = dict(bar._drag_snap or {})
        check("occupied target lane snaps to a new lane instead",
              bool(snap.get("insert")) and int(snap.get("lane", -1)) == lane0 + 1,
              str(snap))
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(cx + 200, cy))
        app.processEvents()
        check("dragging >100px moves the clip",
              page.segments[0]["t_in"] > t_in0 + 0.05,
              f"{t_in0} -> {page.segments[0]['t_in']}")
        check("dropping on an occupied lane lands in another lane",
              page.segments[0].get("lane") != page.segments[1].get("lane"),
              str([s.get("lane") for s in page.segments]))

        # 两条轨道之间新建一层：虚影 = 半条 + 缝隙 + 半条
        page.clear_segments()
        page.segments = [{"t_in": 0.3, "t_end": 1.3, "name": "A", "lane": 0},
                         {"t_in": 1.5, "t_end": 2.5, "name": "B", "lane": 1}]
        page._refresh_segments()
        app.processEvents()
        gap_cand = next(c for c in bar._vertical_candidates(0)
                        if c["insert"] and c["lane"] == 1)
        check("gap ghost is one band + one gap tall",
              abs(gap_cand["height"] - (bar.band_height()
                                        + bar.band_gap())) < 0.01,
              str(gap_cand["height"]))
        check("lane ghost is exactly one band tall",
              abs((bar._vertical_candidates(0)[0])["height"]
                  - bar.band_height()) < 0.01,
              str(bar._vertical_candidates(0)[0]["height"]))

        # 纵向：往下一层
        band = next(b for b in bar._bands
                    if b["kind"] == "segment" and b["item"].get("name") == "A")
        rect = bar._band_rect(band)
        cx, cy = int(rect.center().x()), int(rect.center().y())
        lane_before = int(band["lane"])
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(cx, cy))
        QTest.mouseMove(bar, QPoint(cx, cy + 30))
        QTest.mouseMove(bar, QPoint(cx, cy + 60))
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(cx, cy + 60))
        app.processEvents()
        check("vertical drag moves the clip to another lane",
              page.segments[0].get("lane") not in (None, lane_before),
              f"lane {lane_before} -> {page.segments[0].get('lane')}")

        # Alt 只关横向吸附，纵向照样吸轨道
        page.clear_segments()
        page.segments = [{"t_in": 0.3, "t_end": 1.3, "name": "A", "lane": 0},
                         {"t_in": 2.0, "t_end": 3.0, "name": "B", "lane": 1}]
        page._refresh_segments()
        app.processEvents()
        band = next(b for b in bar._bands
                    if b["kind"] == "segment" and b["item"].get("name") == "B")
        rect = bar._band_rect(band)
        cx, cy = int(rect.center().x()), int(rect.center().y())
        lane_before = int(band["lane"])
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(cx, cy))
        QTest.mouseMove(bar, QPoint(cx, cy + 3))         # 进入拖动状态
        bar._update_drag(QPoint(cx, cy - 60), True)      # Alt 按住 + 往上拖
        app.processEvents()
        check("alt does not disable vertical snapping",
              bar._drag_alt and bar._drag_lane != lane_before,
              f"alt={bar._drag_alt} lane={bar._drag_lane} (was {lane_before})")
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(cx, cy - 60))
        app.processEvents()
        page.clear_segments()
        app.processEvents()

        # Alt 关横向吸附：靠物理按键兜底（Qt 的修饰键可能被系统菜单吞掉）
        import vff.ui.overview_bar as ob
        page.clear_segments()
        page.clear_range()
        bar.clear_selection()
        bar.band_scroll = 0
        app.processEvents()
        fps_d = bar.fps or 25.0
        scale = bar._usable() / max(1e-9, bar._visible())    # 像素 / 帧
        a_hi = 44
        target = a_hi + max(3, int(round(57 / max(scale, 1e-6))) - 1)

        def reset_bands():
            page.segments = [
                {"t_in": 20 / fps_d, "t_end": a_hi / fps_d, "name": "A"},
                {"t_in": target / fps_d, "t_end": (target + 30) / fps_d,
                 "name": "B"}]
            page._refresh_segments()
            app.processEvents()

        def drag_to(dx_px: int):
            band = next(b for b in bar._bands if b["item"].get("name") == "A")
            r_ = bar._band_rect(band)
            x_, y_ = int(r_.center().x()), int(r_.center().y())
            raw = int(bar.frame_at(x_ + dx_px)) - int(bar.frame_at(x_))
            QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(x_, y_))
            QTest.mouseMove(bar, QPoint(x_ + 20, y_))
            QTest.mouseMove(bar, QPoint(x_ + dx_px, y_))
            app.processEvents()
            delta_ = bar._drag_delta
            QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(x_ + dx_px, y_))
            app.processEvents()
            return delta_, raw

        saved_alt = ob.alt_down
        reset_bands()
        ob.alt_down = lambda: False
        snap_delta, raw_delta = drag_to(57)
        reset_bands()
        ob.alt_down = lambda: True
        free_delta, raw2 = drag_to(57)
        ob.alt_down = saved_alt
        app.processEvents()
        check("alt releases horizontal snapping",
              snap_delta != raw_delta and free_delta == raw2,
              f"raw={raw_delta} snap={snap_delta} free={free_delta}")
        page.clear_segments()
        app.processEvents()

        # 贴边：首尾紧贴（共用一帧）不算重叠，同层可以前后紧贴
        page.segments = [{"t_in": 0.3, "t_end": 1.3, "name": "A"},
                         {"t_in": 1.3, "t_end": 2.3, "name": "B"}]
        page._refresh_segments()
        bar.band_scroll = 0
        app.processEvents()
        lanes = sorted({int(b["lane"]) for b in bar._bands
                        if b["kind"] == "segment"})
        check("touching clips share one lane", lanes == [0], str(lanes))
        band_a = next(b for b in bar._bands if b["item"].get("name") == "A")
        r_ = bar._band_rect(band_a)
        cx, cy = int(r_.center().x()), int(r_.center().y())
        bar._begin_drag(band_a, QPoint(cx, cy))
        bar._dragging = True
        snap_flush = bar._vertical_snap(cy, 0)
        snap_overlap = bar._vertical_snap(cy, 2)
        check("flush edge still allows the same lane",
              snap_flush is not None and snap_flush["insert"] is False,
              str(snap_flush))
        check("real 1-frame overlap still forces a new lane",
              snap_overlap is not None and snap_overlap["insert"] is True,
              str(snap_overlap))
        bar._press_band = None
        bar._dragging = False
        bar._drag_origins = []
        bar._drag_snap = None
        app.processEvents()

        # Shift 多选 → 多条一起拖
        page.segments = [{"t_in": 0.3, "t_end": 1.3, "name": "A"},
                         {"t_in": 2.0, "t_end": 3.0, "name": "B"}]
        page._refresh_segments()
        bar.band_scroll = 0
        app.processEvents()
        band_a = next(b for b in bar._bands if b["item"].get("name") == "A")
        band_b = next(b for b in bar._bands if b["item"].get("name") == "B")
        for item in (band_a, band_b):
            r_ = bar._band_rect(item)
            QTest.mouseClick(bar, Qt.LeftButton, Qt.ShiftModifier,
                             pos=QPoint(int(r_.center().x()),
                                        int(r_.center().y())))
        app.processEvents()
        check("shift click selects two bands", len(bar.selection) == 2,
              str(bar.selection))
        before = [seg["t_in"] for seg in page.segments]
        r_ = bar._band_rect(band_a)
        cx, cy = int(r_.center().x()), int(r_.center().y())
        QTest.mousePress(bar, Qt.LeftButton, pos=QPoint(cx, cy))
        QTest.mouseMove(bar, QPoint(cx + 120, cy))
        QTest.mouseMove(bar, QPoint(cx + 200, cy))
        QTest.mouseRelease(bar, Qt.LeftButton, pos=QPoint(cx + 200, cy))
        app.processEvents()
        after = [seg["t_in"] for seg in page.segments]
        check("multi selection drags together",
              all(a > b + 0.05 for a, b in zip(after, before)),
              f"{before} -> {after}")


        # ---- 底栏帧刻度尺：越放大越密，间距不低于 4px ----
        bar = page.overview
        bar.set_zoom(1.0)
        bar._pan_to(0)
        app.processEvents()
        step_wide = bar.ruler_step()
        bar.set_zoom(min(bar._max_zoom(), 12.0))
        bar._pan_to(0)
        app.processEvents()
        step_zoom = bar.ruler_step()
        ppf = bar._usable() / max(1e-9, bar._visible())
        check("frame ruler never coarsens when zoomed in",
              step_zoom <= step_wide, f"{step_wide} -> {step_zoom}")
        check("frame ruler keeps >= 4px between ticks",
              step_zoom * ppf >= 4.0 - 1e-9,
              f"{step_zoom} x {ppf:.4f} = {step_zoom * ppf:.2f}px")
        bar.set_zoom(bar._max_zoom())
        bar._pan_to(0)
        app.processEvents()
        px_per_frame = bar._usable() / max(1e-9, bar._visible())
        check("max zoom gives one frame per 20px",
              abs(px_per_frame - 20.0) <= 0.6 or bar._max_zoom() <= 1.001,
              f"{px_per_frame:.2f}px/frame zoom={bar.zoom:.1f}")
        check("max zoom shows per-frame ruler ticks",
              bar.ruler_step() == 1, str(bar.ruler_step()))
        bar.set_zoom(1.0)
        bar._pan_to(0)
        app.processEvents()

        # ---- 工具带：每帧一根细竖线 + 上界标记 ----
        vis2 = page.view._visible_rows()
        if vis2:
            top2, bucket2 = vis2[0]
            rects2 = [page.view._row_rect(i) for i in bucket2]
            rects2 = [r for r in rects2 if r is not None]
            lane_top2, lane_bottom2, base2 = page.view._lane_band(rects2)
            check("thumbnail ladder band is tall enough to hit",
                  lane_bottom2 - lane_top2 >= 12,
                  f"{lane_bottom2 - lane_top2}px")
            lines2 = page.view._frame_lines(bucket2, rects2,
                                            rects2[0].left(), rects2[-1].right())
            frames2 = (page.view.timeline.span(bucket2[-1])[1]
                       - page.view.timeline.frame_at(bucket2[0]))
            check("thumbnail ladder draws one line per frame",
                  len(lines2) >= int(frames2 * 0.8),
                  f"{len(lines2)} lines / {frames2:.0f} frames")
            check("thumbnail ladder lines start at the row edge",
                  bool(lines2) and lines2[0] <= rects2[0].left() + 2,
                  f"first={lines2[0] if lines2 else None} "
                  f"left={rects2[0].left()}")

        # 刻度 / 点击 / 拖拽必须是同一帧（点哪儿就是哪一帧）
        vis3 = [r for r in page.view._visible_rows() if r[0] >= 0] \
            or page.view._visible_rows()
        if vis3:
            _t3, bucket3 = vis3[0]
            rects3 = [page.view._row_rect(i) for i in bucket3]
            rects3 = [r for r in rects3 if r is not None]
            lo3, hi3, xa, xb = page.view._row_span(bucket3, rects3)
            lines3 = page.view._frame_lines(bucket3, rects3, xa, xb)
            mism = 0
            f3 = int(round(lo3))
            for xl in lines3:
                if page.view._frame_at_x(bucket3, rects3, xl) != f3:
                    mism += 1
                back = page.view._x_for_frame(bucket3, rects3, f3)
                if back is None or abs(back - xl) > 0.51:
                    mism += 1
                f3 += 1
            check("frame ruler ticks match frames exactly", mism == 0,
                  f"mismatches={mism}")
            check("frame ruler covers every frame in the row",
                  len(lines3) == int(hi3 - lo3) + 1,
                  f"{len(lines3)} vs {int(hi3 - lo3) + 1}")
            idx3 = page.model.index(bucket3[min(1, len(bucket3) - 1)], 0)
            r3 = page.view.visualRect(idx3)
            if r3.isValid() and r3.height() > 60:
                xq = int(r3.left() + r3.width() * 0.4)
                yq = int(r3.top() + 40)
                seen = set()
                for _ in range(4):
                    QTest.mouseClick(page.view.viewport(), Qt.LeftButton,
                                     pos=QPoint(xq, yq))
                    app.processEvents()
                    seen.add(page.cursor_frame)
                check("clicking one spot always picks the same frame",
                      len(seen) == 1
                      and seen == {page.view._frame_in_item(idx3, xq)},
                      f"{sorted(seen)}")
            else:
                check("clicking one spot always picks the same frame", False,
                      str(r3.getRect()))

        # ---- B1.6c: Delete / 退格 只删「选中的区间」；队列里的片段受保护 ----
        page.clear_segments()
        page.clear_range()
        bar.clear_selection()
        app.processEvents()
        for i in range(3):
            page.set_in_point(10 + i * 30)
            page.set_out_point(20 + i * 30)
            page.add_segment()
        app.processEvents()
        page.clear_range()
        app.processEvents()
        check("three segments added", len(page.segments) == 3,
              str(len(page.segments)))
        bands = [b for b in bar._bands if b["kind"] == "segment"]
        check("three segment bands", len(bands) == 3, str(len(bands)))
        before = len(page.segments)
        page._delete_selected()
        app.processEvents()
        check("Delete with nothing selected deletes nothing",
              len(page.segments) == before, str(len(page.segments)))
        bar.selection = {bar._band_key(bands[0])}
        page._delete_selected()
        app.processEvents()
        check("segments in the export queue survive bottom Delete",
              len(page.segments) == 3, str(len(page.segments)))

        # 多选（区间 + 片段）：只删区间，队列里的片段照样锁定
        page.clear_range()
        page.set_in_point(5)
        page.set_out_point(15)
        app.processEvents()
        rng_band = next((b for b in bar._bands if b["kind"] == "range"), None)
        seg_band = next((b for b in bar._bands if b["kind"] == "segment"), None)
        if rng_band is not None and seg_band is not None:
            bar.selection = {bar._band_key(rng_band), bar._band_key(seg_band)}
            page._delete_selected()
            app.processEvents()
            check("mixed selection delete keeps queued segments",
                  len(page.segments) == 3
                  and not any(b["kind"] == "range" for b in bar._bands),
                  f"segments={len(page.segments)} "
                  f"ranges={len([b for b in bar._bands if b['kind'] == 'range'])}")
        else:
            check("mixed selection delete keeps queued segments", False,
                  f"rng={rng_band} seg={seg_band}")

        # 区间 → 队列：搬过去（区间从底栏消失，不并排留一条一样的）
        page.clear_segments()
        page.clear_range()
        page.ranges = [{"in": 24, "out": 36, "color": 0}]
        page.active_range = 0
        page.in_frame, page.out_frame = 24, 36
        page._refresh_range()
        app.processEvents()
        check("one range band before adding",
              len([b for b in bar._bands if b["kind"] == "range"]) == 1,
              str([(b["kind"], b["lo"], b["hi"]) for b in bar._bands]))
        page.add_segment()
        app.processEvents()
        kinds = [(b["kind"], int(b["lo"]), int(b["hi"])) for b in bar._bands]
        check("adding a range moves it into the queue",
              len(page.segments) == 1 and page.range_spans() == []
              and len([k for k in kinds if k[0] == "segment"]) == 1
              and not [k for k in kinds if k[0] == "range"],
              f"segments={len(page.segments)} ranges={page.range_spans()} "
              f"bands={kinds}")
        check("moved band keeps the same place",
              bool([k for k in kinds if k[0] == "segment"
                    and k[1] == 24 and k[2] == 36]),
              str(kinds))
        page.clear_segments()
        page.clear_range()
        app.processEvents()

        # 加入队列：加的是「选中的那条」，且重复加不产生副本
        page.clear_segments()
        page.ranges = [{"in": 24, "out": 36, "color": 0},
                       {"in": 100, "out": 120, "color": 1}]
        page.active_range = 0
        page.in_frame, page.out_frame = 24, 36
        page._refresh_range()
        app.processEvents()
        bar.clear_selection()
        second = next((b for b in bar._bands
                       if b["kind"] == "range" and int(b["lo"]) == 100), None)
        if second is not None:
            bar.selection = {bar._band_key(second)}
            page.add_segment()
            app.processEvents()
            fps = page._fps()
            want = (round(100 / fps, 4), round(120 / fps, 4))
            got = [(round(seg["t_in"], 4), round(seg["t_end"], 4))
                   for seg in page.segments]
            check("adding a selected range queues that range",
                  got == [want], f"{got} want {want}")
            rest = [b for b in bar._bands if b["kind"] == "range"]
            check("the other range stays on the bar",
                  [int(b["lo"]) for b in rest] == [24], str(rest))
            check("queued span leaves no duplicate range",
                  not [b for b in rest if int(b["lo"]) == 100],
                  str([(int(b["lo"]), int(b["hi"])) for b in rest]))
            # 选中队列里的片段再点加入 → 什么都不加
            seg_band = next((b for b in bar._bands
                             if b["kind"] == "segment"), None)
            if seg_band is not None:
                bar.selection = {bar._band_key(seg_band)}
                page.add_segment()
                app.processEvents()
                check("adding a queued segment does not copy it",
                      len(page.segments) == 1, str(len(page.segments)))
            else:
                check("adding a queued segment does not copy it", False)
            bar.clear_selection()
            page.clear_segments()
            page.clear_range()
            app.processEvents()
            for i in range(3):
                page.set_in_point(10 + i * 30)
                page.set_out_point(20 + i * 30)
                page.add_segment()
            app.processEvents()
        else:
            for label in ("adding a selected range queues that range",
                          "the other range stays on the bar",
                          "queued span leaves no duplicate range",
                          "adding a queued segment does not copy it"):
                check(label, False)

        # 右键删片段（信号已接线；Delete 已经删不掉了）
        if page.segments:
            count = len(page.segments)
            bar.clear_selection()
            bar.segmentRemoveRequested.emit(0)
            app.processEvents()
            check("right-click removes a queued segment",
                  len(page.segments) == count - 1,
                  f"{count} -> {len(page.segments)}")
            # 把刚删掉的那一段补回来（同一时间段不会加出第二条）
            page.set_in_point(10)
            page.set_out_point(20)
            page.add_segment()
            app.processEvents()
        # 片段改名：右侧行双击/右键都走这条路；底栏也跟着显示
        page.clear_segments()
        page.clear_range()
        page.set_in_point(24)
        page.set_out_point(36)
        page.add_segment()
        app.processEvents()
        row = page.side._rows[0] if page.side._rows else None
        check("segment row falls back to the number",
              row is not None and row.main.text() == "#1",
              row.main.text() if row is not None else "no row")
        if row is not None:
            check("segment row puts span then duration on the second line",
                  "→" in row.sub.text() and "秒" in row.sub.text()
                  and row.sub.text().index("→") < row.sub.text().index("秒"),
                  row.sub.text())
            row.begin_rename()
            row.edit.setText("开场镜头")
            row.edit.returnPressed.emit()
            app.processEvents()
            check("inline rename updates the segment",
                  bool(page.segments)
                  and page.segments[0].get("name") == "开场镜头",
                  str(page.segments[0].get("name") if page.segments else None))
            row2 = page.side._rows[0] if page.side._rows else None
            check("renamed row shows the name on top",
                  row2 is not None and row2.main.text() == "开场镜头",
                  row2.main.text() if row2 is not None else "no row")
            check("bottom bar shows the segment name",
                  any(b.get("label") == "开场镜头" for b in bar._bands
                      if b["kind"] == "segment"),
                  str([(b["kind"], b.get("label")) for b in bar._bands]))
            page.rename_segment(0, "")
            app.processEvents()
            check("clearing the name goes back to the number",
                  page.side._rows and page.side._rows[0].main.text() == "#1",
                  page.side._rows[0].main.text() if page.side._rows else "no row")
            page.rename_segment(0, "结尾")
            app.processEvents()
            check("rename_segment works from the bar's right-click path",
                  page.segments[0].get("name") == "结尾",
                  str(page.segments[0].get("name")))
        page.clear_segments()
        app.processEvents()
        for i in range(3):
            page.set_in_point(10 + i * 30)
            page.set_out_point(20 + i * 30)
            page.add_segment()
        app.processEvents()

        bar.clear_selection()
        if page.side._rows:
            QTest.keyClick(page.side._rows[0], Qt.Key_Delete)
            app.processEvents()
            check("Delete key on a segment row removes it",
                  len(page.segments) == 2, str(len(page.segments)))
        else:
            print("SKIP  Delete key on a segment row  (没有行)")

        bar.clear_selection()
        page.view.setFocus()
        app.processEvents()
        QTest.keyClick(page.view, Qt.Key_Backspace)
        app.processEvents()
        check("Backspace without a selection keeps the segments",
              len(page.segments) == 2, str(len(page.segments)))

        page.clear_range()
        page.set_in_point(5)
        page.set_out_point(15)
        app.processEvents()
        check("one range ready to delete", len(page.range_spans()) == 1,
              str(page.range_spans()))
        bar.clear_selection()
        page._delete_selected()
        app.processEvents()
        check("Delete without a selection keeps the range",
              len(page.range_spans()) == 1, str(page.range_spans()))
        range_bands = [b for b in bar._bands if b["kind"] == "range"]
        if range_bands:
            bar.selection = {bar._band_key(range_bands[0])}
            page._delete_selected()
            app.processEvents()
            check("Delete removes the selected range",
                  page.range_spans() == [], str(page.range_spans()))
        else:
            print("SKIP  Delete removes the selected range")

        # Ctrl+A：选中底部所有条（不是「全选帧」）
        bar.clear_selection()
        win.activateWindow()
        page.view.setFocus(Qt.OtherFocusReason)
        app.processEvents()
        QTest.keyClick(page.view, Qt.Key_A, Qt.ControlModifier)
        app.processEvents()
        check("Ctrl+A selects every band, not every frame",
              len(bar.selection) == len(bar._bands) and bar.selection,
              f"sel={len(bar.selection)} bands={len(bar._bands)}")
        bar.clear_selection()
        app.processEvents()

        band0, panel0 = bar.band_height(), bar.height()
        event = QWheelEvent(QPointF(bar.width() * 0.5, bar.track_y()),
                            QPointF(bar.width() * 0.5, bar.track_y()),
                            QPoint(0, 0), QPoint(0, 120), Qt.NoButton,
                            Qt.ShiftModifier, Qt.NoScrollPhase, False)
        bar.wheelEvent(event)
        app.processEvents()
        check("shift+wheel only changes bar height, not the panel",
              bar.band_height() > band0 and bar.height() == panel0,
              f"band {band0}->{bar.band_height()} h {panel0}->{bar.height()}")
        bar.scale_bands(-1)
        page.clear_segments()
        page.clear_range()
        app.processEvents()

        # ---- B1.7: 侧栏两块卡片 + 外层滚动 ----
        side = page.side
        check("sidebar has two cards",
              side.card_edit.is_expanded() and side.card_segments.is_expanded(),
              f"{side.card_edit.is_expanded()},{side.card_segments.is_expanded()}")
        check("sidebar scroll is as-needed",
              side.scroll.verticalScrollBarPolicy() == Qt.ScrollBarAsNeeded,
              str(side.scroll.verticalScrollBarPolicy()))
        tall = side.card_edit.height() + side.card_segments.height()
        edit_h = side.card_edit.height()
        check("sidebar scrolls when cards are tall",
              tall > side.scroll.viewport().height(),
              f"cards={tall} viewport={side.scroll.viewport().height()}")
        side.set_card_expanded("edit", False)
        app.processEvents()
        collapsed = wait(app, lambda: side.card_edit.height() < edit_h, 2)
        check("edit card collapses",
              not side.card_edit.body.isVisible() and collapsed,
              f"h={side.card_edit.height()} before={edit_h}")
        check("collapse state saved",
              str(win.setting.value("ui/sidebar_edit_expanded")).lower() == "false",
              str(win.setting.value("ui/sidebar_edit_expanded")))
        side.set_card_expanded("edit", True)
        seg_h = side.card_segments.height()
        side.set_card_expanded("segments", False)
        app.processEvents()
        seg_collapsed = wait(app, lambda: side.card_segments.height() < seg_h, 2)
        check("segments card collapses",
              not side.card_segments.body.isVisible() and seg_collapsed,
              f"h={side.card_segments.height()} before={seg_h}")
        side.set_card_expanded("segments", True)
        app.processEvents()

        # ---- B1.8: 片段列表随数量长高，5 条起步，上限取 2/3 高度 ----
        from vff.ui.sidebar import SEG_ROW_H, EditSidebar
        probe = EditSidebar()
        probe.resize(372, 760)
        probe.show()
        app.processEvents()
        probe.set_extract_progress(50, True, True)
        app.processEvents()
        check("loading bar fits sidebar",
              probe.bar.width() <= probe.scroll.viewport().width(),
              f"bar={probe.bar.width()} viewport={probe.scroll.viewport().width()}")

        def seg_h(count):
            probe.set_segments([(i, i + 1, None, "") for i in range(count)])
            app.processEvents()
            return probe.seg_scroll.height()

        h0, h5, h8, h20 = seg_h(0), seg_h(5), seg_h(8), seg_h(20)
        max_rows = max(5, int((probe.height() * 2.0 / 3.0) // (SEG_ROW_H + 3)))
        expected_cap = (max_rows * SEG_ROW_H
                        + max(0, max_rows - 1) * 3 + 4)
        check("segments list shows at least 5 rows",
              h0 >= 5 * SEG_ROW_H and h5 >= 5 * SEG_ROW_H,
              f"0={h0} 5={h5}")
        check("segments list grows with count", h8 > h5, f"5={h5} 8={h8}")
        check("segments list caps at 2/3 height",
              h20 == expected_cap and max_rows >= 5,
              f"20={h20} cap={expected_cap} rows={max_rows}")
        probe.close()

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
                    expect = view._frame_at_x(two[1], rects2,
                                              rects2[1].center().x())
                    check("horizontal drag still works",
                          horiz is not None and abs(horiz - expect) <= 1,
                          f"in={horiz} expect={expect}")
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

        # ---- B3.5: 随意框选 = 按下点 → 松手点（不是格子的左右边界） ----
        page.clear_range()
        page.set_display_side(200)
        win.resize(1200, 760)
        app.processEvents()
        rows = view._visible_rows()
        three = next((r for r in rows if len(r[1]) >= 3), None)
        if three is not None:
            rects = [view._row_rect(i) for i in three[1]]
            rects = [r for r in rects if r is not None]
            p0 = QPoint(rects[0].left() + int(rects[0].width() * 0.72),
                        rects[0].center().y())
            p1 = QPoint(rects[2].left() + int(rects[2].width() * 0.28),
                        rects[2].center().y())
            want_in = view._frame_at_pos(p0)
            want_out = view._frame_at_pos(p1)
            cell_in = three[1][0] * page.step
            cell_out = three[1][2] * page.step
            QTest.mousePress(view.viewport(), Qt.LeftButton, pos=p0)
            QTest.mouseMove(view.viewport(), p1)
            app.processEvents()
            QTest.mouseRelease(view.viewport(), Qt.LeftButton, pos=p1)
            app.processEvents()
            check("box select starts at press point",
                  page.in_frame == want_in,
                  f"in={page.in_frame} want={want_in} cell={cell_in}")
            check("box select ends at release point",
                  page.out_frame == want_out,
                  f"out={page.out_frame} want={want_out} cell={cell_out}")
            check("box select is not cell left edges",
                  page.in_frame != cell_in and page.out_frame != cell_out,
                  f"{page.in_frame}..{page.out_frame} vs {cell_in}..{cell_out}")
            check("overview follows box select",
                  bar.in_frame == page.in_frame and bar.out_frame == page.out_frame,
                  f"{bar.in_frame}..{bar.out_frame}")
        else:
            for name in ("box select starts at press point",
                         "box select ends at release point",
                         "box select is not cell left edges",
                         "overview follows box select"):
                check(name, False, "没有找到 3 格以上的行")
        page.set_display_side(160)
        app.processEvents()

        # ---- B4: 按镜头（坐标不规则）时，时间轴仍要精确到帧 ----
        from PySide6.QtCore import QSize
        from vff.timeline import SceneTimeline
        from vff.ui.wall_view import WallView
        from vff.ui.widgets import FrameDelegate, FrameModel, FrameRef
        view2 = WallView()
        view2.setViewMode(WallView.IconMode)
        view2.setFlow(WallView.LeftToRight)
        view2.setWrapping(True)
        view2.setResizeMode(WallView.Adjust)
        view2.setUniformItemSizes(True)
        model2 = FrameModel()
        view2.setModel(model2)
        view2.setItemDelegate(FrameDelegate(120, 68, view2))
        view2.setGridSize(QSize(130, 92))
        view2.resize(1000, 260)
        view2.show()
        frames2 = [0, 75, 90, 240, 241, 600]
        model2.append([FrameRef("x", f / 25.0, f) for f in frames2])
        timeline2 = SceneTimeline(frames2, 750)
        view2.set_timeline(timeline2)
        app.processEvents()
        rows2 = view2._visible_rows()
        if rows2:
            _top2, bucket2 = rows2[0]
            rects2 = [r for r in (view2._row_rect(i) for i in bucket2) if r is not None]
            bucket2 = bucket2[:len(rects2)]
            mid = 80                      # 落在镜头 75 与 90 之间
            x_mid = view2._x_for_frame(bucket2, rects2, mid)
            back = view2._frame_at_x(bucket2, rects2, x_mid) if x_mid is not None else None
            _lo2, _hi2, xa2, xb2 = view2._row_span(bucket2, rects2)
            check("scene timeline interpolates",
                  x_mid is not None and xa2 - 1 <= x_mid <= xb2 + 1
                  and back is not None and abs(back - mid) <= 1,
                  f"x={x_mid} row={xa2}..{xb2} back={back}")
            far = view2._frame_in_row(bucket2, rects2, 10 ** 6)
            check("scene drag clamps to shot span",
                  far == int(timeline2.span(bucket2[-1])[1]), str(far))
            check("scene timeline kind", timeline2.kind == "scene"
                  and timeline2.count() == len(frames2))
        else:
            check("scene timeline interpolates", False)
            check("scene drag clamps to shot span", False)
            check("scene timeline kind", False)
        view2.close()
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
            check("export recorded in list",
                  any(str(e.get("path")) == str(out) for e in page.exports),
                  str(page.exports[:2]))
            check("sidebar lists the exported file",
                  page.side.exp_list.count() >= 1
                  and str(page.side.exp_list.item(0).data(Qt.UserRole)) == str(out),
                  f"{page.side.exp_list.count()} 条")

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
        page._batch_start(batch_dir, exact=True, names=["开场", "", ""])
        batch_done = wait(app, lambda: page._batch is None
                          and page.lbl_export.text().startswith("✅"), 240)
        batch_files = sorted(batch_dir.glob("*.mp4"))
        check("batch exported 3", batch_done and len(batch_files) == 3,
              f"{len(batch_files)} 个 · {page.lbl_export.text()[:40]}")
        check("batch custom + series names",
              {p.name for p in batch_files}
              == {"开场.mp4", "Cut - 2_c0.mp4", "Cut - 3_c0.mp4"},
              str([p.name for p in batch_files]))
        if len(batch_files) == 3:
            fps = page.info.fps or 30.0
            expected = {"开场.mp4": (36 - 24) / fps,
                        "Cut - 2_c0.mp4": (60 - 48) / fps,
                        "Cut - 3_c0.mp4": (90 - 72) / fps}
            durs = {f.name: ffmpeg.probe(f).duration for f in batch_files}
            check("batch durations",
                  all(abs(durs.get(name, 99.0) - want) < 0.15
                      for name, want in expected.items()),
                  str({k: round(v, 2) for k, v in durs.items()}))
            check("batch exact mode", "帧精确" in page.lbl_export.text(),
                  page.lbl_export.text()[:40])

        # ---- 2b. 保存缓存 + 关闭前提示（v1.1.9） ----
        check("page cache ready", page.cache_ready(),
              f"finished={page._finished} rows={page.model.rowCount()}")
        uncached = win._uncached_pages()
        check("uncached pages found", any(p is page for p in uncached),
              str(len(uncached)))
        cache_dlg = CloseCacheDialog(win, uncached,
                                     {"mode": "never", "skip_close_prompt": True},
                                     win.cache_store)
        cache_dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
        cache_dlg.show()
        app.processEvents()
        dlg_text = " ".join(w.text() for w in cache_dlg.findChildren(QLabel))
        check("close dialog lists uncached",
              "还没保存" in dlg_text and "c0.mp4" in dlg_text, dlg_text[:80])
        check("close dialog defaults to never",
              cache_dlg.radio_never.isChecked()
              and not cache_dlg.check_skip.isEnabled()
              and cache_dlg.values()["skip_close_prompt"] is False)
        cache_dlg.radio_count.setChecked(True)
        cache_values = cache_dlg.values()
        check("close dialog values",
              cache_values["mode"] == "count" and cache_dlg.action == "cancel",
              str(cache_values))
        cache_dlg.close()
        saved, failed = win._apply_cache_policy(
            {"mode": "count", "max_videos": 20,
             "usage_minutes": 10, "usage_exports": 2,
             "skip_close_prompt": True}, uncached)
        check("cache saved to store",
              saved >= 1 and failed == 0 and win.cache_store.has(page.cache_digest())
              and win.cache_store.summary()["count"] >= 1,
              f"saved={saved} failed={failed} "
              f"count={win.cache_store.summary()['count']}")
        check("saved cache can be restored",
              win.cache_store.materialize(page.cache_digest(), tmp / "restored"),
              str(tmp / "restored"))
        restored = sorted((tmp / "restored").glob("f_*.jpg"))
        check("restored frame count", len(restored) == page.model.rowCount(),
              f"{len(restored)} vs {page.model.rowCount()}")

        # ---- 2b2. 片段状态（片段 / 入点出点 / 导出记录）跟着缓存放，重开能恢复 ----
        # 区间也要随缓存保存：先画一段（「加入片段」会把区间搬进队列，所以这里单独画）
        page.set_in_point(72)
        page.set_out_point(90)
        app.processEvents()
        digest = page.cache_digest()
        page.save_state()
        state = win.cache_store.read_state(digest)
        check("cache stores segment state",
              len(state.get("segments") or []) == 3
              and (state.get("range") or {}).get("in") is not None
              and len(state.get("exports") or []) >= 4,
              f"segments={len(state.get('segments') or [])} "
              f"range={state.get('range')} "
              f"exports={len(state.get('exports') or [])}")
        counters = {"segments": len(page.segments),
                    "in": page.in_frame, "exports": len(page.exports)}

        import vff.ui.track_page as tp

        reopened = []
        original_init = tp.TrackPage.__init__

        def _capture(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            reopened.append(self)

        tp.TrackPage.__init__ = _capture
        try:
            win.close_tab(win.tabs.indexOf(page))
            entry = win.cache_store.entry(digest)
            ok_open = bool(entry) and win.open_cached_entry(entry)
        finally:
            tp.TrackPage.__init__ = original_init
        new_page = reopened[0] if reopened else None
        loaded = wait(app, lambda: new_page is not None
                      and new_page.cache_ready(), 90)
        check("reopened from cache", ok_open and loaded and new_page is not None,
              f"ok={ok_open} loaded={loaded}")
        if new_page is not None:
            check("segments restored from cache",
                  len(new_page.segments) == counters["segments"],
                  f"{len(new_page.segments)} vs {counters['segments']}")
            check("in point restored from cache",
                  new_page.in_frame == counters["in"],
                  f"{new_page.in_frame} vs {counters['in']}")
            check("export list restored from cache",
                  len(new_page.exports) >= counters["exports"],
                  f"{len(new_page.exports)} vs {counters['exports']}")
            check("sidebar export rows restored",
                  new_page.side.exp_list.count() >= counters["exports"],
                  str(new_page.side.exp_list.count()))

        # ---- 2c. 从缓存导入 / 设置里的条目列表 ----
        from vff.ui.cache_list import CacheEntryTable, CacheImportDialog
        import_dlg = CacheImportDialog(win, win.cache_store, win.open_cached_entry)
        import_dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
        import_dlg.show()
        app.processEvents()
        check("import dialog lists entries",
              import_dlg.table.table.rowCount() >= 1,
              str(import_dlg.table.table.rowCount()))
        import_dlg.close()

        entry_dlg = SettingsDialog(win, settings=win.app_settings,
                                   store=win.cache_store,
                                   load_cache=win.open_cached_entry)
        entry_dlg.setAttribute(Qt.WA_DontShowOnScreen, True)
        entry_dlg.show()
        app.processEvents()
        entry_dlg.btn_entries.setChecked(True)
        app.processEvents()
        check("settings entries expandable",
              entry_dlg.entry_table is not None
              and entry_dlg.entry_table.isVisible()
              and entry_dlg.entry_table.table.rowCount() >= 1,
              str(entry_dlg.entry_table.table.rowCount()
                  if entry_dlg.entry_table else None))
        entry_dlg.close()

        opened = []
        load_dlg = SettingsDialog(win, settings=win.app_settings,
                                  store=win.cache_store,
                                  load_cache=lambda e: opened.append(e))
        load_dlg.entry_table.table.selectRow(0)
        load_dlg.entry_table.open_selected()
        for _ in range(30):
            app.processEvents()
            if opened:
                break
        check("settings entry load callback", len(opened) == 1, str(len(opened)))
        load_dlg.deleteLater()
        app.processEvents()

        # 删除条目（用独立的库 + 独立的小缓存）
        from vff.cache_store import CacheStore as Store2
        ui_store = Store2(tmp / "ui_store", win.app_settings)
        ui_src = tmp / "ui_src"
        ui_src.mkdir()
        for i in range(1, 3):
            (ui_src / f"f_{i:06d}.jpg").write_bytes(b"x" * 50)
        (ui_src / ".done").write_text("2", encoding="utf-8")
        ui_ok, ui_why = ui_store.save_dir(
            "aaaaaaaaaaaaaaaa", ui_src,
            {"video": str(tmp / "ui.mp4"), "video_size": 1, "video_mtime": 1,
             "mode": "step", "step": 24, "threshold": None,
             "tw": 320, "th": 180, "frames": 2})
        ui_table = CacheEntryTable(ui_store)
        check("entry table builds",
              ui_ok and ui_table.table.rowCount() == 1, ui_why)
        orig_question = QMessageBox.question
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        try:
            ui_table.table.selectRow(0)
            ui_table.remove_selected()
        finally:
            QMessageBox.question = orig_question
        check("entry table deletes cache",
              ui_table.table.rowCount() == 0
              and ui_store.summary()["count"] == 0)

        # 策略 3：用得不多的不保存；用够时间才保存
        p1 = win.tabs.widget(1)
        win.cache_store.drop(p1.cache_digest())
        usage_cfg = {"mode": "usage", "max_videos": 20, "usage_minutes": 10,
                     "usage_exports": 2, "skip_close_prompt": True}
        p1.used_seconds, p1.export_count = 0.0, 0
        kept, failed1 = win._apply_cache_policy(usage_cfg, [p1])
        check("usage policy skips unused video",
              kept == 0 and failed1 == 0
              and not win.cache_store.has(p1.cache_digest()),
              f"kept={kept} failed={failed1}")
        p1.used_seconds = 660.0        # 11 分钟
        kept2, failed2 = win._apply_cache_policy(usage_cfg, [p1])
        check("usage policy saves used video",
              kept2 == 1 and failed2 == 0
              and win.cache_store.has(p1.cache_digest()),
              f"kept={kept2} failed={failed2}")

        # 关闭前提示规则：不自动保存每次都问；自动保存 + 不再提醒才静默
        from PySide6.QtWidgets import QDialog
        from vff.ui import settings_dialog as sd_mod
        p2 = win.tabs.widget(2)
        win.cache_store.drop(p2.cache_digest())
        prompts = []
        orig_close_exec = sd_mod.CloseCacheDialog.exec
        sd_mod.CloseCacheDialog.exec = lambda self: (prompts.append(self)
                                                     or QDialog.Rejected)
        try:
            win.app_settings.update_cache(mode="never", skip_close_prompt=True)
            proceed = win._handle_close_cache()
            check("never mode always prompts",
                  proceed is False and len(prompts) == 1,
                  f"proceed={proceed} prompts={len(prompts)}")
            win.app_settings.update_cache(mode="usage", skip_close_prompt=True)
            proceed2 = win._handle_close_cache()
            check("auto-save + skip closes silently",
                  proceed2 is True and len(prompts) == 1,
                  f"proceed={proceed2} prompts={len(prompts)}")
        finally:
            sd_mod.CloseCacheDialog.exec = orig_close_exec

        # 关窗口时不再弹提示（上面已经勾了「不再提醒」，且是自动保存策略）
        win.app_settings.update_cache(mode="usage", skip_close_prompt=True)

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
        win.add_video(ts_clip, step=24)     # 固定 24 帧：让起点落在关键帧之间（测「帧精确」）
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
            from vff.export_names import plan_filenames
            b_ok, b_exact, b_texts = dialogs_mod.ask_batch_export(
                None, "导出 2 段", ["00:01 → 00:02", "00:03 → 00:04"],
                ["", ""], plan_filenames(["", ""], "c0", ".mp4"), True,
                "只重编码切点", "D:/out")
            check("batch naming dialog builds",
                  b_ok and b_exact and b_texts == ["", ""],
                  f"{b_ok},{b_exact},{b_texts}")
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

        # ---- 3b. 更新提醒弹窗：四个选择 + 策略落盘 ----
        from vff.ui import update_dialog as update_dialog_mod
        from vff.ui.update_dialog import UpdatePromptDialog
        import vff.ui.main_window as mw_mod
        from vff.settings import update_should_prompt

        fake = CheckResult(True, "v9.9.9", "https://example.invalid/releases",
                           "", "冒烟", "https://example.invalid/a.exe",
                           46000000, "")
        prompt = UpdatePromptDialog(win, tag="v9.9.9", current=APP_VERSION,
                                    size=46000000,
                                    url="https://example.invalid")
        prompt.setAttribute(Qt.WA_DontShowOnScreen, True)
        prompt.show()
        app.processEvents()
        texts = [prompt.btn_now.text(), prompt.btn_week.text(),
                 prompt.btn_skip.text(), prompt.btn_mute.text()]
        prompt_text = " ".join(w.text() for w in prompt.findChildren(QLabel))
        check("update prompt has four choices",
              all(texts) and prompt.choice == "later", str(texts))
        check("update prompt shows version + size",
              "v9.9.9" in prompt_text and "MB" in prompt_text,
              prompt_text[:60])
        prompt._pick("week")
        check("update prompt records choice",
              prompt.choice == "week", prompt.choice)
        prompt2 = UpdatePromptDialog(win, tag="v9.9.9")
        prompt2.setAttribute(Qt.WA_DontShowOnScreen, True)
        prompt2.show()
        app.processEvents()
        prompt2.reject()
        check("closing update prompt means later", prompt2.choice == "later")
        prompt2.deleteLater()

        orig_exec = update_dialog_mod.UpdatePromptDialog.exec
        orig_enabled = mw_mod.update_checks_enabled
        mw_mod.update_checks_enabled = lambda: True
        pending = {"choice": "skip"}

        def _fake_exec(self):
            self.choice = pending["choice"]
            return 1

        update_dialog_mod.UpdatePromptDialog.exec = _fake_exec
        try:
            win.app_settings.set_update_prefs(
                muted=False, skip_version="", snooze_until=0.0,
                snooze_version="", last_prompt_at={}, auto_check=True)
            win._prompt_update(fake)
            prefs = win.app_settings.update_prefs()
            check("skip writes the ignored version",
                  prefs["skip_version"] == "v9.9.9", str(prefs))
            check("ignored version stops prompting",
                  update_should_prompt(prefs, "v9.9.9")[0] is False)
            check("newer version still prompts",
                  update_should_prompt(prefs, "v9.10.0")[0] is True)

            pending["choice"] = "week"
            win._prompt_update(fake)
            prefs = win.app_settings.update_prefs()
            check("week snoozes seven days",
                  prefs["snooze_until"] > time.time() + 6 * 86400
                  and update_should_prompt(prefs, "v9.9.9")[0] is False,
                  f"{prefs['snooze_until'] - time.time():.0f}s")

            pending["choice"] = "mute"
            win._prompt_update(fake)
            prefs = win.app_settings.update_prefs()
            check("mute silences the badge",
                  prefs["muted"] and win.badge.state_name() == "idle",
                  f"muted={prefs['muted']} badge={win.badge.state_name()}")
            win._maybe_prompt_update(fake)
            check("muted never prompts", win._update_prompt is None)
            check("manual check still bypasses policy",
                  update_should_prompt(prefs, "v9.9.9", manual=True)[0] is True)

            # 队列忙：先不弹，隔 10 秒再看（最多 6 轮）
            from unittest.mock import PropertyMock, patch as mock_patch
            win.app_settings.set_update_prefs(
                muted=False, skip_version="", snooze_until=0.0,
                snooze_version="", last_prompt_at={}, auto_check=True)
            win._update_retry.stop()
            win._update_retries = 0
            with mock_patch.object(type(win.jobs), "busy",
                                   new_callable=PropertyMock) as busy_mock:
                busy_mock.return_value = True
                win._maybe_prompt_update(fake)
            check("busy queue delays the prompt",
                  win._update_prompt is None and win._update_retry.isActive(),
                  f"retries={win._update_retries}")
        finally:
            update_dialog_mod.UpdatePromptDialog.exec = orig_exec
            mw_mod.update_checks_enabled = orig_enabled
            win._update_retry.stop()
            win._update_retries = 0
            win.app_settings.set_update_prefs(
                muted=False, skip_version="", snooze_until=0.0,
                snooze_version="", last_prompt_at={}, auto_check=True)
            win.app_settings.save()
            win.badge.set_state("idle")

        # 热补丁包优先：有 code 资产时走「不用重启」那条路（不真的下载）
        hotfix_calls = []
        orig_start_code = win._start_code_download
        orig_question = QMessageBox.question
        win._start_code_download = (
            lambda r: hotfix_calls.append(r.code_url) or True)
        QMessageBox.question = lambda *a, **k: QMessageBox.Yes
        try:
            win._update_result = CheckResult(
                True, "v9.9.10", "https://example.invalid/r", "", "冒烟",
                "", 0, "", "https://example.invalid/c.zip", 2048, "")
            win._ask_download()
        finally:
            win._start_code_download = orig_start_code
            QMessageBox.question = orig_question
            win._update_result = None
        check("hotfix pack takes the no-restart path",
              hotfix_calls == ["https://example.invalid/c.zip"], str(hotfix_calls))
        check("settings shows no hotfix by default",
              "没有热补丁" in settings_dlg.lbl_hotfix.text(),
              settings_dlg.lbl_hotfix.text())

        # 下载时把「原址 + 镜像」一起交给下载器（国内直连 GitHub 常常几十 KB/s）
        started = {}
        orig_start = win.downloader.start

        def _fake_start(url, size, digest, dest, mirrors=()):
            started.update(url=url, mirrors=list(mirrors))
            return True

        win.downloader.start = _fake_start
        try:
            win._start_download(CheckResult(
                True, "v9.9.11", "", "", "",
                "https://github.com/o/r/releases/download/v9/Purer.exe", 1000, ""))
        finally:
            win.downloader.start = orig_start
            win.badge.set_state("idle")
        check("download passes mirrors to the downloader",
              started.get("url", "").endswith("Purer.exe")
              and len(started.get("mirrors") or []) >= 2,
              str(started))

        # 热补丁不适用（包要求更高的 exe）→ 自动改走整包更新
        fallback = {}
        orig_apply = win._apply_hotfix
        orig_dl = win._start_download
        win._apply_hotfix = lambda p: (False, "代码包要求 exe ≥ 9.9.0", win)
        win._start_download = lambda r: fallback.update(tag=r.tag) or True
        try:
            win._finish_code_download("x.zip", CheckResult(
                True, "v9.9.12", "", "", "",
                "https://github.com/x/Purer.exe", 1000, "",
                "https://github.com/x/code.zip", 200, ""))
        finally:
            win._apply_hotfix = orig_apply
            win._start_download = orig_dl
            win.badge.set_state("idle")
        check("incompatible hotfix falls back to the full exe",
              fallback.get("tag") == "v9.9.12", str(fallback))

        # ---- 4. 会话快照：保存 → 换个窗口恢复（更新重启 / 崩溃恢复都靠它） ----
        from vff import session as session_state

        live = win.session_payload()
        check("session payload covers every tab",
              len(live["tabs"]) == win.tabs.count() and win.tabs.count() >= 3,
              f"{len(live['tabs'])} vs {win.tabs.count()}")
        check("session tab carries settings",
              all(t.get("path") and t.get("step") for t in live["tabs"]),
              str(live["tabs"][:1]))
        check("session saves to disk",
              win.save_session("smoke") and session_state.load()["tabs"],
              str(session_state.session_file()))

        # 造一份"上次的会话"：两个视频 + 当前帧 + 入出点 + 片段 + 导出记录
        # （先停掉 30 秒自动存盘，否则它可能在我们 restore 之前把文件覆盖回去）
        win._session_timer.stop()
        crafted = {
            "version": 1, "saved_at": time.time(), "reason": "update",
            "window": {"geometry": "", "maximized": False},
            "tabs": [
                {"path": str(clips[0]), "step": 24, "mode": "step",
                 "threshold": None, "display_side": 200, "sidebar_width": 380,
                 "cursor_frame": 60, "in_frame": 24, "out_frame": 90,
                 "scroll_row": 2,
                 "markers": [50, 70],
                 "segments": [{"t_in": 1.0, "t_end": 2.0, "name": "开场"}],
                 "exports": [{"path": str(tmp / "old.mp4"), "at": 1.0,
                              "size": 4096, "kind": "帧精确"}]},
                {"path": str(clips[1]), "step": 24, "mode": "step",
                 "threshold": None, "display_side": 160, "sidebar_width": 372,
                 "cursor_frame": 30, "in_frame": None, "out_frame": None,
                 "scroll_row": 0, "segments": [], "exports": []},
            ],
            "current": 1,
        }
        check("session file written", session_state.save(crafted),
              str(session_state.session_file()))

        win2 = MainWindow()
        win2.setAttribute(Qt.WA_DontShowOnScreen, True)
        win2.resize(1200, 760)
        win2.show()
        app.processEvents()
        restored = win2.restore_session()
        check("session restores tab count", restored == 2 and win2.tabs.count() == 2,
              f"restored={restored} tabs={win2.tabs.count()}")
        page_a = win2.tabs.widget(0)
        page_b = win2.tabs.widget(1)
        ready = wait(app, lambda: page_a is not None and page_b is not None
                     and not page_a.busy and not page_b.busy
                     and page_a.model.rowCount() > 0 and page_b.model.rowCount() > 0,
                     180)
        check("session pages loaded", ready, "not ready")
        if ready:
            check("session keeps tab order",
                  Path(page_a.path) == clips[0] and Path(page_b.path) == clips[1],
                  f"{page_a.path} | {page_b.path}")
            check("session restores current tab",
                  win2.tabs.currentIndex() == 1, str(win2.tabs.currentIndex()))
            check("session restores range",
                  page_a.in_frame == 24 and page_a.out_frame == 90,
                  f"{page_a.in_frame}..{page_a.out_frame}")
            check("session restores cursor",
                  page_a.cursor_frame == 60, str(page_a.cursor_frame))
            check("session restores segments",
                  len(page_a.segments) == 1
                  and page_a.segments[0]["name"] == "开场",
                  str(page_a.segments))
            check("session restores markers",
                  page_a.markers == [50, 70]
                  and page_a.view.range_markers == [50, 70]
                  and page_a.overview.markers == [50, 70],
                  str(page_a.markers))
            check("session restores export list",
                  any("old.mp4" in str(e.get("path")) for e in page_a.exports),
                  str(page_a.exports[:2]))
            check("session restores display side",
                  page_a.display_side == 200, str(page_a.display_side))
            check("session restore is one-shot",
                  page_a._restore_pending is None)
            check("second tab keeps its own state",
                  page_b.in_frame is None and page_b.out_frame is None)

        # 坏会话：不能崩，也不能恢复出东西
        session_state.session_file().write_text("{ broken", encoding="utf-8")
        check("broken session is ignored",
              session_state.load()["tabs"] == []
              and win2.restore_session() == 0,
              str(session_state.load()["tabs"]))

        win2.app_settings.update_cache(mode="usage", skip_close_prompt=True)
        win2.app_settings.save()
        win2.close()
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
