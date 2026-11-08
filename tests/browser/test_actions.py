"""Row actions and the Up next and Duplicates views in a real browser (Playwright + Chromium).

What it proves: a note edited in a Games row detail is saved for that person and survives a reload; the note editor
is only enabled for a person with a status, and clearing a status that has a note asks first (inline, no dialog);
the save queue keeps going after one save's post-processing throws; Move up on Up next sends the reordered list,
and a 409 (the list changed elsewhere) refetches and shows the current list; "These are separate copies" hides a
duplicate group; a copy split out to a new title becomes its own Games row and merging it back folds it in again;
a removed copy's removal reason is editable in Items; catalog strings are shown as text, never markup; and the new
views fit a phone screen.

The server is seeded with the fixture backup and CSV (tests/_seed.py): 7 active copies, 4 works, duplicate groups
Bloodborne x2 (PS4) and Dead to Rights x2 (Xbox), persons Player A and Player B. Expectations come from
GET /api/catalog.

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

from _browser import CHROME_HTTP_ERROR, READY, BrowserCase, active, work_id  # noqa: E402
from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows  # noqa: E402
from _support import multipart  # noqa: E402

PLAY_PUT = lambda r: r.url.endswith("/api/play-state") and r.request.method == "PUT"  # noqa: E731


class Actions(BrowserCase):
    def setUp(self):
        super().setUp()
        self.serve()
        self.cat = self.catalog()
        self.a, self.b = (p["id"] for p in self.cat["persons"][:2])
        self.bb = work_id(self.cat, "Bloodborne")
        self.dk = work_id(self.cat, "Donkey Kong Bananza")
        self.dtr = work_id(self.cat, "Dead to Rights")

    def play(self, person, work, status, **kw):
        r = self.server.put("/api/play-state", {"person": person, "work": work, "status": status, **kw})
        self.assertEqual(r.status, 200, r.text())

    def state(self) -> dict:
        return {(s["person"], s["work"]): (s["status"], s["note"]) for s in self.catalog()["play_state"]}

    def expand(self, page, work):
        page.click(f'tr.work-row[data-work="{work}"] button.disclose')
        page.wait_for_selector(f'tr.detail-row[data-detail="w{work}"]')

    def games_rows(self, page) -> list[str]:
        return page.eval_on_selector_all("#view-games tr.work-row button.disclose", "els => els.map(e => e.textContent.trim())")

    # ---- notes and status
    def test_note_edit_persists(self):
        page = self.open(self.context(), "/games")
        self.expand(page, self.bb)
        note = f'textarea[data-note="{self.b}:{self.bb}"]'
        self.assertTrue(page.is_disabled(note), "no status yet: the note editor is off")
        self.assertIn("Set a status", page.inner_text(f'tr.detail-row[data-detail="w{self.bb}"]'))
        with page.expect_response(PLAY_PUT) as resp:
            page.select_option(f'select.status[data-dwork="{self.bb}"][data-person="{self.b}"]', "backlog")
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function(f"!document.querySelector('{note}').disabled")
        page.fill(note, "Borrowed from <a friend> & due back")
        self.assertIn(f"{len('Borrowed from <a friend> & due back')} / 2000",
                      page.inner_text(f'tr.detail-row[data-detail="w{self.bb}"]'))
        with page.expect_response(PLAY_PUT) as resp:
            page.click(f'button[data-savenote="{self.b}:{self.bb}"]')
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(self.state(), {(self.b, self.bb): ("backlog", "Borrowed from <a friend> & due back")})
        page.reload()
        page.wait_for_function(READY)
        self.expand(page, self.bb)
        self.assertEqual(page.input_value(note), "Borrowed from <a friend> & due back")
        self.assertEqual(page.input_value(f'textarea[data-note="{self.a}:{self.bb}"]'), "")
        self.assertTrue(page.is_disabled(f'textarea[data-note="{self.a}:{self.bb}"]'))
        self.assert_clean()

    def test_clearing_a_status_with_a_note_asks_first(self):
        self.play(self.a, self.bb, "playing", note="Chalice dungeons next")
        page = self.open(self.context(), "/games")
        sent = []
        page.on("request", lambda r: sent.append(r.method) if r.url.endswith("/api/play-state") else None)
        sel = f'select.status[data-work="{self.bb}"]'
        page.select_option(sel, "")
        page.wait_for_selector(f'button[data-clearyes="{self.a}:{self.bb}"]')
        self.assertIn("This also deletes the note", page.inner_text(f'tr.work-row[data-work="{self.bb}"]'))
        self.assertEqual(sent, [], "nothing is sent before the confirmation")
        self.assertEqual(page.input_value(sel), "playing")
        page.click(f'button[data-clearno="{self.a}:{self.bb}"]')
        self.assertEqual(page.locator("button[data-clearyes]").count(), 0)
        self.assertEqual(self.state(), {(self.a, self.bb): ("playing", "Chalice dungeons next")})
        page.select_option(sel, "")
        with page.expect_response(PLAY_PUT) as resp:
            page.click(f'button[data-clearyes="{self.a}:{self.bb}"]')
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % self.bb)
        self.assertEqual(page.input_value(sel), "")
        self.assertEqual(self.state(), {})
        # Without a note there is nothing to lose: clearing goes straight through.
        self.play(self.a, self.dk, "backlog")
        page.reload()
        page.wait_for_function(READY)
        with page.expect_response(PLAY_PUT) as resp:
            page.select_option(f'select.status[data-work="{self.dk}"]', "")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(self.state(), {})
        self.assert_clean()

    def test_save_queue_survives_a_throw(self):
        page = self.open(self.context(), "/games")
        # The next style.setProperty call (the header colour, set on every render) throws once.
        page.evaluate("""() => {
          const orig = CSSStyleDeclaration.prototype.setProperty;
          window.__boom = true;
          CSSStyleDeclaration.prototype.setProperty = function (...a) {
            if (window.__boom) { window.__boom = false; throw new Error('boom in render'); }
            return orig.apply(this, a);
          };
        }""")
        with page.expect_response(PLAY_PUT) as resp:
            page.select_option(f'select.status[data-work="{self.bb}"]', "finished")
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function("window.__boom === false")
        with page.expect_response(PLAY_PUT, timeout=5000) as resp:
            page.select_option(f'select.status[data-work="{self.dk}"]', "playing")
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function("document.querySelector('select.status[data-work=\"%d\"]').dataset.saved === '1'" % self.dk)
        self.assertEqual(self.state(), {(self.a, self.bb): ("finished", None), (self.a, self.dk): ("playing", None)})
        self.assert_clean(allow=("a save failed",))

    # ---- Up next
    def up_next_titles(self, page) -> list[int]:
        return [int(x) for x in page.eval_on_selector_all("#view-up-next li[data-uwork]", "els => els.map(e => e.dataset.uwork)")]

    def test_up_next_move_up_persists(self):
        self.play(self.a, self.bb, "up_next")
        self.play(self.a, self.dk, "up_next")
        self.assertEqual(self.catalog()["up_next"], {str(self.a): [self.bb, self.dk]})
        page = self.open(self.context(), "/up-next")
        self.assertEqual(self.up_next_titles(page), [self.bb, self.dk])
        self.assertTrue(page.is_disabled(f'button[data-move="up"][data-mwork="{self.bb}"]'))
        self.assertTrue(page.is_disabled(f'button[data-move="down"][data-mwork="{self.dk}"]'))
        with page.expect_response(lambda r: r.url.endswith("/api/up-next")) as resp:
            page.click(f'button[data-move="up"][data-mwork="{self.dk}"]')
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"person": self.a, "order": [self.dk, self.bb]})
        self.assertEqual(self.catalog()["up_next"], {str(self.a): [self.dk, self.bb]})
        self.assertEqual(self.up_next_titles(page), [self.dk, self.bb])
        # The moved item's button is disabled now (it is first), so focus stays on the item's other button.
        page.wait_for_function("document.activeElement.dataset.mwork === '%d'" % self.dk)
        page.reload()
        page.wait_for_function(READY)
        self.assertEqual(self.up_next_titles(page), [self.dk, self.bb])
        # Player B has nothing up next.
        page.select_option("#person", str(self.b))
        self.assertEqual(self.up_next_titles(page), [])
        self.assertIn("Nothing is up next for Player B", page.inner_text("#view-up-next"))
        self.assert_clean()

    def test_note_save_keeps_up_next_position(self):
        self.play(self.a, self.bb, "up_next")
        self.play(self.a, self.dk, "up_next")
        page = self.open(self.context(), "/games")
        self.expand(page, self.bb)
        page.fill(f'textarea[data-note="{self.a}:{self.bb}"]', "First in line")
        with page.expect_response(PLAY_PUT) as resp:
            page.click(f'button[data-savenote="{self.a}:{self.bb}"]')
        self.assertEqual(resp.value.status, 200)
        page.wait_for_selector(f'tr.detail-row[data-detail="w{self.bb}"] .ok')
        # The server keeps Bloodborne first; the page (no reload) must show the same order.
        self.assertEqual(self.catalog()["up_next"], {str(self.a): [self.bb, self.dk]})
        page.click('header a[data-view="up-next"]')
        self.assertEqual(self.up_next_titles(page), [self.bb, self.dk])
        with page.expect_response(lambda r: r.url.endswith("/api/up-next")) as resp:
            page.click(f'button[data-move="down"][data-mwork="{self.bb}"]')
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"person": self.a, "order": [self.dk, self.bb]})
        self.assertEqual(self.catalog()["up_next"], {str(self.a): [self.dk, self.bb]})
        self.assertEqual(self.up_next_titles(page), [self.dk, self.bb])
        self.assert_clean()

    def test_up_next_conflict_refetches(self):
        self.play(self.a, self.bb, "up_next")
        self.play(self.a, self.dk, "up_next")
        page = self.open(self.context(), "/up-next")
        self.play(self.a, self.dtr, "up_next")  # someone else adds one after the page loaded
        with page.expect_response(lambda r: r.url.endswith("/api/up-next")) as resp:
            page.click(f'button[data-move="up"][data-mwork="{self.dk}"]')
        self.assertEqual(resp.value.status, 409)
        page.wait_for_function("document.querySelectorAll('#view-up-next li[data-uwork]').length === 3")
        self.assertEqual(self.up_next_titles(page), [self.bb, self.dk, self.dtr])
        self.assertIn("changed", page.inner_text("#view-up-next"))
        with page.expect_response(lambda r: r.url.endswith("/api/up-next")) as resp:
            page.click(f'button[data-move="down"][data-mwork="{self.bb}"]')
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(self.catalog()["up_next"], {str(self.a): [self.dk, self.bb, self.dtr]})
        self.assert_clean(allow=("HTTP 409", CHROME_HTTP_ERROR))

    # ---- Duplicates
    def test_duplicates_ack_hides(self):
        groups = self.cat["dup_groups"]
        self.assertEqual(len(groups), 2)
        page = self.open(self.context(), "/duplicates")
        self.assertEqual(page.locator("#view-duplicates tr.dup-row").count(), 2)
        text = page.inner_text("#view-duplicates")
        self.assertIn("Bloodborne", text)
        self.assertIn("Dead to Rights", text)
        self.assertIn("Boxed", text)
        g = next(x for x in groups if x["work"] == self.bb)
        with page.expect_response(lambda r: r.url.endswith("/api/dup-ack")) as resp:
            page.click(f'button[data-ack="{g["work"]}:{g["platform"]}"]')
        self.assertEqual(resp.value.status, 200)
        page.wait_for_function("document.querySelectorAll('#view-duplicates tr.dup-row').length === 1")
        page.reload()
        page.wait_for_function(READY)
        self.assertEqual(page.locator("#view-duplicates tr.dup-row").count(), 1)
        self.assertNotIn("Bloodborne", page.inner_text("#view-duplicates"))
        self.assertEqual([x["work"] for x in self.catalog()["dup_groups"]], [self.dtr])
        self.assert_clean()

    # ---- merge and split
    def test_merge_and_split(self):
        page = self.open(self.context(), "/games#q=bloodborne")
        self.assertEqual(self.games_rows(page), ["Bloodborne"])
        self.expand(page, self.bb)
        cib = next(c for c in self.cat["copies"] if c["work_id"] == self.bb and c["condition"] == "CIB")
        page.click(f'button[data-split="{cib["id"]}"]')
        title = f'input[data-splitt="{cib["id"]}"]'
        self.assertEqual(page.input_value(title), "Bloodborne", "prefilled with the copy's raw title")
        page.fill(title, "Bloodborne (second copy)")
        with page.expect_response(lambda r: r.url.endswith("/api/work/split")) as resp:
            page.click(f'button[data-splitgo="{cib["id"]}"]')
        self.assertEqual(resp.value.status, 200)
        new = resp.value.json()["work"]
        page.wait_for_function("document.querySelectorAll('#view-games tr.work-row').length === 2")
        self.assertEqual(sorted(self.games_rows(page)), ["Bloodborne", "Bloodborne (second copy)"])
        self.assertEqual(next(c for c in self.catalog()["copies"] if c["id"] == cib["id"])["work_id"], new)
        # Merge the new work back into Bloodborne, picked by searching titles.
        page.wait_for_selector(f'tr.detail-row[data-detail="w{new}"]')
        page.fill(f'input[data-mergeq="{new}"]', "blood")
        page.click(f'tr.detail-row[data-detail="w{new}"] button[data-mergepick="{self.bb}"]')
        with page.expect_response(lambda r: r.url.endswith("/api/work/merge")) as resp:
            page.click(f'button[data-mergego="{new}"]')
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"into": self.bb, "from": new})
        page.wait_for_function("document.querySelectorAll('#view-games tr.work-row').length === 1")
        self.assertEqual(self.games_rows(page), ["Bloodborne"])
        cat = self.catalog()
        self.assertNotIn(new, [w["id"] for w in cat["works"]])
        self.assertEqual(sorted(c["id"] for c in cat["copies"] if c["work_id"] == self.bb),
                         sorted(c["id"] for c in self.cat["copies"] if c["work_id"] == self.bb))
        self.assert_clean()

    def test_merge_search_excludes_the_work_itself(self):
        page = self.open(self.context(), "/games")
        self.expand(page, self.bb)
        page.fill(f'input[data-mergeq="{self.bb}"]', "d")
        picks = page.eval_on_selector_all(f'tr.detail-row[data-detail="w{self.bb}"] button[data-mergepick]',
                                          "els => els.map(e => Number(e.dataset.mergepick))")
        self.assertNotIn(self.bb, picks)
        self.assertIn(self.dk, picks)
        self.assertIn(self.dtr, picks)
        self.assert_clean()

    # ---- Items
    def remove_one(self) -> dict:
        gone = next(r for r in FIXTURE_ROWS if r["title"] == "Test Unmapped Game")
        rows = [r for r in FIXTURE_ROWS if r is not gone]
        ctype, body = multipart({"ged": ("b.ged", build_ged(rows)), "csv": ("b.csv", build_csv(fixture_csv_rows()))})
        staged = self.server.post("/api/import/gameeye", body, {"Content-Type": ctype})
        self.assertEqual(staged.status, 200, staged.text())
        applied = self.server.post(f"/api/import/{staged.json()['import_id']}/apply", {})
        self.assertEqual(applied.status, 200, applied.text())
        return next(c for c in self.catalog()["copies"] if c["removed_at"] is not None)

    def test_removed_reason_editable(self):
        removed = self.remove_one()
        keep = active(self.catalog())[0]
        page = self.open(self.context(), "/items#rm=1")
        page.click(f'tr.copy-row[data-copy="{keep["id"]}"] button.disclose')
        detail = page.inner_text(f'tr.detail-row[data-detail="c{keep["id"]}"]')
        self.assertIn(keep["source_id"], detail)
        self.assertEqual(page.locator(f'select[data-reason="{keep["id"]}"]').count(), 0, "active copies have no reason")
        page.click(f'tr.copy-row[data-copy="{removed["id"]}"] button.disclose')
        detail = page.inner_text(f'tr.detail-row[data-detail="c{removed["id"]}"]')
        for want in (removed["source_id"], "gameeye", "199"):
            self.assertIn(want, detail)
        with page.expect_response(lambda r: r.url.endswith(f"/api/copy/{removed['id']}")) as resp:
            page.select_option(f'select[data-reason="{removed["id"]}"]', "sold")
        self.assertEqual(resp.value.status, 200)
        self.assertEqual(resp.value.request.post_data_json, {"removed_reason": "sold"})
        got = lambda: next(c for c in self.catalog()["copies"] if c["id"] == removed["id"])["removed_reason"]  # noqa: E731
        self.assertEqual(got(), "sold")
        page.wait_for_selector(f'tr.copy-row[data-copy="{removed["id"]}"] .tag:has-text("sold")')
        page.reload()
        page.wait_for_function(READY)
        page.click(f'tr.copy-row[data-copy="{removed["id"]}"] button.disclose')
        self.assertEqual(page.input_value(f'select[data-reason="{removed["id"]}"]'), "sold")
        with page.expect_response(lambda r: r.url.endswith(f"/api/copy/{removed['id']}")) as resp:
            page.select_option(f'select[data-reason="{removed["id"]}"]', "")
        self.assertEqual(resp.value.request.post_data_json, {"removed_reason": None})
        self.assertIsNone(got())
        self.assert_clean()

    # ---- strings and layout
    def test_catalog_strings_are_text(self):
        evil = '<img src=x onerror="window.__xss=1">Markup & Co'
        rows = FIXTURE_ROWS + [dict(FIXTURE_ROWS[0], uuid="00000000-0000-4000-8000-000000000009", item_id=2009,
                                    title=evil)]
        ctype, body = multipart({"ged": ("b.ged", build_ged(rows))})
        staged = self.server.post("/api/import/gameeye", body, {"Content-Type": ctype})
        self.assertEqual(self.server.post(f"/api/import/{staged.json()['import_id']}/apply", {}).status, 200)
        w = work_id(self.catalog(), evil)
        self.play(self.a, w, "up_next", note='<b>bold</b> "quoted"')
        page = self.open(self.context(), "/games")
        self.expand(page, w)
        self.assertIn(evil, page.inner_text(f'tr.work-row[data-work="{w}"]'))
        self.assertEqual(page.input_value(f'textarea[data-note="{self.a}:{w}"]'), '<b>bold</b> "quoted"')
        page.click('header a[data-view="up-next"]')
        self.assertIn(evil, page.inner_text("#view-up-next"))
        self.assertEqual(page.locator("#app img, #app b").count(), 0)
        self.assertIsNone(page.evaluate("window.__xss"))
        self.assert_clean()

    def test_phone_width_new_views(self):
        self.play(self.a, self.bb, "up_next", note="A long note " * 20)
        ctx = self.context(width=390, height=844)
        for path in ("/up-next", "/duplicates", "/import", "/games"):
            with self.subTest(path=path):
                page = self.open(ctx, path)
                if path == "/games":
                    self.expand(page, self.bb)
                    cp = next(c for c in self.cat["copies"] if c["work_id"] == self.bb)
                    page.click(f'button[data-split="{cp["id"]}"]')
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
                page.close()
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
