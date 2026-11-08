"""catalog.html in a real browser (Playwright + Chromium), served by player_select_server.py.

What it proves: every page path loads its own view with no console errors, failed requests or requests off the
server; the Games and Items views list what /api/catalog says they should; the header carries the --nav links and
switching views keeps the filter hash and survives a reload; a status set for one person is not shown for another
and survives a reload; a stale catalog ETag (412) is refetched and the save retried once; and the page still works
when localStorage throws.

The server is seeded with the fixture backup and CSV through the real importer (tests/_seed.py): 7 active copies,
persons Player A and Player B. Expected counts are worked out here from GET /api/catalog.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _browser import CHROME_HTTP_ERROR, BrowserCase, active, platform_id, work_id  # noqa: E402

PATHS = {"/": "games", "/games": "games", "/items": "items", "/up-next": "up-next",
         "/duplicates": "duplicates", "/import": "import", "/items/": "items"}
VIEWS = ("games", "items", "up-next", "duplicates", "import")
NAV = "Steam=http://steam.example.com:8484/"


class PageSmoke(BrowserCase):
    def visible_views(self, page) -> list[str]:
        return [v for v in VIEWS if page.is_visible(f"#view-{v}")]

    def test_every_path_loads_without_console_errors(self):
        self.serve(nav=[NAV])
        ctx = self.context()
        for path, view in PATHS.items():
            with self.subTest(path=path):
                page = self.open(ctx, path)
                self.assertEqual(self.visible_views(page), [view])
                self.assertEqual(page.get_attribute(f'header a[data-view="{view}"]', "aria-current"), "page")
                labels = page.eval_on_selector_all("header a[data-view]", "els => els.map(e => e.textContent.trim())")
                self.assertEqual(labels, ["Games", "Items", "Up next", "Duplicates", "Import"])
                self.assertEqual(page.eval_on_selector("#navlinks a", "a => [a.textContent, a.href]"),
                                 ["Steam", "http://steam.example.com:8484/"])
                page.close()
        self.assert_clean()

    def test_games_count_matches_api(self):
        self.serve()
        cat = self.catalog()
        want = len({c["work_id"] for c in active(cat) if c["work_id"] is not None})
        self.assertGreater(want, 0)
        page = self.open(self.context(), "/games")
        self.assertEqual(self.rows(page, "games"), want)
        self.assertEqual(self.count(page), want)
        # Platform chips use the short labels; a work with two copies on one platform shows both conditions.
        bb = page.inner_text(f'tr.work-row[data-work="{work_id(cat, "Bloodborne")}"]')
        self.assertIn("PS4 · Boxed", bb)
        self.assertIn("PS4 · CIB", bb)
        dk = page.inner_text(f'tr.work-row[data-work="{work_id(cat, "Donkey Kong Bananza")}"]')
        self.assertIn("Switch 2 · CIB", dk)
        self.assertIn("Platformer", dk)
        self.assertIn("88", dk)
        self.assert_clean()

    def test_items_include_hardware(self):
        self.serve()
        cat = self.catalog()
        copies = active(cat)
        page = self.open(self.context(), "/items")
        self.assertEqual(self.rows(page, "items"), len(copies))
        self.assertEqual(self.count(page), len(copies))
        system = next(c for c in copies if c["category"] == "system")
        row = page.inner_text(f'tr.copy-row[data-copy="{system["id"]}"]')
        self.assertIn("Nintendo Switch 2", row)
        self.assertIn("System", row)
        self.assert_clean()

    def test_view_links_push_state_and_keep_the_hash(self):
        self.serve()
        cat = self.catalog()
        ps4 = platform_id(cat, "Sony PlayStation 4")
        page = self.open(self.context(), f"/games#p={ps4}")
        page.click('header a[data-view="items"]')
        page.wait_for_function("location.pathname === '/items'")
        self.assertEqual(page.evaluate("location.hash"), f"#p={ps4}")
        self.assertEqual(self.visible_views(page), ["items"])
        self.assertEqual(self.rows(page, "items"), sum(1 for c in active(cat) if c["platform_id"] == ps4))
        page.reload()
        page.wait_for_function("document.documentElement.dataset.ready === '1'")
        self.assertEqual(self.visible_views(page), ["items"])
        page.go_back()
        page.wait_for_function("location.pathname === '/games'")
        self.assertEqual(self.visible_views(page), ["games"])
        self.assert_clean()

    def test_status_is_per_person(self):
        self.serve()
        cat = self.catalog()
        bb = work_id(cat, "Bloodborne")
        a, b = (p["id"] for p in cat["persons"][:2])
        ctx = self.context()
        page = self.open(ctx, "/games")
        self.assertEqual(page.input_value("#person"), str(a), "defaults to the first person")
        sel = f'select.status[data-work="{bb}"]'
        with page.expect_response(lambda r: r.url.endswith("/api/play-state") and r.request.method == "PUT") as resp:
            page.select_option(sel, "finished")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual([(s["person"], s["work"], s["status"]) for s in self.catalog()["play_state"]],
                         [(a, bb, "finished")])
        page.select_option("#person", str(b))
        self.assertEqual(page.input_value(sel), "")
        page.reload()
        page.wait_for_function("document.documentElement.dataset.ready === '1'")
        self.assertEqual(page.input_value("#person"), str(b), "the picked person is remembered")
        self.assertEqual(page.input_value(sel), "")
        page.select_option("#person", str(a))
        self.assertEqual(page.input_value(sel), "finished")
        # Clearing the status deletes the row.
        with page.expect_response(lambda r: r.url.endswith("/api/play-state")) as resp:
            page.select_option(sel, "")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(self.catalog()["play_state"], [])
        self.assert_clean()

    def test_stale_etag_is_refetched_and_retried_once(self):
        self.serve()
        cat = self.catalog()
        bb, dk = work_id(cat, "Bloodborne"), work_id(cat, "Donkey Kong Bananza")
        a, b = (p["id"] for p in cat["persons"][:2])
        page = self.open(self.context(), "/games")
        # Someone else changes the catalog after the page loaded it.
        self.assertEqual(self.server.put("/api/play-state", {"person": b, "work": dk, "status": "playing"}).status, 200)
        statuses = []
        page.on("response", lambda r: statuses.append(r.status) if r.url.endswith("/api/play-state") else None)
        page.select_option(f'select.status[data-work="{bb}"]', "backlog")
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % bb)
        self.assertEqual(statuses, [412, 200])
        got = {(s["person"], s["work"]): s["status"] for s in self.catalog()["play_state"]}
        self.assertEqual(got, {(b, dk): "playing", (a, bb): "backlog"})
        # The refetched catalog is shown: Player B's change is there.
        page.select_option("#person", str(b))
        self.assertEqual(page.input_value(f'select.status[data-work="{dk}"]'), "playing")
        self.assert_clean(allow=("HTTP 412", CHROME_HTTP_ERROR))

    def test_status_save_works_behind_a_proxy_that_weakens_etags(self):
        # Cloudflare (Respect Strong ETags off, the default) serves the catalog's ETag as W/"..." to visitors
        # that accept gzip; the page sends that value back in If-Match and the save must still apply.
        self.serve()
        cat = self.catalog()
        bb = work_id(cat, "Bloodborne")
        a = cat["persons"][0]["id"]
        ctx = self.context()
        seen = []

        def weaken(route):
            resp = route.fetch()
            headers = dict(resp.headers)
            if "etag" in headers and not headers["etag"].startswith("W/"):
                headers["etag"] = "W/" + headers["etag"]
            route.fulfill(response=resp, headers=headers)
        ctx.route("**/api/catalog", weaken)
        ctx.route("**/api/play-state", weaken)
        page = self.open(ctx, "/games")
        page.on("request", lambda r: seen.append(r.headers.get("if-match")) if r.url.endswith("/api/play-state") else None)
        statuses = []
        page.on("response", lambda r: statuses.append(r.status) if r.url.endswith("/api/play-state") else None)
        page.select_option(f'select.status[data-work="{bb}"]', "finished")
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % bb)
        self.assertEqual(statuses, [200])
        self.assertTrue(seen and seen[0].startswith("W/"), seen)
        # A second save sends the weak ETag from the first save's response.
        page.select_option(f'select.status[data-work="{bb}"]', "playing")
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').value === 'playing'"
                               " && document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % (bb, bb))
        self.assertEqual(statuses, [200, 200])
        self.assertEqual([(s["person"], s["work"], s["status"]) for s in self.catalog()["play_state"]],
                         [(a, bb, "playing")])
        self.assert_clean()

    def test_status_select_keeps_keyboard_focus_after_saving(self):
        self.serve()
        cat = self.catalog()
        bb = work_id(cat, "Bloodborne")
        a = cat["persons"][0]["id"]
        page = self.open(self.context(), "/games")
        sel = f'select.status[data-work="{bb}"]'
        page.focus(sel)
        with page.expect_response(lambda r: r.url.endswith("/api/play-state")) as resp:
            page.keyboard.press("ArrowDown")
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % bb)
        self.assertEqual(page.evaluate("[document.activeElement.className, document.activeElement.dataset.work]"),
                         ["status", str(bb)])
        self.assertEqual(page.input_value(sel), "backlog")
        self.assertEqual([(s["person"], s["work"], s["status"]) for s in self.catalog()["play_state"]],
                         [(a, bb, "backlog")])
        self.assert_clean()

    def test_save_error_is_shown_inline(self):
        self.serve()
        cat = self.catalog()
        bb = work_id(cat, "Bloodborne")
        ctx = self.context()
        ctx.route("**/api/play-state", lambda r: r.fulfill(
            status=400, content_type="application/json",
            body='{"error": "bad_request", "detail": "test detail from the server"}'))
        page = self.open(ctx, "/games")
        page.select_option(f'select.status[data-work="{bb}"]', "playing")
        page.wait_for_selector(f'tr.work-row[data-work="{bb}"] .err:has-text("test detail from the server")')
        self.assertEqual(page.input_value(f'select.status[data-work="{bb}"]'), "", "the select goes back")
        self.assert_clean(allow=("HTTP 400", CHROME_HTTP_ERROR))

    def test_page_works_without_storage(self):
        self.serve()
        cat = self.catalog()
        ctx = self.context(init_script=(
            "Object.defineProperty(window, 'localStorage', {configurable: true,"
            " get() { throw new DOMException('denied', 'SecurityError'); }});"))
        page = self.open(ctx, "/games")
        self.assertEqual(self.rows(page, "games"),
                         len({c["work_id"] for c in active(cat) if c["work_id"] is not None}))
        self.assertEqual(page.input_value("#person"), str(cat["persons"][0]["id"]))
        page.select_option("#person", str(cat["persons"][1]["id"]))
        self.assertEqual(page.input_value("#person"), str(cat["persons"][1]["id"]))
        self.assert_clean()

    def test_phone_width_has_no_horizontal_scroll(self):
        self.serve()
        ctx = self.context(width=390, height=844)
        for path in ("/games", "/items"):
            with self.subTest(path=path):
                page = self.open(ctx, path)
                page.click("#filtersBtn")
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                page.close()
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
