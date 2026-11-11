# -*- coding: utf-8 -*-
"""版本号解析与「要不要更新」判定（纯函数：不联网、不依赖 Qt）。

GitHub 上的 tag 长这样：``v1.1.3`` / ``Purer.1.1.4`` / ``1.2.0-beta.1``，
这里只比数字部分，后缀（-beta.1 之类）不参与比较。
"""

from __future__ import annotations

import re

# 第一个「1.2.3」形状的数字串（tag 前缀 v / Purer. 之类都会被跳过）
_NUM = re.compile(r"\d+(?:\.\d+)*")

STATES = ("idle", "checking", "latest", "stale", "error")


def parse_version(text) -> tuple[int, ...]:
    """``'Purer.1.1.4'`` → ``(1, 1, 4)``；解析不出来返回空元组。"""
    if not text:
        return ()
    found = _NUM.search(str(text))
    if not found:
        return ()
    return tuple(int(part) for part in found.group(0).split("."))


def normalize(text) -> str:
    """``'Purer.1.1.4'`` → ``'v1.1.4'``；没有数字返回空串。"""
    version = parse_version(text)
    if not version:
        return ""
    return "v" + ".".join(str(part) for part in version)


def is_newer(latest, current) -> bool:
    """latest 比 current 新？

    缺位按 0 补齐，所以 ``1.1.4`` 与 ``1.1.4.0`` 相等，``1.10`` 比 ``1.9`` 新。
    """
    new, cur = parse_version(latest), parse_version(current)
    if not new:
        return False
    width = max(len(new), len(cur))
    new = new + (0,) * (width - len(new))
    cur = cur + (0,) * (width - len(cur))
    return new > cur


def state_for(ok: bool, latest, current) -> str:
    """三色约定：``latest``（绿）/ ``stale``（橙）/ ``error``（灰）。

    查不到、或者查到了但 tag 里没有数字 —— 一律算「没查成」，显示灰色，
    千万别在不知道版本号的情况下报「已是最新」。
    """
    if not ok or not parse_version(latest):
        return "error"
    return "stale" if is_newer(latest, current) else "latest"
