from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_csv, csv_row, fixture_csv_rows  # noqa: E402
from ps.importer import ImportFailure, stage  # noqa: E402
from test_importer_apply import ApplyBase  # noqa: E402
from test_importer_stage import T0, u  # noqa: E402

COUNTS = {"enriched": 4, "unmatched": 0, "ambiguous": 2}


class CsvBase(ApplyBase):
    def meta(self, n, source="gameeye_csv"):
        cid = self.copy(n)["id"]
        return {r["field"]: r["value"] for r in self.q(
            "SELECT field, value FROM copy_meta WHERE copy_id=? AND source=?", cid, source)}

    def run_csv(self, rows, now=T0):
        res, _ = self.stage(csv=build_csv(rows), now=now)
        return res, self.apply(res["import_id"], now=now + 1)


class StageCsvTest(CsvBase):
    def test_stage_reports_csv_counts(self):
        res, _ = self.stage(csv=build_csv(fixture_csv_rows()))
        self.assertEqual(res["summary"]["csv"], COUNTS)
        stored = json.loads(self.q("SELECT summary_json FROM import_run")[0][0])
        self.assertEqual(stored["csv"], COUNTS)

    def test_no_csv_no_key(self):
        res, _ = self.stage()
        self.assertNotIn("csv", res["summary"])

    def test_bad_csv_400(self):
        with self.assertRaises(ImportFailure) as cm:
            self.stage(csv=b"not,a,gameeye,export\r\n")
        self.assertEqual((cm.exception.status, cm.exception.code), (400, "bad_csv"))
        self.assertEqual(list(self.imports.glob("*.csv")), [])
        self.assertEqual(self.q("SELECT COUNT(*) FROM import_run")[0][0], 0)

    def test_unmatched_csv_row_counted(self):
        rows = fixture_csv_rows() + [csv_row(Title="Not In Backup")]
        res, _ = self.stage(csv=build_csv(rows))
        self.assertEqual(res["summary"]["csv"], {**COUNTS, "unmatched": 1})


class ApplyCsvTest(CsvBase):
    def test_apply_writes_meta(self):
        _, out = self.run_csv(fixture_csv_rows())
        self.assertEqual(out["summary"]["csv"], COUNTS)
        m = self.meta(1)
        self.assertEqual(m["publisher"], "Test Publisher A")
        self.assertEqual(json.loads(m["genre"]), ["Action", "Adventure"])
        self.assertEqual(m["metacritic"], "92")
        self.assertEqual(self.meta(3), {})
        self.assertEqual(self.meta(4), {})
        fetched = {r[0] for r in self.q("SELECT fetched_at FROM copy_meta")}
        self.assertEqual(fetched, {T0 + 1})
        self.assertEqual(list(self.imports.iterdir()), [])

    def test_boxed_and_cib_get_their_own_prices(self):
        self.run_csv(fixture_csv_rows())
        self.assertEqual(self.meta(1)["price_cib"], "24.99")
        self.assertEqual(self.meta(2)["price_cib"], "27.50")

    def test_reapply_upserts(self):
        self.run_csv(fixture_csv_rows())
        rows = fixture_csv_rows()
        rows[0]["PriceCIB"] = "30.00"
        self.run_csv(rows, now=T0 + 100)
        self.assertEqual(self.meta(1)["price_cib"], "30.00")
        self.assertEqual(self.q("SELECT COUNT(*) FROM copy_meta WHERE copy_id=? AND "
                                "field='price_cib'", self.copy(1)["id"])[0][0], 1)

    def test_field_null_in_new_csv_deletes_row(self):
        self.run_csv(fixture_csv_rows())
        self.assertIn("price_new", self.meta(1))
        rows = fixture_csv_rows()
        rows[0]["PriceNew"] = "?"
        self.run_csv(rows, now=T0 + 100)
        self.assertNotIn("price_new", self.meta(1))
        self.assertIn("price_loose", self.meta(1))
        self.assertIn("price_new", self.meta(2))

    def test_unmatched_copy_keeps_old_meta_and_other_sources(self):
        self.run_csv(fixture_csv_rows())
        cid = self.copy(1)["id"]
        with self.store.write() as c:
            c.execute("INSERT INTO copy_meta VALUES (?, 'publisher', 'Other', 'other', 1)", (cid,))
        res, _ = self.stage(csv=build_csv(fixture_csv_rows()[1:]), now=T0 + 100)
        self.apply(res["import_id"], now=T0 + 110)
        self.assertEqual(self.meta(1)["publisher"], "Test Publisher A")
        self.assertEqual(self.meta(1, "other"), {"publisher": "Other"})

    def test_no_csv_leaves_meta_untouched(self):
        self.run_csv(fixture_csv_rows())
        rows = self.rows_with(**{u(5): {"title": "Donkey Kong Bananza 2"}})
        res, _ = self.stage(rows, now=T0 + 100)
        out = self.apply(res["import_id"], now=T0 + 110)
        self.assertNotIn("csv", out["summary"])
        self.assertEqual(self.meta(1)["price_cib"], "24.99")

    def test_missing_staged_csv_410(self):
        res, _ = self.stage(csv=build_csv(fixture_csv_rows()))
        for p in self.imports.glob("*.csv"):
            p.unlink()
        with self.assertRaises(ImportFailure) as cm:
            self.apply(res["import_id"])
        self.assertEqual((cm.exception.status, cm.exception.code), (410, "expired"))
        self.assertEqual(self.q("SELECT status FROM import_run")[0][0], "staged")

    def test_shared_csv_kept_for_other_staged_run(self):
        csv = build_csv(fixture_csv_rows())
        a, _ = self.stage(csv=csv, now=T0)
        rows = self.rows_with(**{u(5): {"title": "Donkey Kong Bananza 2"}})
        b, _ = self.stage(rows, csv=csv, now=T0 + 1)
        self.apply(a["import_id"], now=T0 + 2)
        self.assertEqual(len(list(self.imports.glob("*.csv"))), 1)


if __name__ == "__main__":
    unittest.main()
