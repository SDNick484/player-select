"""Synthetic GameEye backup builder shared by tests (no real data)."""
from __future__ import annotations

import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path

OWNERSHIP_COLUMNS = ("uuid", "item_id", "platform_id", "category_id", "title", "ownership_mask",
                     "country_id", "created_at", "updated_at", "purchase_price")

_DEFAULTS = {"uuid": "00000000-0000-4000-8000-000000000000", "item_id": 1000, "platform_id": 46,
             "category_id": 0, "title": "Test Game", "ownership_mask": 7, "country_id": 1,
             "created_at": 1700000000, "updated_at": 1700000000, "purchase_price": 0.0}


def ged_row(**kw) -> dict:
    unknown = set(kw) - set(OWNERSHIP_COLUMNS)
    if unknown:
        raise TypeError(f"unknown column(s): {sorted(unknown)}")
    return {**_DEFAULTS, **kw}


def _u(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


FIXTURE_ROWS = [
    ged_row(uuid=_u(1), item_id=2001, platform_id=46, title="Bloodborne", ownership_mask=5,
            created_at=1700000000, updated_at=1700000000),
    ged_row(uuid=_u(2), item_id=2001, platform_id=46, title="Bloodborne", ownership_mask=7,
            created_at=1700100000, updated_at=1700100000),
    ged_row(uuid=_u(3), item_id=2002, platform_id=14, title="Dead to Rights", ownership_mask=7,
            created_at=1700200000, updated_at=1700200000),
    ged_row(uuid=_u(4), item_id=2002, platform_id=14, title="Dead to Rights", ownership_mask=7,
            created_at=1700210000, updated_at=1700210000),
    ged_row(uuid=_u(5), item_id=2003, platform_id=178, title="Donkey Kong Bananza",
            ownership_mask=7, created_at=1700300000, updated_at=1700300000),
    ged_row(uuid=_u(6), item_id=2004, platform_id=178, category_id=1, title="Nintendo Switch 2",
            ownership_mask=7, created_at=1700400000, updated_at=1700400000),
    ged_row(uuid=_u(7), item_id=2005, platform_id=199, title="Test Unmapped Game",
            ownership_mask=7, created_at=1700500000, updated_at=1700500000),
    ged_row(uuid=_u(8), item_id=2006, platform_id=46, category_id=9, title="Test Odd Category",
            ownership_mask=1, created_at=1700600000, updated_at=1700600000),
]


def build_ged(rows, *, user_version: int = 20, member: str = "ownership_database.db",
              extra_members=(), drop_columns=(), table: str = "ownership") -> bytes:
    cols = [c for c in OWNERSHIP_COLUMNS if c not in drop_columns]
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "ownership_database.db"
        con = sqlite3.connect(db_path)
        try:
            con.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
            marks = ", ".join("?" for _ in cols)
            con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
                            [tuple(r[c] for c in cols) for r in rows])
            con.execute(f"PRAGMA user_version = {int(user_version)}")
            con.commit()
        finally:
            con.close()
        data = db_path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, data)
        for name in extra_members:
            z.writestr(name, b"extra")
    return buf.getvalue()
