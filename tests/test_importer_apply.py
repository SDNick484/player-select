from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from unittest import mock  # noqa: E402

from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows, ged_row  # noqa: E402
from ps import importer  # noqa: E402
from ps.catalog import set_play_state  # noqa: E402
from ps.importer import ImportFailure, apply, attach_work, stage  # noqa: E402
from test_importer_stage import StageBase, T0, u  # noqa: E402


class ApplyBase(StageBase):
    def apply(self, run_id, confirm=False, now=T0 + 10):
        return apply(self.store, self.maps, self.imports, run_id, confirm_removals=confirm, now=now)

    def stage_apply(self, rows=None, now=T0, **kw):
        res, ged = self.stage(rows, now=now)
        return self.apply(res["import_id"], now=now + 1, **kw)

    def rows_with(self, **overrides):
        out = []
        for r in FIXTURE_ROWS:
            out.append(dict(r, **overrides[r["uuid"]]) if r["uuid"] in overrides else r)
        return out

    def copy(self, n):
        return self.q("SELECT * FROM copy WHERE source_id=?", u(n))[0]


class ApplyFirstTest(ApplyBase):
    def test_apply_first_import(self):
        res, _ = self.stage()
        out = self.apply(res["import_id"])
        self.assertEqual(out, {"import_id": res["import_id"], "applied": True,
                               "summary": {"added": 7, "edited": 0, "removed": 0, "restored": 0,
                                           "unchanged": 0, "skipped": 1}})
        copies = self.q("SELECT * FROM copy")
        self.assertEqual(len(copies), 7)
        for c in copies:
            self.assertEqual((c["form"], c["account_id"], c["source"]), ("physical", None, "gameeye"))
            self.assertEqual(c["last_seen_import"], res["import_id"])
        a, b, system = self.copy(1), self.copy(2), self.copy(6)
        self.assertEqual(a["work_id"], b["work_id"])
        self.assertEqual(self.q("SELECT title FROM work WHERE id=?", a["work_id"])[0][0], "Bloodborne")
        self.assertIsNone(system["work_id"])
        self.assertEqual(a["created_at"], 1700000000)
        self.assertEqual(a["source_item_id"], "2001")
        self.assertEqual(len(self.q("SELECT 1 FROM platform WHERE name='GameEye platform 199 (unmapped)'")), 1)
        run = self.q("SELECT status, applied_at FROM import_run")[0]
        self.assertEqual((run[0], run[1]), ("applied", T0 + 10))
        self.assertEqual(list(self.imports.glob("*.ged")), [])

    def test_reimport_same_file_short_circuits(self):
        self.stage_apply()
        res, _ = self.stage(now=T0 + 100)
        self.assertEqual(res, {"import_id": None, "summary": {"unchanged": 7}})

    def test_reimport_same_file_with_csv_all_unchanged(self):
        self.stage_apply()
        csv = build_csv(fixture_csv_rows())
        res, _ = self.stage(csv=csv, now=T0 + 100)
        self.assertIsNotNone(res["import_id"])
        out = self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(out["summary"], {"added": 0, "edited": 0, "removed": 0, "restored": 0,
                                          "unchanged": 7, "skipped": 1,
                                          "csv": {"enriched": 4, "unmatched": 0, "ambiguous": 2}})
        self.assertEqual(list(self.imports.iterdir()), [])

    def test_last_seen_import_set_on_unchanged(self):
        self.stage_apply()
        rows = self.rows_with(**{u(5): {"title": "Donkey Kong Bananza 2"}})
        res, _ = self.stage(rows, now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        for n in (1, 2, 3, 4, 5, 6, 7):
            self.assertEqual(self.copy(n)["last_seen_import"], res["import_id"])


class ApplyGuardTest(ApplyBase):
    def fail(self, run_id, status, code, **kw):
        with self.assertRaises(ImportFailure) as cm:
            self.apply(run_id, **kw)
        self.assertEqual((cm.exception.status, cm.exception.code), (status, code))

    def test_stale_base_409(self):
        a, _ = self.stage()
        b, _ = self.stage(self.rows_with(**{u(1): {"title": "Bloodborne GOTY"}}))
        self.apply(a["import_id"])
        self.fail(b["import_id"], 409, "stale_base")

    def test_apply_twice_409(self):
        a, _ = self.stage()
        self.apply(a["import_id"])
        self.fail(a["import_id"], 409, "not_staged")

    def test_unknown_run_404(self):
        self.fail(999, 404, "not_found")

    def test_expired_410(self):
        a, _ = self.stage()
        self.fail(a["import_id"], 410, "expired", now=T0 + 3601)
        self.assertEqual(self.q("SELECT status FROM import_run")[0][0], "expired")
        self.fail(a["import_id"], 410, "expired", now=T0 + 3602)

    def test_missing_staged_file_410(self):
        a, _ = self.stage()
        for p in self.imports.glob("*.ged"):
            p.unlink()
        self.fail(a["import_id"], 410, "expired")
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy")[0][0], 0)

    def test_removal_guard_428_then_confirm(self):
        self.stage_apply()
        res, _ = self.stage(FIXTURE_ROWS[:3], now=T0 + 100)
        self.assertTrue(res["removal_guard"])
        self.fail(res["import_id"], 428, "confirm_removals", now=T0 + 110)
        self.assertEqual(self.q("SELECT status FROM import_run WHERE id=?", res["import_id"])[0][0],
                         "staged")
        out = self.apply(res["import_id"], confirm=True, now=T0 + 110)
        self.assertEqual(out["summary"]["removed"], 4)
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy WHERE removed_at IS NOT NULL")[0][0], 4)
        self.assertEqual(self.copy(5)["removed_at"], T0 + 110)


class ApplyEditTest(ApplyBase):
    def test_title_edit_reattaches_work(self):
        self.stage_apply()
        old = self.copy(5)["work_id"]
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Donkey Kong Bananza [Deluxe]"}}),
                            now=T0 + 100)
        out = self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(out["summary"]["edited"], 1)
        c = self.copy(5)
        self.assertEqual(c["raw_title"], "Donkey Kong Bananza [Deluxe]")
        self.assertEqual(c["edition"], "Deluxe")
        self.assertEqual(c["work_id"], old)
        # The work's only copy is retitled: the work is renamed in place (owner decision 2026-11-08).
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Something Else"}}), now=T0 + 200)
        self.apply(res["import_id"], now=T0 + 210)
        c = self.copy(5)
        self.assertEqual(c["work_id"], old)
        self.assertEqual(self.q("SELECT title FROM work WHERE id=?", c["work_id"])[0][0], "Something Else")
        # A copy whose work keeps another copy moves to a work of its own, as before.
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Something Else"},
                                              u(1): {"title": "Bloodborne Remastered"}}), now=T0 + 300)
        self.apply(res["import_id"], now=T0 + 310)
        self.assertNotEqual(self.copy(1)["work_id"], self.copy(2)["work_id"])

    def test_pinned_copy_keeps_work_on_title_edit(self):
        self.stage_apply()
        old = self.copy(5)["work_id"]
        with self.store.write() as c:
            c.execute("UPDATE copy SET work_pinned=1 WHERE source_id=?", (u(5),))
        counts = self.q("SELECT (SELECT COUNT(*) FROM work), (SELECT COUNT(*) FROM work_alias)")[0][:]
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Something Else [Gold]"}}), now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(self.q("SELECT (SELECT COUNT(*) FROM work), "
                                "(SELECT COUNT(*) FROM work_alias)")[0][:], counts)
        c = self.copy(5)
        self.assertEqual((c["work_id"], c["raw_title"], c["edition"], c["work_pinned"]),
                         (old, "Something Else [Gold]", "Gold", 1))

    def test_remove_and_restore(self):
        self.stage_apply()
        with self.store.write() as c:
            c.execute("UPDATE copy SET removed_at=1, removed_reason='sold' WHERE source_id=?", (u(4),))
        res, _ = self.stage(self.rows_with(**{u(1): {"updated_at": 1700999999}}), now=T0 + 100)
        out = self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(out["summary"]["restored"], 1)
        c = self.copy(4)
        self.assertIsNone(c["removed_at"])
        self.assertIsNone(c["removed_reason"])
        # now remove: drop row 4 from the backup
        rows = [r for r in FIXTURE_ROWS if r["uuid"] != u(4)]
        res, _ = self.stage(rows, now=T0 + 200)
        out = self.apply(res["import_id"], now=T0 + 210)
        self.assertEqual(out["summary"]["removed"], 1)
        self.assertEqual(self.copy(4)["removed_at"], T0 + 210)

    def test_pinned_restore_with_new_title_creates_no_work(self):
        self.stage_apply()
        old = self.copy(5)["work_id"]
        with self.store.write() as c:
            c.execute("UPDATE copy SET work_pinned=1, removed_at=1 WHERE source_id=?", (u(5),))
        counts = self.q("SELECT (SELECT COUNT(*) FROM work), (SELECT COUNT(*) FROM work_alias)")[0][:]
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Another Name"}}), now=T0 + 100)
        out = self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(out["summary"]["restored"], 1)
        c = self.copy(5)
        self.assertEqual((c["work_id"], c["raw_title"], c["removed_at"]), (old, "Another Name", None))
        self.assertEqual(self.q("SELECT (SELECT COUNT(*) FROM work), "
                                "(SELECT COUNT(*) FROM work_alias)")[0][:], counts)

    def test_restored_copy_work_keeps_play_state(self):
        self.stage_apply()
        wid = self.copy(5)["work_id"]
        with self.store.write() as c:
            c.execute("INSERT INTO person (name, color) VALUES ('Player A', '#111111')")
            pid = c.execute("SELECT id FROM person").fetchone()[0]
            c.execute("INSERT INTO play_state (person_id, work_id, status, updated_at) "
                      "VALUES (?, ?, 'finished', 1)", (pid, wid))
        rows = [r for r in FIXTURE_ROWS if r["uuid"] != u(5)]
        res, _ = self.stage(rows, now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        self.assertIsNotNone(self.copy(5)["removed_at"])
        res, _ = self.stage(now=T0 + 200)
        self.apply(res["import_id"], now=T0 + 210)
        c = self.copy(5)
        self.assertIsNone(c["removed_at"])
        self.assertEqual(c["work_id"], wid)
        self.assertEqual(self.q("SELECT status FROM play_state WHERE work_id=?", wid)[0][0], "finished")

    def test_category_change_game_to_accessory_and_back(self):
        self.stage_apply()
        res, _ = self.stage(self.rows_with(**{u(5): {"category_id": 5}}), now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        c = self.copy(5)
        self.assertEqual((c["category"], c["work_id"]), ("accessory", None))
        res, _ = self.stage(now=T0 + 200)
        self.apply(res["import_id"], now=T0 + 210)
        c = self.copy(5)
        self.assertEqual(c["category"], "game")
        self.assertIsNotNone(c["work_id"])

    def test_pinned_copy_becomes_non_game(self):
        self.stage_apply()
        with self.store.write() as c:
            c.execute("UPDATE copy SET work_pinned=1 WHERE source_id=?", (u(5),))
        res, _ = self.stage(self.rows_with(**{u(5): {"category_id": 5}}), now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        c = self.copy(5)
        self.assertEqual((c["work_id"], c["work_pinned"]), (None, 0))

    def test_platform_change_updates_platform_id(self):
        self.stage_apply()
        res, _ = self.stage(self.rows_with(**{u(5): {"platform_id": 46}}), now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        pid = self.q("SELECT id FROM platform WHERE gameeye_id=46")[0][0]
        c = self.copy(5)
        self.assertEqual((c["platform_id"], c["source_platform_id"]), (pid, 46))


class RetitleTest(ApplyBase):
    """Owner decision 2026-11-08: a retitle that would strand the old work carries its state along."""

    def persons(self):
        with self.store.write() as c:
            c.execute("INSERT INTO person (name, color, sort) VALUES ('Player A', '#111111', 0)")
            c.execute("INSERT INTO person (name, color, sort) VALUES ('Player B', '#222222', 1)")
            return [r[0] for r in c.execute("SELECT id FROM person ORDER BY sort")]

    def play(self, person, work, status, note=None, now=T0 + 20):
        with self.store.write() as c:
            set_play_state(c, person, work, status, note, now=now)

    def state(self):
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in self.q(
            "SELECT person_id, work_id, status, note, updated_at FROM play_state")}

    def up_next(self, person):
        return [r[0] for r in self.q("SELECT work_id FROM up_next_order WHERE person_id=? ORDER BY position",
                                     person)]

    def retitle(self, n, title, now=T0 + 100):
        res, _ = self.stage(self.rows_with(**{u(n): {"title": title}}), now=now)
        return self.apply(res["import_id"], now=now + 10)

    def title(self, wid):
        return self.q("SELECT title FROM work WHERE id=?", wid)[0][0]

    def test_typo_fix_keeps_status_note_and_up_next(self):
        self.stage_apply(self.rows_with(**{u(5): {"title": "Donky Kong Bananza"}}))
        a, b = self.persons()
        old = self.copy(5)["work_id"]
        bb = self.copy(1)["work_id"]
        self.play(a, old, "finished", "Beat it on the second try")
        self.play(b, bb, "up_next")
        self.play(b, old, "up_next")
        before = self.state()
        works = self.q("SELECT COUNT(*) FROM work")[0][0]
        out = self.retitle(5, "Donkey Kong Bananza")
        self.assertEqual(out["summary"]["edited"], 1)
        c = self.copy(5)
        self.assertEqual(c["work_id"], old, "the copy stays on its work")
        self.assertEqual(self.title(old), "Donkey Kong Bananza")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.up_next(b), [bb, old])
        self.assertEqual(self.q("SELECT COUNT(*) FROM work")[0][0], works, "no new work")
        aliases = {r[0] for r in self.q("SELECT work_key FROM work_alias WHERE work_id=?", old)}
        self.assertEqual(aliases, {"donky kong bananza", "donkey kong bananza"})

    def test_retitle_onto_an_existing_work_merges_state(self):
        self.stage_apply()
        a, b = self.persons()
        dk, dtr = self.copy(5)["work_id"], self.copy(3)["work_id"]
        self.play(a, dk, "finished", "Note on the old work", now=T0 + 30)
        self.play(a, dtr, "playing", "Note on the target", now=T0 + 20)
        self.play(b, dk, "up_next", now=T0 + 25)
        self.retitle(5, "Dead to Rights")
        self.assertEqual(self.copy(5)["work_id"], dtr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM work WHERE id=?", dk)[0][0], 0, "old work merged away")
        self.assertEqual(self.title(dtr), "Dead to Rights")
        # merge rules (spec 5.4): the later updated_at wins the status; the other note is appended
        self.assertEqual(self.state(), {
            (a, dtr): ("finished", "Note on the old work\n\nNote on the target", T0 + 30),
            (b, dtr): ("up_next", None, T0 + 25)})
        self.assertEqual(self.up_next(b), [dtr])
        self.assertEqual(self.q("SELECT work_id FROM work_alias WHERE work_key='donkey kong bananza'")[0][0], dtr)

    def test_old_work_with_another_copy_keeps_todays_behaviour(self):
        self.stage_apply()
        a, _b = self.persons()
        bb = self.copy(1)["work_id"]
        self.play(a, bb, "finished", "Both copies")
        self.retitle(1, "Bloodborne Game of the Year Edition")
        c1, c2 = self.copy(1), self.copy(2)
        self.assertNotEqual(c1["work_id"], bb)
        self.assertEqual(c2["work_id"], bb)
        self.assertEqual(self.title(bb), "Bloodborne")
        self.assertEqual(self.title(c1["work_id"]), "Bloodborne Game of the Year Edition")
        self.assertEqual(self.state(), {(a, bb): ("finished", "Both copies", T0 + 20)})

    def test_every_copy_retitled_in_one_import_carries_state(self):
        self.stage_apply()
        a, _b = self.persons()
        bb = self.copy(1)["work_id"]
        self.play(a, bb, "finished", "Both copies")
        res, _ = self.stage(self.rows_with(**{u(1): {"title": "Bloodborne GOTY"}, u(2): {"title": "Bloodborne GOTY"}}),
                            now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        w = self.copy(1)["work_id"]
        self.assertEqual(self.copy(2)["work_id"], w)
        self.assertEqual(self.title(w), "Bloodborne GOTY")
        self.assertEqual(self.state(), {(a, w): ("finished", "Both copies", T0 + 20)})

    def test_old_work_with_a_removed_copy_keeps_todays_behaviour(self):
        self.stage_apply()
        a, _b = self.persons()
        bb = self.copy(1)["work_id"]
        with self.store.write() as c:
            c.execute("UPDATE copy SET removed_at=1 WHERE source_id=?", (u(2),))
        self.play(a, bb, "finished")
        self.retitle(1, "Bloodborne Game of the Year Edition")
        self.assertNotEqual(self.copy(1)["work_id"], bb)
        self.assertEqual(self.copy(2)["work_id"], bb)
        self.assertEqual(self.title(bb), "Bloodborne")

    def test_retitle_with_a_restore_carries_state(self):
        self.stage_apply(self.rows_with(**{u(5): {"title": "Donky Kong Bananza"}}))
        a, _b = self.persons()
        old = self.copy(5)["work_id"]
        self.play(a, old, "abandoned")
        with self.store.write() as c:
            c.execute("UPDATE copy SET removed_at=1 WHERE source_id=?", (u(5),))
        out = self.retitle(5, "Donkey Kong Bananza")
        self.assertEqual(out["summary"]["restored"], 1)
        self.assertEqual(self.copy(5)["work_id"], old)
        self.assertEqual(self.title(old), "Donkey Kong Bananza")
        self.assertEqual(self.state()[(a, old)][0], "abandoned")

    def test_merge_conflict_refuses_the_apply_and_keeps_it_staged(self):
        self.stage_apply()
        a, _b = self.persons()
        dk, dtr = self.copy(5)["work_id"], self.copy(3)["work_id"]
        self.play(a, dk, "finished", "x" * 1500)
        self.play(a, dtr, "playing", "y" * 1500)
        res, _ = self.stage(self.rows_with(**{u(5): {"title": "Dead to Rights"}}), now=T0 + 100)
        with self.assertRaises(ImportFailure) as cm:
            self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual((cm.exception.status, cm.exception.code), (409, "note_too_long"))
        self.assertEqual(self.copy(5)["work_id"], dk)
        self.assertEqual(self.q("SELECT status FROM import_run WHERE id=?", res["import_id"])[0][0], "staged")


class ApplyAtomicTest(ApplyBase):
    def test_failure_mid_apply_writes_nothing_and_stays_staged(self):
        self.stage_apply()
        snapshot = lambda: [tuple(r) for r in self.q("SELECT * FROM copy ORDER BY id")] + \
            [tuple(r) for r in self.q("SELECT * FROM work ORDER BY id")]  # noqa: E731
        before = snapshot()
        rows = self.rows_with(**{u(1): {"title": "Bloodborne GOTY"}, u(5): {"title": "Something Else"}})
        rows = [r for r in rows if r["uuid"] != u(4)] + [ged_row(uuid=u(20), title="Brand New Game")]
        res, _ = self.stage(rows, now=T0 + 100)
        real, calls = importer._apply_change, []

        def boom(*a, **kw):
            calls.append(1)
            if len(calls) == 3:
                raise RuntimeError("disk on fire")
            return real(*a, **kw)
        with mock.patch.object(importer, "_apply_change", boom):
            with self.assertRaises(RuntimeError):
                self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(len(calls), 3, "two changes were written before the failure")
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.q("SELECT status, applied_at FROM import_run WHERE id=?", res["import_id"])[0][:],
                         ("staged", None))
        self.assertEqual(len(list(self.imports.glob("*.ged"))), 1, "the staged file is kept")
        out = self.apply(res["import_id"], now=T0 + 120)
        self.assertEqual(out["summary"]["added"], 1)


class AttachWorkTest(ApplyBase):
    def test_attach_work_alias_or_create(self):
        with self.store.write() as c:
            a = attach_work(c, "The Witcher III: Wild Hunt")
            b = attach_work(c, "Witcher 3 Wild Hunt [Complete Edition]")
            d = attach_work(c, "Other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, d)
        self.assertEqual(self.q("SELECT title FROM work WHERE id=?", a)[0][0],
                         "The Witcher III: Wild Hunt")


if __name__ == "__main__":
    unittest.main()
