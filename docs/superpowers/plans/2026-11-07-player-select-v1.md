# Player Select v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the v1 catalog: a stdlib-only server that imports a GameEye `.ged` backup (plus optional CSV enrichment) through a dry-run diff, and a one-page UI for browsing copies, per-person play state, Up next and duplicate review.

**Architecture:** `ps/` holds pure, HTTP-free core logic over one SQLite database (parsers, title normalizer, importer, catalog read/write model). `player_select_server.py` is a thin `http.server` layer copied in style from SBT's `backlog_server.py`. `catalog.html` is one body fragment wrapped by the server, with filters in the URL hash like SBT's `backlog.html`.

**Tech Stack:** Python 3.11–3.14 standard library (`sqlite3`, `http.server`, `zipfile`, `csv`, `email`, `unittest`); Playwright, shellcheck and Docker in CI only.

**Spec:** `docs/superpowers/specs/2026-11-07-player-select-v1-design.md` (commit `ba279df`). Read it with this plan; section numbers below (§) refer to it. SBT means `SDNick484/steam-backlog-triage` at `2013768`; where a task says "copy from SBT", copy that file and change only what the task names.

## Global Constraints

- Product code: Python standard library only. Floor 3.11, CI matrix `3.11 3.12 3.13 3.14`.
- No `cgi` module (removed in 3.13): multipart is parsed with `email.parser.BytesParser(policy=email.policy.HTTP)`.
- SQLite connections use `isolation_level=None` and explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` (`Connection.autocommit` is 3.12+). Every connection: `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`; the writer also sets `journal_mode=WAL`.
- All timestamps are epoch seconds UTC. Every function that stamps time takes `now: int` so tests control the clock; only the server calls `int(time.time())`.
- Default port `8486`. Response headers on every response: `X-PlayerSelect-Server: 1`, `X-PlayerSelect-Api: 1`. Error body: `{"error": "<code>", "detail": "<text>"}`.
- Limits: upload body 32 MiB (`32 * 1024 * 1024`) → 413; extracted DB 256 MiB → 400 `too_large`; JSON request body 64 KiB; note 2000 characters; staged run TTL 3600 s; removal guard when removals > 25% of active GameEye copies; expected `user_version` 20.
- Staged upload files: `<data>/imports/<sha256>.ged` and `<data>/imports/<sha256>.csv`, mode `0600`, directory mode `0700`.
- Public repo. No real `.ged`, `.db`, collection CSV, tokens, household names, hostnames, IPs or account IDs in any tracked file or commit message. Test persons are `Player A`, `Player B`, `Player C`. Test uuids are `00000000-0000-4000-8000-0000000000NN`. The real CSV may be read locally to sanity-check parsers, never copied into fixtures.
- The GameEye CSV header line is never written literally in a tracked file (the privacy scan flags it): code and tests build it at run time by joining `ps.gameeye_csv.CSV_COLUMNS`.
- Commits: author `SDNick484 <SDNick484@users.noreply.github.com>`.
- Tests: `unittest` only; no test may be skipped in CI (copy SBT's "fail if skipped" step). Test modules insert their own directory on `sys.path` and import helpers as `from _support import ...` / `from _ged import ...`, as SBT does.
- Full suite, the done-check every task ends with: `python -m unittest discover -s tests -p 'test_*.py'` from the repo root → `OK`. (Discovery does not descend into `tests/browser/`, which has no `__init__.py`; browser tests run with `-s tests/browser`.)

## Review Focus

1. **A `.ged` whose zip header understates the member size** (zip bomb): extraction must stop at 256 MiB of actual bytes written and answer 400 `too_large`, not trust `ZipInfo.file_size`. Test: Task 5, `test_copy_capped_stops_at_limit`.
2. **A copy whose category changes between game and non-game across imports**: the `CHECK ((category = 'game') = (work_id IS NOT NULL))` constraint must hold, so the edit clears or attaches the work instead of raising `IntegrityError` → 500. Test: Task 8, `test_category_change_game_to_accessory_and_back`.
3. **Rows with NULL or empty `title`, NULL `platform_id` or NULL `category_id`**: skipped with a warning (`bad_row` / `unknown_category`), never mapped to an empty work key that would merge unrelated rows. Test: Task 7, `test_null_fields_skipped_with_warning`.
4. **Applying a run twice, or a run whose staged file vanished**: second apply → 409 `not_staged`; missing staged file → 410 `expired`; never a 500 or a half-applied run. Tests: Task 8, `test_apply_twice_409`, `test_missing_staged_file_410`.
5. **A multipart upload whose file bytes contain CRLF and `--` sequences**, a missing `ged` part, or an empty `ged` part: bytes come through exactly; missing/empty `ged` → 400 `missing_ged`. Tests: Task 13, `test_parse_multipart_binary_exact`, `test_missing_ged_400`.

## Decisions this plan makes inside the spec

Each is an interpretation where the spec is silent; the owning task pins it with a test. Veto any at review.

- **D-a** `ambiguous` counts copies left unenriched (2 for a same-day same-condition pair); `unmatched` counts CSV rows that matched no copy at all.
- **D-b** A CSV row with no value for a field that a previous import stored deletes that `copy_meta` row (the CSV is the source of truth for its own fields).
- **D-c** Split with a title whose key already has an alias attaches the copy to that existing work (still pinned) rather than failing on the `work_alias` primary key.
- **D-d** Merge: where both works have an `up_next_order` row for a person, the `into` row is kept; `dup_ack` rows of both works are deleted (the merged copy set voids them anyway).
- **D-e** Unknown-platform placeholder rows are created at **apply**, not stage (stage is a dry run); stage only warns.
- **D-f** Apply re-reads the staged files and recomputes the mapping; the stored `import_change` rows are for display and audit. The 409 base check guarantees the recomputation equals what was shown.
- **D-g** HTTP codes: unknown id → 404 `not_found`; malformed input → 400 `bad_request` (or a named code below); state conflicts → 409 (`order_mismatch`, `not_removed`, `not_a_duplicate`, `stale_base`, `not_staged`).
- **D-h** `PUT /api/copy/<id>` accepts `{"removed_reason": null}` to clear a reason.
- **D-i** Nightly backup runs `/opt/player-select/backup.sh` from the cron block instead of an inline command: crontab treats `%` in `$(date +%F)` as a newline.
- **D-j** The installer writes a placeholder `persons.json` (`[{"name": "Player 1", "color": "#4c78a8"}]`, mode 0600) on first install only; the owner edits it. The server exits non-zero if `--persons` names a file that is unreadable or not a JSON list of `{name, color?}`, and starts with the persons already in the DB if the file is absent.
- **D-k** CI unit job uses `unittest discover` rather than SBT's hand-maintained module list, so tasks do not edit `ci.yml` to register tests.

## Open questions for the plan gate (answers change Tasks 1 and 6)

- **Q1 CSV join key.** The CSV's `Country` column holds names (`United States of America`, `World`, `Japan`); the spec stores `.ged` `country_id` as a raw ID and puts the ID→name map out of scope, so the §6.3 join on `(platform, title, country, date)` never matches. **Default in this plan:** join on `(platform name, raw title, UTC created date)`, narrow by ownership label, country not used. On the 2026-11-07 export that leaves one more same-day pair ambiguous (2 more copies unenriched). **Alternative:** add a `countries` object to `gameeye_id_maps.json` for the three IDs in use and keep country in the key.
- **Q2 Personal data in the committed spec.** The spec on `main` names the household members and the personal domain. Once Task 1's privacy scan runs with `PRIVACY_DENYLIST`, CI fails on it. **Default in this plan:** Task 1 redacts those strings in the spec (wording only, no design change) and the plan itself contains none. Rewriting the existing commit's history is left to the owner.

---

## File Structure

| Path | Responsibility | Task |
|---|---|---|
| `LICENSE`, `.gitignore`, `README.md` | MIT, ignores (§12), README | 1, 17 |
| `.github/workflows/ci.yml`, `.github/dependabot.yml` | CI (§11) | 1, 14, 16 |
| `.github/scripts/privacy_scan.py`, `lint.sh`, `systemd_verify.sh` | privacy scan with GameEye rules; lint | 1, 16 |
| `migrations/001_init.sql` | §5.1 schema, verbatim | 2 |
| `ps/__init__.py` | empty | 2 |
| `ps/db.py` | connect, migrate, `Store` (writer lock, transactions) | 2 |
| `ps/titles.py` | `normalize`, `display_title` | 3 |
| `data_static/gameeye_id_maps.json` | ID maps, category 3 fixed | 4 |
| `ps/idmaps.py` | load maps, family table, platform sync | 4 |
| `ps/gameeye_ged.py` | `.ged` file → `GedFile` (no app DB) | 5 |
| `ps/gameeye_csv.py` | CSV bytes → `CsvRow`s; CSV↔copy matching (pure) | 6 |
| `ps/importer.py` | stage, diff, apply, expire, enrichment | 7–9 |
| `ps/catalog.py` | read model, play state, up next, removal reason, persons, merge, split, dup-ack | 10–11 |
| `player_select_server.py` | HTTP + JSON API (§7.2) | 12–13 |
| `catalog.html` | the page (§7.1) | 12 (stub), 14–15 |
| `backup.sh`, `install/alpine.sh`, `contrib/systemd/*` | deployment (§12) | 16 |
| `adapters/README.md` | snapshot contract (§8) | 17 |
| `tests/_support.py` | server process, HTTP client, multipart encoder | 12 |
| `tests/_ged.py` | synthetic `.ged` and CSV builders, fixture rows | 5 |
| `tests/_seed.py` | applies the fixture import to a fresh store | 10 |
| `tests/fixtures/titles.json` | normalizer vectors | 3 |

## Milestones and branches

Each milestone is one branch and one PR into `main`, opened when its last task passes review. If the previous PR is not merged yet, branch from it (stacked PR) and say so in the PR body.

| Milestone | Branch | Tasks |
|---|---|---|
| M1 Foundations and parsers | `feature/v1-m1-foundations` | 1–6 |
| M2 Importer | `feature/v1-m2-importer` | 7–9 |
| M3 Catalog and API | `feature/v1-m3-api` | 10–13 |
| M4 UI | `feature/v1-m4-ui` | 14–15 |
| M5 Deploy and docs | `feature/v1-m5-deploy` | 16–17 |

---

### Task 1: Repository scaffolding, CI and privacy scanner

**Files:**
- Create: `LICENSE`, `.gitignore`, `README.md` (skeleton), `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.github/scripts/privacy_scan.py`, `.github/scripts/lint.sh`, `tests/test_privacy_scan.py`, `tests/browser/requirements.txt`
- Modify: `docs/superpowers/specs/2026-11-07-player-select-v1-design.md` (Q2 redaction only)

**Interfaces:**
- Produces: `privacy_scan.py [--root DIR] [--no-history] [--require-denylist]`, exit 0/1/2 as in SBT; new rule names `data-file` and `gameeye-csv`.

- [ ] **Step 1: Copy SBT's `tests/test_privacy_scan.py` and add the failing GameEye tests**

Build every flagged value at run time, as SBT's file does. `HEADER = ",".join(["Platform", "Category", "UserRecordType", "Title", "Country"])` (the first five GameEye CSV columns; the rule needs only these).

```python
def test_tracked_ged_file_flagged(self):        # repo.add("x/backup.ged", "PK...") ; commit
    self.assertIn("data-file", out); self.assertNotEqual(rc, 0)
def test_tracked_db_and_wal_flagged(self):      # "a.db", "a.db-wal" → two data-file findings
def test_gameeye_csv_header_flagged(self):      # "notes.md" containing HEADER + ",ReleaseType"
    self.assertIn("notes.md:1: gameeye-csv", out)
def test_gameeye_csv_header_quoted_or_spaced_flagged(self):  # '"Platform", "Category", ...' also flagged
def test_fixture_with_allow_marker_exempt_from_csv_rule(self):  # tests/fixtures/s.csv with HEADER and a later line "privacy-scan: allow"
    self.assertEqual(rc, 0)
def test_fixture_without_marker_flagged(self):
def test_csv_header_value_never_printed(self):  # HEADER not in out
```

- [ ] **Step 2: Run, verify the new tests fail**

Run: `python -m unittest -v tests.test_privacy_scan`
Expected: the SBT tests fail with "No such file" until the script exists; after copying the script in Step 3a, only the seven new tests fail.

- [ ] **Step 3: Copy SBT's `privacy_scan.py` and add the two rules**

- `data-file`: a listed path whose name ends in `.ged`, `.db`, `.db-wal`, `.db-shm` or `.sqlite` is a finding (checked by name, before the binary skip).
- `gameeye-csv`: a line that, after removing `"` and spaces, starts with the five-column prefix above. A file under `tests/fixtures/` containing `privacy-scan: allow` anywhere is exempt from this rule only. Build the prefix from a tuple at run time so the script does not flag itself.
- Update the docstring rule list. Keep SBT's steamid/ip/email/denylist rules unchanged.

- [ ] **Step 4: Scaffolding files**

- `LICENSE`: MIT, `Copyright (c) 2026 SDNick484`.
- `.gitignore`: SBT's Python entries (`__pycache__/`, `*.pyc`, `.venv/`, `venv/`, `*.log`) plus §12: `data/`, `*.ged`, `*.db`, `*.db-*`, `*_collection.csv`, `.env`, `.player-select-env`, `persons.json`.
- `README.md`: title, one-paragraph purpose, "Status: under construction (v1 plan in docs/superpowers/plans/)". Task 17 fills it.
- `tests/browser/requirements.txt`: copy SBT's (`playwright==1.49.1`, test-only comment).
- `.github/dependabot.yml`: copy SBT's.
- `.github/scripts/lint.sh`: copy SBT's without the systemd step (Task 16 adds it back).
- `.github/workflows/ci.yml`: copy SBT's with `tests` (unit matrix, `REQUIRE_NODE`/`REQUIRE_OPENSSL` removed, setup-node removed), `lint` and `privacy-scan` jobs only; delete both MCP contract jobs and `env:` MCP keys; keep the weekly `schedule`, permissions, concurrency and every action SHA pin as is. Unit step: `python -m unittest discover -v -s tests -p 'test_*.py' 2>&1 | tee unittest.log`, then SBT's skipped-test guard. Rename the privacy job to `Privacy scan (IPs, emails, GameEye data, denylist)`.

- [ ] **Step 5: Redact the spec (Q2 default; skip if the owner chose otherwise)**

In the spec, replace the household first names (§1 list, §2 "Only … uses the UI", §9 PSN and Xbox rows) with role words ("five household members", "the owner", "the owner's NPSSO", "one child's account"), and the two hostnames under the personal domain (header, §2, §12) with `games.<your-domain>` / `steam.<your-domain>`. No other wording changes.

- [ ] **Step 6: Run the checks**

Run: `python -m unittest -v tests.test_privacy_scan` → OK
Run: `python .github/scripts/privacy_scan.py --no-history` (with `PRIVACY_DENYLIST` set in the environment if available) → `no findings`
Run: `sh .github/scripts/lint.sh` → `lint passed`

- [ ] **Step 7: Commit**

```bash
git add -A && git commit -m "Scaffold the repository: license, CI, privacy scan with GameEye rules"
```

---

### Task 2: Database, schema and migration runner

**Files:**
- Create: `ps/__init__.py`, `ps/db.py`, `migrations/001_init.sql`, `tests/test_db.py`

**Interfaces:**
- Produces:
  - `ps.db.MIGRATIONS: Path` (repo `migrations/`)
  - `connect(path: str | Path, *, writer: bool = False) -> sqlite3.Connection` (`row_factory = sqlite3.Row`)
  - `migrate(conn, migrations_dir: Path = MIGRATIONS, *, now: int) -> list[int]` (versions newly applied)
  - `class Store(path)`: `.write()` context manager yielding the single writer connection inside `BEGIN IMMEDIATE` under a `threading.Lock`, committing on exit and rolling back on exception; `.read()` context manager yielding a fresh per-call connection, closed on exit; `.path`.

- [ ] **Step 1: Write failing tests** (`tests/test_db.py`, temp dir per test)

```python
TABLES = {"schema_version", "person", "platform", "account", "work", "work_alias", "import_run",
          "copy", "copy_meta", "play_state", "up_next_order", "dup_ack", "import_change"}
def test_migrate_fresh_creates_all_tables(self):  # names from sqlite_master type='table' == TABLES; versions == [1]
def test_migrate_twice_applies_nothing(self):     # second migrate() == []
def test_failed_migration_rolls_back(self):       # dir with 001 + "002_bad.sql": "CREATE TABLE x(a); SELECT no_such_fn();"
    # raises sqlite3.OperationalError; table x absent; schema_version versions == [1]
def test_pragmas(self):                           # writer: foreign_keys 1, journal_mode 'wal', busy_timeout 5000
def test_copy_game_requires_work(self):           # insert category 'game', work_id NULL → IntegrityError
def test_removed_reason_requires_removed_at(self): # → IntegrityError
def test_store_write_rolls_back_on_exception(self):
def test_store_read_sees_committed_write(self):
```

- [ ] **Step 2: Run, verify failure**: `python -m unittest -v tests.test_db` → ImportError on `ps.db`.

- [ ] **Step 3: Implement**

`migrations/001_init.sql` is §5.1 verbatim. `migrate`: current version is 0 when `schema_version` does not exist, else `max(version)`; apply each `NNN_*.sql` above it in order, each as one `executescript("BEGIN IMMEDIATE;\n" + sql + "\nINSERT INTO schema_version VALUES (N, now);\nCOMMIT;")`, issuing `ROLLBACK` and re-raising on error.

- [ ] **Step 4: Run** `python -m unittest -v tests.test_db` → OK; full suite → OK.

- [ ] **Step 5: Commit** `git commit -m "Add the SQLite schema, migration runner and store"`

---

### Task 3: Title normalization

**Files:**
- Create: `ps/titles.py`, `tests/fixtures/titles.json`, `tests/test_titles.py`

**Interfaces:**
- Produces: `normalize(raw: str) -> tuple[str, str | None]` (work_key, edition); `display_title(raw: str) -> str` (raw with every `[...]` removed, whitespace collapsed).

- [ ] **Step 1: Write the vectors and failing tests**

`tests/fixtures/titles.json` is a list of `{"raw", "key", "edition"}`. It must contain at least:

| raw | key | edition |
|---|---|---|
| `Witcher III: Wild Hunt` | `witcher 3 wild hunt` | null |
| `Witcher III: Wild Hunt [Complete Edition]` | `witcher 3 wild hunt` | `Complete Edition` |
| `The Elder Scrolls V: Skyrim` | `elder scrolls 5 skyrim` | null |
| `Elder Scrolls V: Skyrim` | `elder scrolls 5 skyrim` | null |
| `Legend of Zelda: Ocarina of Time` | `legend of zelda ocarina of time` | null |
| `Legend of Zelda: Ocarina of Time [Foil Cover Art]` | `legend of zelda ocarina of time` | `Foil Cover Art` |
| `Jurassic Park` | `jurassic park` | null (known false merge across Sega CD/Genesis; fixed by split) |
| `Ratchet & Clank` | `ratchet and clank` | null |
| `Pokémon Snap` | `pokemon snap` | null |
| `Final Fantasy XVI` | `final fantasy 16` | null |
| `Final Fantasy XVII` | `final fantasy xvii` | null |
| `Mega Man X` | `mega man 10` | null (spec rule; a known risk, recorded deliberately) |
| `Theme Park` | `theme park` | null |
| `Halo 2 [Limited] [Steelbook]` | `halo 2` | `Limited` |
| `  Spaced   Out  ` | `spaced out` | null |
| `[Prototype]` | `` | `Prototype` |

```python
def test_vectors(self):          # for each vector, subTest(raw): normalize(raw) == (key, edition)
def test_spec_merges_present(self):  # the Witcher, Skyrim and Ocarina rows exist and pairs share keys
def test_display_title(self):    # display_title("Witcher III: Wild Hunt [Complete Edition]") == "Witcher III: Wild Hunt"
```

- [ ] **Step 2: Run, verify failure** (`python -m unittest -v tests.test_titles`).
- [ ] **Step 3: Implement** the §5.4 steps in order. Roman map is whole words `ii`…`xvi` → `2`…`16`; `i` is not converted; leading `the` is removed only as a whole word.
- [ ] **Step 4: Local sanity check (not committed):** normalizing the `Games` rows of the real CSV yields 455 keys and 9 keys spanning more than one platform. Then full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Add title normalization with fixture vectors"`

---

### Task 4: GameEye ID maps and platforms

**Files:**
- Create: `data_static/gameeye_id_maps.json` (the project copy, with `"3": "Toys To Life"`), `ps/idmaps.py`, `tests/test_idmaps.py`

**Interfaces:**
- Consumes: `ps.db.connect`, `migrate`.
- Produces:
  - `@dataclass(frozen=True) IdMaps(platforms: dict[int, str], categories: dict[int, str], ownership: dict[int, str])`
  - `load(path: Path = DEFAULT_PATH) -> IdMaps` (raises `ValueError` on a malformed file)
  - `FAMILY: dict[int, str]` exactly the §5.2 table; `family_of(gameeye_id: int) -> str` (`"other"` when absent)
  - `CATEGORY_CODE = {0: "game", 1: "system", 2: "controller", 3: "toy", 4: "print", 5: "accessory"}`
  - `condition_label(mask: int | None) -> str` (`"Loose"`, `"Boxed"`, `"CIB"`, `"New"`, `"Unknown (n)"`, `"Unknown"` for None)
  - `UNMAPPED = "GameEye platform {id} (unmapped)"`
  - `sync_platforms(conn, maps) -> None` (upsert one row per mapped ID by `gameeye_id`, setting name and family; renames placeholders)
  - `ensure_platform(conn, gameeye_id: int, maps) -> tuple[int, bool]` (platform row id, created_now)

- [ ] **Step 1: Failing tests**

```python
def test_category_3_label(self):       # load().categories[3] == "Toys To Life"
def test_counts(self):                  # 36 platforms; ownership == {1: "Loose", 5: "Boxed", 7: "CIB", 31: "New"}
def test_family_table_covers_map(self): # set(load().platforms) == set(FAMILY)
def test_family_spot_checks(self):      # 46/105/37 playstation; 106 xbox; 178/76 nintendo; 32 sega; 27 atari;
                                        # 1 pc; 33 nec; 168 media; 152 other; 999 other
def test_condition_label(self):         # 7 "CIB"; 3 "Unknown (3)"; None "Unknown"
def test_sync_platforms_idempotent(self):    # twice → 36 rows
def test_ensure_platform_unmapped_once(self): # 199 → (id, True) then (id, False); name UNMAPPED; family "other"
def test_sync_renames_placeholder(self):  # placeholder for 199, then sync with maps where 199 = "Test Console" → renamed
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `python -m unittest -v tests.test_idmaps` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Add GameEye ID maps, platform families and platform sync"`

---

### Task 5: Synthetic backup builder and `.ged` reader

**Files:**
- Create: `tests/_ged.py`, `ps/gameeye_ged.py`, `tests/test_gameeye_ged.py`

**Interfaces:**
- Produces (tests): `OWNERSHIP_COLUMNS: tuple[str, ...]` (the nine §6.1 columns plus `purchase_price`), `ged_row(**kw) -> dict`, `FIXTURE_ROWS: list[dict]`, `build_ged(rows, *, user_version=20, member="ownership_database.db", extra_members=(), drop_columns=(), table="ownership") -> bytes`.
  `FIXTURE_ROWS`, in this order with uuids `…01`–`…08`: `01` Bloodborne Boxed (mask 5) and `02` Bloodborne CIB (mask 7) on 46, created on different days; `03`, `04` Dead to Rights CIB on 14, same `created_at` day; `05` Donkey Kong Bananza CIB on 178; `06` Nintendo Switch 2 system (category 1) on 178; `07` a game titled `Test Unmapped Game` on unmapped platform 199; `08` a game with category 9. `country_id` 1 for all, `created_at` fixed epoch values. Also `fixture_csv_rows() -> list[dict]` (Task 6): one CSV row for each of `01`–`06`, labels from the ID maps.
- Produces (product):
  - `class GedError(ValueError)` with `.code` in `{"not_zip", "bad_members", "too_large", "not_sqlite", "missing_table", "missing_column", "duplicate_uuid"}`
  - `@dataclass(frozen=True) GedRow(uuid: str, item_id: str | None, platform_id: int | None, category_id: int | None, title: str | None, ownership_mask: int | None, country_id: str | None, created_at: int | None, updated_at: int | None)`
  - `@dataclass GedFile(rows: list[GedRow], user_version: int)`
  - `read_ged(path: Path, workdir: Path, *, max_bytes: int = 256 * 1024 * 1024) -> GedFile`
  - `_copy_capped(src: BinaryIO, dst: BinaryIO, limit: int) -> int` (raises `GedError("too_large")` once more than `limit` bytes have been read)

- [ ] **Step 1: Failing tests**

```python
def test_reads_fixture(self):          # 8 rows; user_version 20; platform_id int; country_id "1"; created_at int
def test_not_a_zip(self):              # code "not_zip"
def test_two_members(self):            # code "bad_members"
def test_wrong_member_name(self):      # code "bad_members"
def test_declared_too_large(self):     # max_bytes smaller than the member → "too_large"
def test_copy_capped_stops_at_limit(self):  # BytesIO(b"x" * 101), limit 100 → "too_large"; limit 101 → 101
def test_member_not_sqlite(self):      # "not_sqlite"
def test_missing_table(self):          # table="things" → "missing_table"
def test_missing_column(self):         # drop_columns=("country_id",) → "missing_column", "country_id" in str(e)
def test_duplicate_uuid(self):         # "duplicate_uuid"
def test_user_version_reported(self):  # 21 → GedFile.user_version == 21
def test_null_title_kept_none(self):
def test_extracted_db_not_modified(self):  # bytes of the extracted file identical before and after read
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Extract by streaming the member through `_copy_capped`; open `file:<path>?mode=ro&immutable=1` with `uri=True`; check columns with `PRAGMA table_info(ownership)`. `sqlite3.DatabaseError` on open/query → `not_sqlite`.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_gameeye_ged` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Add the .ged reader and a synthetic backup builder for tests"`

---

### Task 6: CSV parser and CSV↔copy matching

**Files:**
- Create: `ps/gameeye_csv.py`, `tests/test_gameeye_csv.py`
- Modify: `tests/_ged.py` (add the CSV builder)

**Interfaces:**
- Consumes: `ps.idmaps.IdMaps`.
- Produces (tests): `csv_row(**kw) -> dict` keyed by `CSV_COLUMNS`; `fixture_csv_rows() -> list[dict]` (see Task 5); `build_csv(rows, *, trailing_comma=True, last_line_trailing=True, leading_space=False) -> bytes` (header from `",".join(CSV_COLUMNS)`).
- Produces (product):
  - `CSV_COLUMNS: tuple[str, ...]` (24 names, in GameEye order)
  - `class CsvError(ValueError)`
  - `@dataclass(frozen=True) CsvRow(platform: str, category: str, title: str, country: str | None, created_date: datetime.date, ownership: str, publisher: str | None, developer: str | None, genre: tuple[str, ...] | None, metacritic: int | None, price_loose: float | None, price_cib: float | None, price_new: float | None)`
  - `parse_csv(data: bytes) -> list[CsvRow]`
  - `meta_fields(row: CsvRow) -> dict[str, str]` (only non-null fields; `genre` as `json.dumps(list)`, `metacritic` as `str(int)`, prices as `repr`-free `f"{x:.2f}"`)
  - `@dataclass(frozen=True) CopyKey(source_id: str, platform: str, raw_title: str, created_at: int | None, condition: str)`
  - `@dataclass MatchResult(meta: dict[str, dict[str, str]], enriched: int, ambiguous: int, unmatched: int)`
  - `match(copies: list[CopyKey], rows: list[CsvRow], maps: IdMaps) -> MatchResult`

- [ ] **Step 1: Failing tests**

```python
def test_25_fields_with_trailing_comma(self):
def test_last_line_24_fields(self):           # last_line_trailing=False parses
def test_wrong_field_count_raises(self):      # a 23-field row → CsvError
def test_header_mismatch_raises(self):
def test_sentinels(self):                     # "", "?", "Missing Field", "-1.0" → None; price "0.0" → None
def test_leading_space_quoted(self):          # leading_space=True: ' "-1.0"' → None, ' "Sega"' → "Sega"
def test_created_date(self):                  # "Nov 8, 2026" → date(2026, 11, 8)
def test_genre_split(self):                   # "Action, Adventure" → ("Action", "Adventure")
def test_meta_fields(self):                   # {"genre": '["Action", "Adventure"]', "metacritic": "87", "price_cib": "24.99"}; null omitted
def test_match_unique(self):                  # enriched 1
def test_match_narrows_by_ownership(self):    # Bloodborne Boxed + CIB, same day → each gets its own row's price
def test_match_same_day_same_condition_ambiguous(self):  # Dead to Rights ×2 → ambiguous 2, no meta for either (D-a)
def test_match_ignores_country(self):         # CSV country "World" still matches (Q1 default)
def test_match_utc_day(self):                 # created_at 2026-11-09T07:30Z matches "Nov 9, 2026", not "Nov 8, 2026"
def test_match_title_exact_with_brackets(self):  # "Ocarina … [Foil Cover Art]" matches only that copy
def test_unknown_platform_label_unmatched(self):  # label not in maps → counted unmatched
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Parse with `csv.reader(io.TextIOWrapper(..., encoding="utf-8-sig"), skipinitialspace=True)` and strip each field; accept 25 fields only if the last is empty. If Q1 picks the country map, add `country` to `CopyKey` and the join and change `test_match_ignores_country` accordingly.
- [ ] **Step 4: Local sanity check (not committed):** `parse_csv` on the real CSV returns 614 rows without error. Full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Add the GameEye CSV parser and copy matching"`

**M1 PR:** push `feature/v1-m1-foundations`, open the PR (title "M1: foundations and parsers"), list Tasks 1–6 and paste the full-suite output.

---

### Task 7: Importer, part 1: stage, diff, expiry

**Files:**
- Create: `ps/importer.py`, `tests/test_importer_stage.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
  - `MAX_UPLOAD = 32 * 1024 * 1024`, `STAGE_TTL = 3600`, `REMOVAL_GUARD = 0.25`, `EXPECTED_USER_VERSION = 20`
  - `class ImportFailure(Exception)`: `.status: int`, `.code: str`, `.detail: str`
  - `@dataclass(frozen=True) Mapped(source_id, source_item_id, gameeye_platform_id: int, platform_name: str, category: str, raw_title: str, edition: str | None, work_key: str | None, condition_mask: int | None, country: str | None, created_at: int | None, updated_at: int | None)` (`work_key` None for non-games)
  - `stage(store, maps, imports_dir: Path, ged: bytes, csv: bytes | None, *, now: int) -> dict` (the §6.1 response body)
  - `expire(conn, imports_dir: Path, *, now: int) -> int`

- [ ] **Step 1: Failing tests** (temp store, migrated, platforms synced; `T0 = 1_800_000_000`)

```python
def test_first_stage(self):
    # summary == {"added": 7, "edited": 0, "removed": 0, "restored": 0, "unchanged": 0, "skipped": 1}
    # dry_run True; removal_guard False; isinstance(import_id, int)
    # {"kind": "unmapped_platform"} and {"kind": "unknown_category"} among warnings (kind match)
    # copy table still empty; import_run status "staged", base_run_id None; 7 import_change "add" rows
    # imports/<sha>.ged exists, mode 0o600
def test_change_entry_shape(self):     # {"op": "add", "source_id": "<uuid 05>", "title": "Donkey Kong Bananza",
                                       #  "platform": "Nintendo Switch 2", "condition": "CIB"} in changes
def test_user_version_warning(self):   # {"kind": "user_version", "detail": "expected 20, got 21"}
def test_same_file_as_last_applied(self):  # seed an applied run with this sha and 7 active copies →
                                           # {"import_id": None, "summary": {"unchanged": 7}}; no new run row
def test_same_file_with_csv_stages(self):
def test_bad_ged_is_400(self):         # not a zip → ImportFailure(400, "not_zip"); duplicate uuid → 400 "duplicate_uuid"
def test_oversize_is_413(self):        # patch MAX_UPLOAD to 10 → ImportFailure(413, "too_large")
def test_null_fields_skipped_with_warning(self):  # title None or "" → "bad_row"; platform_id None → "bad_row";
                                                  # category None → "unknown_category"; all counted in skipped
# Diff against seeded copies (insert copy rows directly):
def test_edit_detected_per_field(self):  # raw_title, platform, category, condition_mask, country each → "edit"
def test_updated_at_alone_not_edit(self):
def test_remove_when_absent(self):
def test_restore_when_removed_and_present(self):
def test_restore_with_edit_keeps_both(self):  # op "restore", after_json carries the new title
def test_removal_guard(self):            # 4 active, 2 absent → True; 1 absent → False (25% is not > 25%)
def test_expire(self):                   # staged at now-3601 → "expired", files gone; now-3599 untouched;
                                         # stage() expires lazily before its own work
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Order: `expire`; size check; sha256; short-circuit (§6.1 step 2); write staged files 0600 via temp file + `os.replace`; `read_ged` into `imports/<sha>.d/` (removed after reading); map; diff on `source_platform_id` (GameEye ID), not the internal platform id; store run + changes in one write transaction. `summary_json` holds the response summary plus `removal_guard` and `warnings`. CSV handling is Task 9: here, attach and store `csv_sha256` but do not parse.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_importer_stage` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Importer: stage a .ged backup as a dry-run diff"`

---

### Task 8: Importer, part 2: apply

**Files:**
- Modify: `ps/importer.py`
- Create: `tests/test_importer_apply.py`

**Interfaces:**
- Produces: `apply(store, maps, imports_dir: Path, run_id: int, *, confirm_removals: bool, now: int) -> dict` → `{"import_id": id, "applied": True, "summary": {...}}`; `attach_work(conn, raw_title: str) -> int` (alias lookup or create; used again by Tasks 10–11).

- [ ] **Step 1: Failing tests**

```python
def test_apply_first_import(self):
    # 7 copies: form "physical", account_id None, source "gameeye", last_seen_import == run id
    # both Bloodborne copies share one work whose title is "Bloodborne"; the system copy has work_id None
    # a platform row named "GameEye platform 199 (unmapped)" exists (D-e)
    # run status "applied", applied_at == now; staged files deleted
def test_reimport_same_file_short_circuits(self):  # {"import_id": None, "summary": {"unchanged": 7}}
def test_reimport_same_file_with_csv_all_unchanged(self):  # staged; added/edited/removed/restored 0, unchanged 7
def test_stale_base_409(self):        # stage A and B, apply A, apply B → ImportFailure(409, "stale_base")
def test_apply_twice_409(self):       # → 409 "not_staged"
def test_unknown_run_404(self):
def test_expired_410(self):           # apply at now + 3601 → 410 "expired"
def test_missing_staged_file_410(self):
def test_removal_guard_428_then_confirm(self):  # 428 "confirm_removals"; then confirm_removals=True succeeds
def test_title_edit_reattaches_work(self):
def test_pinned_copy_keeps_work_on_title_edit(self):  # raw_title and edition updated, work_id unchanged
def test_remove_and_restore(self):    # removed_at set; restore clears removed_at and removed_reason
def test_restored_copy_work_keeps_play_state(self):
def test_category_change_game_to_accessory_and_back(self):  # work_id NULL then re-attached; no IntegrityError
def test_last_seen_import_set_on_unchanged(self):
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** in one `store.write()` (`BEGIN IMMEDIATE`): `expire`; checks in the order 404, 410, 409 `not_staged`, 409 `stale_base`, 428; recompute from staged files (D-f); `ensure_platform` for each GameEye platform ID; insert/update/remove/restore; `last_seen_import`; mark applied; delete staged files after commit.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_importer_apply` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Importer: apply a staged run"`

---

### Task 9: Importer, part 3: CSV enrichment

**Files:**
- Modify: `ps/importer.py`
- Create: `tests/test_importer_csv.py`

**Interfaces:**
- Consumes: `parse_csv`, `match`, `meta_fields` (Task 6).
- Produces: `summary["csv"] = {"enriched": n, "unmatched": n, "ambiguous": n}` on stage when a CSV is attached; `copy_meta` rows with `source = "gameeye_csv"` on apply.

- [ ] **Step 1: Failing tests**

```python
def test_stage_reports_csv_counts(self):    # fixture + build_csv(fixture_csv_rows()) → {"enriched": 4, "unmatched": 0, "ambiguous": 2}
                                            # (Bloodborne ×2, Bananza, Switch 2 system enriched; Dead to Rights ×2 ambiguous)
def test_bad_csv_400(self):                 # ImportFailure(400, "bad_csv")
def test_unmatched_csv_row_counted(self):
def test_apply_writes_meta(self):           # genre JSON list; fetched_at == now; Dead to Rights copies have no meta
def test_boxed_and_cib_get_their_own_prices(self):
def test_reapply_upserts(self):             # changed price replaces the value; one row per (copy, field, source)
def test_field_null_in_new_csv_deletes_row(self):  # D-b
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Build `CopyKey`s for every mapped row of the backup (not only changed ones). On apply, enrich from the recomputed match, and delete `gameeye_csv` rows for fields the matched CSV row leaves null.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_importer_csv` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Importer: enrich copies from the GameEye CSV"`

**M2 PR:** "M2: importer" with Tasks 7–9.

---

### Task 10: Catalog read model, persons, play state, Up next, removal reason

**Files:**
- Create: `ps/catalog.py`, `tests/_seed.py`, `tests/test_catalog.py`

**Interfaces:**
- Consumes: importer `stage`/`apply`, `tests/_ged.FIXTURE_ROWS`.
- Produces (tests): `seed(store, maps, tmp: Path, *, csv: bool = True, now=T0) -> None` (stages and applies the fixture).
- Produces (product):
  - `STATUSES = ("backlog", "up_next", "playing", "finished", "abandoned", "not_for_me")`, `REASONS = ("sold", "traded", "lost", "gifted", "mistake")`, `NOTE_MAX = 2000`
  - `class CatalogError(Exception)`: `.status`, `.code`, `.detail`
  - `seed_persons(conn, persons: list[dict]) -> None` (insert missing by name, `sort` = list index, never delete)
  - `catalog(conn) -> dict` with exactly the §7.2 keys:
    `persons [{id, name, color}]` (by sort), `platforms [{id, name, family, gameeye_id}]`, `works [{id, title}]`,
    `copies [{id, work_id, platform_id, category, form, raw_title, edition, condition_mask, condition, country, source, source_id, created_at, removed_at, removed_reason}]` (removed included),
    `meta {copy_id: {field: value}}` (`genre` as list, `metacritic` as int, prices as float),
    `play_state [{person, work, status, note, updated_at}]`, `up_next {person_id: [work_id, ...]}`,
    `dup_groups [{work, platform, copies: [ids]}]` (unacknowledged only; Task 11 fills), `last_import {id, applied_at, summary} | None`
  - `set_play_state(conn, person: int, work: int, status: str, note: str | None, *, now: int) -> dict | None` (returns previous `{status, note}` or None)
  - `set_up_next(conn, person: int, order: list[int]) -> None`
  - `set_removed_reason(conn, copy_id: int, reason: str | None) -> None`

- [ ] **Step 1: Failing tests**

```python
def test_catalog_keys(self):          # set(catalog(conn)) == the nine §7.2 keys
def test_copy_shape(self):            # a Bloodborne CIB copy has condition "CIB", form "physical"
def test_meta_types(self):            # genre list, metacritic int
def test_seed_persons(self):          # [Player A, Player B] twice → 2 rows; then [Player A] → Player B still there
def test_play_state_set_and_previous(self):  # first previous None; second {"status": "finished", "note": None}
def test_play_state_empty_status_deletes(self):
def test_note_stripped_and_limited(self):    # "  hi  " → "hi"; 2001 chars → CatalogError(400, "note_too_long")
def test_bad_status_400(self):
def test_unknown_person_or_work_404(self):
def test_states_are_per_person(self):        # A finished, B has no row for that work
def test_up_next_append_and_remove(self):    # two works set up_next → order [w1, w2]; w1 → playing → [w2]
def test_set_up_next_reorders(self):
def test_set_up_next_mismatch_409(self):     # "order_mismatch"; duplicate ids → 400
def test_removed_reason(self):    # active copy → 409 "not_removed"; removed → stored; null clears (D-h); "x" → 400; unknown → 404
def test_last_import(self):
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Callers own the transaction (`store.write()`); these functions do not commit.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_catalog` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Catalog read model, play state and Up next"`

---

### Task 11: Merge, split and duplicates

**Files:**
- Modify: `ps/catalog.py`
- Create: `tests/test_catalog_edit.py`

**Interfaces:**
- Produces: `merge(conn, into: int, from_: int, *, now: int) -> None`; `split(conn, copy_id: int, title: str) -> int` (work id); `dup_groups(conn) -> list[dict]` (used by `catalog`); `ack_dup(conn, work: int, platform: int, *, now: int) -> None`.

- [ ] **Step 1: Failing tests**

```python
def test_merge_repoints(self):            # copies, aliases, up_next_order move to into; from deleted
def test_merge_play_state_later_wins_and_notes_append(self):  # into note "a" (older), from note "b" (newer)
                                          # → status from the newer row, note "b\n\na"
def test_merge_up_next_collision_keeps_into(self):  # D-d
def test_merge_drops_dup_acks(self):      # D-d
def test_merge_same_or_unknown(self):     # into == from → 400; unknown → 404
def test_split_creates_pinned_work(self): # new work titled as given; copy work_pinned 1
def test_split_survives_reimport(self):   # re-stage/apply the same backup: copy keeps the split work
def test_split_onto_existing_key_attaches(self):  # D-c
def test_split_rejects_non_game_and_empty_title(self):  # 400
def test_dup_groups_from_fixture(self):   # Bloodborne (46) and Dead to Rights (14) groups, copies sorted
def test_ack_hides_group(self):
def test_ack_void_when_set_changes(self): # ack, then a third copy imported → group returns
def test_removed_copy_dissolves_group(self):
def test_ack_non_group_409(self):         # "not_a_duplicate"
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** `copy_ids` is `json.dumps(sorted(ids))`; compare as strings.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_catalog_edit` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Catalog: merge, split and duplicate review"`

---

### Task 12: Server: page, catalog and JSON routes

**Files:**
- Create: `player_select_server.py`, `catalog.html` (stub: `<main id="app">Player Select</main>`), `tests/_support.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `Store`, `migrate`, `idmaps.load/sync_platforms`, `catalog.*`.
- Produces (tests): `_support.py` adapted from SBT's: `ROOT`, `free_port`, `wait_for`, `Response`, `request(port, method, path, body=None, headers=None)`, `raw_request(port, head: bytes)`, `multipart(fields: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]` (content type, body), `PERSONS = [{"name": "Player A", "color": "#1f77b4"}, {"name": "Player B", "color": "#ff7f0e"}]`, and `Server(*, persons=PERSONS, nav=None, extra=None)` with `start/stop/cleanup/get/put/post/req`, `.data`, `.port`. Drop SBT's Steam mocks and export stubs.
- Produces (product): CLI per §4.3; `API_VERSION = "1"`; `ROUTES: list[tuple[str, str]]` (method, path pattern) used by the README test in Task 17; `etag_of`, `none_match`, `config_script`, `SKELETON_HEAD/TAIL`, `atomic_write` copied from SBT.

- [ ] **Step 1: Failing tests**

```python
def test_headers_on_every_response(self):  # /healthz, /api/catalog, 404, a malformed request line (raw_request):
                                           # x-playerselect-server "1", x-playerselect-api "1"
def test_page_routes(self):        # "/", "/games", "/items", "/up-next", "/duplicates", "/import": 200 text/html,
                                   # body contains "window.APP_CONFIG" and the --nav label
def test_healthz(self):            # 200 "ok"
def test_catalog_etag_and_304(self):  # If-None-Match → 304; after a PUT the ETag changes
def test_play_state_put(self):     # {"person": A, "work": W, "status": "finished"} → 200, "previous" None
def test_play_state_if_match(self):  # stale → 412 {"error": "precondition_failed", ...}, ETag header; current → 200
def test_json_content_type_required(self):  # PUT/POST JSON routes without it → 415 "unsupported_media_type"
def test_bad_json_400(self):
def test_error_shape(self):        # every 4xx body has exactly "error" and "detail"
def test_unknown_route_404(self):  # {"error": "not_found", ...}
def test_unknown_person_404(self):
def test_up_next_copy_merge_split_dup_ack_routes(self):  # one happy path and one error each
def test_persons_seeded_at_start(self):   # catalog persons names == ["Player A", "Player B"]
def test_bad_persons_file_exits(self):    # file contains "{" → process exits non-zero, stderr names the file
def test_restart_is_idempotent(self):     # stop, start on the same --data: same catalog
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** in SBT's shape (`App`, `make_handler`, `send_response` adding the two headers, `send`, `json`). Map `CatalogError` and `ImportFailure` to their status and code. Catalog GET and the PUT If-Match check both compute the ETag over `dump(catalog(conn))`; the PUT does so under the write transaction. Start-up: `makedirs(data/imports, 0o700)`, migrate, `sync_platforms`, `seed_persons`.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_server` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Server: page, catalog and JSON routes"`

---

### Task 13: Server: import routes and multipart

**Files:**
- Modify: `player_select_server.py`
- Create: `tests/test_server_import.py`

**Interfaces:**
- Produces: `parse_multipart(content_type: str, body: bytes) -> dict[str, bytes]` (field name → bytes; raises `ValueError` on a missing boundary or unparsable body).

- [ ] **Step 1: Failing tests**

```python
def test_parse_multipart_binary_exact(self):  # file bytes b"\r\n--x\r\n\x00\xff" round-trip exactly
def test_parse_multipart_no_boundary(self):   # ValueError
def test_stage_and_apply(self):    # POST /api/import/gameeye (ged + csv) → 200 body as importer; apply → 200;
                                   # GET /api/catalog shows 7 copies
def test_stage_without_csv(self):
def test_oversize_413(self):       # raw_request with Content-Length 33554433 and no body → 413 "too_large"
def test_not_multipart_415(self):
def test_missing_ged_400(self):    # no "ged" part, or empty → 400 "missing_ged"
def test_apply_requires_json(self):  # 415
def test_apply_errors_pass_through(self):  # 404, 409, 428 codes from the importer
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Check Content-Length before reading the body. Parse by prefixing `Content-Type: <header>\r\nMIME-Version: 1.0\r\n\r\n` and using `BytesParser(policy=email.policy.HTTP)`; read parts with `get_payload(decode=True)`.
- [ ] **Step 4: Run** `python -m unittest -v tests.test_server_import` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Server: import upload and apply routes"`

**M3 PR:** "M3: catalog and API" with Tasks 10–13.

---

### Task 14: Page, part 1: shell, person picker, Games and Items, filters

**Files:**
- Modify: `catalog.html`, `.github/workflows/ci.yml` (add SBT's `page-smoke` job; test step runs `python -m unittest discover -v -s tests/browser -p 'test_*.py'` with the skipped-test guard)
- Create: `tests/browser/test_page.py`, `tests/browser/test_filters.py`

**Interfaces:**
- Consumes: `/api/catalog`, `PUT /api/play-state`, `window.APP_CONFIG.nav`.
- Produces: URL hash filter keys (`#k=v&k=v`, arrays repeat the key): `p` platform ids, `fam` family, `cat` category, `cond` condition label, `ed` `yes|no`, `form`, `st` status (with `none` for untriaged), `g` genre, `gUnk`, `mcmin`, `mcmax`, `mcUnk`, `pub`, `pubUnk`, `rm` show removed, `q` text. Person picker `localStorage` key `ps-person` (try/catch, default first person). Stable selectors: `#view-games`, `#view-items`, `tr.work-row`, `tr.copy-row`, `#count`, `#person`, `select.status[data-work]`.

- [ ] **Step 1: Failing browser tests** (each test starts `Server`, stages and applies the fixture with the CSV through the API, copies SBT's Playwright setup and `REQUIRE_BROWSER` handling)

```python
def test_every_path_loads_without_console_errors(self):  # the six page paths; no console errors, no failed requests
def test_games_count_matches_api(self):   # tr.work-row count == works with ≥1 active copy, computed from /api/catalog
def test_items_include_hardware(self):    # tr.copy-row count == active copies; the Switch 2 system row present
def test_filters_round_trip_url(self):    # pick a platform and category in the UI; reload; same controls and #count
def test_filter_from_hash(self):          # goto "/items#cat=system" → 1 row
def test_include_unknown_metacritic(self):  # mcmin=80 with and without mcUnk changes the count as computed in Python
def test_status_is_per_person(self):      # set "finished" as Player A; switch to B → blank; back to A after reload → "finished"
def test_page_works_without_storage(self):  # init script makes localStorage throw → page renders Games
```

- [ ] **Step 2: Run** `python -m unittest -v tests.browser.test_page tests.browser.test_filters` → FAIL (stub page).
- [ ] **Step 3: Implement** the header (Games · Items · Up next · Duplicates · Import, person picker, nav links) and the Games and Items views per §7.1. Follow `frontend-design` for styling; reuse SBT's filter plumbing (`DEFAULT_F`, `clean`, `paramsToRaw`, `filtersToParams`, `writeHash`). In Games a work matches if any active copy matches.
- [ ] **Step 4: Run** the two modules → OK (locally `REQUIRE_BROWSER=1`); full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Page: Games and Items views with URL filters"`

---

### Task 15: Page, part 2: Up next, Duplicates, Import and row actions

**Files:**
- Modify: `catalog.html`
- Create: `tests/browser/test_actions.py`, `tests/browser/test_import.py`

**Interfaces:**
- Consumes: every §7.2 route.

- [ ] **Step 1: Failing browser tests**

```python
def test_import_flow(self):           # empty server; Import view; set the fixture .ged (and CSV) on the file inputs;
                                      # diff shows "Added 7" and the warnings; Apply → Games shows 4 rows
def test_removal_guard_requires_confirm(self):  # second backup missing most rows → Apply disabled until the
                                                # confirm checkbox is ticked; then applies
def test_note_edit_persists(self):    # edit Player B's note in a Games row detail; reload; note shown
def test_up_next_move_up_persists(self):  # two works up_next; Move up on the second; /api/catalog order reversed
def test_duplicates_ack_hides(self):  # 2 groups; "These are separate copies" on one → 1 left after reload
def test_merge_and_split(self):       # split a Bloodborne copy to a new title → 2 Games rows; merge back → 1
def test_removed_reason_editable(self):  # removed copy in Items with rm=1: set "sold"; persisted
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement** the remaining views and actions per §7.1. Mutations send `If-Match` with the last catalog ETag on play-state PUTs and refetch on 412.
- [ ] **Step 4: Run** `python -m unittest discover -v -s tests/browser -p 'test_*.py'` → OK; full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Page: Up next, Duplicates, Import and row actions"`

**M4 PR:** "M4: UI" with Tasks 14–15.

---

### Task 16: Installer, backup and systemd units

**Files:**
- Create: `install/alpine.sh`, `backup.sh`, `contrib/systemd/player-select.service`, `contrib/systemd/player-select-backup.service`, `contrib/systemd/player-select-backup.timer`, `tests/installer/check.sh`, `tests/test_backup.py`, `.github/scripts/systemd_verify.sh` (copy from SBT)
- Modify: `.github/scripts/lint.sh` (add the systemd step back), `.github/workflows/ci.yml` (add SBT's `installer` job; unit job installs the `sqlite3` CLI and sets `REQUIRE_SQLITE3=1`)

**Interfaces:**
- Produces: `backup.sh [DATA_DIR]` (default `$HOME/data`): `sqlite3 "$DATA/player_select.db" ".backup '$DATA/backups/player_select-$(date +%F).db'"`, then delete `player_select-*.db` in `backups/` older than 14 days (`find -mtime +13`); exit 78 if the DB is absent.
- Installer paths and options: §4.2 and §12 (`PREFIX=/opt/player-select`, user `games`, `HOME_DIR=/home/games`, data `$HOME_DIR/data` with `imports/` 0700 and `backups/`, service `player-select`, `SAVED=/etc/player-select.conf` with `SAVED_PORT`, `SAVED_NAV`, `SAVED_TZ_NAME`, `SAVED_BACKUP_SCHEDULE`; defaults port 8486, schedule `30 3 * * *`); installs `player_select_server.py`, `backup.sh`, `catalog.html`, `ps/`, `migrations/`, `data_static/`; cron block `# BEGIN player-select` / `# END player-select` in `/etc/crontabs/games` running `$PREFIX/backup.sh $HOME_DIR/data`; init `command_args` = `$PREFIX/player_select_server.py --port $PORT --data $HOME_DIR/data --persons $HOME_DIR/persons.json $NAV_ARG`; placeholder `persons.json` per D-j.

- [ ] **Step 1: Failing tests**

`tests/test_backup.py` (skips only without `sqlite3`, and fails instead when `REQUIRE_SQLITE3=1`):
```python
def test_backup_creates_dated_copy(self):   # backups/player_select-YYYY-MM-DD.db opens and has the table rows
def test_backup_prunes_older_than_14_days(self):  # a file with mtime 15 days ago removed; 13 days kept
def test_backup_without_db_exits_78(self):
```

`tests/installer/check.sh`, adapted from SBT's (same stubs, same three runs plus refusals), asserting:
first run with `PORT=8586 NAV=Steam=http://steam.example.com:8484/ TZ_NAME=Etc/UTC 'BACKUP_SCHEDULE=0 4 * * *'` (the script creates a dummy `/usr/share/zoneinfo/Etc/UTC` if absent) saves all four options; init script has `--port 8586`, `--persons /home/games/persons.json` and the nav; exactly one `player-select` block with the schedule, unrelated cron lines kept, an old block replaced; `persons.json` mode 600; `imports/` mode 700; the installed server imports (`cd /opt/player-select && python3 -c 'import player_select_server, ps.importer'`); **service healthy**: start the installed server as `games` on a free port with the installed `--data` and `--persons`, `wget /healthz` → `ok`, stop it. Second run with nothing given: options remembered, `persons.json` byte-for-byte unchanged, crontab identical. Third run `PORT=8686 NAV=`: port changed, nav removed, schedule kept. Refusal: `NAV` with a space.

- [ ] **Step 2: Run, verify failure:** `python -m unittest -v tests.test_backup`; `docker run --rm -v "$PWD:/src:ro" python:3.12-alpine sh /src/tests/installer/check.sh`.
- [ ] **Step 3: Implement** `backup.sh`, `install/alpine.sh` (SBT's structure; `apk add python3 tzdata ca-certificates sqlite`, no venv), the units (copied from SBT's shape: service with `ReadWritePaths=/home/games/data`, backup oneshot + `OnCalendar=*-*-* 03:30:00` timer).
- [ ] **Step 4: Run** the two commands above → OK / `installer checks passed`; `sh .github/scripts/lint.sh` → `lint passed` (shellcheck covers `backup.sh`, `install/alpine.sh`, `tests/installer/check.sh`); full suite → OK.
- [ ] **Step 5: Commit** `git commit -m "Alpine installer, nightly SQLite backup and systemd units"`

---

### Task 17: README and adapter contract

**Files:**
- Modify: `README.md`
- Create: `adapters/README.md`, `tests/test_docs.py`

**Interfaces:**
- Consumes: `player_select_server.ROUTES`.

- [ ] **Step 1: Failing tests**

```python
def test_readme_lists_every_route(self):  # each (method, path) in ROUTES appears in README.md's API table
def test_readme_has_security_warning(self):  # a "Security" section stating there is no authentication and
                                             # the server must sit behind an authenticating proxy, LAN or VPN
def test_adapter_example_parses(self):    # the first ```json block in adapters/README.md parses; keys
                                          # {"schema", "source", "account", "fetched_at", "items"}; item keys
                                          # {"source_id", "title", "platform", "form", "hours", "last_played"}
```

- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Write** the README (what it is; quick start on Alpine; GameEye export how-to; import walkthrough; Security; API table; data and backups; development and tests) and `adapters/README.md` (§8: process model, `run-adapter.sh` + `flock` lock path, snapshot schema and example, ingestion through stage/diff/apply, fixture-test requirement). Placeholder hostnames only (`games.example.com`).
- [ ] **Step 4: Run** `python -m unittest -v tests.test_docs` → OK; full suite → OK; `python .github/scripts/privacy_scan.py --no-history` → `no findings`.
- [ ] **Step 5: Commit** `git commit -m "Document usage, security, the API and the adapter contract"`

**M5 PR:** "M5: deploy and docs" with Tasks 16–17. Then superpowers:finishing-a-development-branch.
