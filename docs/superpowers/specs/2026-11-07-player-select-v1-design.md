# Player Select v1: design

- **Date:** 2026-11-07
- **Status:** design agreed in brainstorming; awaiting review of this written spec
- **Repo:** `SDNick484/player-select` (public, MIT)
- **Host:** Alpine LXC on Proxmox (same container as steam-backlog-triage), `games.<your-domain>` via Cloudflare Tunnel + Access
- **Sibling:** [`SDNick484/steam-backlog-triage`](https://github.com/SDNick484/steam-backlog-triage) (read at commit `114368a`), referred to below as **SBT**

## 1. Purpose

Player Select is a self-hosted catalog of the household's console games: physical copies from the
GameEye app now, digital copies from PlayStation, Xbox and Nintendo later. It answers "what do we own,
on what, in what form" and records per-person play state for five household members.

### v1 delivers

1. An idempotent, re-runnable import of a GameEye `.ged` backup, uploaded through the web UI, with a
   dry-run diff (add / edit / remove / restore) applied only on confirmation.
2. Optional enrichment from GameEye's native CSV export (publisher, developer, genre, Metacritic,
   market prices).
3. A catalog UI with Games and Items views, filters, duplicate review, per-person play state and an
   Up next list.
4. A data model that already holds digital copies, several accounts per platform and per-person
   state, so digital sources need no schema rework.
5. A time-boxed spike plan (section 9) to establish what the PSN, Xbox and Nintendo options can
   return today.

### Not in v1

- Any digital importer (only the adapter contract, section 8, and the spike).
- Triage scoring / "what next" ranking.
- IGDB metadata, cover art, cross-referencing Steam / SBT data.
- Home Assistant hooks, MCP server.
- Platform compatibility ("what can run what").
- Login or identity inside the app.

## 2. Users and access

- **Only the owner uses the UI.** The owner records state for any family member through a person picker.
- No authentication in the app. Access control is at the edge: Cloudflare Access for
  `games.<your-domain>`, or LAN / Tailscale. Same model and README warning as SBT.
- The person picker's current value is stored in the browser's `localStorage` (wrapped in
  try/catch); it defaults to the first person.

## 3. Decisions that depart from SBT

Everything not listed here follows SBT: Python standard library only, `http.server`, one HTML page with
no build step, the skeleton + `window.APP_CONFIG` page wrapper, `--nav LABEL=URL` header links,
ETag / `If-None-Match` / `If-Match` semantics, `atomic_write` for files, `install/alpine.sh` with an
unprivileged user and OpenRC `supervise-daemon`, BusyBox crond blocks between `# BEGIN/END` markers,
remembered installer options, `contrib/systemd/`, `unittest`, Playwright, installer-twice test,
shellcheck, privacy scanner with `PRIVACY_DENYLIST`, SHA-pinned actions, Dependabot.

| # | Departure | Reason |
|---|---|---|
| D1 | **SQLite** (stdlib `sqlite3`, WAL, `foreign_keys=ON`) instead of JSON files | Upsert on uuid, diff reports, soft deletes and joins across work / copy / account / person are relational. Still zero third-party dependencies. |
| D2 | **Internal integer IDs**; source IDs stored per copy | SBT keys everything on Steam `appid`; one game here can have GameEye, PSN, Xbox, Nintendo and later IGDB / Steam IDs. |
| D3 | **Play state keyed by (person, work)** | SBT's lanes are global; the family needs per-person state. |
| D4 | **Import by upload** with dry-run diff, not a scheduled exporter | The `.ged` backup is produced on a phone; there is nothing for cron to pull. |
| D5 | **`ps/` package** for core logic instead of one flat file per role | The importer, diff and SQL would push the server well past SBT's ~750 lines, and the importer must be testable without HTTP. |
| D6 | **Status field replaces SBT's lanes** (section 5.5) | Physical copies have no playtime, so state is manual; one field avoids contradictory lane + progress values. |
| D7 | **Digital sources as separate adapter processes** writing snapshot JSON (section 8) | Unofficial APIs break and some are Node packages; adapters isolate both from the stdlib core. |
| D8 | **No Claude-artifact mode** | State lives in SQLite on the server; an artifact copy would have nothing to run against. |
| D9 | **Nightly SQLite backup** cron block | The DB holds hand-entered state (play state, notes, merges, removal reasons) that no import can recreate. |
| D10 | **No scoring in v1** | SBT's couch score is Steam-Deck-specific; console triage scoring is a later spec. |

## 4. Architecture

### 4.1 Repository layout

```
player-select/
├── player_select_server.py        # HTTP server + JSON API
├── catalog.html                   # the page (body fragment, wrapped by the server)
├── ps/                            # core library, stdlib only
│   ├── __init__.py
│   ├── db.py                      # connect, migrate, writer lock, transactions
│   ├── titles.py                  # normalize(raw_title) -> (work_key, edition)
│   ├── idmaps.py                  # loads data_static/gameeye_id_maps.json
│   ├── gameeye_ged.py             # .ged bytes -> list[GedRow]  (no DB access)
│   ├── gameeye_csv.py             # CSV bytes -> list[CsvRow]   (no DB access)
│   ├── importer.py                # stage / apply / expire
│   └── catalog.py                 # read model for /api/catalog; merge, split, dup-ack, play state
├── migrations/001_init.sql
├── data_static/gameeye_id_maps.json
├── adapters/README.md             # snapshot contract (section 8); no adapters in v1
├── spikes/                        # throwaway spike scripts + FINDINGS.md (never imported)
├── install/alpine.sh
├── contrib/systemd/
├── docs/superpowers/specs/
├── tests/
└── .github/
```

### 4.2 Runtime layout (Alpine LXC)

```
/opt/player-select/                       code, root-owned
/home/games/data/player_select.db         SQLite, WAL
/home/games/data/imports/                 staged uploads, mode 0600
/home/games/data/backups/                 nightly .backup copies, keep 14
/home/games/.player-select-env            future adapter tokens, 0600, never read by the server
/home/games/persons.json                  person seed (names, colors), not in the repo
/etc/init.d/player-select                 OpenRC service, port 8486
/etc/player-select.conf                   remembered installer options
```

### 4.3 Server

- `player_select_server.py [--port 8486] [--bind 0.0.0.0] [--data DIR] [--page catalog.html] [--nav LABEL=URL ...] [--persons FILE]`
- `ThreadingHTTPServer`. One writer connection guarded by a `threading.Lock`; reads use a
  per-request connection. `PRAGMA busy_timeout=5000`.
- Migrations run at start-up: `migrations/NNN_*.sql` applied in order inside a transaction, tracked
  in `schema_version`.
- Persons are seeded from `--persons` (JSON list of `{name, color}`) on start-up: inserted if missing
  by name, never deleted. Names and colors are personal data and are not committed.

## 5. Data model

### 5.1 Schema (`migrations/001_init.sql`)

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);

CREATE TABLE person (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT, sort INTEGER NOT NULL DEFAULT 0);

CREATE TABLE platform (
  id INTEGER PRIMARY KEY,
  gameeye_id INTEGER UNIQUE,
  name TEXT NOT NULL,
  family TEXT NOT NULL CHECK (family IN
    ('playstation','xbox','nintendo','sega','atari','pc','nec','media','other')));

CREATE TABLE account (
  id INTEGER PRIMARY KEY,
  family TEXT NOT NULL CHECK (family IN ('playstation','xbox','nintendo','steam')),
  person_id INTEGER REFERENCES person(id),
  label TEXT NOT NULL,
  external_id TEXT,
  UNIQUE (family, external_id));

CREATE TABLE work (id INTEGER PRIMARY KEY, title TEXT NOT NULL, igdb_id INTEGER UNIQUE);
CREATE TABLE work_alias (work_key TEXT PRIMARY KEY, work_id INTEGER NOT NULL REFERENCES work(id));

CREATE TABLE import_run (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  csv_sha256 TEXT,
  base_run_id INTEGER REFERENCES import_run(id),
  status TEXT NOT NULL CHECK (status IN ('staged','applied','expired','rejected')),
  staged_at INTEGER NOT NULL,
  applied_at INTEGER,
  summary_json TEXT NOT NULL);

CREATE TABLE copy (
  id INTEGER PRIMARY KEY,
  work_id INTEGER REFERENCES work(id),
  work_pinned INTEGER NOT NULL DEFAULT 0 CHECK (work_pinned IN (0,1)),
  platform_id INTEGER NOT NULL REFERENCES platform(id),
  category TEXT NOT NULL CHECK (category IN
    ('game','system','controller','accessory','print','toy')),
  form TEXT NOT NULL CHECK (form IN ('physical','digital','subscription_claim')),
  account_id INTEGER REFERENCES account(id),
  raw_title TEXT NOT NULL,
  edition TEXT,
  condition_mask INTEGER,
  country TEXT,
  source TEXT NOT NULL CHECK (source IN ('gameeye','psn','xbox','nintendo','manual')),
  source_id TEXT NOT NULL,
  source_item_id TEXT,
  source_platform_id INTEGER,
  created_at INTEGER,
  updated_at INTEGER,
  last_seen_import INTEGER REFERENCES import_run(id),
  removed_at INTEGER,
  removed_reason TEXT CHECK (removed_reason IN ('sold','traded','lost','gifted','mistake')),
  UNIQUE (source, source_id),
  CHECK ((category = 'game') = (work_id IS NOT NULL)),
  CHECK (removed_reason IS NULL OR removed_at IS NOT NULL));
CREATE INDEX copy_work ON copy(work_id);
CREATE INDEX copy_platform ON copy(platform_id);

CREATE TABLE copy_meta (
  copy_id INTEGER NOT NULL REFERENCES copy(id),
  field TEXT NOT NULL CHECK (field IN ('publisher','developer','genre','metacritic',
                                       'price_loose','price_cib','price_new')),
  value TEXT NOT NULL,
  source TEXT NOT NULL,
  fetched_at INTEGER NOT NULL,
  PRIMARY KEY (copy_id, field, source));

CREATE TABLE play_state (
  person_id INTEGER NOT NULL REFERENCES person(id),
  work_id INTEGER NOT NULL REFERENCES work(id),
  status TEXT NOT NULL CHECK (status IN
    ('backlog','up_next','playing','finished','abandoned','not_for_me')),
  note TEXT CHECK (note IS NULL OR length(note) <= 2000),
  hours REAL,
  last_played INTEGER,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (person_id, work_id));

CREATE TABLE up_next_order (
  person_id INTEGER NOT NULL REFERENCES person(id),
  work_id INTEGER NOT NULL REFERENCES work(id),
  position INTEGER NOT NULL,
  PRIMARY KEY (person_id, work_id));

CREATE TABLE dup_ack (
  work_id INTEGER NOT NULL REFERENCES work(id),
  platform_id INTEGER NOT NULL REFERENCES platform(id),
  copy_ids TEXT NOT NULL,              -- sorted JSON list; ack is void if the active set differs
  acked_at INTEGER NOT NULL,
  PRIMARY KEY (work_id, platform_id));

CREATE TABLE import_change (
  run_id INTEGER NOT NULL REFERENCES import_run(id),
  op TEXT NOT NULL CHECK (op IN ('add','edit','remove','restore')),
  source_id TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT);
```

All timestamps are epoch seconds UTC.

### 5.2 Mappings from GameEye

| `.ged ownership` column | `copy` column | Rule |
|---|---|---|
| `uuid` | `source_id` | `source = 'gameeye'` |
| `item_id` | `source_item_id` | |
| `platform_id` | `platform_id`, `source_platform_id` | via `gameeye_id_maps.json`; unknown → placeholder platform (5.3) |
| `category_id` | `category` | 0 game, 1 system, 2 controller, 3 toy, 4 print, 5 accessory; unknown → row skipped with a warning |
| `title` | `raw_title`, `edition`, `work_id` | via `titles.normalize()` (5.4) |
| `ownership_mask` | `condition_mask` | stored as-is; labels 1 Loose, 5 Boxed, 7 CIB, 31 New, else `Unknown (n)` |
| `country_id` | `country` | stored as the raw ID string; a country name map is out of scope |
| `created_at`, `updated_at` | same | epoch seconds |
| (every other column, incl. purchase price) | — | ignored |

`form = 'physical'`, `account_id = NULL` for every GameEye row.

The platform `family` is assigned by a static table in `ps/idmaps.py` keyed on GameEye platform ID
(e.g. 46, 105, 10–13, 37 → playstation; 14, 15, 47, 106 → xbox; 2, 3, 5–7, 9, 39, 45, 76, 97, 178 →
nintendo; 16–20, 32 → sega; 27 → atari; 1 → pc; 33 → nec; 120, 123, 168 → media; 22, 152 → other).

`gameeye_id_maps.json` must be corrected so category 3 reads `Toys To Life`, the label GameEye's CSV
actually uses.

### 5.3 Unknown platform IDs

An unmapped `platform_id` is a warning, not an error. The importer creates (once) a platform row
`name = 'GameEye platform <id> (unmapped)'`, `gameeye_id = <id>`, `family = 'other'`, and lists it in
the diff's `warnings`. When the map is later updated, start-up reconciliation renames the row and sets
its family.

### 5.4 Works and title normalization

`titles.normalize(raw) -> (work_key, edition)`:

1. `edition` = text inside the first `[...]` (trimmed), else `None`.
2. Key: NFKD → ASCII; lower-case; remove all `[...]`; `&` → ` and `; non-alphanumerics → space;
   drop a leading `the`; whole-word roman numerals `ii`–`xvi` → arabic; collapse whitespace.

Measured on the 2026-11-07 export: 523 game rows, 464 raw titles, 455 works, 9 cross-platform works.
Known correct merges: *Witcher III: Wild Hunt* (PS4) + *[Complete Edition]* (Switch); *Elder Scrolls
V: Skyrim* (PS3, Switch); *Ocarina of Time* (N64, Switch 2 *[Foil Cover Art]*). Known false merge:
*Jurassic Park* (Sega CD vs Genesis are different games).

Work attachment on import: look up `work_key` in `work_alias`; if absent create `work(title = raw
title without the bracket)` and the alias. Copies with `work_pinned = 1` are never reassigned.

- **Merge** (`into`, `from`): repoint `work_alias`, `copy`, `up_next_order`, `dup_ack`; merge
  `play_state` per person (keep the later `updated_at`; if both have notes, append the other note after
  a blank line); delete `from`. One transaction.
- **Split** (`copy`, `title`): create a new work and alias for `normalize(title)`, set the copy's
  `work_id` and `work_pinned = 1`.

`tests/fixtures/titles.json` holds raw → expected `(work_key, edition)` vectors, including the cases
above; any normalizer change must update it deliberately.

### 5.5 Play state

| `status` | Meaning | SBT lane |
|---|---|---|
| (no row) | untriaged | none |
| `backlog` | want to play someday | someday |
| `up_next` | next; ordered per person in `up_next_order` | next |
| `playing` | in progress | — |
| `finished` | done | done |
| `abandoned` | stopped | — |
| `not_for_me` | won't play | skip |

Setting a status other than `up_next` removes the work from that person's `up_next_order`; setting
`up_next` appends it. `hours` and `last_played` stay NULL in v1; future adapters write only those two
columns and never change `status`.

### 5.6 Duplicates

A duplicate group is a `(work_id, platform_id)` with more than one active (`removed_at IS NULL`)
copy. It is hidden when `dup_ack` has a row whose `copy_ids` equals the group's current sorted copy
IDs; any change to the set brings it back. The 2026-11-07 export has 55 such pairs, 38 of them a
`Boxed` (Dec 2024) + `CIB` (Jan 2025) pattern on PS2/PS3/PS4/Wii that may be data-entry artifacts.

## 6. Import

### 6.1 Stage: `POST /api/import/gameeye`

`multipart/form-data` with `ged` (required) and `csv` (optional). Steps:

1. Reject bodies over 32 MB (413) and non-multipart requests (415).
2. Compute SHA-256. If it equals the latest **applied** run's `file_sha256` and no CSV is attached,
   return `200 {"import_id": null, "summary": {"unchanged": N}}` without staging.
3. Write to `data/imports/<sha>.ged` (0600). Validate the zip: exactly one member named
   `ownership_database.db`, uncompressed size ≤ 256 MB. Otherwise 400.
4. Extract alongside and open with `file:<path>?mode=ro&immutable=1`. Require table `ownership` with
   columns `uuid, item_id, platform_id, category_id, title, ownership_mask, country_id, created_at,
   updated_at`; otherwise 400. `PRAGMA user_version != 20` adds a warning.
5. Map rows (5.2, 5.3). Duplicate `uuid` inside one backup → 400.
6. Diff against all `source = 'gameeye'` copies:

   | op | Rule |
   |---|---|
   | `add` | uuid not in DB |
   | `edit` | uuid present and any of `raw_title`, `platform`, `category`, `condition_mask`, `country` differ (`updated_at` alone is not an edit) |
   | `remove` | active in DB, absent from the backup |
   | `restore` | `removed_at` set in DB, present in the backup (combined with any edit) |

7. CSV enrichment, if attached (6.3).
8. Store `import_run(status='staged', base_run_id = latest applied run or NULL)` and its
   `import_change` rows.

Response:

```json
{"import_id": 7, "dry_run": true,
 "summary": {"added": 3, "edited": 1, "removed": 1, "restored": 0, "unchanged": 609,
             "skipped": 0, "csv": {"enriched": 612, "unmatched": 1, "ambiguous": 1}},
 "removal_guard": false,
 "warnings": [{"kind": "user_version", "detail": "expected 20, got 21"}],
 "changes": [{"op": "add", "source_id": "…", "title": "Donkey Kong Bananza",
              "platform": "Nintendo Switch 2", "condition": "CIB"}]}
```

`removal_guard` is true when removals exceed 25% of currently active GameEye copies.

### 6.2 Apply: `POST /api/import/<id>/apply`

Body `{"confirm_removals": false}` (JSON). In one `BEGIN IMMEDIATE` transaction:

- 404 unknown id; 410 if expired; 409 if `base_run_id` is not the latest applied run, or the run is
  not `staged`; 428 if `removal_guard` and `confirm_removals` is not true.
- Insert adds; update edits (re-attach the work unless pinned; a pinned copy only gets `raw_title` and
  `edition` updated); set `removed_at = now` on removes; clear `removed_at` and `removed_reason` on
  restores.
- Set `last_seen_import = <id>` on every copy present in the backup.
- Upsert `copy_meta` rows from the CSV with `source = 'gameeye_csv'`.
- Mark the run `applied`, delete the staged files.

Play state is attached to works, so a removed or restored copy never loses state.

### 6.3 CSV enrichment

`ps/gameeye_csv.py`:

- Accepts rows with 25 fields (trailing comma) or 24 (last line without it); strips surrounding spaces
  inside and outside quotes.
- Null values: empty, `?`, `Missing Field`, `-1.0`, `0.0` for prices.
- `CreatedAt` (`Nov 8, 2026`) is a **UTC** calendar date.
- Labels for platform and category resolve through `gameeye_id_maps.json`.

Join each staged copy on `(platform name, raw title, country, UTC date of created_at)`. If several CSV
rows match, narrow by ownership label. If still not exactly one, the copy is `ambiguous` and gets no
metadata (expected on the 2026-11-07 data: *Dead to Rights*, Xbox, two CIB copies added the same
day). CSV rows that match no copy are counted as `unmatched`. `genre` is stored as a JSON list split on
`, `.

### 6.4 Expiry

Staged runs older than one hour are set to `expired` and their files deleted, lazily at the start of
every import request. No background thread.

## 7. UI and API

### 7.1 Page

Header: **Games · Items · Up next · Duplicates · Import**, the person picker, `--nav` links.

- **Games:** one row per work with at least one active copy. Title, platform chips
  (`PS4 · CIB`, `Switch · CIB · Complete Edition`), the selected person's status, genre and Metacritic
  when known. Row detail: all copies, every person's status and note (editable), **Merge into…**,
  **Split this copy out**.
- **Items:** one row per copy, hardware included. Platform, category, condition, edition, country,
  CIB value, added date. Detail: source fields; removal reason editable when removed.
- **Up next:** the selected person's `up_next` works in order; drag or Move up / Move down.
- **Duplicates:** unacknowledged groups (5.6), each with **These are separate copies**.
- **Import:** `.ged` and optional CSV pickers → staged diff grouped by op, warnings, removal-guard
  confirm, **Apply**.

Filters, shared by Games and Items, combined with AND, stored in the URL, each with SBT's
*include unknown* option where data can be missing: platform (multi), platform family, category,
condition, has edition, form, status (selected person), genre, Metacritic min/max, publisher,
show removed, text search. In Games a work matches if any of its copies matches.

### 7.2 API

Every response carries `X-PlayerSelect-Server: 1` and `X-PlayerSelect-Api: 1`. Errors are
`{"error": "<code>", "detail": "<text>"}`.

| Method | Path | Body / result |
|---|---|---|
| GET | `/`, `/games`, `/items`, `/up-next`, `/duplicates`, `/import` | the page |
| GET | `/api/catalog` | `{persons, platforms, works, copies, meta, play_state, up_next, dup_groups, last_import}`; ETag, 304 |
| PUT | `/api/play-state` | `{"person": 3, "work": 31, "status": "finished", "note": "…"}`; `status: ""` deletes the row; returns `previous`; honors `If-Match` against the catalog ETag (412) |
| PUT | `/api/up-next` | `{"person": 3, "order": [31, 52]}`; must equal that person's `up_next` set |
| PUT | `/api/copy/<id>` | `{"removed_reason": "sold"}`; only allowed when removed |
| POST | `/api/work/merge` | `{"into": 31, "from": 87}` |
| POST | `/api/work/split` | `{"copy": 205, "title": "Jurassic Park (Sega CD)"}` |
| POST | `/api/dup-ack` | `{"work": 11, "platform": 2}` |
| POST | `/api/import/gameeye` | section 6.1 |
| POST | `/api/import/<id>/apply` | section 6.2 |
| GET | `/healthz` | `ok` |

POST and PUT bodies other than the import upload require `Content-Type: application/json` (415).

## 8. Adapter contract (for later digital sources)

Not implemented in v1; documented in `adapters/README.md` so the schema is checked against it.

- An adapter is a standalone program in `adapters/<source>/` with its own venv or `node_modules`,
  never imported by `ps/`. It reads credentials from `~/.player-select-env` and is run by a
  `run-adapter.sh <source>` wrapper holding `flock` on `data/.adapter-<source>.lock`, as SBT's
  `run-export.sh` does.
- It writes `data/snapshots/<source>-<account>.json`:

```json
{"schema": 1, "source": "psn", "account": {"external_id": "…", "label": "Owner PSN"},
 "fetched_at": "2026-11-20T04:15:00Z",
 "items": [{"source_id": "…", "title": "…", "platform": "Sony PlayStation 5",
            "form": "digital", "hours": 12.5, "last_played": "2026-11-18T21:03:00Z"}]}
```

- The core ingests snapshots through the same stage / diff / apply path as GameEye, keyed on
  `(source, source_id)`.
- Each adapter has a fixture-based test suite built from sanitized spike output.

## 9. Spike plan: digital sources

Time-boxed, about one working day, independent of v1 delivery. No product code.

**Rules.** Scripts in `spikes/<source>/`, run on the dev box, tokens in a gitignored `.env`, raw
responses in gitignored `data/spikes/`. Committed output per spike: `spikes/<source>/FINDINGS.md` and
one sanitized fixture that passes the privacy scan.

**Questions each spike answers.**

1. Owned library: does an endpoint list what the account owns, including never-launched games?
2. Coverage: which platforms (PS3 / Vita, Xbox 360 back-compat, Switch vs Switch 2)?
3. Ownership type: purchase vs subscription claim (PS Plus) vs family share?
4. Play history: per-title hours and last played?
5. Multiple accounts: can one setup read every household account?
6. Auth durability: unattended token lifetime and refresh.

**Starting hypotheses (public docs as of 2026-11-07, unverified against these accounts).**

| Source | Owned library | Play history | Note |
|---|---|---|---|
| PSNAWP (Python) | `GameEntitlementsIterator`, PS4/PS5 only | title stats | `entitlementType`, `isSubscription` may separate PS Plus claims |
| psn-api (npm 2.18) | `getPurchasedGames()`, PS4/PS5 only | `getUserPlayedGames()` | nothing documented on PS Plus |
| Xbox title history v2 / OpenXBL | not from title history | yes | owned library needs a collections endpoint: the main question |
| pynintendoparental (Python 2.6.3) | no | per device and player, minutes | async (`aiohttp`); library means eShop history or manual |

**Timeboxes.**

| Spike | Time | First | Then |
|---|---|---|---|
| PSN | 3 h | PSNAWP entitlements with the owner's NPSSO; compare count with the PS5 library; inspect a known PS Plus claim | psn-api for comparison; refresh-token lifetime |
| Xbox | 3 h | OpenXBL title history for one child's account; look for an owned/collections call | whether one login reads the other accounts |
| Nintendo | 2 h | pynintendoparental per-player titles and minutes on the Switch 2 | eShop purchase history: export, signed-in-browser scrape, or manual |
| Write-up | 1 h | fill the matrix; recommend the first adapter | |

**Decision matrix** (one row per source): owned library, platforms covered, purchase vs claim, hours,
all accounts, unattended auth, Python-native.

**Pick rule.** First adapter = a source with an owned-library answer for the accounts the family
actually plays on, tie-broken by unattended auth, then Python-native. If no source has an owned
library, a play-history source can go first, since it fills `hours` / `last_played` on works that
physical copies already created.

## 10. Testing

TDD per implementation task. `unittest` only for product tests.

- **`tests/_ged.py`**: builds a synthetic `.ged` (zip + SQLite, `user_version = 20`, `ownership`
  table) from dicts. No real backup ever enters the repo. Fixture rows mirror real cases with fake
  uuids: a Boxed + CIB *Bloodborne* pair (PS4, platform 46), a same-day CIB *Dead to Rights* pair
  (Xbox, 14), *Donkey Kong Bananza* (Switch 2, 178), the Switch 2 system (category 1), an unmapped
  platform 199, an unknown category 9.
- **Parsers**: `.ged` validation failures (not a zip, two members, missing table, missing column,
  duplicate uuid); CSV sentinels, 24/25-field rows, UTC dates.
- **Titles**: `tests/fixtures/titles.json` vectors.
- **Importer**: add / edit / remove / restore; applying the same file twice yields all `unchanged`;
  stale base → 409; expired → 410; removal guard → 428 then success with confirm; pinned copy keeps
  its work on a title edit; a restored copy's work keeps its play state; unmapped platform warning;
  CSV ambiguity leaves the pair unenriched.
- **Catalog**: merge (including play-state conflict and note append), split, dup-ack voided when the
  copy set changes, up-next ordering rules.
- **Server**: every route, ETag / 304 / 412, 413 / 415 / 404, error shapes.
- **Playwright**: page loads without console errors; filters round-trip through the URL; a status set
  for one person is not shown for another; fixture import → diff → apply.
- **Installer**: `install/alpine.sh` twice in `python:3.12-alpine` (and once with a changed option):
  conf preserved, one cron block each for backup, service healthy.

## 11. CI

Copied from SBT: Python 3.11–3.14 unit matrix; Playwright job; installer job; shellcheck and
`py_compile`; privacy scan with the `PRIVACY_DENYLIST` repository secret; actions pinned to full SHAs;
Dependabot weekly. The privacy scanner additionally fails on tracked `*.ged` / `*.db` files and on any
tracked file containing GameEye's CSV header line, outside `tests/fixtures/` synthetic samples marked
`privacy-scan: allow`.

## 12. Deployment

- `install/alpine.sh`, same structure as SBT: `apk add python3 tzdata ca-certificates sqlite`
  (the `sqlite` CLI for backups), user `games`, code in `/opt/player-select`, OpenRC service
  `player-select` on port 8486, options remembered in `/etc/player-select.conf` (`PORT`, `NAV`,
  `TZ_NAME`, `BACKUP_SCHEDULE`).
- Nightly backup cron block (default `30 3 * * *`):
  `sqlite3 player_select.db ".backup backups/player_select-$(date +%F).db"` and delete copies older
  than 14 days.
- Cloudflare Tunnel: `games.<your-domain> → http://localhost:8486`, behind the same Access policy
  as `steam.<your-domain>`.
- `.gitignore`: `data/`, `*.ged`, `*.db`, `*.db-*`, `*_collection.csv`, `.env`,
  `.player-select-env`, `persons.json`, plus SBT's Python entries.

## 13. Open items for later specs

- Triage scoring for consoles (no Deck/ProtonDB signals; candidate inputs: length, Metacritic, status,
  per-person interest).
- IGDB enrichment and cover art; per-field precedence IGDB > GameEye CSV.
- Cross-reference with SBT's Steam library.
- Platform compatibility for "which console can play this".
- Country ID → name map for GameEye.
