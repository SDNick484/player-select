# Adapters

An adapter reads one digital source (PlayStation, Xbox, Nintendo, ...) and writes a snapshot file that Player Select ingests.

**No adapter exists in v1.** This page is the contract they will follow, written now so the data model and importer are checked against it. Which source goes first is decided by the spikes described in the design spec (`docs/superpowers/specs/2026-11-07-player-select-v1-design.md`, section 9).

## Process model

- An adapter is a **standalone program** in `adapters/<source>/`, for example `adapters/psn/`. It has its own virtualenv or `node_modules`.
- It is **never imported by `ps/`**. Unofficial APIs break and some are Node packages; keeping adapters in their own processes keeps both away from the standard-library core.
- It reads its **credentials from `~/.player-select-env`** (mode 0600, `KEY=VALUE` lines). The server never reads that file and never sees the tokens.
- It is started by `run-adapter.sh <source>`, which holds `flock` on `data/.adapter-<source>.lock` for the whole run, so two runs of one adapter (cron and by hand) can never overlap. This is the same pattern as `run-export.sh` in steam-backlog-triage.
- It writes **one snapshot per account** to `data/snapshots/<source>-<account>.json`, replacing the file atomically (write a temporary file in the same directory, then rename it).

## Snapshot schema

```json
{"schema": 1, "source": "psn", "account": {"external_id": "0000000000", "label": "Player A PSN"},
 "fetched_at": "2026-11-20T04:15:00Z",
 "items": [{"source_id": "EP0000-CUSA00000_00-EXAMPLE0000000000", "title": "Example Game",
            "platform": "Sony PlayStation 5", "form": "digital", "hours": 12.5,
            "last_played": "2026-11-18T21:03:00Z"}]}
```

| Field | Meaning |
|---|---|
| `schema` | Snapshot format version. `1` for now; the core refuses a version it does not know |
| `source` | `psn`, `xbox` or `nintendo`; also the first part of the file name |
| `account.external_id` | The account's stable ID at the source. With the source's family it identifies the `account` row |
| `account.label` | A name for people to read; shown in the UI |
| `fetched_at` | When the adapter read the source, UTC, ISO 8601 |
| `items[].source_id` | The source's stable ID for this title on this account. With `source` it is the copy's key: `(source, source_id)` |
| `items[].title` | The title as the source names it. It goes through the same title normalization as GameEye titles |
| `items[].platform` | A platform name; it must match a platform the catalog knows |
| `items[].form` | `digital`, or `subscription_claim` for a title claimed through a subscription (for example PS Plus) rather than bought |
| `items[].hours` | Hours played, or `null` when the source does not say |
| `items[].last_played` | When the title was last played, UTC, ISO 8601, or `null` |

## Ingestion

The core ingests snapshots through the **same stage, diff and apply path as a GameEye backup**, keyed on `(source, source_id)`:

1. A snapshot is staged as a dry run. The diff lists added, edited and removed copies, with the same removal guard.
2. Applying it adds digital copies, updates changed ones and soft-deletes copies that left the library.
3. Applying the same snapshot twice changes nothing.

An adapter writes **only `hours` and `last_played`** into play state. It never sets or changes `status`: a person decides whether a game is in their backlog, playing or finished. Notes and statuses entered by hand are never touched by an import.

## Tests

Every adapter needs a fixture-based test suite. Build the fixtures from **sanitized spike output**: real response shapes, with account IDs, names and tokens replaced by placeholders, so the tests still pass the repository's privacy scan. Tests run the adapter's parsing against the fixtures and check the snapshot it writes against the schema above. They never call the real service, so a breakage in an unofficial API shows up as a failing fixture refresh and not as a broken CI run.

Never commit credentials, raw responses or real snapshots. `data/` and `.player-select-env` are ignored.
