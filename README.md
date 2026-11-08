# Player Select

A self-hosted catalog of a household's console game collection, written in Python using only the standard library.

Today it imports your **physical** collection from the GameEye app. Digital libraries (PlayStation, Xbox, Nintendo) come later; the data model already has room for them. On top of the catalog it records **play state per person**, so each member of the household has their own backlog, Up next list, notes and status for every game.

- **Import from GameEye:** upload a `.ged` backup in the browser. You see a dry-run diff (added, edited, removed, restored) and nothing changes until you apply it. Importing the same file twice changes nothing.
- **Optional enrichment:** upload GameEye's native CSV export with the backup to add publisher, developer, genre, Metacritic and market prices.
- **Games and Items views:** one row per game (copies grouped across platforms) or one row per copy, hardware included. Filters combine with AND and are kept in the URL.
- **Play state:** pick a person in the header, then set a status (backlog, up next, playing, finished, abandoned, not for me) and a note on any game. Up next is an ordered list per person.
- **Duplicates:** the same title twice on one platform is listed for review. Acknowledge it as "separate copies" or fix it in GameEye.
- **Merge and split:** fix a wrong title match by merging two games or splitting one copy out.

The page is a single HTML file with no build step. Data lives in one SQLite file.

## Files

| File | What it does |
|---|---|
| `player_select_server.py` | Serves the page and its JSON API |
| `catalog.html` | The page |
| `ps/` | The core library: database, GameEye parsers, importer, catalog queries. Imported by the server, so keep it next to `player_select_server.py` |
| `migrations/` | SQL schema, applied in order at start-up |
| `data_static/gameeye_id_maps.json` | GameEye platform, category and ownership names (the backup contains only IDs) |
| `backup.sh` | Online SQLite backup, keeps 14 days |
| `install/alpine.sh` | Installer for Alpine Linux (OpenRC); works well in a small Proxmox LXC |
| `contrib/systemd/` | Service, backup service and backup timer for systemd hosts |
| `adapters/README.md` | The contract for future digital-library adapters (none exist yet) |
| `docs/superpowers/` | The v1 design spec and plan |
| `tests/`, `.github/` | Tests and CI (see [Development and tests](#development-and-tests)) |

## What you need

- **Python 3.11 or newer.** Nothing else to install for the server; there are no third-party packages.
- **The `sqlite3` command-line tool**, only for the nightly backup (`apk add sqlite` on Alpine; the installer does it).
- **GameEye on your phone**, with your collection in it. Two files come out of it:
  - **The `.ged` backup.** This is the import source. It is a zip holding one SQLite database.
  - **The native CSV export.** Optional. It adds publisher, developer, genre, Metacritic and prices, but it carries no stable IDs, so it can only enrich copies that the backup already created.

  Create a backup in GameEye and export its CSV, then copy both files to the computer you browse from. You upload them in the **Import** view.

## Install on Alpine (OpenRC)

```sh
git clone https://github.com/SDNick484/player-select.git
cd player-select
sh install/alpine.sh
```

Run it as root. The installer sets up:
- the `sqlite` package (for backups), plus `python3`, `tzdata` and `ca-certificates`
- the code in `/opt/player-select`, owned by root
- the stock Alpine `games` account, reused as it is. Nothing about it is modified; all paths are explicit.
- the data in `/home/games/data` (mode 0700): the database, `imports/` (staged uploads) and `backups/`, each mode 0700
- a placeholder `/home/games/persons.json`, written once (mode 0600) and never overwritten
- a nightly backup, added as its own marked block in the `games` user's crontab (default 03:30)
- the catalog as an OpenRC service, `player-select`, on port 8486
- your options in `/etc/player-select.conf`, so a re-run keeps them

Re-running the installer updates the code and keeps your data, `persons.json` and options. Options are environment variables; pass one again only to change it:
- `PORT`: the catalog's port (default `8486`).
- `NAV=Label=URL`: adds a header link, with no spaces in it. `NAV=` (empty) removes it.
- `TZ_NAME`: sets the system time zone, for example `America/Los_Angeles`.
- `BACKUP_SCHEDULE`: a cron expression for the nightly backup (default `30 3 * * *`).

```sh
PORT=8486 NAV=Steam=http://steam-host:8484/ TZ_NAME=America/Los_Angeles \
  BACKUP_SCHEDULE="15 2 * * *" sh install/alpine.sh
```

Then open `http://<server-ip>:8486/`.

### Who is in the person picker

The picker is seeded from `persons.json`, a JSON list of `{name, color}` objects (the color is optional):

```json
[{"name": "Player 1", "color": "#4c78a8"}, {"name": "Player 2", "color": "#e45756"}]
```

Edit it, then restart the service:

```sh
vim /home/games/persons.json
rc-service player-select restart
```

The file is the source: on every start-up, names that are not in the database yet are added, and names in the file get the file's color and position (the picker follows the file's order). A person who is no longer in the file is removed only if they have no play state and no Up next entries, so replacing the installer's placeholder `Player 1` with your own names removes it. A person with play state is kept (after the file's names) and nothing of theirs is lost; the log says so, one line per person kept or removed (`persons: kept 'Player 2' ...`). Renaming someone in the file therefore adds a new person; the old name stays while it has play state. The server exits with an error if the file is unreadable or is not a list of objects like the above, and starts with the persons already in the database if the file does not exist. Names and colors are personal data, so the file is not in the repository (and `.gitignore` lists it).

## Running without the installer

```sh
python3 player_select_server.py --data ~/data --persons ~/persons.json
```

then open `http://localhost:8486/`. Options:

| Option | Default | Meaning |
|---|---|---|
| `--port` | `8486` | Port to listen on |
| `--bind` | `0.0.0.0` | Address to listen on. Use `127.0.0.1` behind a proxy on the same machine |
| `--data` | `~/data` | Directory for `player_select.db` and `imports/` (created if missing) |
| `--page` | `catalog.html` next to the server | The page file |
| `--nav LABEL=URL` | none | Adds a header link; repeat for several, e.g. `--nav "Steam=https://steam.example.com/"` |
| `--persons FILE` | none | Persons file, as above |

The schema is created and migrated when the server starts.

## systemd hosts

Copy the files in `contrib/systemd/` to `/etc/systemd/system/` and adjust the paths and user to match your install. Put the code in `/opt/player-select` and create a `games` user, or edit the units. Then run:

```sh
systemctl daemon-reload
systemctl enable --now player-select player-select-backup.timer
```

`player-select-backup.timer` runs `backup.sh` at 03:30 every day.

## Importing a GameEye backup

Open **Import** in the page header.

1. **Stage.** Choose the `.ged` backup, optionally the CSV, and upload. Nothing is saved to the catalog yet. The server reads the backup and compares it with what it already has.
2. **Review the diff.** Changes are grouped as *add*, *edit*, *remove* (in the catalog, absent from the backup) and *restore* (previously removed, back in the backup). Warnings are listed too, for example an unknown GameEye platform ID or an unexpected backup version. If a CSV is attached, the summary says how many copies it enriched, how many CSV rows matched nothing and how many copies were ambiguous (two identical copies added the same day get no metadata).
3. **Removal guard.** If the backup would remove more than 25% of the active GameEye copies, the page asks you to confirm. This catches a backup from the wrong device or an empty collection.
4. **Apply.** The changes are written in one transaction. Removed copies are kept, soft-deleted, so play state, notes and removal reasons survive; you can record a reason (sold, traded, lost, gifted, mistake) in **Items**.

**Retitles keep their state.** If you fix a title in GameEye (say a typo) and that copy was its game's only copy, the game is renamed in place: statuses, notes and Up next positions stay with it. If the new title matches a game you already have, the copy joins it and the two games' play state is merged as a manual merge would (the later status wins, notes are joined); if the merged note would be over 2,000 characters, apply is refused with `409 note_too_long` until you shorten one. A copy whose game has other copies moves to the game for its new title, and the old game keeps its state. Copies you split out by hand are never moved.

Importing the same backup again is a no-op: the response says everything is unchanged and nothing is staged. A staged run expires after an hour. If another import was applied after you staged, apply is refused and you stage again.

Uploads are limited to 32 MiB, and the database inside the backup to 256 MiB. Staged files are kept in `imports/` (mode 0600) only until applied or expired.

## Security

**There is no authentication.** The server has no login and does not know who is calling it.
- **Keep it private:** bind it to your LAN or a VPN (Tailscale, WireGuard), or put it behind an authenticating reverse proxy such as Cloudflare Tunnel + Access (for example `games.example.com`, under the same Access policy as `steam.example.com`).
- **Don't expose it directly to the internet.** Anyone who can reach it can change play state, merge or split games and import a different collection.
- **The person picker is not a login.** It only chooses whose play state you are editing. Its value is kept in your browser.
- **Your collection stays on the server.** Backups, exports and the database are not in the repository; `.gitignore` excludes them.

## API

Every response carries `X-PlayerSelect-Server: 1` and `X-PlayerSelect-Api: 1`. Responses are sent with `Cache-Control: no-store`.

| Method | Path | |
|---|---|---|
| GET | `/`, `/games`, `/items`, `/up-next`, `/duplicates`, `/import` | The page (a trailing slash is fine) |
| GET | `/healthz` | `ok` |
| GET | `/api/catalog` | The whole read model: persons, platforms, works, copies, meta, play state, Up next, duplicate groups, last import. Has an `ETag`; `304` for a matching `If-None-Match` |
| PUT | `/api/play-state` | `{"person": 3, "work": 31, "status": "finished", "note": "..."}`. `"status": ""` deletes the row. Returns the value it replaced as `previous` |
| PUT | `/api/up-next` | `{"person": 3, "order": [31, 52]}`. The list must be exactly that person's current Up next set (409 `order_mismatch` otherwise) |
| PUT | `/api/copy/<id>` | `{"removed_reason": "sold"}` (or `null` to clear). Only for removed copies (409 `not_removed` otherwise) |
| POST | `/api/work/merge` | `{"into": 31, "from": 87}` |
| POST | `/api/work/split` | `{"copy": 205, "title": "Jurassic Park (Sega CD)"}` |
| POST | `/api/dup-ack` | `{"work": 11, "platform": 2}`: mark a duplicate group as separate copies |
| POST | `/api/import/gameeye` | `multipart/form-data` with `ged` (required) and `csv`. Stages a dry run and returns `import_id`, `summary`, `removal_guard`, `warnings` and `changes`. `import_id` is `null` when the backup is identical to the last applied one |
| POST | `/api/import/<id>/apply` | `{"confirm_removals": false}`. Applies a staged run. 404 unknown, 410 expired, 409 stale or not staged, 428 removal guard without `confirm_removals: true` |

**Requests.** Every POST and PUT except the import upload needs `Content-Type: application/json` (415 otherwise) and a body of at most 64 KiB (413). A note is at most 2,000 characters.

**ETags.** `GET /api/catalog` sends an `ETag`. `PUT /api/play-state` may send `If-Match` with an ETag from an earlier GET; if the catalog has changed since, the server answers `412` with `precondition_failed` and writes nothing. A PUT without `If-Match` always applies. The page sends `If-Match` with the last catalog ETag on every play-state PUT and, on a `412`, reloads the catalog and retries once.

`If-Match` is compared with any `W/` prefix removed on both sides (`*` matches anything). This is a deliberate deviation from RFC 9110, which asks for strong comparison: a CDN such as Cloudflare (with "Respect Strong ETags" off, the default) turns the strong ETag into `W/"…"` for visitors that accept gzip or brotli, and the page echoes that value back, so strict comparison would refuse every save. After deploying behind a proxy, check what it sends:

```sh
curl -sI -H 'Accept-Encoding: gzip' https://games.example.com/api/catalog
```

A `W/"…"` ETag there is fine. A missing `ETag` header is not: without it the page cannot detect concurrent changes.

**Errors.** Every 4xx and 5xx body is JSON, including the ones the standard library produces:

```json
{"error": "not_removed", "detail": "copy is still active"}
```

`error` is a short code (`bad_request`, `not_found`, `precondition_failed`, `too_large`, `unsupported_media_type`, and named ones such as `order_mismatch`, `not_removed`, `not_a_duplicate`, `stale_base`, `not_staged`, `expired`, `confirm_removals`). `detail` is text for people.

## Data and backups

| Path | What |
|---|---|
| `/home/games/data/player_select.db` | The SQLite database (WAL mode, so `-wal` and `-shm` files sit beside it) |
| `/home/games/data/imports/` | Staged uploads, `<sha256>.ged` and `<sha256>.csv`, mode 0600 |
| `/home/games/data/backups/` | Nightly copies, `player_select-YYYY-MM-DD.db` |
| `/home/games/persons.json` | The person seed |
| `/home/games/.player-select-env` | Reserved for future [adapters](adapters/README.md); the server never reads it |
| `/etc/player-select.conf` | Remembered installer options |

The database holds things no import can recreate: play state, notes, merges and splits, removal reasons. Back it up.

`backup.sh` uses SQLite's online backup (`.backup`), which is safe while the server runs. It writes `backups/player_select-YYYY-MM-DD.db` (to a temporary name first, so a failed run never replaces that day's good copy) and deletes copies older than 14 days. Backups are mode 0600, and the server creates its own files owner-only too (umask 077). The installer schedules it nightly; to run it now:

```sh
su -s /bin/sh games -c '/opt/player-select/backup.sh /home/games/data'
```

Copy `backups/` to another machine as well. A backup in the same container does not protect against losing the container.

**Restore:**

```sh
rc-service player-select stop
cp /home/games/data/backups/player_select-2026-11-07.db /home/games/data/player_select.db
rm -f /home/games/data/player_select.db-wal /home/games/data/player_select.db-shm
chown games:games /home/games/data/player_select.db
rc-service player-select start
```

Remove the `-wal` and `-shm` files: left over from the old database, they would be applied to the restored one. On systemd, use `systemctl stop player-select` and `systemctl start player-select`.

## Development and tests

The product needs only the Python standard library. The tests use `unittest`; CI adds test-only tools (Playwright, shellcheck) that are never installed on a server.

```sh
# Unit tests: parsers, importer, catalog, server routes, backup, docs, privacy scanner
python3 -m unittest discover -s tests -p 'test_*.py'

# The page in a real browser
pip install -r tests/browser/requirements.txt && playwright install chromium
python3 -m unittest discover -v -s tests/browser -p 'test_*.py'

# The Alpine installer, stubbed (changes /etc and /opt: run it in a container)
docker run --rm -v "$PWD:/src:ro" python:3.12-alpine sh /src/tests/installer/check.sh

# shellcheck, py_compile, systemd-analyze verify
sh .github/scripts/lint.sh

# Privacy scan of tracked files (add --no-history to skip commit messages)
python3 .github/scripts/privacy_scan.py
```

- **Unit tests** discover every `tests/test_*.py`, so a new module needs no registration. The backup tests need the `sqlite3` tool. Test data is synthetic: `tests/_ged.py` builds a fake `.ged` backup in memory, and the people in tests are `Player A`, `Player B` and `Player C`.
- **Browser tests** drive Chromium against the real server. Set `BROWSER_EXECUTABLE` to use an installed Chrome instead of Playwright's own, and `REQUIRE_BROWSER=1` to make a missing Playwright an error instead of a skip. CI sets it.
- **Installer test** runs `install/alpine.sh` several times in `python:3.12-alpine`, with the system commands stubbed.
- **Docs test** (`tests/test_docs.py`) fails if a route in `player_select_server.ROUTES` is missing from the API table above.

CI runs on pushes to `main`, pull requests, weekly and on demand. The unit tests run on Python 3.11 to 3.14; no test may be skipped. Every third-party action is pinned to a full commit SHA, and Dependabot updates them weekly.

### Privacy scan

This repository is public, so a CI job (`.github/scripts/privacy_scan.py`) fails if a tracked file or a commit contains personal data: a real GameEye backup (`*.ged`) or database, a collection CSV, an IPv4 address that is not loopback, documentation or private, an email that is not a `noreply` or `example` address, or any term from a private denylist.

The denylist holds what no pattern can know (your hostnames, domain, family names, device names). It lives in a repository secret named `PRIVACY_DENYLIST`: one term per line, matched case-insensitively against file contents, file names and commit messages. Add it under **Settings**, **Secrets and variables**, **Actions**, **New repository secret**. Until it exists the job runs the pattern checks only and shows a warning. To check locally:

```sh
PRIVACY_DENYLIST="$(cat my-terms.txt)" python3 .github/scripts/privacy_scan.py
```

A line can opt out with the text `privacy-scan: allow` (say why beside it).

## Not in v1

- Any digital importer. Only the [adapter contract](adapters/README.md) exists.
- Triage scoring or a "what next" ranking.
- IGDB metadata and cover art, and cross-referencing Steam data.
- Home Assistant hooks and an MCP server.
- Platform compatibility ("which console can play this").
- Login or identity inside the app.

## License

MIT
