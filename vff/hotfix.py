# -*- coding: utf-8 -*-
"""窄范围热补丁：只换 ``vff`` 里的纯 Python 代码，**不重启进程**。

适用范围（刻意画死）：

* 只能改 ``vff/`` 下的纯 Python（逻辑 / 文案 / 少量 UI）；
* 改不了 Qt 资源、打包数据、C 层行为、ffmpeg 参数语义 —— 那些必须走 exe 更新；
* 一个代码包只对 **比它旧的 exe** 生效（``pack.json`` 里的 ``exe_min`` 再兜一层）；
* 装坏了自动回退到上一个包 / 内置代码，绝不留在"打不开"的状态。

流程：``validate_zip`` → ``stage``（解到 ``code/staging``）→
``purer_boot.hot_swap``（换目录 + 清 ``vff.*`` + 重建窗口）。
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import purer_boot

from .config import APP_VERSION

MAX_ZIP_BYTES = 20 * 1024 * 1024        # 代码包正常 100~200 KB，20 MB 是防呆上限
MAX_FILES = 2000


def _exe_version() -> str:
    """exe 版本：打包后读 exe 版本资源；源码运行时退回 ``APP_VERSION``。"""
    return purer_boot.exe_version() or APP_VERSION


def state() -> dict:
    """给界面 / --code-info 用：现在到底跑的是哪份代码？"""
    active = purer_boot.active_pack()
    installed = purer_boot.read_pack()
    exe_ver = _exe_version()
    ok, why = purer_boot.compatible(installed, exe_ver) if installed else (False, "")
    return {
        "exe_version": exe_ver,
        "effective_version": purer_boot.effective_version(exe_ver)
        or APP_VERSION,
        "active": bool(active),
        "active_version": active.get("version", ""),
        "installed_version": installed.get("version", ""),
        "installed_path": installed.get("path", ""),
        "installed_ok": bool(ok),
        "installed_reason": why,
        "previous": purer_boot.previous_dir().is_dir(),
        "error": purer_boot.last_error(),
    }


def purge_files() -> bool:
    """删掉代码包文件（不重建界面 → 下次启动才回到内置代码）。"""
    shutil.rmtree(purer_boot.pack_dir(), ignore_errors=True)
    shutil.rmtree(purer_boot.previous_dir(), ignore_errors=True)
    return True


# --------------------------------------------------------------------------- #
# 校验 / 解包
# --------------------------------------------------------------------------- #
def _safe_member(name: str) -> bool:
    text = str(name or "").replace("\\", "/")
    if not text or text.startswith("/") or ":" in text:
        return False
    return ".." not in text.split("/")


def validate_zip(path) -> tuple[dict, str]:
    """返回 ``(pack_info, 错误)``；错误非空就说明这个包不能用。"""
    target = Path(path)
    if not target.is_file():
        return {}, "代码包不存在"
    try:
        size = target.stat().st_size
    except OSError as exc:
        return {}, f"读不到代码包：{exc}"
    if size <= 0 or size > MAX_ZIP_BYTES:
        return {}, f"代码包大小不对：{size} 字节"
    try:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) > MAX_FILES:
                return {}, f"代码包里文件太多：{len(names)}"
            if not all(_safe_member(name) for name in names):
                return {}, "代码包里有不安全路径"
            if "pack.json" not in names:
                return {}, "代码包缺 pack.json"
            if "vff/__init__.py" not in names:
                return {}, "代码包缺 vff/__init__.py"
            raw = json.loads(archive.read("pack.json").decode("utf-8"))
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return {}, f"代码包读不出来：{exc}"
    if not isinstance(raw, dict):
        return {}, "pack.json 不是对象"
    info = {
        "version": str(raw.get("version") or "").strip(),
        "exe_min": str(raw.get("exe_min") or "").strip(),
        "built_at": float(raw.get("built_at") or 0.0),
        "files": int(raw.get("files") or 0),
        "note": str(raw.get("note") or "")[:200],
    }
    if not purer_boot.parse_version(info["version"]):
        return {}, "pack.json 里的版本号不合法"
    ok, why = purer_boot.compatible(info, _exe_version())
    if not ok:
        return {}, why
    return info, ""


def stage(path) -> tuple[Path, str]:
    """解到 ``code/staging``（先清空）。返回 ``(目录, 错误)``。"""
    info, why = validate_zip(path)
    if not info:
        return Path(), why
    target = purer_boot.staging_dir()
    shutil.rmtree(target, ignore_errors=True)
    try:
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                if not _safe_member(name):
                    continue
                dest = target / name.replace("\\", "/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (OSError, zipfile.BadZipFile) as exc:
        shutil.rmtree(target, ignore_errors=True)
        return Path(), f"解包失败：{exc}"
    return target, ""


# --------------------------------------------------------------------------- #
# 安装 / 回退 / 移除（都要重建界面）
# --------------------------------------------------------------------------- #
def install(zip_path, old_window=None) -> tuple[bool, str, object]:
    """装一个代码包并立刻生效（不重启进程）。"""
    staged, why = stage(zip_path)
    if not staged:
        return False, why, purer_boot.HOLDER.get("win")
    exe_ver = _exe_version()
    ok, why, win = purer_boot.hot_swap(None, staged, exe_ver,
                                       old_window=old_window,
                                       note="不用重启")
    if not ok and staged.exists():          # 失败时别把暂存目录留着
        shutil.rmtree(staged, ignore_errors=True)
    return ok, why, win


def rollback(old_window=None) -> tuple[bool, str, object]:
    """回到上一个代码包（没有就回内置代码）。"""
    return purer_boot.rollback_to_previous(None, _exe_version(),
                                           old_window=old_window)


def remove(old_window=None) -> tuple[bool, str, object]:
    """删掉代码包，回到 exe 内置代码。"""
    return purer_boot.remove_pack(None, _exe_version(),
                                  old_window=old_window)
