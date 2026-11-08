"""The Games and Items filters in a real browser: URL hash in, URL hash out, and the counts they give.

Each filter is set through the URL hash, as a bookmark would, or through the panel's controls, and the page's
#count is compared with a count worked out here in Python from GET /api/catalog. In Games a work matches when any
of its copies matches; in Items each copy is a row.

Set BROWSER_EXECUTABLE to use an installed Chrome, and REQUIRE_BROWSER=1 (CI does) to make a missing Playwright an
error instead of a skip.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from _browser import READY, BrowserCase, active, meta_of, platform_id, work_id  # noqa: E402
from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows  # noqa: E402
from _support import multipart  # noqa: E402


def games(copies) -> int:
    return len({c["work_id"] for c in copies if c["work_id"] is not None})


class Filters(BrowserCase):
    def setUp(self):
        super().setUp()
        self.serve()
        self.cat = self.catalog()
        self.ctx = self.context()

    def check(self, hash_: str, predicate, *, copies=None, nonzero=True):
        """Open Games and Items with the hash; both counts must equal what the predicate gives."""
        pool = active(self.cat) if copies is None else copies
        hit = [c for c in pool if predicate(c)]
        if nonzero:
            self.assertGreater(len(hit), 0, "the fixture should have copies for this filter: " + hash_)
        for view, want in (("games", games(hit)), ("items", len(hit))):
            with self.subTest(hash=hash_, view=view):
                page = self.open(self.ctx, f"/{view}#{hash_}")
                self.assertEqual(self.count(page), want)
                self.assertEqual(self.rows(page, view), want)
                page.close()
        self.assert_clean()
        return hit

    def meta(self, c, field):
        return meta_of(self.cat, c).get(field)

    # ---- the cases the plan names
    def test_filter_from_hash(self):
        page = self.open(self.ctx, "/items#cat=system")
        self.assertEqual(self.rows(page, "items"), 1)
        self.assertEqual(self.count(page), 1)
        self.assertIn("Nintendo Switch 2", page.inner_text("#view-items tr.copy-row"))
        self.assert_clean()

    def test_filters_round_trip_url(self):
        ps4 = platform_id(self.cat, "Sony PlayStation 4")
        want = sum(1 for c in active(self.cat) if c["platform_id"] == ps4 and c["category"] == "game")
        page = self.open(self.ctx, "/items")
        page.click("#filtersBtn")
        page.click(f'button[data-fa="p"][data-fv="{ps4}"]')
        page.click('button[data-fa="cat"][data-fv="game"]')
        self.assertEqual(self.count(page), want)
        h = parse_qs(page.evaluate("location.hash").lstrip("#"))
        self.assertEqual(h, {"p": [str(ps4)], "cat": ["game"]})
        page.reload()
        page.wait_for_function(READY)
        page.click("#filtersBtn")
        self.assertEqual(page.get_attribute(f'button[data-fa="p"][data-fv="{ps4}"]', "aria-pressed"), "true")
        self.assertEqual(page.get_attribute('button[data-fa="cat"][data-fv="game"]', "aria-pressed"), "true")
        self.assertEqual(page.get_attribute('button[data-fa="cat"][data-fv="system"]', "aria-pressed"), "false")
        self.assertEqual(self.count(page), want)
        self.assertEqual(self.rows(page, "items"), want)
        # Clear filters empties the hash.
        page.click("#clearBtn")
        self.assertEqual(page.evaluate("location.hash"), "")
        self.assertEqual(self.count(page), len(active(self.cat)))
        self.assert_clean()

    def test_include_unknown_metacritic(self):
        known = self.check("mcmin=80", lambda c: (self.meta(c, "metacritic") or 0) >= 80)
        wider = self.check("mcmin=80&mcUnk=1", lambda c: self.meta(c, "metacritic") is None
                           or self.meta(c, "metacritic") >= 80)
        self.assertGreater(len(wider), len(known))
        self.check("mcmax=90", lambda c: self.meta(c, "metacritic") is not None and self.meta(c, "metacritic") <= 90)
        self.check("mcmin=80&mcmax=90", lambda c: self.meta(c, "metacritic") is not None
                   and 80 <= self.meta(c, "metacritic") <= 90)

    # ---- the other dimensions
    def test_platform_family_and_condition(self):
        self.check("fam=nintendo", lambda c: self.platform(c)["family"] == "nintendo")
        ps4, xbox = platform_id(self.cat, "Sony PlayStation 4"), platform_id(self.cat, "Microsoft Xbox")
        self.check(f"p={ps4}&p={xbox}", lambda c: c["platform_id"] in (ps4, xbox))
        self.check("cond=Boxed", lambda c: c["condition"] == "Boxed")
        self.check("cond=Boxed&cond=CIB&cat=game", lambda c: c["condition"] in ("Boxed", "CIB")
                   and c["category"] == "game")
        self.check("form=physical", lambda c: c["form"] == "physical")
        self.check("ed=no", lambda c: not c["edition"])

    def test_genre_and_publisher_with_unknown(self):
        self.check("g=Action", lambda c: "Action" in (self.meta(c, "genre") or []))
        self.check("g=Action&gUnk=1", lambda c: not self.meta(c, "genre") or "Action" in self.meta(c, "genre"))
        self.check("g=Platformer&g=Adventure", lambda c: {"Platformer", "Adventure"} & set(self.meta(c, "genre") or []))
        self.check("pub=Test+Publisher+C", lambda c: self.meta(c, "publisher") == "Test Publisher C")
        self.check("pub=Test+Publisher+C&pubUnk=1",
                   lambda c: self.meta(c, "publisher") in (None, "Test Publisher C"))

    def test_text_search(self):
        self.check("q=bloodb", lambda c: "bloodb" in c["raw_title"].lower())
        self.check("q=switch+2", lambda c: self.platform(c)["name"] == "Nintendo Switch 2"
                   or "switch 2" in c["raw_title"].lower())

    def test_status_filter_uses_the_selected_person(self):
        a, b = (p["id"] for p in self.cat["persons"][:2])
        bb, dk = work_id(self.cat, "Bloodborne"), work_id(self.cat, "Donkey Kong Bananza")
        for person, work, status in ((a, bb, "finished"), (b, dk, "playing")):
            self.assertEqual(self.server.put("/api/play-state", {"person": person, "work": work,
                                                                 "status": status}).status, 200)
        page = self.open(self.ctx, "/games#st=finished")
        self.assertEqual(self.count(page), 1)
        self.assertIn("Bloodborne", page.inner_text("#view-games"))
        page.select_option("#person", str(b))
        self.assertEqual(self.count(page), 0)
        self.assertIn("No games match", page.inner_text("#view-games"))
        page.close()
        # Untriaged for Player B: every game work but Donkey Kong Bananza. Hardware has no status.
        self.check("st=none", lambda c: c["work_id"] is not None and c["work_id"] != dk)
        self.check("st=none&st=playing", lambda c: c["work_id"] is not None)

    # The focused element's tag and identifying data attributes (data-fam is only a colour marker).
    ACTIVE = ("(() => { const e = document.activeElement; const d = {...e.dataset}; delete d.fam;"
              " return [e.tagName, d]; })()")

    def test_keyboard_focus_survives_a_filter_change(self):
        ps4 = platform_id(self.cat, "Sony PlayStation 4")
        page = self.open(self.ctx, "/items")
        page.click("#filtersBtn")
        btn = f'button[data-fa="p"][data-fv="{ps4}"]'
        page.focus(btn)
        page.keyboard.press("Space")
        self.assertEqual(parse_qs(page.evaluate("location.hash").lstrip("#")), {"p": [str(ps4)]})
        self.assertEqual(self.count(page), sum(1 for c in active(self.cat) if c["platform_id"] == ps4))
        self.assertEqual(page.evaluate(self.ACTIVE), ["BUTTON", {"fa": "p", "fv": str(ps4)}])
        self.assertEqual(page.get_attribute(btn, "aria-pressed"), "true")
        # Tab moves on from the same place, not from the top of the page.
        page.keyboard.press("Tab")
        self.assertNotEqual(page.evaluate("document.activeElement.tagName"), "BODY")
        # A checkbox and a select keep focus too.
        page.focus('input[data-fb="mcUnk"]')
        page.keyboard.press("Space")
        self.assertIn("mcUnk=1", page.evaluate("location.hash"))
        self.assertEqual(page.evaluate(self.ACTIVE), ["INPUT", {"fb": "mcUnk"}])
        page.focus('select[data-fs="mcmin"]')
        page.keyboard.press("ArrowDown")
        page.wait_for_function("location.hash.includes('mcmin=')")
        self.assertEqual(page.evaluate(self.ACTIVE), ["SELECT", {"fs": "mcmin"}])
        self.assert_clean()

    def test_publisher_search_keeps_focus_and_caret(self):
        page = self.open(self.ctx, "/games")
        page.click("#filtersBtn")
        page.fill('input[data-ps="pub"]', "publisher c")
        page.press('input[data-ps="pub"]', "ArrowLeft")
        page.press('input[data-ps="pub"]', "ArrowLeft")
        # A filter change from outside (a hashchange, as from a bookmark) re-renders the panel.
        page.evaluate("location.hash = 'g=Action'")
        page.wait_for_function("document.querySelector('button[data-fa=\"g\"][data-fv=\"Action\"]')"
                               ".getAttribute('aria-pressed') === 'true'")
        self.assertEqual(page.evaluate(self.ACTIVE), ["INPUT", {"ps": "pub"}])
        self.assertEqual(page.evaluate("[document.activeElement.selectionStart, document.activeElement.value]"),
                         [len("publisher c") - 2, "publisher c"])
        self.assert_clean()

    def test_invalid_values_in_the_hash_are_ignored(self):
        page = self.open(self.ctx, "/items#cat=bogus&mcmin=abc&p=99999&st=nope&ed=maybe&cat=system")
        self.assertEqual(self.count(page), 1)
        self.assertEqual(parse_qs(page.evaluate("location.hash").lstrip("#")), {"cat": ["system"]})
        self.assert_clean()

    def test_show_removed(self):
        # A second backup without the unmapped game: its copy is removed (1 of 7 is under the removal guard).
        gone = next(r for r in FIXTURE_ROWS if r["title"] == "Test Unmapped Game")
        rows = [r for r in FIXTURE_ROWS if r is not gone]
        ctype, body = multipart({"ged": ("b.ged", build_ged(rows)), "csv": ("b.csv", build_csv(fixture_csv_rows()))})
        staged = self.server.post("/api/import/gameeye", body, {"Content-Type": ctype})
        self.assertEqual(staged.status, 200, staged.text())
        applied = self.server.post(f"/api/import/{staged.json()['import_id']}/apply", {})
        self.assertEqual(applied.status, 200, applied.text())
        self.cat = self.catalog()
        everything = self.cat["copies"]
        self.assertEqual(len(everything) - len(active(self.cat)), 1)
        self.check("", lambda c: c["removed_at"] is None, nonzero=True)
        self.check("rm=1", lambda c: True, copies=everything)
        page = self.open(self.ctx, "/items#rm=1")
        removed = next(c for c in everything if c["removed_at"] is not None)
        self.assertIn("removed", page.get_attribute(f'tr.copy-row[data-copy="{removed["id"]}"]', "class"))
        self.assert_clean()

    def platform(self, c):
        return next(p for p in self.cat["platforms"] if p["id"] == c["platform_id"])


if __name__ == "__main__":
    unittest.main()
