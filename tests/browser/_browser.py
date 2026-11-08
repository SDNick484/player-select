"""Playwright setup shared by the page tests (adapted from SBT's tests/browser). Not a test module itself.

Set BROWSER_EXECUTABLE to use an installed Chrome instead of Playwright's own Chromium, and REQUIRE_BROWSER=1
(CI does) to make a missing Playwright an error instead of a skip.
"""
from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from _support import Server  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

REQUIRE = os.environ.get("REQUIRE_BROWSER") == "1"
READY = "document.documentElement.dataset.ready === '1'"
# A request the page makes on purpose that answers >= 400 shows up twice: the response and Chrome's console line.
CHROME_HTTP_ERROR = "Failed to load resource: the server responded with a status of"


def active(cat: dict) -> list[dict]:
    return [c for c in cat["copies"] if c["removed_at"] is None]


def meta_of(cat: dict, copy: dict) -> dict:
    return cat["meta"].get(str(copy["id"]), {})


def work_id(cat: dict, title: str) -> int:
    return next(w["id"] for w in cat["works"] if w["title"] == title)


def platform_id(cat: dict, name: str) -> int:
    return next(p["id"] for p in cat["platforms"] if p["name"] == name)


@unittest.skipIf(sync_playwright is None and not REQUIRE, "playwright is not installed")
class BrowserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sync_playwright is None:
            raise RuntimeError("REQUIRE_BROWSER=1 but playwright is not installed")
        cls.pw = sync_playwright().start()
        exe = os.environ.get("BROWSER_EXECUTABLE")
        cls.browser = cls.pw.chromium.launch(**({"executable_path": exe} if exe else {}), args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.problems: list[str] = []

    def serve(self, **kw) -> Server:
        """Start player_select_server.py on the seeded fixture (7 active copies, persons Player A and B)."""
        server = Server(**kw)
        self.addCleanup(server.cleanup)
        server.start()
        self.server = server
        self.base = f"http://127.0.0.1:{server.port}"
        return server

    def context(self, width=1280, height=900, init_script: str | None = None):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, locale="en-US")
        self.addCleanup(ctx.close)
        if init_script:
            ctx.add_init_script(init_script)

        # The page is self-hosted with no CDN: any request off the local server is a failure.
        def external(route):
            self.problems.append(f"external request: {route.request.url}")
            route.abort()
        ctx.route(lambda u: not u.startswith(self.base), external)
        return ctx

    def watch(self, page):
        page.on("console", lambda m: self.problems.append(f"console {m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: self.problems.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: self.problems.append(f"request failed: {r.url} {r.failure}"))
        page.on("response", lambda r: self.problems.append(f"HTTP {r.status}: {r.url}") if r.status >= 400 else None)
        page.on("dialog", lambda d: (self.problems.append(f"dialog: {d.message}"), d.dismiss()))
        return page

    def open(self, ctx, path="/games"):
        page = self.watch(ctx.new_page())
        page.goto(self.base + path)
        page.wait_for_function(READY, timeout=15000)
        return page

    def catalog(self) -> dict:
        return self.server.get("/api/catalog").json()

    @staticmethod
    def count(page) -> int:
        m = re.match(r"\s*([\d,]+)", page.inner_text("#count"))
        if not m:
            raise AssertionError(f"#count has no number: {page.inner_text('#count')!r}")
        return int(m.group(1).replace(",", ""))

    @staticmethod
    def rows(page, view: str) -> int:
        sel = "#view-games tr.work-row" if view == "games" else "#view-items tr.copy-row"
        return page.locator(sel).count()

    def assert_clean(self, allow=()):
        left = [p for p in self.problems if not any(a in p for a in allow)]
        self.assertEqual(left, [])
