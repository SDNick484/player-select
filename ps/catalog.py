"""Catalog read model and the small mutations behind play state, Up next and removal reasons.

Mutating functions run inside the caller's transaction (``Store.write()``) and never commit.
"""
from __future__ import annotations

import json
import sqlite3

from .idmaps import condition_label
from .titles import normalize

STATUSES = ("backlog", "up_next", "playing", "finished", "abandoned", "not_for_me")
REASONS = ("sold", "traded", "lost", "gifted", "mistake")
NOTE_MAX = 2000


class CatalogError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


INT64_MIN, INT64_MAX = -(2 ** 63), 2 ** 63 - 1


def is_int(v) -> bool:
    """A JSON integer SQLite can bind (signed 64-bit); larger ones are malformed input, not a 500."""
    return isinstance(v, int) and not isinstance(v, bool) and INT64_MIN <= v <= INT64_MAX


_is_int = is_int


def seed_persons(conn: sqlite3.Connection, persons: list[dict]) -> list[str]:
    """Make the person table follow the persons file (owner decision 2026-11-08).

    Names in the file are inserted or get the file's color and position (sort = list index); ids are
    kept, so play state stays attached. A person missing from the file is deleted only when they have
    no play_state and no up_next_order rows; otherwise they are kept, sorted after the file's names.
    Returns one log line per person kept or removed. An empty list (no file) changes nothing.
    """
    if not persons:
        return []
    for i, p in enumerate(persons):
        conn.execute("INSERT INTO person (name, color, sort) VALUES (?, ?, ?) ON CONFLICT (name)"
                     " DO UPDATE SET color=excluded.color, sort=excluded.sort",
                     (p["name"], p.get("color"), i))
    names = {p["name"] for p in persons}
    log, kept = [], 0
    for r in conn.execute("SELECT id, name FROM person ORDER BY sort, id").fetchall():
        if r["name"] in names:
            continue
        played = conn.execute("SELECT EXISTS (SELECT 1 FROM play_state WHERE person_id=?)"
                              " OR EXISTS (SELECT 1 FROM up_next_order WHERE person_id=?)",
                              (r["id"], r["id"])).fetchone()[0]
        # account rows (reserved for adapters) reference person too: never delete under them either
        owns = conn.execute("SELECT EXISTS (SELECT 1 FROM account WHERE person_id=?)",
                            (r["id"],)).fetchone()[0]
        if played or owns:
            conn.execute("UPDATE person SET sort=? WHERE id=?", (len(persons) + kept, r["id"]))
            kept += 1
            why = "play state" if played else "accounts"
            log.append(f"persons: kept {r['name']!r} (not in the persons file, but has {why})")
        else:
            conn.execute("DELETE FROM person WHERE id=?", (r["id"],))
            log.append(f"persons: removed {r['name']!r} (not in the persons file, no play state)")
    return log


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
            " country, source, source_id, created_at, updated_at, removed_at, removed_reason"
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
            "meta": meta, "play_state": play_state, "up_next": up_next, "dup_groups": dup_groups(conn),
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


# ---- merge, split, duplicates -----------------------------------------------------------

def merge(conn: sqlite3.Connection, into: int, from_: int, *, now: int) -> None:
    """Fold work ``from_`` into ``into`` (spec 5.4, D-d); ``now`` is unused, play_state keeps stamps."""
    if not _is_int(into) or not _is_int(from_):
        raise _bad("into and from must be integers")
    if into == from_:
        raise _bad("cannot merge a work into itself")
    _require(conn, "work", into, "work")
    _require(conn, "work", from_, "work")
    # Decide every play_state outcome first so a 409 happens before anything is written.
    plan = []
    for pid in [r[0] for r in conn.execute(
            "SELECT DISTINCT person_id FROM play_state WHERE work_id IN (?, ?) ORDER BY person_id",
            (into, from_))]:
        rows = {r["work_id"]: r for r in conn.execute(
            "SELECT * FROM play_state WHERE person_id=? AND work_id IN (?, ?)", (pid, into, from_))}
        if len(rows) == 1:
            plan.append((pid, None))
            continue
        a, b = rows[into], rows[from_]
        win, lose = (b, a) if b["updated_at"] > a["updated_at"] else (a, b)
        note = win["note"]
        if win["note"] and lose["note"]:
            note = win["note"] + "\n\n" + lose["note"]
        elif lose["note"]:
            note = lose["note"]
        if note is not None and len(note) > NOTE_MAX:
            raise CatalogError(409, "note_too_long",
                               f"merged note would exceed {NOTE_MAX} characters; shorten one first")
        plan.append((pid, (win["status"], note, win["updated_at"])))
    conn.execute("UPDATE work_alias SET work_id=? WHERE work_id=?", (into, from_))
    conn.execute("UPDATE copy SET work_id=? WHERE work_id=?", (into, from_))
    for pid, merged in plan:
        if merged is None:
            conn.execute("UPDATE play_state SET work_id=? WHERE person_id=? AND work_id=?",
                         (into, pid, from_))
        else:
            conn.execute("DELETE FROM play_state WHERE person_id=? AND work_id=?", (pid, from_))
            conn.execute("UPDATE play_state SET status=?, note=?, updated_at=?"
                         " WHERE person_id=? AND work_id=?", (*merged, pid, into))
    conn.execute("DELETE FROM up_next_order WHERE work_id=? AND person_id IN"
                 " (SELECT person_id FROM up_next_order WHERE work_id=?)", (from_, into))
    conn.execute("UPDATE up_next_order SET work_id=? WHERE work_id=?", (into, from_))
    for pid, _ in plan:
        st = conn.execute("SELECT status FROM play_state WHERE person_id=? AND work_id=?",
                          (pid, into)).fetchone()[0]
        if st == "up_next":
            conn.execute(
                "INSERT OR IGNORE INTO up_next_order (person_id, work_id, position)"
                " SELECT ?, ?, COALESCE(MAX(position) + 1, 0) FROM up_next_order WHERE person_id=?",
                (pid, into, pid))
        else:
            conn.execute("DELETE FROM up_next_order WHERE person_id=? AND work_id=?", (pid, into))
    conn.execute("DELETE FROM dup_ack WHERE work_id IN (?, ?)", (into, from_))
    conn.execute("DELETE FROM work WHERE id=?", (from_,))


def split(conn: sqlite3.Connection, copy_id: int, title: str) -> int:
    """Move one game copy onto the work for ``title`` (created if needed) and pin it there."""
    if not _is_int(copy_id):
        raise _bad("copy id must be an integer")
    row = conn.execute("SELECT category FROM copy WHERE id=?", (copy_id,)).fetchone()
    if row is None:
        raise _not_found("copy")
    if row["category"] != "game":
        raise _bad("only game copies can be split")
    if not isinstance(title, str) or not title.strip():
        raise _bad("title must be a non-empty string")
    title = title.strip()
    key = normalize(title)[0]
    if not key:
        raise _bad("title has no letters or digits")
    alias = conn.execute("SELECT work_id FROM work_alias WHERE work_key=?", (key,)).fetchone()
    if alias:
        wid = alias[0]
    else:
        wid = conn.execute("INSERT INTO work (title) VALUES (?)", (title,)).lastrowid
        conn.execute("INSERT INTO work_alias (work_key, work_id) VALUES (?, ?)", (key, wid))
    conn.execute("UPDATE copy SET work_id=?, work_pinned=1 WHERE id=?", (wid, copy_id))
    return wid


def _groups(conn: sqlite3.Connection) -> list[dict]:
    groups: dict[tuple[int, int], list[int]] = {}
    for r in conn.execute("SELECT id, work_id, platform_id FROM copy"
                          " WHERE removed_at IS NULL AND work_id IS NOT NULL ORDER BY id"):
        groups.setdefault((r["work_id"], r["platform_id"]), []).append(r["id"])
    return [{"work": w, "platform": p, "copies": ids}
            for (w, p), ids in sorted(groups.items()) if len(ids) > 1]


def dup_groups(conn: sqlite3.Connection) -> list[dict]:
    """Unacknowledged duplicate groups (spec 5.6), ordered by work then platform."""
    acked = {(r["work_id"], r["platform_id"]): r["copy_ids"]
             for r in conn.execute("SELECT work_id, platform_id, copy_ids FROM dup_ack")}
    return [g for g in _groups(conn)
            if acked.get((g["work"], g["platform"])) != json.dumps(g["copies"])]


def ack_dup(conn: sqlite3.Connection, work: int, platform: int, *, now: int) -> None:
    """Mark the current copy set of a duplicate group as reviewed ("separate copies")."""
    if not _is_int(work) or not _is_int(platform):
        raise _bad("work and platform must be integers")
    _require(conn, "work", work, "work")
    _require(conn, "platform", platform, "platform")
    for g in _groups(conn):
        if (g["work"], g["platform"]) == (work, platform):
            conn.execute(
                "INSERT INTO dup_ack (work_id, platform_id, copy_ids, acked_at) VALUES (?,?,?,?)"
                " ON CONFLICT (work_id, platform_id) DO UPDATE SET copy_ids=excluded.copy_ids,"
                " acked_at=excluded.acked_at", (work, platform, json.dumps(g["copies"]), now))
            return
    raise CatalogError(409, "not_a_duplicate", "no duplicate group for that work and platform")
