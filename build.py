# -*- coding: utf-8 -*-
"""构建 Purer.exe（单文件、无控制台窗口）

用法：  python build.py
依赖：  PySide6、PyInstaller
产物：  Purer.exe（本目录）
"""
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "icon.ico"
APP_EXE = "Purer"
EXCLUDES = [
    "tkinter", "numpy", "pandas", "matplotlib", "PIL", "scipy",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtNetwork",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSvgWidgets", "PySide6.QtHelp", "PySide6.QtCharts",
]


def png_bytes(pm):
    from PySide6.QtCore import QBuffer, QByteArray
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(pngs, path):
    """把若干 PNG 直接封进 ICO 容器（Vista+ 支持 PNG 压缩图标）。"""
    n = len(pngs)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    offset = 6 + 16 * n
    body = b""
    for size, data in pngs:
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data
    path.write_bytes(header + entries + body)


def make_icon():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication([])  # noqa: F841
    import Purer as V
    base = V.make_icon().pixmap(256, 256)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [(s, png_bytes(base.scaled(s, s))) for s in sizes]
    write_ico(pngs, ICON)
    print(f"icon -> {ICON} ({ICON.stat().st_size} bytes, {len(sizes)} sizes)")


def write_version() -> Path:
    """exe 属性里的产品信息（中文说明）。版本号取自 vff/config.py。"""
    sys.path.insert(0, str(ROOT))
    from vff.config import APP_NAME, APP_TAGLINE, APP_VERSION

    parts = [int(p) for p in APP_VERSION.split(".")[:4]]
    while len(parts) < 4:
        parts.append(0)
    quad = ", ".join(str(p) for p in parts)
    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    path = build_dir / "version_info.txt"
    path.write_text(f'''VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({quad}), prodvers=({quad}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', '{APP_NAME} - {APP_TAGLINE}'),
      StringStruct('FileVersion', '{APP_VERSION}.0'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_EXE}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
''', encoding="utf-8")
    return path


def build():
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--onefile", "--windowed", "--name", APP_EXE,
            "--icon", str(ICON), "--version-file", str(write_version()),
            "--paths", str(ROOT), "--collect-submodules", "vff",
            "--workpath", str(ROOT / "build"), "--distpath", str(ROOT),
            "--specpath", str(ROOT / "build"), "--log-level", "WARN"]
    for mod in EXCLUDES:
        args += ["--exclude-module", mod]
    args.append(str(ROOT / f"{APP_EXE}.py"))
    print(" ".join(args))
    r = subprocess.run(args, cwd=str(ROOT))
    if r.returncode != 0:
        raise SystemExit(f"PyInstaller 失败：{r.returncode}")
    exe = ROOT / f"{APP_EXE}.exe"
    if not exe.exists():
        raise SystemExit("未生成 exe")
    print(f"exe -> {exe}  {exe.stat().st_size / 1048576:.1f} MB")
    shutil.rmtree(ROOT / "build", ignore_errors=True)


if __name__ == "__main__":
    make_icon()
    build()
