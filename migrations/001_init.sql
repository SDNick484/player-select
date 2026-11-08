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
