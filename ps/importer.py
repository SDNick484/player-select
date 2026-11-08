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

from ps.catalog import CatalogError, merge
from ps.gameeye_csv import CopyKey, CsvError, CsvRow, MatchResult, match, parse_csv
from ps.gameeye_ged import GedError, GedFile, read_ged
from ps.idmaps import CATEGORY_CODE, UNMAPPED, IdMaps, condition_label, ensure_platform
from ps.titles import display_title, normalize

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
        missing = list(r.problems)
        if r.title is None or not r.title.strip():
            missing.append("title missing")
        if r.platform_id is None and "platform_id out of range" not in r.problems:
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


# ---- CSV enrichment (spec 6.1 step 7, 6.3) -------------------------------------------------

def _parse_csv(data: bytes) -> list[CsvRow]:
    try:
        return parse_csv(data)
    except CsvError as e:
        raise ImportFailure(400, "bad_csv", str(e)) from e


def _match_csv(rows: list[CsvRow], mapped: list[Mapped], maps: IdMaps) -> MatchResult:
    keys = [CopyKey(m.source_id, m.platform_name, m.raw_title, m.created_at,
                    condition_label(m.condition_mask)) for m in mapped]
    return match(keys, rows, maps)


def _csv_summary(res: MatchResult) -> dict:
    return {"enriched": res.enriched, "unmatched": res.unmatched, "ambiguous": res.ambiguous}


def _write_meta(conn: sqlite3.Connection, meta: dict[str, dict[str, str]], now: int) -> None:
    """Upsert matched fields; a field the matched CSV row leaves null loses its stored row (D-b)."""
    ids = {r[0]: r[1] for r in conn.execute(
        "SELECT source_id, id FROM copy WHERE source='gameeye'")}
    for sid, fields in meta.items():
        cid = ids[sid]
        for field, value in fields.items():
            conn.execute(
                "INSERT INTO copy_meta (copy_id, field, value, source, fetched_at)"
                " VALUES (?, ?, ?, 'gameeye_csv', ?) ON CONFLICT (copy_id, field, source)"
                " DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at",
                (cid, field, value, now))
        stale = [f for f in _META_FIELDS if f not in fields]
        conn.execute(f"DELETE FROM copy_meta WHERE copy_id=? AND source='gameeye_csv' AND field IN"
                     f" ({','.join('?' * len(stale))})", (cid, *stale))


_META_FIELDS = ("publisher", "developer", "genre", "metacritic",
                "price_loose", "price_cib", "price_new")


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
    csv_rows = _parse_csv(csv) if csv is not None else None
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
        if csv_rows is not None:
            summary["csv"] = _csv_summary(_match_csv(csv_rows, mapped, maps))
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


# ---- apply (spec 5.4, 6.2) ---------------------------------------------------------------

def attach_work(conn: sqlite3.Connection, raw_title: str) -> int:
    """Return the work for a raw title: alias lookup, else create the work and its alias."""
    key = normalize(raw_title)[0]
    row = conn.execute("SELECT work_id FROM work_alias WHERE work_key=?", (key,)).fetchone()
    if row:
        return row[0]
    wid = conn.execute("INSERT INTO work (title) VALUES (?)", (display_title(raw_title),)).lastrowid
    conn.execute("INSERT INTO work_alias (work_key, work_id) VALUES (?, ?)", (key, wid))
    return wid


def _reattach(conn: sqlite3.Connection, copy_id: int, old: int | None, raw_title: str, now: int) -> int:
    """The work for an unpinned game copy whose title may have changed (owner decision 2026-11-08).

    If the new key would move the copy off a work that has no other copies (active or removed), the
    old work's state comes along: with no alias for the new key the old work is retitled and gains
    the alias; with an alias elsewhere the copy goes there and the old work is merged into it.
    Otherwise this is plain alias lookup / creation (attach_work).
    """
    key = normalize(raw_title)[0]
    alias = conn.execute("SELECT work_id FROM work_alias WHERE work_key=?", (key,)).fetchone()
    target = alias[0] if alias else None
    if old is None or target == old:
        return attach_work(conn, raw_title)
    others = conn.execute("SELECT COUNT(*) FROM copy WHERE work_id=? AND id<>?", (old, copy_id)).fetchone()[0]
    if others:
        return attach_work(conn, raw_title)
    if target is None:
        conn.execute("UPDATE work SET title=? WHERE id=?", (display_title(raw_title), old))
        conn.execute("INSERT INTO work_alias (work_key, work_id) VALUES (?, ?)", (key, old))
        return old
    try:
        merge(conn, target, old, now=now)
    except CatalogError as e:
        raise ImportFailure(e.status, e.code,
                            f"{raw_title!r} joins an existing game, but merging its play state failed: "
                            f"{e.detail}") from e
    return target


_NAMES = {"add": "added", "edit": "edited", "remove": "removed", "restore": "restored"}


def _apply_change(conn, c: dict, plat: int | None, now: int) -> None:
    m, sid = c["after"], c["source_id"]
    if c["op"] == "remove":
        conn.execute("UPDATE copy SET removed_at=? WHERE source='gameeye' AND source_id=?",
                     (now, sid))
        return
    if c["op"] == "add":
        work = attach_work(conn, m.raw_title) if m.category == "game" else None
        conn.execute(
            "INSERT INTO copy (work_id, platform_id, category, form, account_id, raw_title, edition,"
            " condition_mask, country, source, source_id, source_item_id, source_platform_id,"
            " created_at, updated_at) VALUES (?, ?, ?, 'physical', NULL, ?, ?, ?, ?, 'gameeye', ?,"
            " ?, ?, ?, ?)",
            (work, plat, m.category, m.raw_title, m.edition, m.condition_mask, m.country, sid,
             m.source_item_id, m.gameeye_platform_id, m.created_at, m.updated_at))
        return
    cur = conn.execute("SELECT id, work_id, work_pinned FROM copy WHERE source='gameeye' AND "
                       "source_id=?", (sid,)).fetchone()
    if m.category != "game":
        wid, pinned = None, 0
    elif cur["work_pinned"] and cur["work_id"] is not None:
        wid, pinned = cur["work_id"], 1
    else:
        wid, pinned = _reattach(conn, cur["id"], cur["work_id"], m.raw_title, now), 0
    conn.execute(
        "UPDATE copy SET work_id=?, work_pinned=?, platform_id=?, category=?, raw_title=?,"
        " edition=?, condition_mask=?, country=?, source_item_id=?, source_platform_id=?,"
        " updated_at=?, removed_at=NULL, removed_reason=NULL"
        " WHERE source='gameeye' AND source_id=?",
        (wid, pinned, plat, m.category, m.raw_title, m.edition, m.condition_mask, m.country,
         m.source_item_id, m.gameeye_platform_id, m.updated_at, sid))


def apply(store, maps: IdMaps, imports_dir: Path, run_id: int, *, confirm_removals: bool,
          now: int) -> dict:
    imports_dir = Path(imports_dir)
    with store.write() as conn:       # own transaction: a 410 must not roll the expiry back
        if imports_dir.is_dir():
            expire(conn, imports_dir, now=now)
    with store.write() as conn:
        run = conn.execute("SELECT * FROM import_run WHERE id=? AND source='gameeye'",
                           (run_id,)).fetchone()
        if run is None:
            raise ImportFailure(404, "not_found", f"no import run {run_id}")
        if run["status"] == "expired":
            raise ImportFailure(410, "expired", "staged import expired; upload again")
        if run["status"] != "staged":
            raise ImportFailure(409, "not_staged", f"import run is {run['status']}")
        base = _latest_applied(conn)
        if (base["id"] if base else None) != run["base_run_id"]:
            raise ImportFailure(409, "stale_base", "another import was applied since staging")
        path = imports_dir / f"{run['file_sha256']}.ged"
        if not path.is_file():
            raise ImportFailure(410, "expired", "staged file is gone; upload again")
        if json.loads(run["summary_json"]).get("removal_guard") and confirm_removals is not True:
            raise ImportFailure(428, "confirm_removals",
                                "this import removes over 25% of active copies; confirm to proceed")
        csv_rows = None
        if run["csv_sha256"]:
            csv_path = imports_dir / f"{run['csv_sha256']}.csv"
            if not csv_path.is_file():
                raise ImportFailure(410, "expired", "staged CSV is gone; upload again")
            csv_rows = _parse_csv(csv_path.read_bytes())
        mapped, _warnings, skipped = _map_rows(_read_staged(path, imports_dir), maps)
        changes = _diff(conn, mapped, skipped)
        plats = {m.gameeye_platform_id: ensure_platform(conn, m.gameeye_platform_id, maps)[0]
                 for m in mapped}
        counts = {v: 0 for v in _NAMES.values()}
        for c in changes:
            counts[_NAMES[c["op"]]] += 1
            _apply_change(conn, c, plats.get(c["after"].gameeye_platform_id if c["after"] else None),
                          now)
        conn.executemany("UPDATE copy SET last_seen_import=? WHERE source='gameeye' AND source_id=?",
                         [(run_id, m.source_id) for m in mapped])
        csv_summary = None
        if csv_rows is not None:
            res = _match_csv(csv_rows, mapped, maps)
            _write_meta(conn, res.meta, now)
            csv_summary = _csv_summary(res)
        conn.execute("UPDATE import_run SET status='applied', applied_at=? WHERE id=?",
                     (now, run_id))
        summary = {**counts, "unchanged": len(mapped) - counts["added"] - counts["edited"]
                   - counts["restored"], "skipped": len(skipped)}
        if csv_summary is not None:
            summary["csv"] = csv_summary
        live = _live_shas(conn)
    for sha, ext in ((run["file_sha256"], "ged"), (run["csv_sha256"], "csv")):
        if sha and sha not in live:
            (imports_dir / f"{sha}.{ext}").unlink(missing_ok=True)
    return {"import_id": run_id, "applied": True, "summary": summary}
