# Player Select v1.1: cover art

- **Date:** 2026-11-08
- **Status:** draft for owner review; amended 2026-11-08 (MobyGames one-month bulk backfill: A-D1, A-D8,
  A-D9–A-D11, A3, A4.5, A5, A7, A8, A10)
- **Builds on:** [`2026-11-07-player-select-v1-design.md`](2026-11-07-player-select-v1-design.md) (referred to as
  **v1**; § numbers below are v1's unless prefixed with **A**), at `main` `b9dde75`
- **Prior art in the family:** [`SDNick484/matinee`](https://github.com/SDNick484/matinee) at `f7904df`: TMDB
  posters cached in `$DATA_DIR/images/`, served by the app with a 30-day max-age, and an editor-uploaded cover
  (`custom_poster_file`) that wins over the fetched one and survives a refresh. This spec follows that shape.

## A1. Purpose

Give every game in the catalog a cover, as Matinee does for films and Cookbook Collection for books.

### v1.1 delivers

1. A front cover per **work**, fetched from IGDB, with an optional cover per **(work, platform)** for when the
   PS4 box and the Switch box differ (*Witcher III* vs *[Complete Edition]*, *Ocarina* N64 vs Switch 2 *[Foil
   Cover Art]*).
2. A nightly matching job, plus a **Find covers now** button, that auto-accepts only unambiguous IGDB matches
   and queues the rest for review.
3. A **Covers** view to clear the review queue, and a cover picker in each Games row detail: IGDB candidates,
   IGDB search, upload a photo of your own copy, revert.
4. Covers as a thumbnail column in **Games**, and a **Shelf** layout (a cover grid, Matinee-style) as a
   toggle on the same view.
5. `work.igdb_id` filled for matched works, which v1 reserved for exactly this (§5.1, §13).
6. A one-time **MobyGames backfill** (A4.5), run during a single paid month, that fills per-platform covers
   from real box scans (region-specific front covers) before IGDB fills the gaps.

### Not in v1.1

- Back covers, hero / banner / logo art, screenshots (the schema has a `kind` column so they can be added).
- Art for systems, controllers, accessories, print media and toys (non-game copies have no work).
- Other IGDB metadata (genres, summaries, release dates): a later spec owns IGDB metadata precedence (§13).
- TheGamesDB, libretro-thumbnails, SteamGridDB: candidate second sources, not built now (A9).
- MobyGames as an ongoing source: no nightly Moby job, no Moby search in the picker, no Moby key on the
  server. Once the paid month ends, Moby is never called again (A-D9).

## A2. Decisions

| # | Decision | Reason |
|---|---|---|
| A-D1 | **IGDB is the only ongoing source; MobyGames is a one-time backfill** (amended 2026-11-08) | IGDB is free, official, broad, and returns the `igdb_id` §13 wants, so it owns the nightly job and the picker. MobyGames has per-platform, per-region front-cover scans that IGDB lacks, but costs money (Hobbyist API, $9.99 for one month, non-commercial, 0.2 requests/s), so it runs once over the existing collection and stops. The collection grows slowly, so new games after the backfill are left to IGDB and uploads. |
| A-D2 | **Art is stored on our disk, never hotlinked** | Works when IGDB is down, no third-party requests from the page behind Cloudflare Access, rate limits only hit the job. Same as Matinee. |
| A-D3 | **Scope: work, with an optional per-platform override** | IGDB has one cover per game, so fetched art lands at work scope. Per-platform art comes from the Moby backfill (A4.5), picking a different IGDB entry (e.g. the *Complete Edition* entry) or an upload. |
| A-D4 | **Auto-accept only exact matches** | A wrong cover is worse than a placeholder. Everything else goes to the Covers view with up to 5 candidates. |
| A-D5 | **Uploads are resized in the browser** (canvas), the server stays stdlib | Python's stdlib cannot resize images. The page uploads a 128 px and a 600 px JPEG; the server only checks magic bytes and size. |
| A-D6 | **The server reads IGDB app credentials** from `/home/games/.player-select-igdb` (0600) | **Departs from v1 §4.2** ("`.player-select-env` is never read by the server"). The picker's search and the "use this one" download run in the request. These are app credentials for an official API, not a household account token; `.player-select-env` stays server-blind for adapter tokens. |
| A-D7 | **"Find covers now" starts the job as a separate process** (`run-art.sh`, `flock`), not a thread | Keeps v1's no-background-thread rule (§6.4) and gives the button and cron one code path and one lock. |
| A-D8 | **Uploaded and MobyGames covers are backed up nightly**, IGDB ones are not (amended 2026-11-08) | Uploads are hand-made like play state (D9). Moby files cannot be re-fetched once the subscription ends. IGDB files can. |
| A-D9 | **The Moby backfill is a CLI, never the server** (`python3 -m ps.moby`) | The key is used for one month and then deleted, so it should never become a server dependency. Keeps A-D6 unchanged: the server reads IGDB credentials only. |
| A-D10 | **Moby before IGDB** | Moby scans are the box you own (platform and region); IGDB's one cover per game is the fallback. The IGDB job skips works the backfill fully covered (A4.3). |
| A-D11 | **The backfill downloads review candidates up front** | Ambiguous matches are resolved in the Covers view after the month may have ended, so accepting one must be a local operation with no Moby call. |

## A3. Data model

### A3.1 `migrations/002_art.sql`

```sql
CREATE TABLE art (
  id INTEGER PRIMARY KEY,
  work_id INTEGER NOT NULL REFERENCES work(id),
  platform_id INTEGER REFERENCES platform(id),          -- NULL = the whole work
  kind TEXT NOT NULL DEFAULT 'cover' CHECK (kind IN ('cover')),
  source TEXT NOT NULL CHECK (source IN ('igdb','moby','upload')),
  igdb_game_id INTEGER,                                  -- source = 'igdb' only
  igdb_image_id TEXT,
  moby_game_id INTEGER,                                  -- source = 'moby' only
  moby_image TEXT,                                       -- the scan's image URL, for provenance
  file_small TEXT NOT NULL,                              -- names under data/art/
  file_big TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  CHECK ((source = 'igdb') = (igdb_image_id IS NOT NULL)),
  CHECK ((source = 'moby') = (moby_image IS NOT NULL)));
CREATE UNIQUE INDEX art_scope ON art(work_id, COALESCE(platform_id, 0), kind);

CREATE TABLE art_match (
  work_id INTEGER PRIMARY KEY REFERENCES work(id),
  state TEXT NOT NULL CHECK (state IN ('matched','review','none','dismissed','failed')),
  searched_key TEXT NOT NULL,          -- the work_key searched; a retitle to another key re-queues
  candidates_json TEXT,                -- state 'review': up to 5 candidates (A4.3)
  note TEXT,                           -- e.g. 'igdb_id 1942 already on work 87'
  attempts INTEGER NOT NULL DEFAULT 0,
  checked_at INTEGER NOT NULL);

CREATE TABLE moby_match (                 -- the one-time backfill's bookkeeping (A4.5)
  work_id INTEGER NOT NULL REFERENCES work(id),
  platform_id INTEGER NOT NULL REFERENCES platform(id),
  state TEXT NOT NULL CHECK (state IN ('matched','review','none','dismissed','failed','no_platform')),
  moby_game_id INTEGER,                   -- state 'matched'
  candidates_json TEXT,                   -- state 'review': up to 3, files already downloaded (A-D11)
  note TEXT,
  checked_at INTEGER NOT NULL,
  PRIMARY KEY (work_id, platform_id));
```

`art` holds only the **chosen** cover per scope; changing a cover replaces the row. `art_match` is the job's
bookkeeping, one row per work it has looked at. `moby_match` is per (work, platform) because Moby covers are
per platform; it outlives the subscription only to drive the review queue.

| `art_match.state` | Meaning | Job looks again? |
|---|---|---|
| (no row) | never searched | yes |
| `matched` | a cover was accepted (auto or by hand) | no |
| `review` | candidates waiting in the Covers view | no |
| `none` | IGDB returned nothing usable | after 30 days |
| `dismissed` | owner chose "None of these" or reverted an IGDB cover | no, until the title changes |
| `failed` | network / HTTP error | next run, backing off: skip while `checked_at + 3600·2^attempts > now` (cap 7 days) |

A work whose current `work_key` differs from `searched_key` (a retitle, §5.4) is searched again whatever its
state, unless it has an `upload` cover.

### A3.2 Effective cover

For a copy on platform *P* of work *W*: `art(W, P)` if present, else `art(W, NULL)`, else a placeholder
tile (title on the platform family's color, as the chips use). In **Games**, a row shows the platform-level
cover of the first chip that has one (chip order, §7.1), else the work-level cover (amended 2026-11-08: the
box you own beats the generic IGDB cover, and Moby art is mostly platform-level).

### A3.3 Files

- Directory `data/art/`, mode 0700, files 0600, owned by `games`.
- Names are content-addressed: `<sha256[:20]>.jpg` (or `.png` / `.webp` for uploads, by magic bytes). The
  same image twice is one file; a changed cover is a new URL, so files are served immutable.
- Uploads are prefixed `u-` (`u-3f9a1c…e2.jpg`) and Moby scans `m-`, so `backup.sh` can find both (A7.3).
  Moby files are stored as served (no resize, A-D5): `small` is Moby's thumbnail, `big` its large image
  (≈800 px wide); extension by magic bytes.
- Candidate thumbnails fetched for the picker live in `data/art/cache/` and are deleted after 30 days unused.
- Moby review candidates live in `data/art/moby-review/` as `m-` files and are not swept by the 30-day rule;
  a candidate's files are deleted when its `moby_match` row leaves `review` (accepted files move to
  `data/art/`).
- Garbage collection: after any `art` change, a file no `art` row references is deleted in the same request
  (after commit). The job also sweeps orphans at start.

Size estimate for the 2026-11-07 data: 455 works × (≈6 KB small + ≈60 KB big) ≈ 30 MB for IGDB; Moby
scans are larger (≈150 KB big), so a full backfill adds roughly 80 MB, all of it in the nightly backup.

### A3.4 Changes to v1 operations (§5.4)

- **Merge** (`into`, `from`), same transaction: `from`'s work-level art is dropped if `into` has one, else
  moved; `from`'s platform-level art moves unless `into` already has art for that platform (then dropped).
  `work.igdb_id`: `into` keeps its own; if it has none, it takes `from`'s (`from`'s is set NULL first, since
  `igdb_id` is UNIQUE). `from`'s `art_match` row is deleted. `from`'s `moby_match` rows move unless `into`
  has a row for that platform (then deleted, with any review files). Dropped files are collected after commit.
- **Split**: the new work starts with no art and no `art_match` row, so the next run searches it. The copy's
  old work keeps its art. If the split copy's platform has Moby art on the old work and no other active copy
  of the old work is on that platform, that `art` row and its `moby_match` row move to the new work (the
  scan is of that copy's box).
- **Retitle on import**: the work keeps its art and `art_match`; the key change re-queues it (A3.1).

## A4. Matching

### A4.1 IGDB access (`ps/igdb.py`)

- Credentials file: two lines, `IGDB_CLIENT_ID=…` and `IGDB_CLIENT_SECRET=…`, from a Twitch developer
  application. Absent file → art features report `igdb_unconfigured`; the rest of the app is unaffected.
- Token: `POST https://id.twitch.tv/oauth2/token` with `client_id`, `client_secret`,
  `grant_type=client_credentials`. Cached in `data/.igdb-token` (0600) with its expiry; refreshed when within a
  day of expiry or on a 401 (once).
- Requests: `POST https://api.igdb.com/v4/games`, headers `Client-ID` and `Authorization: Bearer <token>`,
  body in Apicalypse. Paced to ≤ 4 requests/second process-wide; a 429 waits 1 s and retries once.
  Timeout 10 s.
- Images: `https://images.igdb.com/igdb/image/upload/<size>/<image_id>.jpg`. `small` = `t_cover_small_2x`,
  `big` = `t_cover_big_2x` (sizes to confirm in plan task 1, A10).
- The module takes an injectable `fetch(method, url, headers, body) -> (status, headers, bytes)` so tests use
  canned responses and never touch the network. Stdlib `urllib.request` in production.

### A4.2 Platform map: `data_static/igdb_platforms.json`

GameEye platform id → list of IGDB platform ids. Media platforms (120 Disney Infinity, 123 Strategy Guide,
168 Art Books) map to `[]` and their works are never searched. Some GameEye platforms cover two IGDB ones
(SNES / Super Famicom, NES / Famicom, Genesis / Mega Drive). The table is built once by hand from
`GET /v4/platforms` in plan task 1 and committed (public data); a unit test checks every id in
`gameeye_id_maps.json` has an entry, so a new GameEye platform fails CI rather than silently never matching.

### A4.3 The job (`ps/art.py`, `python3 -m ps.art run`)

For each work with at least one active game copy, no `art(W, NULL)`, and at least one active copy's platform
without `art(W, P)` (amended 2026-11-08: a work the Moby backfill covered on every platform is skipped), in the
order: never searched, then retitled, then `none` past 30 days, then `failed` past back-off:

1. `ids` = union of IGDB platform ids of the work's active copies' platforms. Empty → skip.
2. Query:
   ```
   search "<work.title>";
   fields name, cover.image_id, platforms, first_release_date, alternative_names.name, version_parent;
   where platforms = (<ids>) & cover != null;
   limit 10;
   ```
3. A candidate is **exact** when `titles.normalize(name)` or the normalized form of any
   `alternative_names.name` equals the work's key (§5.4), ignoring edition brackets.
4. **Auto-accept** when exactly one candidate is exact, it shares at least one platform with `ids`, and no
   other work already has its `igdb_id`. Then download both sizes, write `art(W, NULL, source='igdb')`, set
   `work.igdb_id`, state `matched`.
5. Otherwise, if there are any candidates: state `review`, `candidates_json` = the top 5: exact candidates first, then
   those sharing a platform with `ids`, otherwise in IGDB's search order. If the only blocker was an `igdb_id` already on another work, `note` says which work:
   usually two works that should be merged. No candidates → state `none`.

Candidate JSON:

```json
[{"igdb_id": 1942, "name": "The Witcher 3: Wild Hunt", "year": 2015,
  "platforms": [6, 48, 49, 130, 167, 169], "image_id": "co1wyy", "exact": true}]
```

Writes happen one work at a time through `Store.write()` (each a short transaction), so the server stays
responsive while the job runs. Downloads happen outside the transaction.

Run summary on stdout, one line, for the cron log:
`art: 455 works, 12 searched, 9 matched, 2 review, 1 none, 0 failed, 18 files, 1.2 MB`.

### A4.4 Expected yield (to be measured, not assumed)

Plan task 1 runs the matcher read-only against the real 455 works on the dev box (results stay local) and
reports the matched / review / none split. If auto-accept is below ~60%, the owner decides whether to widen
rule A4.3.3 (for example, accept a sole candidate whose name differs only by a subtitle) before task 4.

### A4.5 MobyGames backfill (`ps/moby.py`, `python3 -m ps.moby`) (added 2026-11-08)

A one-time CLI, run on the LXC as `games` during the paid month. It never runs from cron and the server
never imports it.

**Access.**

- Key file `/home/games/.player-select-moby` (0600): one line, `MOBY_API_KEY=…`. Passed as
  `--credentials`; deleted by the owner when the month ends (A7.4).
- API v1: `GET https://api.mobygames.com/v1/…?api_key=…` (key URL-encoded). The key never appears in logs,
  exceptions or the stdout summary; URLs are logged with `api_key=<redacted>`.
- Pacing: one API call every **5 s** (Hobbyist 0.2 requests/s), process-wide; a 429 waits 10 s and retries
  once. Image downloads (the cover URLs the API returns) are paced at one per second, separately.
- Same injectable `fetch` as `ps/igdb.py` (A4.1), so tests use canned responses.
- Holds `data/.art.lock` (`flock -n`) for the whole run, so it never overlaps the IGDB job or the
  **Find covers now** button; exits with a message if the lock is held.

**Platform map.** `data_static/moby_platforms.json`, GameEye platform id → list of Moby platform ids, built
from `GET /v1/platforms` on day 1 of the subscription and committed (public data). Media platforms map to
`[]` (state `no_platform`). The same completeness test as A4.2.

**Per work** (works with at least one active game copy, skipping any (work, platform) that already has a
`moby_match` row, so a run can be stopped and resumed):

1. `GET /v1/games?title=<title>&platform=<id>…&format=normal` with every Moby platform id of the work's
   active copies (one call). `title` is a substring filter, not a ranked search, so the query uses
   `work.title` with any `[...]` removed and a leading "The " dropped.
2. A result is **exact** when `titles.normalize()` of its `title` or of any `alternate_titles[].title`
   equals the work key, and it lists at least one of the work's Moby platforms.
3. Exactly one exact result → for each of the work's platforms *P* it was released on:
   `GET /v1/games/<id>/platforms/<moby P>/covers`, then pick the cover (below) and download its
   `thumbnail_image` and `image`. Write `art(W, P, source='moby')`, state `matched`. If the work has exactly
   one platform, the art is still written at platform scope (A3.2 shows it in Games).
   A scope that already has an `upload` cover is never overwritten; its row is `matched` with
   `note = 'upload kept'` and no download.
4. Several exact results, or none exact but some results → state `review` for each platform, with up to 3
   candidates (exact first, then by Moby's order); the CLI fetches each candidate's cover and downloads it
   into `data/art/moby-review/` (A-D11). That is up to 3 extra covers calls per platform, so `--review-cap N`
   (default 60) stops downloading candidates once N (work, platform) pairs are in review; the rest get
   `note = 'review cap'` and their candidates are names only.
5. No results → `none`. Network error → `failed` (a later run retries it).

**Picking the cover.** From the platform's `cover_groups`: covers with `scan_of = "Front Cover"`; prefer the
group whose `countries` contains `--country` (default `United States`), then a group with
`Worldwide`, then the first group. No front cover in any group → state `none` with `note = 'no front cover'`.

**Modes.**

- `--dry-run`: searches only (no covers calls, no downloads, no writes) and prints the matched / review /
  none split. Run this first on day 1; it is the Moby equivalent of A4.4 and costs about one call per work.
- `--limit N`: process at most N works (for a first small real run).
- Summary line: `moby: 455 works, 470 pairs, 402 matched, 41 review, 22 none, 5 failed, 0 no_platform,
  1210 calls, 81 MB`.

**Budget for the 2026-11-07 data.** ≈455 search calls + ≈470 covers calls + up to 180 review covers calls ≈
1,100 calls ≈ 1.5 h at 5 s each, plus ≈1,000 image downloads ≈ 17 min. Comfortably one evening, which
leaves the rest of the month for reruns after merges or retitles.

**After the month.** `moby_match` rows in `review` stay resolvable in the Covers view from the downloaded
files. Rows with `note = 'review cap'`, `none` and `failed` fall through to IGDB, because A4.3 only skips
works covered on every platform.

## A5. API

New routes, same headers and error shape as §7.2. Writes require `Content-Type: application/json` except the
upload.

| Method | Path | Body / result |
|---|---|---|
| GET | `/art/<name>` | a file from `data/art/`; `name` must match `^([um]-)?[0-9a-f]{20}\.(jpg|png|webp|gif)$`; `Cache-Control: public, max-age=31536000, immutable` |
| GET | `/art/moby-review/<name>` | a Moby review candidate from `data/art/moby-review/`; `name` must match `^m-[0-9a-f]{20}\.(jpg|png|webp|gif)$`; max-age 1 day |
| GET | `/art/igdb/<size>/<image_id>.jpg` | candidate thumbnail: served from `data/art/cache/`, fetched from IGDB on a miss; `size` ∈ {`small`}, `image_id` must match `^[a-z0-9]{1,32}$`; max-age 1 day |
| GET | `/api/art/search?work=31&q=witcher+3&platform=97` | live IGDB search → `{"candidates": [...]}` (A4.3 shape); `q` defaults to the work's title, `platform` narrows `ids`; 503 `igdb_unconfigured`, 502 `igdb_error` |
| PUT | `/api/art` | `{"work": 31, "platform": null, "igdb_id": 1942, "image_id": "co1wyy"}` → downloads, replaces the cover for that scope; work scope also sets `work.igdb_id` (409 `igdb_id_in_use` naming the other work) and state `matched` |
| POST | `/api/art/upload` | `multipart/form-data`: `work`, optional `platform`, `small`, `big`; each image ≤ 2 MB, JPEG/PNG/WebP by magic bytes (400 `not_an_image`, 413) |
| PUT | `/api/art/moby` | `{"work": 31, "platform": 97, "moby_game_id": 2245}` → accepts a stored Moby review candidate: moves its files from `moby-review/` to `data/art/`, writes `art(W, P, source='moby')`, state `matched`, deletes the other candidates' files. No network. 404 when the pair is not in `review` or the candidate is not in its list |
| POST | `/api/art/moby/dismiss` | `{"work": 31, "platform": 97}` → `moby_match` state `dismissed`, candidate files deleted; the IGDB job may then cover the work |
| DELETE | `/api/art?work=31&platform=97` | removes that scope's cover; removing a work-level IGDB cover sets state `dismissed` and leaves `work.igdb_id`; removing a Moby cover sets that pair's `moby_match` to `dismissed` (the file is gone for good, A-D8) |
| POST | `/api/art/dismiss` | `{"work": 31}` → state `dismissed` ("None of these") |
| POST | `/api/art/run` | starts `run-art.sh` detached; 202 `{"started": true}`, or 200 `{"started": false, "reason": "running"}` when the lock is held |

`GET /api/catalog` adds:

```json
"art": [{"work": 31, "platform": null, "small": "/art/4be1…9c.jpg", "big": "/art/77d0…01.jpg",
         "source": "igdb"}],
"art_match": {"review": [{"work": 87, "candidates": [...], "note": null}],
              "counts": {"matched": 401, "review": 23, "none": 19, "dismissed": 4, "failed": 0,
                         "unsearched": 8},
              "running": false, "last_run": 1762665300,
              "configured": true},
"moby_match": {"review": [{"work": 87, "platform": 2, "note": null,
                           "candidates": [{"moby_game_id": 2245, "title": "…", "year": 2002,
                                           "small": "/art/moby-review/m-…jpg"}]}],
               "counts": {"matched": 402, "review": 41, "none": 22, "dismissed": 0, "failed": 5,
                          "no_platform": 0}}
```

`moby_match` is omitted when the table is empty (no backfill has run).

`running` is true while `data/.art.lock` is held (checked with a non-blocking `flock` on open).

## A6. UI

- **Games, table layout:** a 40 × 56 cover column left of the title (`loading="lazy"`), placeholder tile when
  none. Row detail shows the big cover with **Change cover…**.
- **Games, shelf layout:** toggle `Table · Shelf` stored in the URL like the filters; a responsive grid of
  covers (≥ 3 columns at 412 px, as Matinee's rule) with title, platform chips and the selected person's
  status under each. Same filters, same row detail opened as a panel.
- **Change cover…** dialog:
  - scope: **All platforms** or one of the work's platforms (radio);
  - the stored candidates, then **Search IGDB** (prefilled with the title);
  - **Upload a photo** (camera-friendly `<input type=file accept="image/*" capture>`); the page draws it on
    a canvas, center-crops to 3:4 if the owner ticks **Crop to box shape**, and uploads 128 px and 600 px
    JPEGs at quality 0.85;
  - **Remove** for an existing cover.
- **Covers** (new header tab, after Duplicates): the review queue, one row per work with its candidates as
  thumbnails; click one to accept (work scope), **None of these** to dismiss, **Search…** for the dialog.
  The tab label carries the queue count. A status line shows the counts, last run time and **Find covers
  now** (disabled while running; the page re-reads the catalog every 5 s until `running` is false).
- Moby review items appear in the same queue, labelled **MobyGames · <platform chip>**, one row per
  (work, platform); clicking a candidate accepts it at that platform's scope, **None of these** dismisses the
  pair. Items with `note = 'review cap'` show candidate names only and a **Search IGDB…** button. The queue
  count includes both.
- When IGDB is not configured the Covers tab says how to configure it (A7.1) and uploads still work.
- Footer: "Cover art from IGDB.com" linking to igdb.com, shown when any IGDB cover is displayed, and
  "Cover scans from MobyGames" linking to mobygames.com when any Moby cover is displayed.

## A7. Deployment

### A7.1 Credentials

Owner creates a Twitch developer application (dev.twitch.tv/console/apps, category "Application Integration",
OAuth redirect `http://localhost`), then on the LXC:

```sh
vim /home/games/.player-select-igdb      # IGDB_CLIENT_ID=… / IGDB_CLIENT_SECRET=…
chown games:games /home/games/.player-select-igdb && chmod 0600 /home/games/.player-select-igdb
```

The server is passed `--igdb-credentials /home/games/.player-select-igdb` by the OpenRC service (installer
default; the file may be absent).

### A7.2 Job and cron

- `run-art.sh [DATA_DIR]`: `flock -n data/.art.lock python3 -m ps.art run …`, exit 0 if the lock is held;
  installed to `/opt/player-select/`.
- Installer option `ART_SCHEDULE` (default `15 4 * * *`, after the 03:30 backup) adds a third line to the
  existing `# BEGIN/END player-select` cron block; remembered in `/etc/player-select.conf`. Logs to
  `/home/games/logs/art.log`.
- `contrib/systemd/`: `player-select-art.service` + `.timer`.
- `data/art/` created by the installer (0700).

### A7.3 Backup

`backup.sh` additionally copies `data/art/u-*`, `data/art/m-*` and `data/art/moby-review/m-*` into
`backups/art/` (`cp -n`; names are content hashes, so no duplicates) and deletes `backups/art/` files no longer
present in either source directory and older than 14 days.

### A7.4 The Moby month (added 2026-11-08)

Subscribe only once `ps/moby.py` and its fixture tests are merged, so the paid month is spent running it.

| Day | Step |
|---|---|
| before | Merge the backfill; deploy; check `data/art/` and backups work with an upload |
| 1 | Subscribe to Hobbyist (monthly). Read the API terms (A10.4). Create `/home/games/.player-select-moby` (0600). Build and commit `moby_platforms.json` from `/v1/platforms`. Run `--dry-run` and decide on the split |
| 1–2 | `python3 -m ps.moby run --limit 20`, look at the covers in Games, then the full run |
| 2–25 | Clear the review queue; after any merges or retitles, rerun (it only touches pairs with no row) |
| 26 | Last rerun; check `backups/art/` holds the `m-` files |
| 27 | Cancel the subscription; `rm /home/games/.player-select-moby` |

```sh
su -s /bin/sh games -c 'cd /opt/player-select && python3 -m ps.moby run \
  --data /home/games/data --credentials /home/games/.player-select-moby --dry-run'
```

## A8. Testing

Same rules as v1 §10 (TDD, `unittest`, no skips, Playwright in CI only).

- **Fixtures:** hand-written IGDB JSON responses with invented games and ids; images are tiny synthetic
  JPEG / PNG / WebP byte strings built in `tests/_art.py`. No real IGDB response or image is committed.
- **`ps/igdb.py`:** token fetch, cache and refresh on 401; pacing (fake clock); 429 retry once; timeouts map
  to `igdb_error`.
- **Matching:** exact title; exact alternative name; edition brackets ignored; no platform overlap → review;
  two exact candidates → review; `igdb_id` already on another work → review with note; no candidates →
  `none`; media platforms skipped; retitle re-queues; `dismissed` never re-searched; back-off on `failed`.
- **Storage:** content-addressed names; same image twice is one file; GC deletes only unreferenced files;
  orphan sweep.
- **Merge / split / retitle** (A3.4), including the `igdb_id` UNIQUE swap on merge.
- **Server:** every A5 route; name regexes reject `../`, uppercase, other extensions (404); upload magic
  bytes, size limits; `igdb_unconfigured` path; `/api/art/run` lock behaviour; catalog ETag changes when art
  changes.
- **Playwright:** placeholder tiles render; table ↔ shelf toggle round-trips through the URL; accepting a
  candidate in Covers shows the cover in Games; upload from a fixture file shows the new cover.
- **Installer:** `ART_SCHEDULE` remembered, one cron line, `data/art` mode 0700.
- **Platform map:** every GameEye platform id has an IGDB entry and a Moby entry.
- **Moby backfill** (canned v1 responses with invented games, ids and URLs): exact title; exact alternate
  title; result not on the work's platform is not exact; one exact → platform-scope art per platform; region
  preference (`--country`, then Worldwide, then first group); no front cover → `none`; upload scope kept;
  several exact → review with files in `moby-review/`; review cap; resume skips pairs with a row; dry run
  makes no covers calls and no writes; pacing at 5 s and 1 s (fake clock); 429 retry once; the key never
  appears in stdout, logs or exception text; lock held → exits without calls.
- **IGDB job vs Moby:** a work covered on every platform is skipped; a work missing one platform is searched.
- **Server:** `PUT /api/art/moby` moves files and makes no network call (the fake `fetch` fails the test if
  called); dismiss deletes candidate files; `m-` names served; Games row prefers platform-level art (A3.2).
- **Merge / split** move `moby_match` rows and Moby art as A3.4 says.
- **Backup:** `m-` files copied and pruned like `u-` files.

## A9. Later sources (not built)

| Source | Would add | Why not now |
|---|---|---|
| TheGamesDB (free API key, monthly allowance) | per-platform front and back box art | second matcher and quota; revisit if IGDB per-platform gaps hurt |
| libretro-thumbnails (git, No-Intro names, ≤ 512 px PNG) | retro cartridge box art with no key | multi-GB clone; name matching against No-Intro conventions |
| SteamGridDB (API key) | hero, logo, stylized grid art | community art, not box scans; fits a later "hero" kind |

The `art.source` CHECK and A4 matcher are the only places a new source touches. MobyGames, once a candidate
here, is now the one-time backfill (A4.5).

**Digital store images** (added 2026-11-08): PSN and Xbox responses usually carry their own image URLs, mostly
square tiles rather than box fronts. v1 §9 question 7 records what each returns; a later `art.source` value can
use them for digital-only works.

## A10. Open items to settle in the plan

1. Confirm against api-docs.igdb.com: image size names and pixel sizes; whether `alternative_names` and
   `version_parent` are still the field names; any multiquery batching limit; current terms on attribution
   and on storing images (the docs site refused automated reads on 2026-11-08, so these are unverified).
2. Measure A4.4 yield before building the review UI.
3. Whether Cookbook Collection's cover handling has anything worth sharing (not reviewed for this spec).
4. MobyGames, on day 1 of the subscription (A7.4): that the Hobbyist terms allow keeping downloaded cover
   images in a private personal catalog after the subscription ends (if not, stop: IGDB covers everything and
   no Moby code path is reached); the actual rate limit for the key; whether image downloads count against
   it; that `/v1/games`, `alternate_titles` and `/covers` still match the public docs (read 2026-11-08, which
   still describe v1 with free-tier limits, so the Hobbyist numbers come from the subscription page).
5. Moby's rate limit and price (Hobbyist $9.99 monthly, 0.2 requests/s, non-commercial) are as listed on
   mobygames.com/mobyplus on 2026-11-08.
