# -*- coding: utf-8 -*-
"""Purer —— 视频抽帧浏览 / 帧精确切片。

入口薄壳：真正的实现在 vff/ 包里。
  源码运行：`python Purer.py` 或 `python -m vff`
  自检：`Purer.exe --selftest`
  命令行：`Purer.exe --help`
"""

from __future__ import annotations

import sys

from vff.__main__ import main


def make_icon():
    """给 build.py 用（延迟导入 Qt，命令行模式不必加载界面库）。"""
    from vff.ui.style import make_icon as _make_icon
    return _make_icon()


if __name__ == "__main__":
    sys.exit(main())
