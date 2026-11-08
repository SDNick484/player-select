from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_ged, ged_row  # noqa: E402
from _seed import T0  # noqa: E402
from ps.catalog import ack_dup, dup_groups, merge, split  # noqa: E402
from ps.importer import apply, stage  # noqa: E402
from test_catalog import CatBase  # noqa: E402


class EditBase(CatBase):
    def setUp(self):
        super().setUp()
        self.bb = self.works["Bloodborne"]
        self.dtr = self.works["Dead to Rights"]
        self.dk = self.works["Donkey Kong Bananza"]

    def merge(self, into, from_, now=T0 + 50):
        with self.store.write() as c:
            merge(c, into, from_, now=now)

    def copy_id(self, n):
        return self.q("SELECT id FROM copy WHERE source_id=?", f"00000000-0000-4000-8000-{n:012d}")[0][0]

    def plat(self, gid):
        return self.q("SELECT id FROM platform WHERE gameeye_id=?", gid)[0][0]

    def reimport(self, rows, now):
        imports = self.dir / "imports"
        res = stage(self.store, self.maps, imports, build_ged(rows), None, now=now)
        apply(self.store, self.maps, imports, res["import_id"], confirm_removals=False, now=now + 1)

    def up(self, person):
        return [r[0] for r in self.q("SELECT work_id FROM up_next_order WHERE person_id=?"
                                     " ORDER BY position", person)]


class MergeTest(EditBase):
    def test_merge_repoints(self):
        self.play(self.a, self.dtr, "up_next")
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM work WHERE id=?", self.dtr)[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy WHERE work_id=?", self.dtr)[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM work_alias WHERE work_id=?", self.dtr)[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy WHERE work_id=?", self.bb)[0][0], 4)
        self.assertEqual(self.q("SELECT work_key FROM work_alias WHERE work_key='dead to rights'")
                         [0][0], "dead to rights")
        self.assertEqual(self.up(self.a), [self.bb])
        self.assertEqual([r[0] for r in self.q("SELECT work_id FROM play_state")], [self.bb])

    def test_merge_keeps_work_pinned(self):
        with self.store.write() as c:
            c.execute("UPDATE copy SET work_pinned=1 WHERE id=?", (self.copy_id(3),))
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.q("SELECT work_pinned FROM copy WHERE id=?", self.copy_id(3))[0][0], 1)
        self.assertEqual(self.q("SELECT work_pinned FROM copy WHERE id=?", self.copy_id(4))[0][0], 0)

    def test_merge_play_state_later_wins_and_notes_append(self):
        self.play(self.a, self.bb, "backlog", "a", now=T0 + 10)
        self.play(self.a, self.dtr, "finished", "b", now=T0 + 20)
        self.merge(self.bb, self.dtr)
        r = self.q("SELECT status, note, updated_at FROM play_state WHERE person_id=?", self.a)
        self.assertEqual([tuple(x) for x in r], [("finished", "b\n\na", T0 + 20)])

    def test_merge_tie_into_wins(self):
        self.play(self.a, self.bb, "playing", "a", now=T0 + 10)
        self.play(self.a, self.dtr, "finished", "b", now=T0 + 10)
        self.merge(self.bb, self.dtr)
        r = self.q("SELECT status, note FROM play_state WHERE person_id=?", self.a)
        self.assertEqual([tuple(x) for x in r], [("playing", "a\n\nb")])

    def test_merge_one_note_only(self):
        self.play(self.a, self.bb, "backlog", None, now=T0 + 10)
        self.play(self.a, self.dtr, "finished", "b", now=T0 + 5)
        self.merge(self.bb, self.dtr)
        r = self.q("SELECT status, note FROM play_state WHERE person_id=?", self.a)
        self.assertEqual([tuple(x) for x in r], [("backlog", "b")])

    def test_merge_one_side_only_per_person(self):
        self.play(self.a, self.bb, "playing")
        self.play(self.b, self.dtr, "finished")
        self.merge(self.bb, self.dtr)
        r = self.q("SELECT person_id, work_id, status FROM play_state ORDER BY person_id")
        self.assertEqual([tuple(x) for x in r], [(self.a, self.bb, "playing"),
                                                 (self.b, self.bb, "finished")])

    def test_merge_note_too_long_409_writes_nothing(self):
        self.play(self.a, self.bb, "backlog", "x" * 1500, now=T0 + 10)
        self.play(self.a, self.dtr, "finished", "y" * 1500, now=T0 + 20)
        before = [tuple(r) for r in self.q("SELECT * FROM play_state ORDER BY work_id")]
        e = self.err(merge, self.bb, self.dtr, now=T0 + 50)
        self.assertEqual((e.status, e.code), (409, "note_too_long"))
        self.assertEqual([tuple(r) for r in self.q("SELECT * FROM play_state ORDER BY work_id")], before)
        self.assertEqual(self.q("SELECT COUNT(*) FROM work WHERE id=?", self.dtr)[0][0], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy WHERE work_id=?", self.dtr)[0][0], 2)

    def test_merge_up_next_collision_keeps_into(self):
        self.play(self.a, self.bb, "up_next", now=T0 + 10)
        self.play(self.a, self.dk, "up_next", now=T0 + 11)
        self.play(self.a, self.dtr, "up_next", now=T0 + 12)
        self.assertEqual(self.up(self.a), [self.bb, self.dk, self.dtr])
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.up(self.a), [self.bb, self.dk])

    def test_merge_up_next_repoints_when_only_from(self):
        self.play(self.a, self.dk, "up_next", now=T0 + 10)
        self.play(self.a, self.dtr, "up_next", now=T0 + 11)
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.up(self.a), [self.dk, self.bb])

    def test_merge_up_next_reconciled_when_newer_state_wins(self):
        self.play(self.a, self.bb, "up_next", now=T0 + 10)
        self.play(self.a, self.dtr, "finished", now=T0 + 20)
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.q("SELECT status FROM play_state WHERE person_id=?", self.a)[0][0],
                         "finished")
        self.assertEqual(self.up(self.a), [])

    def test_merge_up_next_row_created_when_up_next_wins(self):
        self.play(self.a, self.dk, "up_next", now=T0 + 5)
        self.play(self.a, self.bb, "backlog", now=T0 + 10)
        self.play(self.a, self.dtr, "up_next", now=T0 + 20)
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.up(self.a), [self.dk, self.bb])

    def test_merge_drops_dup_acks(self):
        with self.store.write() as c:
            ack_dup(c, self.bb, self.plat(46), now=T0 + 5)
            ack_dup(c, self.dtr, self.plat(14), now=T0 + 5)
        self.merge(self.bb, self.dtr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM dup_ack")[0][0], 0)

    def test_merge_same_or_unknown(self):
        e = self.err(merge, self.bb, self.bb, now=T0)
        self.assertEqual((e.status, e.code), (400, "bad_request"))
        self.assertEqual(self.err(merge, self.bb, 9999, now=T0).status, 404)
        self.assertEqual(self.err(merge, 9999, self.bb, now=T0).status, 404)
        self.assertEqual(self.err(merge, "1", 2, now=T0).status, 400)
        self.assertEqual(self.err(merge, True, 2, now=T0).status, 400)


class SplitTest(EditBase):
    def test_split_creates_pinned_work(self):
        cid = self.copy_id(1)
        with self.store.write() as c:
            wid = split(c, cid, "  Bloodborne Remastered ")
        self.assertNotIn(wid, (self.bb, self.dtr, self.dk))
        self.assertEqual(self.q("SELECT title FROM work WHERE id=?", wid)[0][0],
                         "Bloodborne Remastered")
        self.assertEqual(self.q("SELECT work_id FROM work_alias WHERE work_key='bloodborne remastered'")
                         [0][0], wid)
        r = self.q("SELECT work_id, work_pinned FROM copy WHERE id=?", cid)[0]
        self.assertEqual((r[0], r[1]), (wid, 1))
        self.assertEqual(self.q("SELECT work_id FROM copy WHERE id=?", self.copy_id(2))[0][0], self.bb)

    def test_split_survives_reimport(self):
        cid = self.copy_id(1)
        with self.store.write() as c:
            wid = split(c, cid, "Something Else")
        rows = [dict(r, updated_at=r["updated_at"] + 1) if r["uuid"].endswith("01") else r
                for r in FIXTURE_ROWS]
        self.reimport(rows, T0 + 100)
        self.assertEqual(self.q("SELECT work_id, work_pinned FROM copy WHERE id=?", cid)[0][:],
                         (wid, 1))

    def test_split_onto_existing_key_attaches(self):
        with self.store.write() as c:
            wid = split(c, self.copy_id(1), "The Dead to Rights")
        self.assertEqual(wid, self.dtr)
        r = self.q("SELECT work_id, work_pinned FROM copy WHERE id=?", self.copy_id(1))[0]
        self.assertEqual((r[0], r[1]), (self.dtr, 1))
        self.assertEqual(self.q("SELECT COUNT(*) FROM work")[0][0], len(self.works))

    def test_split_rejects_non_game_and_empty_title(self):
        e = self.err(split, self.copy_id(6), "Thing")
        self.assertEqual((e.status, e.code), (400, "bad_request"))
        for t in ("", "   ", "!!!", None, 5):
            self.assertEqual(self.err(split, self.copy_id(1), t).status, 400, t)
        self.assertEqual(self.err(split, 9999, "X").status, 404)
        self.assertEqual(self.err(split, "1", "X").status, 400)
        self.assertEqual(self.q("SELECT work_pinned FROM copy WHERE id=?", self.copy_id(1))[0][0], 0)


class DupTest(EditBase):
    def groups(self):
        with self.store.read() as c:
            return dup_groups(c)

    def test_dup_groups_from_fixture(self):
        want = [{"work": self.bb, "platform": self.plat(46),
                 "copies": sorted([self.copy_id(1), self.copy_id(2)])},
                {"work": self.dtr, "platform": self.plat(14),
                 "copies": sorted([self.copy_id(3), self.copy_id(4)])}]
        self.assertEqual(self.groups(), want)
        self.assertEqual(self.cat()["dup_groups"], want)

    def test_ack_hides_group(self):
        with self.store.write() as c:
            ack_dup(c, self.bb, self.plat(46), now=T0 + 5)
        self.assertEqual([g["work"] for g in self.groups()], [self.dtr])
        r = self.q("SELECT copy_ids, acked_at FROM dup_ack")[0]
        self.assertEqual((r[0], r[1]), (json.dumps(sorted([self.copy_id(1), self.copy_id(2)])), T0 + 5))
        with self.store.write() as c:   # idempotent upsert
            ack_dup(c, self.bb, self.plat(46), now=T0 + 6)
        self.assertEqual(self.q("SELECT COUNT(*), MAX(acked_at) FROM dup_ack")[0][:], (1, T0 + 6))

    def test_ack_void_when_set_changes(self):
        with self.store.write() as c:
            ack_dup(c, self.bb, self.plat(46), now=T0 + 5)
        self.reimport(FIXTURE_ROWS + [ged_row(uuid="00000000-0000-4000-8000-000000000009",
                                              item_id=2001, platform_id=46, title="Bloodborne",
                                              ownership_mask=31)], T0 + 100)
        g = [x for x in self.groups() if x["work"] == self.bb]
        self.assertEqual(len(g), 1)
        self.assertEqual(len(g[0]["copies"]), 3)

    def test_removed_copy_dissolves_group(self):
        with self.store.write() as c:
            c.execute("UPDATE copy SET removed_at=? WHERE id=?", (T0 + 9, self.copy_id(2)))
        self.assertEqual([g["work"] for g in self.groups()], [self.dtr])

    def test_ack_non_group_409(self):
        e = self.err(ack_dup, self.dk, self.plat(178), now=T0)
        self.assertEqual((e.status, e.code), (409, "not_a_duplicate"))
        self.assertEqual(self.err(ack_dup, 9999, self.plat(46), now=T0).status, 404)
        self.assertEqual(self.err(ack_dup, self.bb, 9999, now=T0).status, 404)
        self.assertEqual(self.err(ack_dup, "1", 1, now=T0).status, 400)


if __name__ == "__main__":
    unittest.main()
