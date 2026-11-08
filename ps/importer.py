"""GameEye import, part 1: stage a `.ged` backup as a dry-run diff (spec 6.1, 6.4)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from ps.gameeye_ged import GedError, GedFile, read_ged
from ps.idmaps import CATEGORY_CODE, UNMAPPED, IdMaps, condition_label
from ps.titles import normalize

MAX_UPLOAD = 32 * 1024 * 1024
STAGE_TTL = 3600
REMOVAL_GUARD = 0.25
EXPECTED_USER_VERSION = 20
MAX_DB_BYTES = 256 * 1024 * 1024


class ImportFailure(Exception):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.status, self.code, self.detail = status, code, detail or code


@dataclass(frozen=True)
class Mapped:
    source_id: str
    source_item_id: str | None
    gameeye_platform_id: int
    platform_name: str
    category: str
    raw_title: str
    edition: str | None
    work_key: str | None          # None for non-games
    condition_mask: int | None
    country: str | None
    created_at: int | None
    updated_at: int | None


# ---- mapping (spec 5.2, 5.3) -------------------------------------------------------------

def _map_rows(ged_file: GedFile, maps: IdMaps) -> tuple[list[Mapped], list[dict], list[str | None]]:
    """Return (mapped rows, warnings, skipped uuids). Pure; no DB access."""
    mapped: list[Mapped] = []
    warnings: list[dict] = []
    skipped: list[str | None] = []
    unmapped: set[int] = set()
    if ged_file.user_version != EXPECTED_USER_VERSION:
        warnings.append({"kind": "user_version",
                         "detail": f"expected {EXPECTED_USER_VERSION}, got {ged_file.user_version}"})
    for r in ged_file.rows:
        missing = []
        if r.title is None or not r.title.strip():
            missing.append("title missing")
        if r.platform_id is None:
            missing.append("platform_id missing")
        if not r.uuid:
            missing.append("uuid missing")
        if missing:
            warnings.append({"kind": "bad_row", "detail": f"{r.uuid}: {', '.join(missing)}"})
            skipped.append(r.uuid or None)
            continue
        if r.category_id not in CATEGORY_CODE:
            what = "category missing" if r.category_id is None else f"category {r.category_id}"
            warnings.append({"kind": "unknown_category", "detail": f"{r.uuid}: {what}"})
            skipped.append(r.uuid)
            continue
        category = CATEGORY_CODE[r.category_id]
        key, edition = normalize(r.title)
        if category == "game" and not key:
            warnings.append({"kind": "bad_row",
                             "detail": f"{r.uuid}: title has no letters or digits"})
            skipped.append(r.uuid)
            continue
        name = maps.platforms.get(r.platform_id)
        if name is None:
            unmapped.add(r.platform_id)
            name = UNMAPPED.format(id=r.platform_id)
        mapped.append(Mapped(r.uuid, r.item_id, r.platform_id, name, category, r.title, edition,
                             key if category == "game" else None, r.ownership_mask, r.country_id,
                             r.created_at, r.updated_at))
    for pid in sorted(unmapped):
        warnings.append({"kind": "unmapped_platform", "detail": f"GameEye platform {pid}"})
    return mapped, warnings, skipped


# ---- diff (spec 6.1 step 6) --------------------------------------------------------------

_STORED = ("SELECT c.source_id, c.raw_title, c.source_platform_id, c.category, c.condition_mask,"
           " c.country, c.removed_at, p.name FROM copy c JOIN platform p ON p.id = c.platform_id"
           " WHERE c.source = 'gameeye'")


def _stored_fields(row: sqlite3.Row) -> dict:
    return {"raw_title": row["raw_title"], "gameeye_platform_id": row["source_platform_id"],
            "category": row["category"], "condition_mask": row["condition_mask"],
            "country": row["country"]}


def _mapped_fields(m: Mapped) -> dict:
    return {"raw_title": m.raw_title, "gameeye_platform_id": m.gameeye_platform_id,
            "category": m.category, "condition_mask": m.condition_mask, "country": m.country}


def _diff(conn: sqlite3.Connection, mapped: list[Mapped], skipped=()) -> list[dict]:
    """Return change dicts: op, source_id, before (dict|None), after (Mapped|None), plus display."""
    stored = {r["source_id"]: r for r in conn.execute(_STORED).fetchall()}
    present = {m.source_id for m in mapped} | {s for s in skipped if s}
    changes = []
    for m in mapped:
        row = stored.get(m.source_id)
        if row is None:
            op, before = "add", None
        else:
            before = _stored_fields(row)
            edited = before != _mapped_fields(m)
            if row["removed_at"] is not None:
                op = "restore"
            elif edited:
                op = "edit"
            else:
                continue
        changes.append({"op": op, "source_id": m.source_id, "before": before, "after": m,
                        "title": m.raw_title, "platform": m.platform_name,
                        "condition": condition_label(m.condition_mask)})
    for sid in sorted(set(stored) - present):
        row = stored[sid]
        if row["removed_at"] is not None:
            continue
        changes.append({"op": "remove", "source_id": sid, "before": _stored_fields(row),
                        "after": None, "title": row["raw_title"], "platform": row["name"],
                        "condition": condition_label(row["condition_mask"])})
    return changes


# ---- files, expiry -----------------------------------------------------------------------

def _write_private(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _live_shas(conn: sqlite3.Connection) -> set[str]:
    out = set()
    for r in conn.execute("SELECT file_sha256, csv_sha256 FROM import_run WHERE status='staged'"):
        out.update(x for x in r if x)
    return out


def expire(conn: sqlite3.Connection, imports_dir: Path, *, now: int) -> int:
    """Mark staged runs older than STAGE_TTL expired and delete files no live run still needs."""
    rows = conn.execute("SELECT id, file_sha256, csv_sha256 FROM import_run "
                        "WHERE status='staged' AND staged_at < ?", (now - STAGE_TTL,)).fetchall()
    for r in rows:
        conn.execute("UPDATE import_run SET status='expired' WHERE id=?", (r["id"],))
    live = _live_shas(conn)
    for r in rows:
        for sha, ext in ((r["file_sha256"], "ged"), (r["csv_sha256"], "csv")):
            if sha and sha not in live:
                (Path(imports_dir) / f"{sha}.{ext}").unlink(missing_ok=True)
    return len(rows)


# ---- stage -------------------------------------------------------------------------------

def _read_staged(path: Path, imports_dir: Path) -> GedFile:
    workdir = Path(tempfile.mkdtemp(dir=imports_dir, prefix=path.stem + ".", suffix=".d"))
    try:
        return read_ged(path, workdir, max_bytes=MAX_DB_BYTES)
    except GedError as e:
        raise ImportFailure(400, e.code, str(e)) from e
    except Exception as e:  # odd column types etc.: still a bad upload, never a 500
        raise ImportFailure(400, "bad_ged", f"unreadable backup: {e}") from e
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _latest_applied(conn: sqlite3.Connection):
    return conn.execute("SELECT id, file_sha256 FROM import_run WHERE source='gameeye' AND "
                        "status='applied' ORDER BY id DESC LIMIT 1").fetchone()


def _active_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM copy WHERE source='gameeye' "
                        "AND removed_at IS NULL").fetchone()[0]


def stage(store, maps: IdMaps, imports_dir: Path, ged: bytes, csv: bytes | None,
          *, now: int) -> dict:
    imports_dir = Path(imports_dir)
    imports_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(imports_dir, 0o700)
    with store.write() as conn:
        expire(conn, imports_dir, now=now)
    if len(ged) > MAX_UPLOAD or (csv is not None and len(csv) > MAX_UPLOAD):
        raise ImportFailure(413, "too_large", f"upload exceeds {MAX_UPLOAD} bytes")
    sha = hashlib.sha256(ged).hexdigest()
    csv_sha = hashlib.sha256(csv).hexdigest() if csv is not None else None
    if csv is None:
        with store.read() as conn:
            last = _latest_applied(conn)
            if last and last["file_sha256"] == sha:
                return {"import_id": None, "summary": {"unchanged": _active_count(conn)}}
    ged_path = imports_dir / f"{sha}.ged"
    _write_private(ged_path, ged)
    try:
        ged_file = _read_staged(ged_path, imports_dir)
    except ImportFailure:
        with store.write() as conn:
            if sha not in _live_shas(conn):
                ged_path.unlink(missing_ok=True)
        raise
    if csv is not None:
        _write_private(imports_dir / f"{csv_sha}.csv", csv)
    mapped, warnings, skipped = _map_rows(ged_file, maps)
    with store.write() as conn:
        base = _latest_applied(conn)
        changes = _diff(conn, mapped, skipped)
        counts = {"added": 0, "edited": 0, "removed": 0, "restored": 0}
        names = {"add": "added", "edit": "edited", "remove": "removed", "restore": "restored"}
        for c in changes:
            counts[names[c["op"]]] += 1
        unchanged = len(mapped) - counts["added"] - counts["edited"] - counts["restored"]
        summary = {**counts, "unchanged": unchanged, "skipped": len(skipped)}
        active = _active_count(conn)
        guard = bool(active) and counts["removed"] > REMOVAL_GUARD * active
        run_id = conn.execute(
            "INSERT INTO import_run (source, file_sha256, csv_sha256, base_run_id, status,"
            " staged_at, summary_json) VALUES ('gameeye', ?, ?, ?, 'staged', ?, ?)",
            (sha, csv_sha, base["id"] if base else None, now,
             json.dumps({**summary, "removal_guard": guard, "warnings": warnings}))).lastrowid
        conn.executemany(
            "INSERT INTO import_change (run_id, op, source_id, before_json, after_json)"
            " VALUES (?, ?, ?, ?, ?)",
            [(run_id, c["op"], c["source_id"],
              json.dumps(c["before"]) if c["before"] is not None else None,
              json.dumps(asdict(c["after"])) if c["after"] is not None else None)
             for c in changes])
    return {"import_id": run_id, "dry_run": True, "summary": summary, "removal_guard": guard,
            "warnings": warnings,
            "changes": [{k: c[k] for k in ("op", "source_id", "title", "platform", "condition")}
                        for c in changes]}
