from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _seed import T0, seed  # noqa: E402
from ps.catalog import (REASONS, STATUSES, CatalogError, catalog, seed_persons,  # noqa: E402
                        set_play_state, set_removed_reason, set_up_next)
from ps.db import Store, connect, migrate  # noqa: E402
from ps.idmaps import load, sync_platforms  # noqa: E402

KEYS = {"persons", "platforms", "works", "copies", "meta", "play_state", "up_next",
        "dup_groups", "last_import"}
PERSONS = [{"name": "Player A", "color": "#111111"}, {"name": "Player B", "color": "#222222"}]


class CatBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        db = self.dir / "ps.db"
        c = connect(db, writer=True)
        migrate(c, now=T0)
        c.close()
        self.store = Store(db)
        self.addCleanup(self.store.close)
        self.maps = load()
        with self.store.write() as c:
            sync_platforms(c, self.maps)
            seed_persons(c, PERSONS)
        seed(self.store, self.maps, self.dir)
        with self.store.read() as c:
            self.a, self.b = [r[0] for r in c.execute("SELECT id FROM person ORDER BY sort")]
            self.works = {r["title"]: r["id"] for r in c.execute("SELECT * FROM work")}

    def cat(self):
        with self.store.read() as c:
            return catalog(c)

    def q(self, sql, *a):
        with self.store.read() as c:
            return c.execute(sql, a).fetchall()

    def play(self, person, work, status, note=None, now=T0 + 5):
        with self.store.write() as c:
            return set_play_state(c, person, work, status, note, now=now)

    def err(self, fn, *a, **kw):
        with self.assertRaises(CatalogError) as cm:
            with self.store.write() as c:
                fn(c, *a, **kw)
        return cm.exception


class CatalogReadTest(CatBase):
    def test_catalog_keys(self):
        self.assertEqual(set(self.cat()), KEYS)

    def test_basic_content(self):
        c = self.cat()
        self.assertEqual([p["name"] for p in c["persons"]], ["Player A", "Player B"])
        self.assertEqual(set(c["persons"][0]), {"id", "name", "color"})
        self.assertEqual(len(c["copies"]), 7)
        self.assertEqual(len(c["works"]), 4)
        self.assertEqual(set(c["platforms"][0]), {"id", "name", "family", "gameeye_id"})
        self.assertEqual(len(c["dup_groups"]), 2)
        self.assertEqual(c["play_state"], [])
        self.assertEqual(c["up_next"], {})

    def test_copy_shape(self):
        c = self.cat()
        bb = [x for x in c["copies"] if x["raw_title"] == "Bloodborne" and x["condition"] == "CIB"]
        self.assertEqual(len(bb), 1)
        self.assertEqual(bb[0]["form"], "physical")
        self.assertEqual(set(bb[0]), {"id", "work_id", "platform_id", "category", "form",
                                      "raw_title", "edition", "condition_mask", "condition",
                                      "country", "source", "source_id", "created_at",
                                      "updated_at", "removed_at", "removed_reason"})

    def test_meta_types(self):
        meta = self.cat()["meta"]
        self.assertEqual(len(meta), 4)
        m = next(v for v in meta.values() if v.get("publisher") == "Test Publisher A")
        self.assertIsInstance(m["genre"], list)
        self.assertEqual(m["genre"], ["Action", "Adventure"])
        for k in ("metacritic",):
            if k in m:
                self.assertIsInstance(m[k], int)
        for k in ("price_loose", "price_cib", "price_new"):
            if k in m:
                self.assertIsInstance(m[k], float)
        json.dumps({str(k): v for k, v in meta.items()})

    def test_last_import(self):
        li = self.cat()["last_import"]
        self.assertEqual(set(li), {"id", "applied_at", "summary"})
        self.assertEqual(li["applied_at"], T0 + 1)
        self.assertEqual(li["summary"]["added"], 7)

    def test_last_import_none(self):
        with self.store.write() as c:
            c.execute("DELETE FROM copy_meta")
            c.execute("DELETE FROM import_change")
            c.execute("UPDATE copy SET last_seen_import=NULL")
            c.execute("DELETE FROM import_run")
        self.assertIsNone(self.cat()["last_import"])

    def test_snapshot_does_not_leave_transaction(self):
        with self.store.read() as c:
            catalog(c)
            self.assertFalse(c.in_transaction)

    def test_open_transaction_is_kept(self):
        with self.store.write() as c:
            catalog(c)
            self.assertTrue(c.in_transaction)


class PersonsTest(CatBase):
    def test_seed_persons_idempotent_never_deletes(self):
        with self.store.write() as c:
            seed_persons(c, PERSONS)
        self.assertEqual(self.q("SELECT COUNT(*) FROM person")[0][0], 2)
        with self.store.write() as c:
            seed_persons(c, [{"name": "Player A", "color": "#999999"}])
        rows = self.q("SELECT name, color, sort FROM person ORDER BY sort")
        self.assertEqual([tuple(r) for r in rows],
                         [("Player A", "#111111", 0), ("Player B", "#222222", 1)])

    def test_seed_persons_adds_with_sort(self):
        with self.store.write() as c:
            seed_persons(c, PERSONS + [{"name": "Player C"}])
        r = self.q("SELECT name, color, sort FROM person WHERE name='Player C'")[0]
        self.assertEqual((r[0], r[1], r[2]), ("Player C", None, 2))


class PlayStateTest(CatBase):
    def test_play_state_set_and_previous(self):
        w = self.works["Bloodborne"]
        self.assertIsNone(self.play(self.a, w, "finished"))
        prev = self.play(self.a, w, "playing", "hi")
        self.assertEqual(prev, {"status": "finished", "note": None})
        ps = self.cat()["play_state"]
        self.assertEqual(ps, [{"person": self.a, "work": w, "status": "playing", "note": "hi",
                               "updated_at": T0 + 5}])

    def test_hours_untouched(self):
        w = self.works["Bloodborne"]
        self.play(self.a, w, "playing")
        with self.store.write() as c:
            c.execute("UPDATE play_state SET hours=3.5, last_played=77")
        self.play(self.a, w, "finished", now=T0 + 9)
        r = self.q("SELECT hours, last_played, updated_at FROM play_state")[0]
        self.assertEqual((r[0], r[1], r[2]), (3.5, 77, T0 + 9))

    def test_empty_status_deletes(self):
        w = self.works["Bloodborne"]
        self.play(self.a, w, "up_next")
        prev = self.play(self.a, w, "")
        self.assertEqual(prev["status"], "up_next")
        self.assertEqual(self.q("SELECT COUNT(*) FROM play_state")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM up_next_order")[0][0], 0)
        self.assertIsNone(self.play(self.a, w, ""))

    def test_note_stripped_and_limited(self):
        w = self.works["Bloodborne"]
        self.play(self.a, w, "backlog", "  hi  ")
        self.assertEqual(self.q("SELECT note FROM play_state")[0][0], "hi")
        self.play(self.a, w, "backlog", "   ")
        self.assertIsNone(self.q("SELECT note FROM play_state")[0][0])
        self.play(self.a, w, "backlog", "x" * 2000)
        e = self.err(set_play_state, self.a, w, "backlog", "x" * 2001, now=T0)
        self.assertEqual((e.status, e.code), (400, "note_too_long"))
        self.assertEqual(len(self.q("SELECT note FROM play_state")[0][0]), 2000)

    def test_bad_status_400(self):
        e = self.err(set_play_state, self.a, self.works["Bloodborne"], "nope", None, now=T0)
        self.assertEqual((e.status, e.code), (400, "bad_request"))

    def test_bad_types_400(self):
        w = self.works["Bloodborne"]
        e = self.err(set_play_state, self.a, w, "backlog", 5, now=T0)
        self.assertEqual((e.status, e.code), (400, "bad_request"))
        e = self.err(set_play_state, "x", w, "backlog", None, now=T0)
        self.assertEqual((e.status, e.code), (400, "bad_request"))

    def test_unknown_person_or_work_404(self):
        w = self.works["Bloodborne"]
        for p, wk in ((9999, w), (self.a, 9999)):
            e = self.err(set_play_state, p, wk, "backlog", None, now=T0)
            self.assertEqual((e.status, e.code), (404, "not_found"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM play_state")[0][0], 0)

    def test_states_are_per_person(self):
        w = self.works["Bloodborne"]
        self.play(self.a, w, "finished")
        ps = self.cat()["play_state"]
        self.assertEqual([x["person"] for x in ps], [self.a])

    def test_statuses_constant(self):
        self.assertEqual(STATUSES, ("backlog", "up_next", "playing", "finished", "abandoned",
                                    "not_for_me"))
        self.assertEqual(REASONS, ("sold", "traded", "lost", "gifted", "mistake"))


class UpNextTest(CatBase):
    def setUp(self):
        super().setUp()
        self.w1, self.w2, self.w3 = (self.works[t] for t in
                                     ("Bloodborne", "Dead to Rights", "Donkey Kong Bananza"))

    def test_up_next_append_and_remove(self):
        self.play(self.a, self.w1, "up_next")
        self.play(self.a, self.w2, "up_next")
        self.assertEqual(self.cat()["up_next"], {self.a: [self.w1, self.w2]})
        self.play(self.a, self.w1, "playing")
        self.assertEqual(self.cat()["up_next"], {self.a: [self.w2]})

    def test_reset_up_next_keeps_position(self):
        self.play(self.a, self.w1, "up_next")
        self.play(self.a, self.w2, "up_next")
        self.play(self.a, self.w1, "up_next", "n")
        self.assertEqual(self.cat()["up_next"][self.a], [self.w1, self.w2])

    def test_per_person(self):
        self.play(self.a, self.w1, "up_next")
        self.play(self.b, self.w2, "up_next")
        self.assertEqual(self.cat()["up_next"], {self.a: [self.w1], self.b: [self.w2]})

    def test_set_up_next_reorders(self):
        for w in (self.w1, self.w2, self.w3):
            self.play(self.a, w, "up_next")
        with self.store.write() as c:
            set_up_next(c, self.a, [self.w3, self.w1, self.w2])
        self.assertEqual(self.cat()["up_next"][self.a], [self.w3, self.w1, self.w2])
        self.assertEqual([r[0] for r in self.q(
            "SELECT position FROM up_next_order ORDER BY position")], [0, 1, 2])

    def test_set_up_next_mismatch_409(self):
        self.play(self.a, self.w1, "up_next")
        self.play(self.a, self.w2, "up_next")
        for order in ([self.w1], [self.w1, self.w2, self.w3], [self.w1, self.w3]):
            e = self.err(set_up_next, self.a, order)
            self.assertEqual((e.status, e.code), (409, "order_mismatch"))
        self.assertEqual(self.cat()["up_next"][self.a], [self.w1, self.w2])

    def test_set_up_next_bad_input(self):
        self.play(self.a, self.w1, "up_next")
        for order in ([self.w1, self.w1], ["x"], [True], "nope", None):
            e = self.err(set_up_next, self.a, order)
            self.assertEqual((e.status, e.code), (400, "bad_request"), order)
        e = self.err(set_up_next, 9999, [])
        self.assertEqual((e.status, e.code), (404, "not_found"))

    def test_set_up_next_empty(self):
        with self.store.write() as c:
            set_up_next(c, self.a, [])
        self.assertEqual(self.cat()["up_next"], {})


class RemovedReasonTest(CatBase):
    def removed_copy(self):
        cid = self.q("SELECT id FROM copy WHERE source_id LIKE '%01'")[0][0]
        with self.store.write() as c:
            c.execute("UPDATE copy SET removed_at=? WHERE id=?", (T0, cid))
        return cid

    def test_active_copy_409(self):
        cid = self.q("SELECT id FROM copy")[0][0]
        e = self.err(set_removed_reason, cid, "sold")
        self.assertEqual((e.status, e.code), (409, "not_removed"))

    def test_store_and_clear(self):
        cid = self.removed_copy()
        with self.store.write() as c:
            set_removed_reason(c, cid, "sold")
        got = [x for x in self.cat()["copies"] if x["id"] == cid][0]
        self.assertEqual(got["removed_reason"], "sold")
        self.assertEqual(got["removed_at"], T0)
        with self.store.write() as c:
            set_removed_reason(c, cid, None)
        got = [x for x in self.cat()["copies"] if x["id"] == cid][0]
        self.assertIsNone(got["removed_reason"])

    def test_bad_reason_400(self):
        cid = self.removed_copy()
        for r in ("x", 5, ""):
            e = self.err(set_removed_reason, cid, r)
            self.assertEqual((e.status, e.code), (400, "bad_request"))

    def test_unknown_copy_404(self):
        e = self.err(set_removed_reason, 9999, "sold")
        self.assertEqual((e.status, e.code), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
