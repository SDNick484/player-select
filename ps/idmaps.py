"""GameEye ID maps, platform families and platform sync."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data_static" / "gameeye_id_maps.json"

UNMAPPED = "GameEye platform {id} (unmapped)"

CATEGORY_CODE = {0: "game", 1: "system", 2: "controller", 3: "toy", 4: "print", 5: "accessory"}

_FAMILIES = {
    "playstation": (46, 105, 10, 11, 12, 13, 37),
    "xbox": (14, 15, 47, 106),
    "nintendo": (2, 3, 5, 6, 7, 9, 39, 45, 76, 97, 178),
    "sega": (16, 17, 18, 19, 20, 32),
    "atari": (27,),
    "pc": (1,),
    "nec": (33,),
    "media": (120, 123, 168),
    "other": (22, 152),
}
FAMILY: dict[int, str] = {gid: fam for fam, ids in _FAMILIES.items() for gid in ids}

_CONDITIONS = {1: "Loose", 5: "Boxed", 7: "CIB", 31: "New"}


@dataclass(frozen=True)
class IdMaps:
    platforms: dict[int, str]
    categories: dict[int, str]
    ownership: dict[int, str]


def family_of(gameeye_id: int) -> str:
    return FAMILY.get(gameeye_id, "other")


def condition_label(mask: int | None) -> str:
    if mask is None:
        return "Unknown"
    return _CONDITIONS.get(mask, f"Unknown ({mask})")


def _int_map(raw: object, key: str) -> dict[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"id maps: '{key}' must be an object")
    out = {}
    for k, v in raw.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            raise ValueError(f"id maps: non-integer key {k!r} in '{key}'") from None
        if not isinstance(v, str) or not v:
            raise ValueError(f"id maps: '{key}' value for {k} must be a non-empty string")
    return out


def load(path: Path = DEFAULT_PATH) -> IdMaps:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"id maps: cannot read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("id maps: top level must be an object")
    for key in ("platforms", "categories", "ownership_mask"):
        if key not in data:
            raise ValueError(f"id maps: missing '{key}'")
    return IdMaps(platforms=_int_map(data["platforms"], "platforms"),
                  categories=_int_map(data["categories"], "categories"),
                  ownership=_int_map(data["ownership_mask"], "ownership_mask"))


def sync_platforms(conn: sqlite3.Connection, maps: IdMaps) -> None:
    """Upsert one platform row per mapped ID (also renames unmapped placeholders).

    Runs inside the caller's transaction; does not commit.
    """
    for gid, name in maps.platforms.items():
        conn.execute(
            "INSERT INTO platform (gameeye_id, name, family) VALUES (?, ?, ?) "
            "ON CONFLICT(gameeye_id) DO UPDATE SET name=excluded.name, family=excluded.family",
            (gid, name, family_of(gid)))


def ensure_platform(conn: sqlite3.Connection, gameeye_id: int, maps: IdMaps) -> tuple[int, bool]:
    """Return (platform row id, created_now), creating a row (placeholder if unmapped)."""
    row = conn.execute("SELECT id FROM platform WHERE gameeye_id=?", (gameeye_id,)).fetchone()
    if row:
        return row[0], False
    name = maps.platforms.get(gameeye_id) or UNMAPPED.format(id=gameeye_id)
    cur = conn.execute("INSERT INTO platform (gameeye_id, name, family) VALUES (?, ?, ?)",
                       (gameeye_id, name, family_of(gameeye_id)))
    return cur.lastrowid, True
