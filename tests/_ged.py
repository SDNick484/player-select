"""Synthetic GameEye backup builder shared by tests (no real data)."""
from __future__ import annotations

import datetime
import io
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from ps.gameeye_csv import CSV_COLUMNS
from ps.idmaps import load as _load_maps

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


_CSV_DEFAULTS = {"Platform": "Sony PlayStation 4", "Category": "Games", "UserRecordType": "Collection",
                 "Title": "Test Game", "Country": "United States of America",
                 "ReleaseType": "Regular", "CreatedAt": "Nov 8, 2026", "Ownership": "CIB"}


def csv_row(**kw) -> dict:
    unknown = set(kw) - set(CSV_COLUMNS)
    if unknown:
        raise TypeError(f"unknown column(s): {sorted(unknown)}")
    return {c: kw.get(c, _CSV_DEFAULTS.get(c, "")) for c in CSV_COLUMNS}


def _csv_date(ts: int) -> str:
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return f"{d:%b} {d.day}, {d.year}"


def fixture_csv_rows() -> list[dict]:
    """One CSV row for each FIXTURE_ROWS uuid 01-06, labels taken from the real ID maps."""
    maps = _load_maps()
    extra = {
        1: dict(Publisher="Test Publisher A", Developer="Test Studio A", Genre="Action, Adventure",
                metacritic="92", PriceLoose="10.00", PriceCIB="24.99", PriceNew="59.99"),
        2: dict(Publisher="Test Publisher A", Developer="Test Studio A", Genre="Action, Adventure",
                metacritic="92", PriceLoose="10.00", PriceCIB="27.50", PriceNew="59.99"),
        3: dict(Publisher="Test Publisher B", Developer="Test Studio B", Genre="Action",
                metacritic="?", PriceLoose="4.00", PriceCIB="9.00", PriceNew="Missing Field"),
        4: dict(Publisher="Test Publisher B", Developer="Test Studio B", Genre="Action",
                metacritic="?", PriceLoose="4.00", PriceCIB="9.00", PriceNew="Missing Field"),
        5: dict(Publisher="Test Publisher C", Developer="Test Studio C", Genre="Platformer",
                metacritic="88", PriceLoose="-1.0", PriceCIB="45.00", PriceNew="0.0"),
        6: dict(Publisher="Test Publisher C", Developer="Test Studio C", Genre="",
                metacritic="", PriceLoose="", PriceCIB="", PriceNew="399.99"),
    }
    out = []
    for n, r in enumerate(FIXTURE_ROWS[:6], start=1):
        out.append(csv_row(
            Platform=maps.platforms[r["platform_id"]], Category=maps.categories[r["category_id"]],
            Ownership=maps.ownership[r["ownership_mask"]], Title=r["title"],
            CreatedAt=_csv_date(r["created_at"]), **extra[n]))
    return out


def _q(v: str) -> str:
    return v if v == "?" else '"' + v.replace('"', '""') + '"'


def build_csv(rows, *, trailing_comma: bool = True, last_line_trailing: bool = True,
              leading_space: bool = False) -> bytes:
    sep = ", " if leading_space else ","
    lines = [",".join(CSV_COLUMNS)]
    for i, row in enumerate(rows):
        line = sep.join(_q(row[c]) for c in CSV_COLUMNS)
        last = i == len(rows) - 1
        if trailing_comma and (last_line_trailing or not last):
            line += ","
        lines.append(line)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")
