"""Read a GameEye `.ged` backup (zip holding one SQLite DB). Pure: never touches the app DB."""
from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

MEMBER = "ownership_database.db"
REQUIRED_COLUMNS = ("uuid", "item_id", "platform_id", "category_id", "title", "ownership_mask",
                    "country_id", "created_at", "updated_at")


class GedError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class GedRow:
    uuid: str
    item_id: str | None
    platform_id: int | None
    category_id: int | None
    title: str | None
    ownership_mask: int | None
    country_id: str | None
    created_at: int | None
    updated_at: int | None
    problems: tuple[str, ...] = ()   # out-of-range values (set to None): the importer skips the row


@dataclass
class GedFile:
    rows: list[GedRow]
    user_version: int


def _copy_capped(src: BinaryIO, dst: BinaryIO, limit: int) -> int:
    total = 0
    while True:
        chunk = src.read(min(65536, limit - total + 1))
        if not chunk:
            return total
        total += len(chunk)
        if total > limit:
            raise GedError("too_large", f"database exceeds {limit} bytes")
        dst.write(chunk)


INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1
TS_MAX = 253402300799                 # 9999-12-31T23:59:59Z: the last second datetime can show


def _int(v):
    return None if v is None else int(v)


def _ranged(v, name: str, lo: int, hi: int, problems: list[str]):
    """int(v), or None with a problem noted when it is outside [lo, hi] (SQLite INTEGER is int64)."""
    n = _int(v)
    if n is not None and not lo <= n <= hi:
        problems.append(f"{name} out of range")
        return None
    return n


def _str(v):
    return None if v is None else str(v)


def read_ged(path: Path, workdir: Path, *, max_bytes: int = 256 * 1024 * 1024) -> GedFile:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise GedError("not_zip", "not a zip file") from e
    with zf:
        infos = zf.infolist()
        if len(infos) != 1 or infos[0].filename != MEMBER:
            raise GedError("bad_members", f"archive must contain exactly {MEMBER}")
        info = infos[0]
        if info.file_size > max_bytes:
            raise GedError("too_large", f"database exceeds {max_bytes} bytes")
        db_path = Path(workdir) / MEMBER
        try:
            with zf.open(info) as src, open(db_path, "wb") as dst:
                _copy_capped(src, dst, max_bytes)
        except (zipfile.BadZipFile, OSError, EOFError) as e:
            raise GedError("not_zip", "corrupt archive") from e
    return _read_db(db_path)


def _read_db(db_path: Path) -> GedFile:
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
        try:
            info = con.execute("PRAGMA table_info(ownership)").fetchall()
            if not info:
                raise GedError("missing_table", "no ownership table")
            have = {r[1] for r in info}
            for c in REQUIRED_COLUMNS:
                if c not in have:
                    raise GedError("missing_column", f"ownership.{c} is missing")
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
            cur = con.execute(f"SELECT {', '.join(REQUIRED_COLUMNS)} FROM ownership")
            rows: list[GedRow] = []
            seen: set[str] = set()
            for r in cur:
                uid = _str(r[0])
                if uid:                   # a missing uuid is a bad row (importer), not a duplicate
                    if uid in seen:
                        raise GedError("duplicate_uuid", f"duplicate uuid {uid}")
                    seen.add(uid)
                p: list[str] = []
                ints = [_ranged(r[i], REQUIRED_COLUMNS[i], INT64_MIN, INT64_MAX, p) for i in (2, 3, 5)]
                stamps = [_ranged(r[i], REQUIRED_COLUMNS[i], 0, TS_MAX, p) for i in (7, 8)]
                rows.append(GedRow(uid, _str(r[1]), ints[0], ints[1], _str(r[4]), ints[2], _str(r[6]),
                                   stamps[0], stamps[1], tuple(p)))
        except sqlite3.DatabaseError as e:
            raise GedError("not_sqlite", "not a readable SQLite database") from e
    except sqlite3.DatabaseError as e:
        raise GedError("not_sqlite", "not a readable SQLite database") from e
    finally:
        if con is not None:
            con.close()
    return GedFile(rows, user_version)
