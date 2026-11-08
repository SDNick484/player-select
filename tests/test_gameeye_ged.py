from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ged import FIXTURE_ROWS, build_ged, ged_row  # noqa: E402
from ps.gameeye_ged import GedError, _copy_capped, read_ged  # noqa: E402


class GedReaderTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        self.work = self.dir / "work"
        self.work.mkdir()

    def read(self, data: bytes, **kw):
        p = self.dir / "in.ged"
        p.write_bytes(data)
        return read_ged(p, self.work, **kw)

    def code(self, data: bytes, **kw) -> str:
        with self.assertRaises(GedError) as cm:
            self.read(data, **kw)
        return cm.exception.code

    def test_reads_fixture(self):
        g = self.read(build_ged(FIXTURE_ROWS))
        self.assertEqual(len(g.rows), 8)
        self.assertEqual(g.user_version, 20)
        r = g.rows[0]
        self.assertEqual(r.uuid, "00000000-0000-4000-8000-000000000001")
        self.assertIsInstance(r.platform_id, int)
        self.assertEqual(r.country_id, "1")
        self.assertIsInstance(r.created_at, int)
        self.assertEqual(r.title, "Bloodborne")
        self.assertEqual(r.ownership_mask, 5)

    def test_not_a_zip(self):
        self.assertEqual(self.code(b"definitely not a zip"), "not_zip")

    def test_two_members(self):
        self.assertEqual(self.code(build_ged(FIXTURE_ROWS, extra_members=("other.txt",))),
                         "bad_members")

    def test_wrong_member_name(self):
        self.assertEqual(self.code(build_ged(FIXTURE_ROWS, member="other.db")), "bad_members")

    def test_declared_too_large(self):
        self.assertEqual(self.code(build_ged(FIXTURE_ROWS), max_bytes=100), "too_large")

    def test_copy_capped_stops_at_limit(self):
        with self.assertRaises(GedError) as cm:
            _copy_capped(io.BytesIO(b"x" * 101), io.BytesIO(), 100)
        self.assertEqual(cm.exception.code, "too_large")
        self.assertEqual(_copy_capped(io.BytesIO(b"x" * 101), io.BytesIO(), 101), 101)

    def test_member_not_sqlite(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ownership_database.db", b"this is not a database" * 100)
        self.assertEqual(self.code(buf.getvalue()), "not_sqlite")

    def test_missing_table(self):
        self.assertEqual(self.code(build_ged(FIXTURE_ROWS, table="things")), "missing_table")

    def test_missing_column(self):
        with self.assertRaises(GedError) as cm:
            self.read(build_ged(FIXTURE_ROWS, drop_columns=("country_id",)))
        self.assertEqual(cm.exception.code, "missing_column")
        self.assertIn("country_id", str(cm.exception))

    def test_duplicate_uuid(self):
        rows = FIXTURE_ROWS + [ged_row(uuid=FIXTURE_ROWS[0]["uuid"])]
        self.assertEqual(self.code(build_ged(rows)), "duplicate_uuid")

    def test_user_version_reported(self):
        self.assertEqual(self.read(build_ged(FIXTURE_ROWS, user_version=21)).user_version, 21)

    def test_null_title_kept_none(self):
        g = self.read(build_ged([ged_row(uuid=FIXTURE_ROWS[0]["uuid"], title=None)]))
        self.assertIsNone(g.rows[0].title)

    def test_extracted_db_not_modified(self):
        data = build_ged(FIXTURE_ROWS)
        self.read(data)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            original = z.read("ownership_database.db")
        self.assertEqual((self.work / "ownership_database.db").read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["ownership_database.db"])


if __name__ == "__main__":
    unittest.main()
