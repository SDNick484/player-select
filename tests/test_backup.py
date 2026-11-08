"""backup.sh: dated online backup, 14-day pruning, exit 78 without a database."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "backup.sh"
HAVE_SQLITE3 = shutil.which("sqlite3") is not None


class BackupTest(unittest.TestCase):
    def setUp(self):
        if not HAVE_SQLITE3:
            if os.environ.get("REQUIRE_SQLITE3") == "1":
                self.fail("sqlite3 CLI is required (REQUIRE_SQLITE3=1)")
            self.skipTest("sqlite3 CLI not installed")
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.data = Path(td.name)

    def make_db(self):
        conn = sqlite3.connect(self.data / "player_select.db")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(1,), (2,), (3,)])
        conn.commit()
        return conn

    def run_backup(self):
        return subprocess.run(["sh", str(BACKUP), str(self.data)], capture_output=True, text=True)

    def test_backup_creates_dated_copy(self):
        conn = self.make_db()  # left open: the online backup must work with a live WAL connection
        self.addCleanup(conn.close)
        r = self.run_backup()
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.data / "backups" / f"player_select-{date.today().isoformat()}.db"
        self.assertTrue(out.is_file())
        copy = sqlite3.connect(out)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT COUNT(*) FROM t").fetchone()[0], 3)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, "one log line per run")

    def test_backup_prunes_older_than_14_days(self):
        self.make_db().close()
        bdir = self.data / "backups"
        bdir.mkdir()
        old, kept, other = (bdir / "player_select-2000-01-01.db", bdir / "player_select-2000-01-02.db",
                            bdir / "notes.txt")
        for f in (old, kept, other):
            f.write_bytes(b"x")
        now = time.time()
        os.utime(old, (now - 15 * 86400, now - 15 * 86400))
        os.utime(kept, (now - 13 * 86400, now - 13 * 86400))
        os.utime(other, (now - 40 * 86400, now - 40 * 86400))
        r = self.run_backup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(other.exists(), "only player_select-*.db is pruned")

    def test_backup_without_db_exits_78(self):
        r = self.run_backup()
        self.assertEqual(r.returncode, 78)
        self.assertFalse((self.data / "backups").exists() and any((self.data / "backups").iterdir()))


if __name__ == "__main__":
    unittest.main()
