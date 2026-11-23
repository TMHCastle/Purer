# -*- coding: utf-8 -*-
"""下载多源 + 测速换源的单测（纯逻辑，不开窗口）。

    python -m unittest tests.test_mirrors -v
"""

from __future__ import annotations

import contextlib
import os
import unittest

from vff.download import build_attempts, speed_kbps, too_slow
from vff.update import (MIRROR_PREFIXES, CheckResult, mirror_url, mirror_urls)

ENV = "PURER_DOWNLOAD_MIRRORS"
_MISSING = object()
ASSET = "https://github.com/o/r/releases/download/v1.2.1/Purer.exe"


@contextlib.contextmanager
def env(value=_MISSING):
    old = os.environ.get(ENV, _MISSING)
    try:
        if value is _MISSING:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = value
        yield
    finally:
        if old is _MISSING:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = old


class TestMirrorUrls(unittest.TestCase):
    def test_default_prefixes(self):
        with env():
            urls = mirror_urls(ASSET)
        self.assertEqual(len(urls), 1 + len(MIRROR_PREFIXES))
        self.assertEqual(urls[0], ASSET)
        self.assertTrue(urls[1].startswith("https://ghfast.top/"))
        self.assertTrue(urls[1].endswith(ASSET))

    def test_env_override(self):
        with env("https://m1.example/, https://m2.example/?u={url}"):
            urls = mirror_urls(ASSET)
        self.assertEqual(urls[1], "https://m1.example/" + ASSET)
        self.assertEqual(urls[2], "https://m2.example/?u=" + ASSET)

    def test_env_empty_disables_mirrors(self):
        with env(""):
            self.assertEqual(mirror_urls(ASSET), [ASSET])

    def test_dedupe_and_empty(self):
        with env("https://same.example/"):
            self.assertEqual(mirror_urls(""), [])
            urls = mirror_urls(ASSET, prefixes=["https://same.example/",
                                                "https://same.example/"])
        self.assertEqual(urls, [ASSET, "https://same.example/" + ASSET])

    def test_mirror_url_shapes(self):
        self.assertEqual(mirror_url("U", "https://x/"),
                         "https://x/U")
        self.assertEqual(mirror_url("U", "https://x"),
                         "https://x/U")
        self.assertEqual(mirror_url("U", "https://x/raw?to={url}"),
                         "https://x/raw?to=U")
        self.assertEqual(mirror_url("U", ""), "")


class TestCheckResultUrls(unittest.TestCase):
    def test_asset_and_code(self):
        result = CheckResult(True, "v1.2.1", "", "", "", ASSET, 10, "",
                             "https://example.invalid/c.zip", 20, "")
        with env("https://m.example/"):
            self.assertEqual(len(result.asset_urls()), 2)
            self.assertEqual(len(result.code_urls()), 2)
            self.assertTrue(result.asset_urls()[1].startswith("https://m.example/"))
            self.assertTrue(result.code_urls()[1].startswith("https://m.example/"))

    def test_empty(self):
        result = CheckResult(True, "v1.2.1")
        self.assertEqual(result.asset_urls(), [])
        self.assertEqual(result.code_urls(), [])


class TestAttempts(unittest.TestCase):
    def test_order_and_labels(self):
        with env(""):
            attempts = build_attempts([ASSET, "https://m.example/x"], "127.0.0.1:7890")
        self.assertEqual(len(attempts), 3)
        # 原址：先代理再直连
        self.assertEqual(attempts[0]["proxy"], "127.0.0.1:7890")
        self.assertEqual(attempts[1]["proxy"], "")
        # 镜像：只直连
        self.assertEqual(attempts[2]["url"], "https://m.example/x")
        self.assertEqual(attempts[2]["proxy"], "")
        self.assertIn("源 1/2", attempts[0]["label"])
        self.assertIn("源 2/2", attempts[2]["label"])

    def test_no_proxy_no_duplicates(self):
        attempts = build_attempts([ASSET, ASSET, ""], "")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["url"], ASSET)


class TestSpeed(unittest.TestCase):
    def test_kbps(self):
        self.assertAlmostEqual(speed_kbps(1024, 1.0), 1.0)
        self.assertEqual(speed_kbps(1024, 0), 0.0)

    def test_too_slow(self):
        total = 45 * 1024 * 1024
        self.assertFalse(too_slow(0, 1.0, total))          # 还在探
        self.assertFalse(too_slow(10 * 1024, 2.0, total))  # 探速窗口内
        self.assertTrue(too_slow(10 * 1024, 3.0, total))   # 3.3 KB/s
        self.assertFalse(too_slow(600 * 1024, 3.0, total))  # 200 KB/s
        # 已经下了 90%：即便平均速度不高也别换（换源要重下）
        self.assertFalse(too_slow(90 * 1024, 3.0, 100 * 1024))


if __name__ == "__main__":
    unittest.main()
