# -*- coding: utf-8 -*-
"""自更新：把下载好的新 exe 换到「自己现在跑的那个位置」（纯函数，好单测）。

Windows 上**正在运行的 exe 不能删、但可以改名** —— 所以顺序是：

    Purer.exe        →（改名）Purer.exe.old
    Purer-1.1.5.exe  →（移动）Purer.exe

然后重新启动自己；新进程起来之后再删 ``.old``（见 ``cleanup_old``）。
改名一旦失败就原样回滚，绝不留下「Purer.exe 不见了」的状态。

源码运行（没打包）时一律拒绝自更新 —— 那是开发机，不该被 exe 覆盖。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

from .cache import cache_root
from .config import APP_ID

CHUNK = 1024 * 1024


# --------------------------------------------------------------------------- #
# 位置与能力
# --------------------------------------------------------------------------- #
def app_exe() -> Path | None:
    """打包后 = 当前 exe 的绝对路径；源码运行返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        return Path(sys.executable).resolve()
    except OSError:
        return Path(sys.executable)


def can_self_update() -> tuple[bool, str]:
    """能不能自己替换自己（不能就退回「打开发布页」）。"""
    exe = app_exe()
    if exe is None:
        return False, "源码运行时不能自更新"
    if not exe.parent.is_dir():
        return False, f"程序目录不存在：{exe.parent}"
    if not os.access(exe.parent, os.W_OK):
        return False, f"程序目录没有写权限：{exe.parent}"
    return True, ""


def download_dir() -> Path:
    return cache_root() / "update"


def download_path(tag: str) -> Path:
    safe = "".join(ch for ch in str(tag or "new")
                   if ch.isalnum() or ch in ".-_") or "new"
    return download_dir() / f"{APP_ID}-{safe}.exe"


def backup_path(exe) -> Path:
    exe = Path(exe)
    return exe.with_name(exe.name + ".old")


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def digest_of(path) -> str:
    """文件的 sha256（十六进制小写）。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def parse_digest(digest: str) -> tuple[str, str]:
    """``"sha256:abcd…"`` → ``("sha256", "abcd…")``；不带前缀就当 sha256。"""
    text = str(digest or "").strip()
    if not text:
        return "", ""
    if ":" in text:
        algo, _, value = text.partition(":")
        return algo.strip().lower(), value.strip().lower()
    return "sha256", text.lower()


def verify_file(path, size: int = 0, digest: str = "") -> tuple[bool, str]:
    """大小 + sha256 都对上才算下载成功（sha256 缺省就只查大小）。"""
    target = Path(path)
    if not target.is_file():
        return False, "下载文件不存在"
    got = target.stat().st_size
    if size and got != int(size):
        return False, f"大小不对：{got} 字节 ≠ 应有 {int(size)} 字节"
    algo, want = parse_digest(digest)
    if not want:
        return True, ""
    if algo != "sha256":
        return False, f"不认识的校验方式：{algo}"
    actual = digest_of(target)
    if actual != want:
        return False, f"sha256 不一致（{actual[:12]}… ≠ {want[:12]}…）"
    return True, ""


# --------------------------------------------------------------------------- #
# 替换
# --------------------------------------------------------------------------- #
def apply_update(new_exe, exe, backup=None) -> tuple[bool, str]:
    """把 ``new_exe`` 换到 ``exe`` 的位置；旧的留成 ``exe.old``，失败回滚。"""
    new_exe, exe = Path(new_exe), Path(exe)
    old = Path(backup) if backup else backup_path(exe)
    if not new_exe.is_file():
        return False, f"找不到下载好的新版本：{new_exe}"
    if not exe.is_file():
        return False, f"找不到当前程序：{exe}"
    try:
        if old.exists():
            old.unlink()
    except OSError:
        pass                       # 删不掉不影响主流程（下一行改名会覆盖失败）

    renamed = False
    try:
        os.replace(exe, old)       # 运行中的 exe 允许改名
        renamed = True
        shutil.move(str(new_exe), str(exe))
    except OSError as exc:
        if renamed:
            try:
                os.replace(old, exe)       # 回滚：把原来的名字还回去
            except OSError:
                pass
        return False, f"替换失败：{exc}"
    return True, ""


def cleanup_old(exe=None) -> bool:
    """新版本已经正常跑起来了，把 ``.old`` 删掉（失败不报错）。"""
    target = Path(exe) if exe else app_exe()
    if target is None:
        return False
    old = backup_path(target)
    try:
        if old.is_file():
            old.unlink()
            return True
    except OSError:
        pass
    return False


def auto_install_enabled() -> bool:
    """``PURER_NO_AUTO_UPDATE=1`` → 只提示，不自动下载安装。"""
    flag = os.environ.get("PURER_NO_AUTO_UPDATE", "").strip().lower()
    return flag not in ("1", "true", "yes", "on")
