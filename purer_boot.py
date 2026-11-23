# -*- coding: utf-8 -*-
"""稳定引导层：决定 ``vff`` 的代码从哪来（内置 / 热补丁包），并负责热替换。

**这个文件永远不进热补丁包，也永远不参与热替换** —— 它是唯一可信的入口：

1. 启动时 :func:`install`：如果 ``%LOCALAPPDATA%\\Purer\\code\\current\\`` 里有一个
   **比当前 exe 更新**、且声明的 ``exe_min`` 不过期的代码包，就在 ``sys.meta_path``
   最前面插一个查找器，让 ``import vff.*`` 优先命中代码包；找不到就自然落回
   PyInstaller 冻结的内置代码（等于"没有补丁"）。
2. 运行时 :func:`hot_swap`：把新包换到 ``current``（旧的留 ``previous``）→
   清掉 ``sys.modules`` 里的 ``vff.*`` → 重新 import → 建新窗口 → 恢复会话。
   任何一步抛异常都自动回滚到上一个包（或内置代码）并重建窗口。

为什么必须这样：PyInstaller onefile 的 Python 代码是 PYZ 里的字节码，磁盘上没有
``.py`` 可改；``PyiFrozenImporter`` 又排在 ``sys.meta_path`` 最前面，往 ``sys.path``
里塞目录根本盖不住它。只能自己插一个只认 ``vff.*`` 的查找器。

依赖只有标准库 —— 它必须能在 ``vff`` 之前跑起来。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

PACK_JSON = "pack.json"
CODE_DIR_NAME = "code"
CURRENT_NAME = "current"
PREVIOUS_NAME = "previous"
STAGING_NAME = "staging"

# 当前真正生效的包信息（install/hot_swap 之后写入；{} = 用的是内置代码）
_ACTIVE: dict = {}
_LAST_ERROR: str = ""

# 主窗口引用放这里：热替换时由这里换掉，顶层 main() 不用知道
HOLDER: dict = {"win": None}


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def app_dir() -> Path:
    """exe 所在目录（打包）/ 项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """持久数据目录（跟 ``vff.config.data_dir`` 保持一致）。"""
    override = os.environ.get("PURER_DATA_DIR")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Purer"
    return Path.home() / ".purer"


def code_root() -> Path:
    override = os.environ.get("PURER_CODE_DIR")
    return Path(override) if override else data_dir() / CODE_DIR_NAME


def pack_dir() -> Path:
    return code_root() / CURRENT_NAME


def previous_dir() -> Path:
    return code_root() / PREVIOUS_NAME


def staging_dir() -> Path:
    return code_root() / STAGING_NAME


# --------------------------------------------------------------------------- #
# 版本 / 兼容性
# --------------------------------------------------------------------------- #
_NUM = re.compile(r"\d+(?:\.\d+)*")


def parse_version(text) -> tuple[int, ...]:
    found = _NUM.search(str(text or ""))
    if not found:
        return ()
    return tuple(int(part) for part in found.group(0).split("."))


def is_newer(latest, current) -> bool:
    new, cur = parse_version(latest), parse_version(current)
    if not new:
        return False
    width = max(len(new), len(cur))
    new = new + (0,) * (width - len(new))
    cur = cur + (0,) * (width - len(cur))
    return new > cur


def file_version(path) -> str:
    """读 exe 的版本资源（``1.2.0``）；拿不到返回空串。

    这样引导层不必 import ``vff`` 就能知道"自己是哪个 exe"，
    从而判断代码包是不是给这个 exe 的。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        version = ctypes.WinDLL("version", use_last_error=True)
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
            return ""
        pointer = ctypes.c_void_p()
        length = wintypes.UINT(0)
        if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer),
                                      ctypes.byref(length)):
            return ""
        if not pointer.value or length.value < 52:
            return ""
        # VS_FIXEDFILEINFO: 前 8 字节是签名，然后 dwFileVersionMS / dwFileVersionLS
        raw = ctypes.string_at(pointer.value, 16)
        ms = int.from_bytes(raw[8:12], "little")
        ls = int.from_bytes(raw[12:16], "little")
        parts = [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
        while len(parts) > 3 and parts[-1] == 0:   # 1.2.0.0 → 1.2.0
            parts.pop()
        return ".".join(str(part) for part in parts)
    except Exception:          # noqa: BLE001  读不到就当没有
        return ""


def exe_version() -> str:
    """当前 exe 的版本（源码运行时看 ``PURER_EXE_VERSION``）。"""
    if getattr(sys, "frozen", False):
        got = file_version(sys.executable)
        if got:
            return got
    return os.environ.get("PURER_EXE_VERSION", "").strip()


def read_pack(folder: Path | None = None) -> dict:
    """读代码包的 ``pack.json``（坏文件/缺字段一律当没有包）。"""
    target = Path(folder) if folder is not None else pack_dir()
    try:
        data = json.loads((target / PACK_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    info = {
        "version": str(data.get("version") or "").strip(),
        "exe_min": str(data.get("exe_min") or "").strip(),
        "built_at": float(data.get("built_at") or 0.0),
        "files": int(data.get("files") or 0),
        "note": str(data.get("note") or "")[:200],
        "path": str(target),
    }
    if not parse_version(info["version"]):
        return {}
    return info


def compatible(pack: dict, current_exe: str) -> tuple[bool, str]:
    """这个包能不能装到当前 exe 上？"""
    if not pack:
        return False, "没有代码包"
    version = pack.get("version") or ""
    if not parse_version(current_exe):
        return False, "拿不到 exe 版本"
    if not is_newer(version, current_exe):
        return False, f"代码包 {version} 不比当前 exe {current_exe} 新（已过期）"
    base = pack.get("exe_min") or ""
    if base and is_newer(base, current_exe):
        return False, f"代码包要求 exe ≥ {base}，当前是 {current_exe}"
    return True, ""


def effective_version(exe_ver: str | None = None) -> str:
    """对外显示的版本：有生效的代码包就用包版本，否则就是 exe 版本。"""
    base = exe_ver if exe_ver is not None else exe_version()
    info = _ACTIVE
    if info and is_newer(info.get("version"), base):
        return info["version"]
    return base


def active_pack() -> dict:
    return dict(_ACTIVE)


def last_error() -> str:
    return _LAST_ERROR


# --------------------------------------------------------------------------- #
# 导入器：只认 vff.*，从代码包目录里找
# --------------------------------------------------------------------------- #
class OverlayFinder:
    """把 ``vff`` / ``vff.*`` 解析到代码包目录；找不到就返回 None 交给内置导入器。"""

    def __init__(self, root: Path):
        self.root = Path(root)

    def __repr__(self) -> str:              # 便于 --code-info / 调试
        return f"<OverlayFinder {self.root}>"

    # importlib.abc.MetaPathFinder 只要求这一个方法
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "vff" and not fullname.startswith("vff."):
            return None
        parts = fullname.split(".")
        base = self.root.joinpath(*parts)
        package_init = base / "__init__.py"
        module_file = base.with_suffix(".py")
        if package_init.is_file():
            return importlib.util.spec_from_file_location(
                fullname, package_init,
                submodule_search_locations=[str(base)])
        if module_file.is_file():
            return importlib.util.spec_from_file_location(fullname, module_file)
        return None


def _drop_finders() -> None:
    sys.meta_path[:] = [f for f in sys.meta_path
                        if not isinstance(f, OverlayFinder)]


def _install_finder(root: Path) -> None:
    _drop_finders()
    sys.meta_path.insert(0, OverlayFinder(root))


def install() -> dict:
    """启动时调用一次：有兼容的代码包就激活（否则什么都不做）。"""
    global _ACTIVE, _LAST_ERROR
    _LAST_ERROR = ""
    info = read_pack()
    if not info:
        _ACTIVE = {}
        return {}
    if not (pack_dir() / "vff" / "__init__.py").is_file():
        _LAST_ERROR = "代码包不完整（缺 vff/__init__.py）"
        _ACTIVE = {}
        return {}
    ok, why = compatible(info, exe_version())
    if not ok:
        _LAST_ERROR = why
        _ACTIVE = {}
        return {}
    _install_finder(pack_dir())
    _ACTIVE = info
    return dict(info)


def prune_stale() -> bool:
    """exe 已经升级到不比包旧了 → 把代码包删掉（它已经没意义）。"""
    info = read_pack()
    if not info:
        return False
    ok, _why = compatible(info, exe_version())
    if ok:
        return False
    try:
        shutil.rmtree(pack_dir(), ignore_errors=True)
        shutil.rmtree(previous_dir(), ignore_errors=True)
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------- #
# 热替换
# --------------------------------------------------------------------------- #
def _purge_vff() -> list[str]:
    removed = []
    for name in list(sys.modules):
        if name == "vff" or name.startswith("vff."):
            del sys.modules[name]
            removed.append(name)
    return removed


def _teardown(win) -> None:
    """尽力把旧窗口停干净（它马上要被丢掉，不能留下还在跑的定时器）。"""
    if win is None:
        return
    try:
        win.prepare_rebuild()               # MainWindow 提供；没有就跳过
    except Exception:                       # noqa: BLE001
        pass
    try:
        win.hide()
        win.close()
        win.deleteLater()
    except Exception:                       # noqa: BLE001
        pass


def _build_window(restore: bool = True):
    module = importlib.import_module("vff.ui.main_window")
    win = module.MainWindow()
    if restore:
        try:
            win.restore_session()
        except Exception:                   # noqa: BLE001  恢复失败也要能开窗
            pass
    win.show()
    HOLDER["win"] = win
    return win


def hot_swap(app, staging: Path, exe_ver: str, old_window=None,
             note: str = "") -> tuple[bool, str, object]:
    """把 ``staging``（已解压好的代码包）换成当前代码，并重建界面。

    返回 ``(成功?, 说明, 新窗口)``。失败时窗口照样会重建（用的还是旧代码）。
    """
    global _ACTIVE
    target = Path(staging)
    info = read_pack(target)
    ok, why = compatible(info, exe_ver)
    if not ok:
        return False, f"代码包不可用：{why}", HOLDER.get("win")
    if not (target / "vff" / "__init__.py").is_file():
        return False, "代码包不完整（缺 vff/__init__.py）", HOLDER.get("win")

    current, previous = pack_dir(), previous_dir()
    backup_moved = False
    try:
        current.parent.mkdir(parents=True, exist_ok=True)
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        if current.exists():
            os.replace(current, previous)       # 同盘改名，快且原子
            backup_moved = True
        os.replace(target, current)
    except OSError as exc:
        if backup_moved and not current.exists():
            try:
                os.replace(previous, current)
            except OSError:
                pass
        return False, f"替换代码包失败：{exc}", HOLDER.get("win")

    # 到这里新代码已经就位。停旧窗口 → 清模块 → 重新 import → 建新窗口
    _teardown(old_window)
    _install_finder(current)
    _purge_vff()
    try:
        win = _build_window(restore=True)
    except Exception as exc:                # noqa: BLE001  新包坏了 → 回退
        _drop_finders()
        shutil.rmtree(current, ignore_errors=True)
        if previous.exists():
            try:
                os.replace(previous, current)
            except OSError:
                pass
        _purge_vff()
        _install_finder(current) if (current / "vff" / "__init__.py").is_file() else None
        _ACTIVE = read_pack(current) if (current / "vff" / "__init__.py").is_file() else {}
        try:
            win = _build_window(restore=True)
        except Exception as exc2:           # noqa: BLE001  连内置都起不来
            return False, f"新代码和内置代码都起不来：{exc} / {exc2}", None
        _status(win, f"⚠ 热补丁没装上，已回退（{exc}）。当前还是 v{exe_ver}")
        return False, f"新代码跑不起来，已回退：{exc}", win

    _ACTIVE = read_pack(current)
    version = _ACTIVE.get("version") or info.get("version") or ""
    _status(win, f"✅ 热修复 v{version} 已生效（没有重启）" + (f" · {note}" if note else ""))
    return True, "", win


def _status(win, text: str) -> None:
    """在新窗口的状态栏说一句话（窗口没有这个属性就算了）。"""
    try:
        label = getattr(win, "lbl_status", None)
        if label is not None:
            label.setText(text)
    except RuntimeError:
        pass


def rollback_to_previous(app, exe_ver: str, old_window=None
                         ) -> tuple[bool, str, object]:
    """撤掉刚装上的代码包，回到 ``previous``（或内置代码），并重建界面。"""
    global _ACTIVE
    current, previous = pack_dir(), previous_dir()
    if not previous.is_dir():
        return False, "没有可回退的旧代码包", HOLDER.get("win")
    _teardown(old_window)
    try:
        shutil.rmtree(current, ignore_errors=True)
        os.replace(previous, current)
    except OSError as exc:
        return False, f"回退失败：{exc}", HOLDER.get("win")
    _purge_vff()
    if (current / "vff" / "__init__.py").is_file():
        info = read_pack(current)
        ok, _why = compatible(info, exe_ver)
        if ok:
            _install_finder(current)
            _ACTIVE = info
        else:
            _drop_finders()
            _ACTIVE = {}
    else:
        _drop_finders()
        _ACTIVE = {}
    try:
        win = _build_window(restore=True)
    except Exception as exc:                # noqa: BLE001
        return False, f"回退后窗口起不来：{exc}", None
    _status(win, "已撤销热补丁，回到 "
            + (f"v{_ACTIVE.get('version')}" if _ACTIVE else f"v{exe_ver}"))
    return True, "", win


def remove_pack(app, exe_ver: str, old_window=None) -> tuple[bool, str, object]:
    """彻底删掉代码包（回到 exe 内置代码），并重建界面。"""
    global _ACTIVE
    _teardown(old_window)
    shutil.rmtree(pack_dir(), ignore_errors=True)
    shutil.rmtree(previous_dir(), ignore_errors=True)
    _drop_finders()
    _ACTIVE = {}
    _purge_vff()
    try:
        win = _build_window(restore=True)
    except Exception as exc:                # noqa: BLE001
        return False, f"删除代码包后窗口起不来：{exc}", None
    _status(win, f"已移除热补丁，回到内置代码 v{exe_ver}")
    return True, "", win


def hold(win) -> None:
    HOLDER["win"] = win


def uninstall_finders() -> None:
    _drop_finders()


__all__ = ["install", "hot_swap", "hold", "HOLDER", "active_pack",
           "effective_version", "exe_version", "file_version", "compatible",
           "read_pack", "parse_version", "is_newer", "code_root", "pack_dir",
           "previous_dir", "staging_dir", "prune_stale", "last_error",
           "OverlayFinder", "uninstall_finders"]
