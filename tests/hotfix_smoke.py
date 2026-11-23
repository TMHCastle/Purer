# -*- coding: utf-8 -*-
"""热补丁端到端冒烟（离屏）：换代码 → 回滚 → 移除。

    python tests\\hotfix_smoke.py

真窗口、真 zip、真 import 重载；全部在临时目录里跑，不碰真实数据。
输出 PASS/FAIL 到 stdout，退出码 0/2。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = Path(tempfile.mkdtemp(prefix="purer_hotfix_smoke_"))
for name in ("cache", "data", "pcache", "code"):
    (BASE / name).mkdir(parents=True, exist_ok=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.update({
    "PURER_NO_UPDATE": "1",
    "PURER_CACHE_DIR": str(BASE / "cache"),
    "PURER_DATA_DIR": str(BASE / "data"),
    "PURER_PERSIST_CACHE_DIR": str(BASE / "pcache"),
    "PURER_CODE_DIR": str(BASE / "code"),
    "PURER_EXE_VERSION": "1.2.0",
})

import purer_boot  # noqa: E402

purer_boot.install()

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from vff import hotfix  # noqa: E402
from vff.config import APP_VERSION  # noqa: E402
from vff.ui.main_window import MainWindow  # noqa: E402
from vff.ui.style import apply_theme  # noqa: E402

LOG: list[str] = []
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    LOG.append(f"{'PASS  ' if ok else 'FAIL  '}{name}"
               + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_pack(version: str, break_main: bool = False) -> Path:
    """用当前源码打一个包；版本号改掉，可选把 main_window 写坏。"""
    target = BASE / f"pack-{version}-{'bad' if break_main else 'ok'}.zip"
    files = [p for p in sorted((ROOT / "vff").rglob("*.py"))
             if ".bak." not in p.name]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, 6) as archive:
        for path in files:
            text = path.read_text(encoding="utf-8")
            if path.name == "config.py":
                text = text.replace(f'APP_VERSION = "{APP_VERSION}"',
                                    f'APP_VERSION = "{version}"', 1)
            if break_main and path.as_posix().endswith("ui/main_window.py"):
                text = "this is not python\n" + text
            archive.writestr(path.relative_to(ROOT).as_posix(), text)
        archive.writestr("pack.json", json.dumps({
            "version": version, "exe_min": "1.2.0", "files": len(files),
            "built_at": 0, "note": "smoke"}, ensure_ascii=False))
    return target


def main() -> int:
    app = QApplication([])
    apply_theme(app)
    win = MainWindow()
    win.setAttribute(Qt.WA_DontShowOnScreen, True)
    win.resize(1000, 640)
    win.show()
    app.processEvents()
    purer_boot.hold(win)
    check("starts on built-in code",
          not purer_boot.active_pack()
          and purer_boot.effective_version() == "1.2.0",
          purer_boot.effective_version())

    ok1, why1, new1 = hotfix.install(make_pack("1.2.9"), old_window=win)
    app.processEvents()
    check("hotfix installs without restart", ok1, why1)
    check("new window uses new code",
          new1 is not None and "1.2.9" in (new1.windowTitle() or ""),
          new1.windowTitle() if new1 is not None else "no window")
    check("effective version follows the pack",
          purer_boot.effective_version() == "1.2.9",
          purer_boot.effective_version())
    check("old window was torn down", getattr(win, "_reloading", False))

    ok2, why2, new2 = hotfix.install(make_pack("1.3.0", break_main=True),
                                     old_window=new1)
    app.processEvents()
    check("broken pack is rejected", not ok2, why2[:70])
    check("window survives the rollback", new2 is not None)
    check("rollback keeps the previous pack",
          purer_boot.effective_version() == "1.2.9",
          purer_boot.effective_version())

    ok3, why3, new3 = hotfix.remove(old_window=new2)
    app.processEvents()
    check("remove returns to built-in code",
          ok3 and purer_boot.effective_version() == "1.2.0",
          f"{ok3} {purer_boot.effective_version()} {why3}")

    for window in (win, new1, new2, new3):
        try:
            if window is not None:
                window.close()
        except Exception:      # noqa: BLE001
            pass
    app.processEvents()

    print("\n".join(LOG))
    print("RESULT:", "PASS" if not FAILS else "FAIL " + ", ".join(FAILS))
    if FAILS:
        print("tmp:", BASE)
    return 0 if not FAILS else 2


if __name__ == "__main__":
    sys.exit(main())
