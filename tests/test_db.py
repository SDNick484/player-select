from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ps.db import MIGRATIONS, Store, connect, migrate  # noqa: E402

TABLES = {"schema_version", "person", "platform", "account", "work", "work_alias", "import_run",
          "copy", "copy_meta", "play_state", "up_next_order", "dup_ack", "import_change"}


def table_names(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


class DbTest(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def conn(self, writer=True):
        c = connect(self.dir / "ps.db", writer=writer)
        self.addCleanup(c.close)
        return c

    def migrated(self):
        c = self.conn()
        migrate(c, now=1000)
        return c

    def store(self):
        self.migrated()  # migrate() runs outside write(): executescript commits any open transaction
        s = Store(self.dir / "ps.db")
        self.addCleanup(s.close)
        return s

    def insert_copy(self, c, **kw):
        c.execute("INSERT INTO platform (id, name, family) VALUES (1, 'P', 'other')")
        row = dict(platform_id=1, category="game", form="physical", raw_title="T",
                   source="manual", source_id="1", work_id=None, removed_at=None, removed_reason=None)
        row.update(kw)
        cols = ",".join(row)
        c.execute(f"INSERT INTO copy ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))

    def test_migrate_fresh_creates_all_tables(self):
        c = self.conn()
        self.assertEqual(migrate(c, now=1000), [1])
        self.assertEqual(table_names(c), TABLES)

    def test_migrate_twice_applies_nothing(self):
        c = self.conn()
        migrate(c, now=1000)
        self.assertEqual(migrate(c, now=2000), [])

    def test_failed_migration_rolls_back(self):
        mig = self.dir / "mig"
        mig.mkdir()
        (mig / "001_init.sql").write_text((MIGRATIONS / "001_init.sql").read_text())
        c = self.conn()
        migrate(c, mig, now=1000)
        (mig / "002_bad.sql").write_text("CREATE TABLE x(a); SELECT no_such_fn();")
        with self.assertRaises(sqlite3.OperationalError):
            migrate(c, mig, now=2000)
        self.assertNotIn("x", table_names(c))
        self.assertEqual([r[0] for r in c.execute("SELECT version FROM schema_version")], [1])
        self.assertFalse(c.in_transaction)

    def test_pragmas(self):
        c = self.conn()
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertIs(c.row_factory, sqlite3.Row)

    def test_reader_pragmas(self):
        self.migrated()
        r = self.conn(writer=False)
        self.assertEqual(r.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(r.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_copy_game_requires_work(self):
        c = self.migrated()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_copy(c, category="game", work_id=None)

    def test_removed_reason_requires_removed_at(self):
        c = self.migrated()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_copy(c, category="system", removed_reason="sold", removed_at=None)

    def test_store_write_rolls_back_on_exception(self):
        s = self.store()
        with self.assertRaises(RuntimeError):
            with s.write() as c:
                c.execute("INSERT INTO person (name) VALUES ('Player A')")
                raise RuntimeError("boom")
        with s.read() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM person").fetchone()[0], 0)
        with s.write() as c:  # lock released, connection usable
            c.execute("INSERT INTO person (name) VALUES ('Player A')")

    def test_store_read_sees_committed_write(self):
        s = self.store()
        self.assertEqual(s.path, self.dir / "ps.db")
        with s.write() as c:
            c.execute("INSERT INTO person (name) VALUES ('Player B')")
        with s.read() as c:
            self.assertEqual([r["name"] for r in c.execute("SELECT name FROM person")], ["Player B"])


if __name__ == "__main__":
    unittest.main()
