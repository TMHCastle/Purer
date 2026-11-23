# -*- coding: utf-8 -*-
"""Purer —— 视频抽帧浏览 / 帧精确切片。

入口薄壳：真正的实现在 vff/ 包里。
  源码运行：`python Purer.py` 或 `python -m vff`
  自检：`Purer.exe --selftest`
  命令行：`Purer.exe --help`
"""

from __future__ import annotations

import sys

import purer_boot

# **顺序要紧**：先让引导层判断「有没有热补丁包」并装好查找器，再 import vff。
# 反过来的话 vff 已经按内置代码缓存进 sys.modules，补丁就白装了。
purer_boot.install()

from vff.__main__ import main  # noqa: E402  （必须在 install() 之后）


def make_icon():
    """给 build.py 用（延迟导入 Qt，命令行模式不必加载界面库）。"""
    from vff.ui.style import make_icon as _make_icon
    return _make_icon()


if __name__ == "__main__":
    sys.exit(main())
