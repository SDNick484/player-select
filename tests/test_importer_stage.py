from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_csv, build_ged, fixture_csv_rows, ged_row  # noqa: E402
from ps import importer  # noqa: E402
from ps.db import Store, connect, migrate  # noqa: E402
from ps.idmaps import load, sync_platforms  # noqa: E402
from ps.importer import ImportFailure, stage  # noqa: E402
from ps.titles import display_title, normalize  # noqa: E402

T0 = 1_800_000_000


def u(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


class StageBase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        self.imports = self.dir / "imports"
        db = self.dir / "ps.db"
        c = connect(db, writer=True)
        migrate(c, now=T0)
        c.close()
        self.store = Store(db)
        self.addCleanup(self.store.close)
        self.maps = load()
        with self.store.write() as conn:
            sync_platforms(conn, self.maps)

    def stage(self, rows=None, csv=None, now=T0, **kw):
        ged = build_ged(FIXTURE_ROWS if rows is None else rows, **kw)
        return stage(self.store, self.maps, self.imports, ged, csv, now=now), ged

    def seed(self, uuid, title="Bloodborne", gid=46, category="game", mask=7, country="1",
             removed_at=None, sp=None):
        with self.store.write() as c:
            pid = c.execute("SELECT id FROM platform WHERE gameeye_id=?", (gid,)).fetchone()[0]
            wid = None
            if category == "game":
                key, _ = normalize(title)
                row = c.execute("SELECT work_id FROM work_alias WHERE work_key=?", (key,)).fetchone()
                if row:
                    wid = row[0]
                else:
                    wid = c.execute("INSERT INTO work (title) VALUES (?)",
                                    (display_title(title),)).lastrowid
                    c.execute("INSERT INTO work_alias VALUES (?, ?)", (key, wid))
            c.execute(
                "INSERT INTO copy (work_id, platform_id, category, form, raw_title, condition_mask,"
                " country, source, source_id, source_platform_id, created_at, updated_at,"
                " removed_at) VALUES (?,?,?,'physical',?,?,?,'gameeye',?,?,1,1,?)",
                (wid, pid, category, title, mask, country, uuid, gid if sp is None else sp,
                 removed_at))

    def seed_applied_run(self, sha):
        with self.store.write() as c:
            return c.execute(
                "INSERT INTO import_run (source, file_sha256, status, staged_at, applied_at,"
                " summary_json) VALUES ('gameeye', ?, 'applied', ?, ?, '{}')",
                (sha, T0 - 100, T0 - 90)).lastrowid

    def q(self, sql, *a):
        with self.store.read() as c:
            return c.execute(sql, a).fetchall()


class FirstStageTest(StageBase):
    def test_first_stage(self):
        res, ged = self.stage()
        self.assertEqual(res["summary"], {"added": 7, "edited": 0, "removed": 0, "restored": 0,
                                          "unchanged": 0, "skipped": 1})
        self.assertIs(res["dry_run"], True)
        self.assertIs(res["removal_guard"], False)
        self.assertIsInstance(res["import_id"], int)
        kinds = [w["kind"] for w in res["warnings"]]
        self.assertIn("unmapped_platform", kinds)
        self.assertIn("unknown_category", kinds)
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy")[0][0], 0)
        run = self.q("SELECT status, base_run_id, file_sha256 FROM import_run")[0]
        self.assertEqual((run[0], run[1]), ("staged", None))
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_change WHERE op='add'")[0][0], 7)
        p = self.imports / f"{hashlib.sha256(ged).hexdigest()}.ged"
        self.assertTrue(p.exists())
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.imports.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.imports.glob("*.d")), [])

    def test_warning_shapes(self):
        res, _ = self.stage()
        self.assertIn({"kind": "unmapped_platform", "detail": "GameEye platform 199"},
                      res["warnings"])
        self.assertIn({"kind": "unknown_category", "detail": f"{u(8)}: category 9"},
                      res["warnings"])

    def test_change_entry_shape(self):
        res, _ = self.stage()
        self.assertIn({"op": "add", "source_id": u(5), "title": "Donkey Kong Bananza",
                       "platform": "Nintendo Switch 2", "condition": "CIB"}, res["changes"])
        unm = [c for c in res["changes"] if c["source_id"] == u(7)][0]
        self.assertEqual(unm["platform"], "GameEye platform 199 (unmapped)")

    def test_summary_json_stored(self):
        res, _ = self.stage()
        s = json.loads(self.q("SELECT summary_json FROM import_run")[0][0])
        self.assertEqual(s["added"], 7)
        self.assertIs(s["removal_guard"], False)
        self.assertEqual(s["warnings"], res["warnings"])

    def test_change_json(self):
        self.stage()
        before, after = self.q("SELECT before_json, after_json FROM import_change "
                               "WHERE source_id=?", u(5))[0]
        self.assertIsNone(before)
        a = json.loads(after)
        self.assertEqual(a["raw_title"], "Donkey Kong Bananza")
        self.assertEqual(a["work_key"], "donkey kong bananza")

    def test_user_version_warning(self):
        res, _ = self.stage(user_version=21)
        self.assertIn({"kind": "user_version", "detail": "expected 20, got 21"}, res["warnings"])

    def test_csv_attached_and_staged(self):
        csv = build_csv(fixture_csv_rows())
        res, _ = self.stage(csv=csv)
        sha = hashlib.sha256(csv).hexdigest()
        p = self.imports / f"{sha}.csv"
        self.assertEqual(p.read_bytes(), csv)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.q("SELECT csv_sha256 FROM import_run")[0][0], sha)
        self.assertEqual(res["summary"]["csv"],
                         {"enriched": 4, "unmatched": 0, "ambiguous": 2})


class RobustnessTest(StageBase):
    def test_leftover_workdir_and_tmp(self):
        ged = build_ged(FIXTURE_ROWS)
        sha = hashlib.sha256(ged).hexdigest()
        self.imports.mkdir()
        (self.imports / f"{sha}.d").mkdir()
        (self.imports / f"{sha}.ged.tmp").write_bytes(b"stale")
        res = stage(self.store, self.maps, self.imports, ged, None, now=T0)
        self.assertEqual(res["summary"]["added"], 7)
        self.assertEqual((self.imports / f"{sha}.ged").read_bytes(), ged)

    def test_double_submit(self):
        a, ged = self.stage()
        b = stage(self.store, self.maps, self.imports, ged, None, now=T0)
        self.assertNotEqual(a["import_id"], b["import_id"])
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_run WHERE status='staged'")[0][0], 2)
        self.assertEqual(sorted(p.name for p in self.imports.iterdir()),
                         [f"{hashlib.sha256(ged).hexdigest()}.ged"])


class ShortCircuitTest(StageBase):
    def _seven(self):
        for n in range(1, 8):
            self.seed(u(n), title=f"Game {n}")

    def test_same_file_as_last_applied(self):
        ged = build_ged(FIXTURE_ROWS)
        self._seven()
        self.seed_applied_run(hashlib.sha256(ged).hexdigest())
        res = stage(self.store, self.maps, self.imports, ged, None, now=T0)
        self.assertEqual(res, {"import_id": None, "summary": {"unchanged": 7}})
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_run")[0][0], 1)

    def test_same_file_with_csv_stages(self):
        ged = build_ged(FIXTURE_ROWS)
        self._seven()
        self.seed_applied_run(hashlib.sha256(ged).hexdigest())
        res = stage(self.store, self.maps, self.imports, ged,
                    build_csv(fixture_csv_rows()), now=T0)
        self.assertIsInstance(res["import_id"], int)
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_run")[0][0], 2)
        self.assertEqual(res["summary"]["added"], 0)


class RejectTest(StageBase):
    def fail(self, ged, **kw):
        with self.assertRaises(ImportFailure) as cm:
            stage(self.store, self.maps, self.imports, ged, None, now=T0, **kw)
        return cm.exception

    def test_bad_ged_is_400(self):
        e = self.fail(b"not a zip")
        self.assertEqual((e.status, e.code), (400, "not_zip"))
        rows = [ged_row(uuid=u(1)), ged_row(uuid=u(1))]
        e = self.fail(build_ged(rows))
        self.assertEqual((e.status, e.code), (400, "duplicate_uuid"))
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_run")[0][0], 0)
        self.assertEqual(list(self.imports.glob("*.ged")), [])
        self.assertEqual(list(self.imports.glob("*.d")), [])

    def test_non_ged_error_is_bad_ged(self):
        with mock.patch.object(importer, "read_ged", side_effect=ValueError("odd")):
            e = self.fail(build_ged(FIXTURE_ROWS))
        self.assertEqual((e.status, e.code), (400, "bad_ged"))

    def test_oversize_is_413(self):
        with mock.patch.object(importer, "MAX_UPLOAD", 10):
            e = self.fail(build_ged(FIXTURE_ROWS))
        self.assertEqual((e.status, e.code), (413, "too_large"))

    def test_null_fields_skipped_with_warning(self):
        rows = [ged_row(uuid=u(1), title=None), ged_row(uuid=u(2), title=""),
                ged_row(uuid=u(3), platform_id=None),
                ged_row(uuid=u(4), category_id=None), ged_row(uuid=u(5), title="Fine")]
        res, _ = self.stage(rows)
        self.assertEqual(res["summary"]["skipped"], 4)
        self.assertEqual(res["summary"]["added"], 1)
        w = res["warnings"]
        self.assertEqual([x["kind"] for x in w].count("bad_row"), 3)
        self.assertIn({"kind": "unknown_category", "detail": f"{u(4)}: category missing"}, w)
        self.assertTrue(any(x["detail"].startswith(f"{u(1)}: ") for x in w))


class OutOfRangeTest(StageBase):
    """Values SQLite cannot store (beyond int64) or that are no sane epoch second: the row is skipped as a
    bad_row, never a 500 at apply or in the CSV join."""

    def check(self, csv=None, **bad):
        rows = [ged_row(uuid=u(1), title="Fine"), ged_row(uuid=u(2), title="Broken", **bad)]
        res, _ = self.stage(rows, csv=csv)
        self.assertEqual((res["summary"]["added"], res["summary"]["skipped"]), (1, 1), bad)
        self.assertTrue(any(w["kind"] == "bad_row" and w["detail"].startswith(f"{u(2)}: ")
                            for w in res["warnings"]), res["warnings"])
        out = importer.apply(self.store, self.maps, self.imports, res["import_id"], confirm_removals=False,
                             now=T0 + 1)
        self.assertEqual(out["summary"]["added"], 1)
        with self.store.read() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT source_id FROM copy")], [u(1)])

    def test_integers_beyond_int64(self):
        # SQLite itself cannot hold these as INTEGER: in a backup they arrive as TEXT digits or as REAL.
        for col in ("platform_id", "category_id", "ownership_mask", "created_at", "updated_at"):
            for v in (str(2 ** 63), str(-(2 ** 63) - 1), 1e30, 1e19):
                with self.subTest(col=col, v=v):
                    self.setUp()
                    self.check(**{col: v})

    def test_timestamps_out_of_range(self):
        for col in ("created_at", "updated_at"):
            for v in (-1, 253402300800):
                with self.subTest(col=col, v=v):
                    self.setUp()
                    self.check(**{col: v})

    def test_huge_created_at_with_a_csv(self):
        self.check(csv=build_csv(fixture_csv_rows()), created_at=1e30)

    def test_bounds_are_accepted(self):
        rows = [ged_row(uuid=u(1), title="Edge", created_at=0, updated_at=253402300799,
                        ownership_mask=2 ** 63 - 1)]
        res, _ = self.stage(rows)
        self.assertEqual((res["summary"]["added"], res["summary"]["skipped"]), (1, 0))

    def test_two_null_uuids_are_bad_rows_not_duplicates(self):
        rows = [ged_row(uuid=u(1), title="Fine"), ged_row(uuid=None, title="A"), ged_row(uuid=None, title="B"),
                ged_row(uuid="", title="C"), ged_row(uuid="", title="D")]
        res, _ = self.stage(rows)
        self.assertEqual((res["summary"]["added"], res["summary"]["skipped"]), (1, 4))
        self.assertEqual([w["kind"] for w in res["warnings"]].count("bad_row"), 4)


class DiffTest(StageBase):
    def one(self, **ged_kw):
        return ged_row(uuid=u(1), title="Bloodborne", platform_id=46, category_id=0,
                       ownership_mask=7, country_id=1, **ged_kw)

    def test_unchanged(self):
        self.seed(u(1))
        res, _ = self.stage([self.one()])
        self.assertEqual(res["summary"]["unchanged"], 1)
        self.assertEqual(res["changes"], [])

    def test_edit_detected_per_field(self):
        cases = {"title": "Bloodborne [GOTY]", "platform_id": 14, "category_id": 5,
                 "ownership_mask": 31, "country_id": 2}
        for field, val in cases.items():
            with self.subTest(field):
                self.setUp()
                self.seed(u(1))
                row = self.one()
                row[field] = val
                res, _ = self.stage([row])
                self.assertEqual(res["summary"]["edited"], 1, field)
                self.assertEqual(res["changes"][0]["op"], "edit")
                before = json.loads(self.q("SELECT before_json FROM import_change")[0][0])
                self.assertEqual(before["raw_title"], "Bloodborne")

    def test_updated_at_alone_not_edit(self):
        self.seed(u(1))
        res, _ = self.stage([self.one(updated_at=1900000000)])
        self.assertEqual(res["summary"]["edited"], 0)
        self.assertEqual(res["summary"]["unchanged"], 1)

    def test_remove_when_absent(self):
        self.seed(u(1))
        self.seed(u(2), title="Other")
        res, _ = self.stage([self.one()])
        self.assertEqual(res["summary"]["removed"], 1)
        ch = [c for c in res["changes"] if c["op"] == "remove"][0]
        self.assertEqual(ch, {"op": "remove", "source_id": u(2), "title": "Other",
                              "platform": "Sony PlayStation 4", "condition": "CIB"})
        after = self.q("SELECT after_json FROM import_change WHERE op='remove'")[0][0]
        self.assertIsNone(after)

    def test_already_removed_and_absent_is_quiet(self):
        self.seed(u(2), title="Other", removed_at=5)
        res, _ = self.stage([])
        self.assertEqual(res["summary"]["removed"], 0)

    def test_restore_when_removed_and_present(self):
        self.seed(u(1), removed_at=5)
        res, _ = self.stage([self.one()])
        self.assertEqual(res["summary"]["restored"], 1)
        self.assertEqual(res["summary"]["edited"], 0)
        self.assertEqual(res["changes"][0]["op"], "restore")

    def test_restore_with_edit_keeps_both(self):
        self.seed(u(1), removed_at=5)
        row = self.one()
        row["title"] = "Bloodborne [GOTY]"
        res, _ = self.stage([row])
        self.assertEqual(res["summary"]["restored"], 1)
        self.assertEqual(res["changes"][0]["op"], "restore")
        after = json.loads(self.q("SELECT after_json FROM import_change")[0][0])
        self.assertEqual(after["raw_title"], "Bloodborne [GOTY]")

    def test_skipped_row_is_not_a_removal(self):
        self.seed(u(1))
        res, _ = self.stage([ged_row(uuid=u(1), title=None)])
        self.assertEqual(res["summary"]["removed"], 0)
        self.assertEqual(res["summary"]["skipped"], 1)

    def test_removal_guard(self):
        for n in range(1, 5):
            self.seed(u(n), title=f"G{n}")
        rows = [ged_row(uuid=u(n), title=f"G{n}", country_id=1) for n in (1, 2)]
        res, _ = self.stage(rows)
        self.assertIs(res["removal_guard"], True)
        rows = [ged_row(uuid=u(n), title=f"G{n}", country_id=1) for n in (1, 2, 3)]
        res, _ = self.stage(rows, user_version=20)
        self.assertIs(res["removal_guard"], False)

    def test_base_run_is_latest_applied(self):
        rid = self.seed_applied_run("ab" * 32)
        self.stage()
        self.assertEqual(self.q("SELECT base_run_id FROM import_run WHERE status='staged'")[0][0],
                         rid)


class ExpireTest(StageBase):
    def staged_run(self, sha, staged_at, status="staged"):
        self.imports.mkdir(exist_ok=True)
        (self.imports / f"{sha}.ged").write_bytes(b"x")
        with self.store.write() as c:
            return c.execute(
                "INSERT INTO import_run (source, file_sha256, status, staged_at, summary_json)"
                " VALUES ('gameeye', ?, ?, ?, '{}')", (sha, status, staged_at)).lastrowid

    def test_expire(self):
        old = self.staged_run("a" * 64, T0 - 3601)
        new = self.staged_run("b" * 64, T0 - 3599)
        with self.store.write() as c:
            n = importer.expire(c, self.imports, now=T0)
        self.assertEqual(n, 1)
        st = dict(self.q("SELECT id, status FROM import_run"))
        self.assertEqual((st[old], st[new]), ("expired", "staged"))
        self.assertFalse((self.imports / f"{'a' * 64}.ged").exists())
        self.assertTrue((self.imports / f"{'b' * 64}.ged").exists())

    def test_expire_keeps_file_shared_with_live_run(self):
        self.staged_run("a" * 64, T0 - 3601)
        self.staged_run("a" * 64, T0 - 10)
        with self.store.write() as c:
            importer.expire(c, self.imports, now=T0)
        self.assertTrue((self.imports / f"{'a' * 64}.ged").exists())

    def test_stage_expires_lazily(self):
        old = self.staged_run("a" * 64, T0 - 3601)
        self.stage()
        self.assertEqual(dict(self.q("SELECT id, status FROM import_run"))[old], "expired")
        self.assertFalse((self.imports / f"{'a' * 64}.ged").exists())


if __name__ == "__main__":
    unittest.main()
