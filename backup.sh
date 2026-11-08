#!/bin/sh
# backup.sh - nightly online backup of the Player Select database.
#
#   backup.sh [DATA_DIR]      (default: /home/games/data)
#
# Uses SQLite's online backup API (`.backup`), which is safe while the server is running in WAL mode, writes
# backups/player_select-YYYY-MM-DD.db and deletes copies older than 14 days. Exits 78 when there is no database.
# The copy is written to a temporary name and moved into place, so a failed run never replaces a good backup;
# backups are private (umask 077: files 0600, the directory 0700).
set -eu
umask 077

DATA="${1:-/home/games/data}"
DB="$DATA/player_select.db"
OUT_DIR="$DATA/backups"

if [ ! -f "$DB" ]; then
  echo "$(date '+%F %T') backup: no database at $DB" >&2
  exit 78
fi

mkdir -p "$OUT_DIR"
chmod 0700 "$OUT_DIR"
OUT="$OUT_DIR/player_select-$(date +%F).db"
TMP="$OUT.tmp"
trap 'rm -f "$TMP"' EXIT
rm -f "$TMP"
sqlite3 "$DB" ".backup '$TMP'"
mv -f "$TMP" "$OUT"
find "$OUT_DIR" -name 'player_select-*.db' -mtime +13 -delete
echo "$(date '+%F %T') backup: wrote $OUT"
