from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ps.db import connect, migrate  # noqa: E402
from ps.idmaps import (CATEGORY_CODE, FAMILY, UNMAPPED, IdMaps, condition_label,  # noqa: E402
                       ensure_platform, family_of, load, sync_platforms)


class IdMapsTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def conn(self):
        c = connect(self.dir / "ps.db", writer=True)
        self.addCleanup(c.close)
        migrate(c, now=1000)
        return c

    def test_category_3_label(self):
        self.assertEqual(load().categories[3], "Toys To Life")

    def test_counts(self):
        m = load()
        self.assertEqual(len(m.platforms), 36)
        self.assertEqual(m.ownership, {1: "Loose", 5: "Boxed", 7: "CIB", 31: "New"})
        self.assertEqual(set(CATEGORY_CODE), set(m.categories))

    def test_family_table_covers_map(self):
        self.assertEqual(set(load().platforms), set(FAMILY))

    def test_family_spot_checks(self):
        for gid, fam in [(46, "playstation"), (105, "playstation"), (37, "playstation"),
                         (106, "xbox"), (178, "nintendo"), (76, "nintendo"), (32, "sega"),
                         (27, "atari"), (1, "pc"), (33, "nec"), (168, "media"),
                         (152, "other"), (999, "other")]:
            self.assertEqual(family_of(gid), fam, gid)

    def test_condition_label(self):
        self.assertEqual(condition_label(7), "CIB")
        self.assertEqual(condition_label(1), "Loose")
        self.assertEqual(condition_label(3), "Unknown (3)")
        self.assertEqual(condition_label(None), "Unknown")

    def test_load_malformed(self):
        p = self.dir / "bad.json"
        p.write_text("{not json")
        with self.assertRaises(ValueError):
            load(p)
        p.write_text(json.dumps({"platforms": {"x": "A"}, "categories": {}, "ownership_mask": {}}))
        with self.assertRaises(ValueError):
            load(p)
        p.write_text(json.dumps({"platforms": {}}))
        with self.assertRaises(ValueError):
            load(p)

    def test_sync_platforms_idempotent(self):
        c = self.conn()
        maps = load()
        for _ in range(2):
            c.execute("BEGIN IMMEDIATE")
            sync_platforms(c, maps)
            c.execute("COMMIT")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM platform").fetchone()[0], 36)
        row = c.execute("SELECT name, family FROM platform WHERE gameeye_id=105").fetchone()
        self.assertEqual((row["name"], row["family"]), ("Sony PlayStation 5", "playstation"))

    def test_ensure_platform_unmapped_once(self):
        c = self.conn()
        maps = load()
        c.execute("BEGIN IMMEDIATE")
        pid, created = ensure_platform(c, 199, maps)
        self.assertTrue(created)
        self.assertEqual(ensure_platform(c, 199, maps), (pid, False))
        c.execute("COMMIT")
        row = c.execute("SELECT name, family FROM platform WHERE id=?", (pid,)).fetchone()
        self.assertEqual((row["name"], row["family"]), (UNMAPPED.format(id=199), "other"))

    def test_ensure_platform_mapped(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        pid, created = ensure_platform(c, 97, load())
        c.execute("COMMIT")
        self.assertTrue(created)
        self.assertEqual(c.execute("SELECT name FROM platform WHERE id=?", (pid,)).fetchone()[0],
                         "Nintendo Switch")

    def test_sync_renames_placeholder(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        pid, _ = ensure_platform(c, 199, load())
        maps = IdMaps(platforms={199: "Test Console"}, categories={}, ownership={})
        sync_platforms(c, maps)
        c.execute("COMMIT")
        row = c.execute("SELECT id, name, family FROM platform WHERE gameeye_id=199").fetchone()
        self.assertEqual((row["id"], row["name"], row["family"]), (pid, "Test Console", "other"))

    def test_sync_keeps_unmapped_placeholder_when_unknown(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        ensure_platform(c, 199, load())
        sync_platforms(c, load())
        c.execute("COMMIT")
        self.assertEqual(c.execute("SELECT name FROM platform WHERE gameeye_id=199").fetchone()[0],
                         UNMAPPED.format(id=199))


if __name__ == "__main__":
    unittest.main()
