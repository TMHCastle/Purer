# -*- coding: utf-8 -*-
"""命令行入口。

    Purer.exe                                  启动界面
    Purer.exe 视频.mp4 目录\\                   直接打开
    Purer.exe --selftest                       无窗口自检
    Purer.exe --where                          打印 ffmpeg/缓存位置
    Purer.exe --probe A.mp4 B.mkv              打印媒体信息（JSON）
    Purer.exe --extract A.mp4 -o 输出目录 --step 24 --long 320
    Purer.exe --export A.mp4 12.5 24 -o 切片.mp4
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import APP_NAME, APP_VERSION, smartcut
from .cache import cache_root
from .config import EXTRACT_LONG_SIDE, DEFAULT_STEP, app_dir, bundle_dir
from .ffmpeg import (Tools, build_extract_variants, build_normalize_args,
                     build_probe_args, command_rejected, probe, read_keyframes,
                     run_hidden, snap_before, stream_progress)
from .media import find_videos, parse_probe_json, thumb_size_for
from .log import install_crash_log, start_debug_watchdog
from .util import fmt_tc, parse_time_arg, safe_filename

USAGE = """Purer {ver} · 视频抽帧浏览 / 帧精确切片

  双击 / 无参数            启动界面
  <视频|文件夹> ...         直接打开
  --selftest               无窗口自检（结果在 %TEMP%\\Purer_selftest.log）
  --where                  打印 ffmpeg / ffprobe / 缓存位置
  --probe <文件...>         打印媒体信息（JSON）
  --extract <文件|目录...>  批量抽帧
        -o, --out <目录>      输出根目录（默认当前目录）
        --step <N>            每隔多少帧取一帧（默认 {step}）
        --long <像素>         缩略图长边（默认 {long}）
  --export <文件> <起> <止> 导出区间（时间支持 12.5 或 00:01:23.456）
        -o, --out <文件>      输出文件（默认自动命名）
        --lossless            强制无损复制（默认：能精确就精确到帧）
        --no-snap             无损模式下不吸附到关键帧
  -h, --help               显示本帮助
"""


def _attach_console() -> None:
    """打包成 --windowed 后，输出句柄要单独处理。

      * 输出被重定向到文件/管道（cmd 的 `>`）：改成 UTF-8，别写 GBK；
      * 从 cmd / PowerShell 直接调用：AttachConsole 接回父进程控制台；
      * 双击运行：两者都不成立，保持静默（文件照常生成）。
    """
    if os.name != "nt":
        return
    if sys.stdout is not None:
        try:
            if not sys.stdout.isatty():
                enc = (sys.stdout.encoding or "").lower().replace("-", "")
                if enc != "utf8":
                    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                    if sys.stderr is not None:
                        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        invalid = ctypes.c_void_p(-1).value
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle not in (0, invalid, None):
            import msvcrt
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            sys.stdout = os.fdopen(fd, "w", encoding="utf-8",
                                   errors="replace", buffering=1)
            return
        if kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            sys.stdout = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
            sys.stderr = open("CONOUT$", "w", encoding="utf-8",
                              errors="replace", buffering=1)
    except Exception:  # noqa: BLE001
        pass


def _p(line: str = "") -> None:
    """控制台可能是 GBK，也可能根本没有控制台。"""
    if sys.stdout is None:
        return
    try:
        print(line)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"))
    except (OSError, ValueError):
        pass


def _pop_value(args: list[str], *names: str) -> str | None:
    for name in names:
        if name in args:
            index = args.index(name)
            if index + 1 >= len(args):
                raise SystemExit(f"{name} 需要一个值")
            value = args[index + 1]
            del args[index:index + 2]
            return value
    return None


def _pop_flag(args: list[str], *names: str) -> bool:
    hit = False
    for name in names:
        if name in args:
            args.remove(name)
            hit = True
    return hit


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_where() -> int:
    Tools.locate()
    _p(Tools.describe())
    if Tools.ffmpeg is None:
        _p("（标准位置没找到；启动界面时会自动扫盘，或手动放一个 ffmpeg.ini）")
    _p(f"缓存目录: {cache_root()}")
    _p(f"程序目录: {app_dir()}")
    bundle = bundle_dir()
    if bundle is not None:
        _p(f"解包目录: {bundle}")
    return 0 if Tools.ffmpeg else 2


def cmd_probe(files: list[str]) -> int:
    if not Tools.locate(deep=True):
        _p("找不到 ffmpeg/ffprobe，先跑 --where 看看")
        return 2
    out = []
    failed = 0
    for name in files:
        path = Path(name)
        try:
            result = run_hidden([str(Tools.ffprobe)] + build_probe_args(path), timeout=40)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()
                                   or f"ffprobe 退出码 {result.returncode}")
            info = parse_probe_json(
                json.loads(result.stdout.decode("utf-8", "replace") or "{}"), path)
            out.append({
                "path": str(path), "duration": round(info.duration, 3),
                "fps": round(info.fps, 3), "width": info.width, "height": info.height,
                "frames": info.frames, "video": info.codec, "audio": info.audio,
                "start_time": round(info.start_time, 3),
            })
        except Exception as e:  # noqa: BLE001
            failed += 1
            out.append({"path": str(path), "error": str(e)})
    _p(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


def _extract_one(src: Path, outdir: Path, step: float, long_side: int) -> tuple[bool, int, str]:
    try:
        info = probe(src)
    except Exception as e:  # noqa: BLE001
        return False, 0, str(e)
    tw, th = thumb_size_for(info.width, info.height, long_side)
    outdir.mkdir(parents=True, exist_ok=True)
    variants = build_extract_variants(src, step, tw, th, outdir, src_fps=info.fps)
    last = [-1.0]

    def on_progress(pct: float):
        if pct - last[0] >= 0.1:
            last[0] = pct
            _p(f"  {pct * 100:5.1f}%  {src.name}")

    code, stderr = 0, ""
    for args in variants:      # 第一个是首选；被这个 ffmpeg 拒了才换下一种写法
        last[0] = -1.0
        code, stderr = stream_progress(args, on_progress=on_progress, timeout=None,
                                       duration=info.duration)
        if code == 0 or not command_rejected(stderr):
            break
    count = len(list(outdir.glob("f_*.jpg")))
    if code != 0:
        tail = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        return False, count, (tail[-1] if tail else f"ffmpeg 退出码 {code}")
    return True, count, ""


def cmd_extract(files: list[str], out_root: Path, step: float, long_side: int) -> int:
    if not Tools.locate(deep=True):
        _p("找不到 ffmpeg/ffprobe，先跑 --where 看看")
        return 2
    targets: list[Path] = []
    for name in files:
        path = Path(name)
        if path.is_dir():
            targets.extend(find_videos(path))
        else:
            targets.append(path)
    if not targets:
        _p("没有可处理的视频")
        return 2

    ok_all = True
    total_thumbs = 0
    for src in targets:
        _p(f"[抽帧] {src}")
        outdir = out_root / safe_filename(src.stem)
        ok, count, err = _extract_one(src, outdir, step, long_side)
        total_thumbs += count
        if ok:
            _p(f"  -> {count} 张  {outdir}")
        else:
            ok_all = False
            _p(f"  !! 失败：{err}")
    _p(f"完成：{len(targets)} 个视频，共 {total_thumbs} 张")
    return 0 if ok_all else 2


def cmd_export(src: Path, start_rel: float, end_rel: float, out: Path | None,
               exact: bool = True, snap: bool = True) -> int:
    if not Tools.locate(deep=True):
        _p("找不到 ffmpeg/ffprobe，先跑 --where 看看")
        return 2
    try:
        info = probe(src)
    except Exception as e:  # noqa: BLE001
        _p(f"读不了视频：{e}")
        return 2
    if end_rel <= start_rel:
        _p("结束时间必须大于开始时间")
        return 2

    if out is None:
        out = src.with_name(
            f"{safe_filename(src.stem)}__{fmt_tc(start_rel).replace(':', '-')}_to_"
            f"{fmt_tc(end_rel).replace(':', '-')}{src.suffix}")

    cleanup_paths: list[Path] = []
    if smartcut.needs_normalize(info, src):
        import tempfile
        norm_dir = Path(tempfile.mkdtemp(prefix="purer_norm_"))
        norm = norm_dir / "norm.mkv"
        _p(f"源时间戳不规范（{src.suffix}，start={info.start_time:.3f}s），先无损转封装…")
        last = [-1.0]

        def on_norm(value: float):
            if value - last[0] >= 0.1:
                last[0] = value
                _p(f"  {value * 100:5.1f}%")

        code, err = stream_progress(build_normalize_args(src, norm),
                                    on_progress=on_norm, timeout=None,
                                    duration=info.duration)
        if code != 0:
            smartcut.cleanup([norm_dir])
            tail = [ln for ln in err.strip().splitlines() if ln.strip()]
            _p(f"转封装失败：{tail[-1] if tail else code}")
            return 2
        try:
            info = probe(norm)
        except Exception as e:  # noqa: BLE001
            smartcut.cleanup([norm_dir])
            _p(f"转封装结果无法读取：{e}")
            return 2
        cleanup_paths.append(norm_dir)
        src = norm

    start_abs = info.absolute_time(start_rel)
    lossless_start = start_rel
    if snap:
        keyframes = read_keyframes(src, max(0.0, start_abs - 90.0), start_abs + 5.0)
        if not keyframes:
            keyframes = read_keyframes(src, max(0.0, start_abs - 600.0), start_abs + 5.0)
        snapped = snap_before(keyframes, start_abs)
        if snapped is not None:
            lossless_start = info.relative_time(snapped)

    cut = smartcut.plan(src, info, start_rel, end_rel, out, exact=exact,
                        lossless_start=lossless_start)
    if exact and cut.mode != "smart":
        _p(f"精确模式不可用：{cut.reason}")

    stages, temp_paths = smartcut.build_stages(src, info, out, cut)
    head = cut.head_duration if cut.mode == "smart" else 0.0
    _p(f"导出 {src.name}: {fmt_tc(cut.start)} + {cut.duration:.3f}s -> {out}"
       + (f"（智能切割：头段 {head:.2f}s 重编码）" if cut.mode == "smart" else "（无损复制）"))

    total = sum(s["duration"] for s in stages) or 0.0
    done = 0.0
    last = [-1.0]

    def on_total(value: float):
        if value - last[0] >= 0.1 or value >= 1.0:
            last[0] = value
            _p(f"  {value * 100:5.1f}%")

    ok_all = True
    err = ""
    for stage in stages:
        duration = stage["duration"]

        def on_stage(p, base=done, dur=duration):
            if total > 0:
                on_total(min(1.0, (base + p * dur) / total))

        code, stage_err = stream_progress(stage["args"], on_progress=on_stage,
                                          timeout=None, duration=duration)
        if code != 0:
            ok_all = False
            tail = [ln for ln in stage_err.strip().splitlines() if ln.strip()]
            err = tail[-1] if tail else f"ffmpeg 退出码 {code}"
            break
        done += duration

    if ok_all and cut.mode == "smart":
        good, why = smartcut.verify(out, cut.duration, info.frame_duration,
                                    cut.head_duration)
        if not good:
            _p(f"智能切割校验失败（{why}），退回无损…")
            smartcut.cleanup(temp_paths)
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            copy = smartcut.Plan(mode="copy", start=lossless_start, end=end_rel)
            stages, temp_paths = smartcut.build_stages(src, info, out, copy)
            done = 0.0
            total = sum(s["duration"] for s in stages) or 0.0
            for stage in stages:
                duration = stage["duration"]

                def on_stage2(p, base=done, dur=duration):
                    if total > 0:
                        on_total(min(1.0, (base + p * dur) / total))

                code, stage_err = stream_progress(stage["args"], on_progress=on_stage2,
                                                  timeout=None, duration=duration)
                if code != 0:
                    ok_all = False
                    tail = [ln for ln in stage_err.strip().splitlines() if ln.strip()]
                    err = tail[-1] if tail else f"ffmpeg 退出码 {code}"
                    break
                done += duration

    smartcut.cleanup(temp_paths + cleanup_paths)
    if not ok_all:
        _p(f"失败：{err}")
        return 2
    size = out.stat().st_size if out.exists() else 0
    _p(f"完成：{out}  {size} 字节")
    return 0


def cmd_help() -> int:
    long_side = EXTRACT_LONG_SIDE
    _p(USAGE.format(ver=APP_VERSION, step=DEFAULT_STEP, long=long_side))
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    cli_hints = {"--selftest", "--where", "--probe", "--extract", "--export",
                 "--help", "-h", "--version", "-V"}
    if cli_hints.intersection(argv):
        _attach_console()

    if _pop_flag(argv, "-h", "--help"):
        return cmd_help()
    if _pop_flag(argv, "--version", "-V"):
        _p(f"{APP_NAME} {APP_VERSION}")
        return 0
    if _pop_flag(argv, "--where"):
        return cmd_where()

    mode = None
    if _pop_flag(argv, "--selftest"):
        mode = "selftest"
    elif _pop_flag(argv, "--probe"):
        mode = "probe"
    elif _pop_flag(argv, "--extract"):
        mode = "extract"
    elif _pop_flag(argv, "--export"):
        mode = "export"

    out_value = _pop_value(argv, "-o", "--out")
    step = float(_pop_value(argv, "--step") or DEFAULT_STEP)
    long_side = int(_pop_value(argv, "--long")
                    or EXTRACT_LONG_SIDE)
    no_snap = _pop_flag(argv, "--no-snap")
    lossless = _pop_flag(argv, "--lossless")

    if mode == "probe":
        if not argv:
            _p("--probe 需要至少一个文件")
            return 2
        return cmd_probe(argv)
    if mode == "extract":
        if not argv:
            _p("--extract 需要至少一个文件或目录")
            return 2
        return cmd_extract(argv, Path(out_value or ".").resolve(), step, long_side)
    if mode == "export":
        if len(argv) < 3:
            _p("--export 需要 <文件> <起> <止>")
            return 2
        try:
            start_rel = parse_time_arg(argv[1])
            end_rel = parse_time_arg(argv[2])
        except ValueError as e:
            _p(f"时间参数不对：{e}")
            return 2
        out_path = Path(out_value).resolve() if out_value else None
        return cmd_export(Path(argv[0]), start_rel, end_rel, out_path,
                          exact=not lossless, snap=not no_snap)

    # ---------------- GUI ----------------
    from PySide6.QtCore import QLocale
    from PySide6.QtWidgets import QApplication

    from .ui.ffmpeg_missing import ensure_ffmpeg
    from .ui.main_window import MainWindow
    from .ui.style import DARK_QSS, dark_palette

    # 窗口版没有控制台：未捕获异常（含 Qt 槽函数里的）落到 %TEMP%\Purer_cache\error.log
    install_crash_log()
    start_debug_watchdog()

    # 本机默认 locale 是 zh_HK，Qt 会把 24 格式化成「〢〤」（苏州码子），强制普通数字
    QLocale.setDefault(QLocale.c())
    QApplication.setApplicationName(APP_NAME)
    app = QApplication([sys.argv[0] or APP_NAME])
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    app.setStyleSheet(DARK_QSS)

    if mode == "selftest":
        from .selftest import run
        return run(app)

    if not ensure_ffmpeg():
        return 1

    from .cache import clean_cache
    clean_cache()

    win = MainWindow()
    screen = app.primaryScreen()
    if screen is not None:
        g = screen.availableGeometry()
        win.resize(min(1480, int(g.width() * 0.94)), min(900, int(g.height() * 0.92)))
        win.move(g.center() - win.rect().center())
    win.show()

    if argv:
        videos = []
        for name in argv:
            path = Path(name)
            if path.is_dir():
                videos.extend(find_videos(path))
            elif path.is_file():
                videos.append(path)
        if videos:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(80, lambda: win.add_videos(videos))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
