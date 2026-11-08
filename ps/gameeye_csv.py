"""GameEye native CSV export: parser and matching against copies imported from the backup."""
from __future__ import annotations

import csv
import datetime
import io
import json
from dataclasses import dataclass

from .idmaps import IdMaps

CSV_COLUMNS: tuple[str, ...] = (
    "Platform", "Category",
    "UserRecordType", "Title", "Country", "ReleaseType", "Publisher", "Developer", "Genre",
    "CreatedAt", "Ownership", "PriceLoose", "PriceCIB", "PriceNew", "YourPrice", "PricePaid",
    "PriceSold", "ItemCondition", "BoxCondition", "ManualCondition", "OtherCondition", "Notes",
    "Tags", "metacritic",
)

_NULLS = {"", "?", "Missing Field", "-1.0"}
_IX = {name: i for i, name in enumerate(CSV_COLUMNS)}


class CsvError(ValueError):
    pass


@dataclass(frozen=True)
class CsvRow:
    platform: str
    category: str
    title: str
    country: str | None
    created_date: datetime.date
    ownership: str
    publisher: str | None
    developer: str | None
    genre: tuple[str, ...] | None
    metacritic: int | None
    price_loose: float | None
    price_cib: float | None
    price_new: float | None


def _text(raw: str) -> str | None:
    v = raw.strip()
    return None if v in _NULLS else v


def _price(raw: str, line: int, name: str) -> float | None:
    v = _text(raw)
    if v is None:
        return None
    try:
        x = float(v.replace(",", ""))  # export groups thousands: "1,021.10"
    except ValueError:
        raise CsvError(f"line {line}: bad number in {name}") from None
    return None if x == 0.0 else x


def _int(raw: str, line: int, name: str) -> int | None:
    v = _text(raw)
    if v is None:
        return None
    try:
        return int(float(v))
    except (ValueError, OverflowError):
        raise CsvError(f"line {line}: bad number in {name}") from None


def _fields(rec: list[str], line: int) -> list[str]:
    n = len(CSV_COLUMNS)
    if len(rec) == n + 1 and rec[-1].strip() == "":
        rec = rec[:n]
    if len(rec) != n:
        raise CsvError(f"line {line}: expected {n} fields, found {len(rec)}")
    return [f.strip() for f in rec]


def _row(rec: list[str], line: int) -> CsvRow:
    f = _fields(rec, line)

    def need(name: str) -> str:
        v = _text(f[_IX[name]])
        if v is None:
            raise CsvError(f"line {line}: missing {name}")
        return v

    try:
        date = datetime.datetime.strptime(need("CreatedAt"), "%b %d, %Y").date()
    except ValueError:
        raise CsvError(f"line {line}: bad CreatedAt") from None
    genre = _text(f[_IX["Genre"]])
    return CsvRow(
        platform=need("Platform"), category=need("Category"), title=need("Title"),
        country=_text(f[_IX["Country"]]), created_date=date, ownership=need("Ownership"),
        publisher=_text(f[_IX["Publisher"]]), developer=_text(f[_IX["Developer"]]),
        genre=tuple(g for g in genre.split(", ") if g) if genre else None,
        metacritic=_int(f[_IX["metacritic"]], line, "metacritic"),
        price_loose=_price(f[_IX["PriceLoose"]], line, "PriceLoose"),
        price_cib=_price(f[_IX["PriceCIB"]], line, "PriceCIB"),
        price_new=_price(f[_IX["PriceNew"]], line, "PriceNew"))


def parse_csv(data: bytes) -> list[CsvRow]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise CsvError(f"not valid UTF-8: {e}") from e
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), skipinitialspace=True))
    except csv.Error as e:
        raise CsvError(f"malformed CSV: {e}") from e
    if not records:
        raise CsvError("empty file")
    if _fields(records[0], 1) != list(CSV_COLUMNS):
        raise CsvError("header does not match the GameEye export columns")
    return [_row(rec, i) for i, rec in enumerate(records[1:], start=2) if rec]


def meta_fields(row: CsvRow) -> dict[str, str]:
    out: dict[str, str] = {}
    if row.publisher is not None:
        out["publisher"] = row.publisher
    if row.developer is not None:
        out["developer"] = row.developer
    if row.genre is not None:
        out["genre"] = json.dumps(list(row.genre))
    if row.metacritic is not None:
        out["metacritic"] = str(row.metacritic)
    for name in ("price_loose", "price_cib", "price_new"):
        v = getattr(row, name)
        if v is not None:
            out[name] = f"{v:.2f}"
    return out


@dataclass(frozen=True)
class CopyKey:
    source_id: str
    platform: str
    raw_title: str
    created_at: int | None
    condition: str


@dataclass
class MatchResult:
    meta: dict[str, dict[str, str]]
    enriched: int
    ambiguous: int
    unmatched: int


def _utc_date(ts: int) -> datetime.date:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()


def match(copies: list[CopyKey], rows: list[CsvRow], maps: IdMaps) -> MatchResult:
    platforms = set(maps.platforms.values())
    categories = set(maps.categories.values())
    index: dict[tuple[str, str, datetime.date], list[int]] = {}
    for i, r in enumerate(rows):
        if r.platform in platforms and r.category in categories:
            index.setdefault((r.platform, r.title, r.created_date), []).append(i)

    candidates_seen: set[int] = set()
    picks: dict[int, list[int]] = {}   # row index -> positions of copies that picked it
    ambiguous: set[int] = set()
    for ci, c in enumerate(copies):
        if c.created_at is None:
            continue
        cand = index.get((c.platform, c.raw_title, _utc_date(c.created_at)), [])
        if not cand:
            continue
        candidates_seen.update(cand)
        if len(cand) > 1:
            cand = [i for i in cand if rows[i].ownership == c.condition]
        if len(cand) == 1:
            picks.setdefault(cand[0], []).append(ci)
        else:
            ambiguous.add(ci)

    meta: dict[str, dict[str, str]] = {}
    for ri, cis in picks.items():
        if len(cis) == 1:
            meta[copies[cis[0]].source_id] = meta_fields(rows[ri])
        else:
            ambiguous.update(cis)
    enriched = sum(1 for ri, cis in picks.items() if len(cis) == 1)
    return MatchResult(meta=meta, enriched=enriched, ambiguous=len(ambiguous),
                       unmatched=len(rows) - len(candidates_seen))
