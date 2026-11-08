"""The Import view in a real browser: pick a backup (and CSV), stage it, read the diff, apply it.

What it proves: on an empty server, staging the fixture backup and CSV shows the summary counts, the warnings and
the changes grouped by operation, and Apply fills Games; re-staging the same backup says nothing changed; a backup
that drops most copies keeps Apply disabled until the removals are confirmed; and a stale or expired staged run is
explained inline with a way to stage again.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from _browser import CHROME_HTTP_ERROR, BrowserCase, active  # noqa: E402
from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows  # noqa: E402
from _support import multipart  # noqa: E402

STAGE = lambda r: r.url.endswith("/api/import/gameeye")  # noqa: E731
APPLY = lambda r: r.url.endswith("/apply")  # noqa: E731


class Import(BrowserCase):
    def files(self, rows=FIXTURE_ROWS, csv=True):
        d = self.server.tmp / "upload"
        d.mkdir(exist_ok=True)
        ged = d / f"backup-{len(rows)}.ged"
        if not ged.exists():  # zip entries carry a timestamp: reuse the bytes so the SHA-256 repeats
            ged.write_bytes(build_ged(rows))
        out = {"ged": str(ged)}
        if csv:
            c = d / "collection.csv"
            c.write_bytes(build_csv(fixture_csv_rows()))
            out["csv"] = str(c)
        return out

    def stage(self, page, files):
        page.set_input_files("#gedFile", files["ged"])
        if "csv" in files:
            page.set_input_files("#csvFile", files["csv"])
        with page.expect_response(STAGE) as resp:
            page.click("#stageBtn")
        page.wait_for_selector('#view-import:not([data-phase="staging"])', state="attached")
        return resp.value

    def server_import(self, rows):
        ctype, body = multipart({"ged": ("b.ged", build_ged(rows)), "csv": ("b.csv", build_csv(fixture_csv_rows()))})
        staged = self.server.post("/api/import/gameeye", body, {"Content-Type": ctype})
        self.assertEqual(staged.status, 200, staged.text())
        applied = self.server.post(f"/api/import/{staged.json()['import_id']}/apply", {})
        self.assertEqual(applied.status, 200, applied.text())

    def test_import_flow(self):
        self.serve(seed=False)
        page = self.open(self.context(), "/import")
        self.assertTrue(page.is_disabled("#stageBtn"), "nothing to stage without a backup")
        resp = self.stage(page, self.files())
        self.assertEqual(resp.status, 200)
        staged = resp.json()
        s = staged["summary"]
        view = page.inner_text("#view-import")
        self.assertIn(f"Added {s['added']}", view)
        self.assertIn("Added 7", view)
        self.assertIn(f"Skipped {s['skipped']}", view)
        self.assertIn(f"Enriched {s['csv']['enriched']}", view)
        for w in staged["warnings"]:
            self.assertIn(w["detail"], view)
        self.assertGreater(len(staged["warnings"]), 0)
        self.assertEqual(page.locator('#view-import li[data-op="add"]').count(), 7)
        self.assertIn("Donkey Kong Bananza", page.inner_text('#view-import [data-group="add"]'))
        self.assertEqual(page.locator("#confirmRm").count(), 0, "no removal guard on a first import")
        with page.expect_response(APPLY) as resp:
            page.click("#applyBtn")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"confirm_removals": False})
        page.wait_for_selector("#view-import .applied")
        self.assertIn("Applied", page.inner_text("#view-import"))
        cat = self.catalog()
        self.assertEqual(len(cat["copies"]), 7)
        page.click('header a[data-view="games"]')
        want = len({c["work_id"] for c in active(cat) if c["work_id"] is not None})
        self.assertEqual(want, 4)
        self.assertEqual(self.rows(page, "games"), want)
        # The same backup again, without the CSV: nothing to apply.
        page.click('header a[data-view="import"]')
        page.click("#restageBtn")
        resp = self.stage(page, self.files(csv=False))
        self.assertEqual(resp.json()["import_id"], None)
        self.assertIn("No changes since the last import", page.inner_text("#view-import"))
        self.assertEqual(page.locator("#applyBtn").count(), 0)
        self.assert_clean()

    def test_removal_guard_requires_confirm(self):
        self.serve(seed=False)
        self.server_import(FIXTURE_ROWS)
        page = self.open(self.context(), "/import")
        resp = self.stage(page, self.files(rows=FIXTURE_ROWS[:2]))
        staged = resp.json()
        self.assertTrue(staged["removal_guard"])
        n = staged["summary"]["removed"]
        self.assertGreater(n, 1)
        self.assertEqual(page.locator('#view-import li[data-op="remove"]').count(), n)
        self.assertTrue(page.is_disabled("#applyBtn"))
        self.assertIn(f"Yes, remove {n} copies", page.inner_text("#view-import"))
        page.check("#confirmRm")
        self.assertFalse(page.is_disabled("#applyBtn"))
        page.uncheck("#confirmRm")
        self.assertTrue(page.is_disabled("#applyBtn"))
        page.check("#confirmRm")
        with page.expect_response(APPLY) as resp:
            page.click("#applyBtn")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"confirm_removals": True})
        page.wait_for_selector("#view-import .applied")
        self.assertEqual(len(active(self.catalog())), 2)
        self.assert_clean()

    def test_stale_run_is_explained_with_a_way_back(self):
        self.serve(seed=False)
        self.server_import(FIXTURE_ROWS)
        page = self.open(self.context(), "/import")
        self.stage(page, self.files(rows=FIXTURE_ROWS[:7]))
        self.server_import(FIXTURE_ROWS)  # someone else applies an import meanwhile (CSV attached: it stages)
        with page.expect_response(APPLY) as resp:
            page.click("#applyBtn")
        self.assertEqual(resp.value.status, 409)
        err = page.inner_text("#view-import .err")
        self.assertIn("another import was applied", err)
        page.click("#restageBtn")
        self.assertTrue(page.is_visible("#gedFile"))
        self.assertEqual(page.locator("#applyBtn").count(), 0)
        self.assert_clean(allow=("HTTP 409", CHROME_HTTP_ERROR))

    def test_stage_error_is_inline(self):
        self.serve(seed=False)
        page = self.open(self.context(), "/import")
        bad = self.server.tmp / "not-a-backup.ged"
        bad.write_bytes(b"this is not a zip")
        page.set_input_files("#gedFile", str(bad))
        with page.expect_response(STAGE) as resp:
            page.click("#stageBtn")
        self.assertEqual(resp.value.status, 400)
        page.wait_for_selector("#view-import .err")
        self.assertFalse(page.is_disabled("#stageBtn"))
        self.assert_clean(allow=("HTTP 400", CHROME_HTTP_ERROR))


if __name__ == "__main__":
    unittest.main()
