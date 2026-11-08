"""Catalog read model and the small mutations behind play state, Up next and removal reasons.

Mutating functions run inside the caller's transaction (``Store.write()``) and never commit.
"""
from __future__ import annotations

import json
import sqlite3

from .idmaps import condition_label

STATUSES = ("backlog", "up_next", "playing", "finished", "abandoned", "not_for_me")
REASONS = ("sold", "traded", "lost", "gifted", "mistake")
NOTE_MAX = 2000


class CatalogError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def seed_persons(conn: sqlite3.Connection, persons: list[dict]) -> None:
    """Insert persons whose name is missing; existing rows are untouched; never deletes."""
    for i, p in enumerate(persons):
        conn.execute("INSERT OR IGNORE INTO person (name, color, sort) VALUES (?, ?, ?)",
                     (p["name"], p.get("color"), i))


# ---- read model --------------------------------------------------------------------------

def _meta_value(field: str, value: str):
    if field == "genre":
        return json.loads(value)
    if field == "metacritic":
        return int(float(value))
    if field.startswith("price_"):
        return float(value)
    return value


def _catalog(conn: sqlite3.Connection) -> dict:
    persons = [{"id": r["id"], "name": r["name"], "color": r["color"]} for r in conn.execute(
        "SELECT id, name, color FROM person ORDER BY sort, id")]
    platforms = [dict(r) for r in conn.execute(
        "SELECT id, name, family, gameeye_id FROM platform ORDER BY id")]
    works = [dict(r) for r in conn.execute("SELECT id, title FROM work ORDER BY id")]
    copies = []
    for r in conn.execute(
            "SELECT id, work_id, platform_id, category, form, raw_title, edition, condition_mask,"
            " country, source, source_id, created_at, removed_at, removed_reason"
            " FROM copy ORDER BY id"):
        d = dict(r)
        d["condition"] = condition_label(d["condition_mask"])
        copies.append(d)
    meta: dict[int, dict] = {}
    for r in conn.execute("SELECT copy_id, field, value FROM copy_meta ORDER BY copy_id, field,"
                          " source"):
        meta.setdefault(r["copy_id"], {})[r["field"]] = _meta_value(r["field"], r["value"])
    play_state = [{"person": r["person_id"], "work": r["work_id"], "status": r["status"],
                   "note": r["note"], "updated_at": r["updated_at"]} for r in conn.execute(
        "SELECT person_id, work_id, status, note, updated_at FROM play_state"
        " ORDER BY person_id, work_id")]
    up_next: dict[int, list[int]] = {}
    for r in conn.execute("SELECT person_id, work_id FROM up_next_order"
                          " ORDER BY person_id, position, work_id"):
        up_next.setdefault(r["person_id"], []).append(r["work_id"])
    run = conn.execute("SELECT id, applied_at, summary_json FROM import_run"
                       " WHERE status='applied' ORDER BY applied_at DESC, id DESC LIMIT 1").fetchone()
    last_import = None if run is None else {
        "id": run["id"], "applied_at": run["applied_at"], "summary": json.loads(run["summary_json"])}
    return {"persons": persons, "platforms": platforms, "works": works, "copies": copies,
            "meta": meta, "play_state": play_state, "up_next": up_next, "dup_groups": [],
            "last_import": last_import}


def catalog(conn: sqlite3.Connection) -> dict:
    """The whole read model as one consistent snapshot. Never commits."""
    if conn.in_transaction:
        return _catalog(conn)
    conn.execute("BEGIN")
    try:
        return _catalog(conn)
    finally:
        conn.execute("ROLLBACK")


# ---- mutations ---------------------------------------------------------------------------

def _bad(detail: str) -> CatalogError:
    return CatalogError(400, "bad_request", detail)


def _not_found(what: str) -> CatalogError:
    return CatalogError(404, "not_found", f"unknown {what}")


def _require(conn, table: str, id_: int, what: str) -> None:
    if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (id_,)).fetchone() is None:
        raise _not_found(what)


def set_play_state(conn: sqlite3.Connection, person: int, work: int, status: str,
                   note: str | None, *, now: int) -> dict | None:
    """Upsert (or, for status "", delete) one person's state for a work; returns the previous one."""
    if not _is_int(person) or not _is_int(work):
        raise _bad("person and work must be integers")
    if not isinstance(status, str) or (status != "" and status not in STATUSES):
        raise _bad("unknown status")
    if note is not None and not isinstance(note, str):
        raise _bad("note must be a string")
    if note is not None:
        note = note.strip() or None
    if note is not None and len(note) > NOTE_MAX:
        raise CatalogError(400, "note_too_long", f"note is limited to {NOTE_MAX} characters")
    _require(conn, "person", person, "person")
    _require(conn, "work", work, "work")
    row = conn.execute("SELECT status, note FROM play_state WHERE person_id=? AND work_id=?",
                       (person, work)).fetchone()
    previous = None if row is None else {"status": row["status"], "note": row["note"]}
    if status == "":
        conn.execute("DELETE FROM play_state WHERE person_id=? AND work_id=?", (person, work))
        conn.execute("DELETE FROM up_next_order WHERE person_id=? AND work_id=?", (person, work))
        return previous
    conn.execute(
        "INSERT INTO play_state (person_id, work_id, status, note, updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT (person_id, work_id) DO UPDATE SET status=excluded.status,"
        " note=excluded.note, updated_at=excluded.updated_at",
        (person, work, status, note, now))
    if status == "up_next":
        conn.execute(
            "INSERT OR IGNORE INTO up_next_order (person_id, work_id, position)"
            " SELECT ?, ?, COALESCE(MAX(position) + 1, 0) FROM up_next_order WHERE person_id=?",
            (person, work, person))
    else:
        conn.execute("DELETE FROM up_next_order WHERE person_id=? AND work_id=?", (person, work))
    return previous


def set_up_next(conn: sqlite3.Connection, person: int, order: list[int]) -> None:
    """Rewrite one person's Up next order; it must be a permutation of their up_next works."""
    if not _is_int(person):
        raise _bad("person must be an integer")
    _require(conn, "person", person, "person")
    if not isinstance(order, list) or not all(_is_int(w) for w in order):
        raise _bad("order must be a list of work ids")
    if len(set(order)) != len(order):
        raise _bad("order contains duplicates")
    current = {r[0] for r in conn.execute(
        "SELECT work_id FROM play_state WHERE person_id=? AND status='up_next'", (person,))}
    if set(order) != current:
        raise CatalogError(409, "order_mismatch", "order must list exactly the up_next works")
    conn.execute("DELETE FROM up_next_order WHERE person_id=?", (person,))
    conn.executemany("INSERT INTO up_next_order (person_id, work_id, position) VALUES (?,?,?)",
                     [(person, w, i) for i, w in enumerate(order)])


def set_removed_reason(conn: sqlite3.Connection, copy_id: int, reason: str | None) -> None:
    """Record why a removed copy left the collection; None clears it."""
    if not _is_int(copy_id):
        raise _bad("copy id must be an integer")
    if reason is not None and (not isinstance(reason, str) or reason not in REASONS):
        raise _bad("unknown removal reason")
    row = conn.execute("SELECT removed_at FROM copy WHERE id=?", (copy_id,)).fetchone()
    if row is None:
        raise _not_found("copy")
    if row["removed_at"] is None:
        raise CatalogError(409, "not_removed", "copy is still active")
    conn.execute("UPDATE copy SET removed_reason=? WHERE id=?", (reason, copy_id))
