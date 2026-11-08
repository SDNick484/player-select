"""SQLite access: connections, migrations and the single-writer Store."""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.*\.sql$")


def connect(path: str | Path, *, writer: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    if writer:
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not has:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS, *, now: int) -> list[int]:
    """Apply pending NNN_*.sql files in order; each is one transaction. Returns versions applied."""
    current = _current_version(conn)
    pending = []
    for f in Path(migrations_dir).iterdir():
        m = _MIGRATION_RE.match(f.name)
        if m and int(m.group(1)) > current:
            pending.append((int(m.group(1)), f))
    applied = []
    for version, f in sorted(pending):
        script = (f"BEGIN IMMEDIATE;\n{f.read_text()}\n"
                  f"INSERT INTO schema_version VALUES ({version}, {int(now)});\nCOMMIT;")
        try:
            conn.executescript(script)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


class Store:
    """One writer connection serialized by a lock; a fresh connection per read."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._writer = connect(self.path, writer=True)

    @contextmanager
    def write(self):
        with self._lock:
            conn = self._writer
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    @contextmanager
    def read(self):
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    def close(self):
        with self._lock:
            self._writer.close()
