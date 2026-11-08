#!/bin/sh
# install/alpine.sh - install Player Select on Alpine Linux (OpenRC + BusyBox crond).
#
# Run as root from a checkout of this repository:
#   git clone https://github.com/SDNick484/player-select.git
#   cd player-select && sh install/alpine.sh
#
# Optional settings, passed as environment variables:
#   PORT             catalog port (default 8486)
#   BACKUP_SCHEDULE  cron schedule for the nightly database backup (default "30 3 * * *": 03:30 daily)
#   NAV              header link to another page, LABEL=URL with no spaces,
#                    e.g. NAV=Steam=http://steam-host:8484/
#   TZ_NAME          set the system time zone, e.g. America/New_York (default: leave as is)
#
# Safe to re-run: code is updated, your data and persons.json are kept, and the options above are remembered
# (in /etc/player-select.conf). Set NAV= (empty) to remove a link.
set -eu

SRC=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/player-select
APP_USER=games
HOME_DIR=/home/$APP_USER
DATA=$HOME_DIR/data
PERSONS=$HOME_DIR/persons.json
# Options from a previous install are reused unless given again on the command line.
SAVED=/etc/player-select.conf
SAVED_PORT=""; SAVED_NAV=""; SAVED_TZ_NAME=""; SAVED_BACKUP_SCHEDULE=""
[ -f "$SAVED" ] && . "$SAVED"
PORT="${PORT:-${SAVED_PORT:-8486}}"
BACKUP_SCHEDULE="${BACKUP_SCHEDULE:-${SAVED_BACKUP_SCHEDULE:-30 3 * * *}}"
NAV="${NAV-${SAVED_NAV:-}}"
TZ_NAME="${TZ_NAME:-${SAVED_TZ_NAME:-}}"

say() { printf '\n==> %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -f /etc/alpine-release ] || { echo "This installer is for Alpine Linux. See the README for other systems."; exit 1; }
case "$NAV" in *[[:space:]\'\"]*) echo "NAV must not contain spaces or quotes."; exit 1;; esac
bad_tz() { echo "TZ_NAME=$TZ_NAME: no such time zone (no /usr/share/zoneinfo/$TZ_NAME). $1"; exit 1; }
# Checked before anything changes. Without tzdata yet (a minimal image) it is checked again once apk has installed
# it, still before /etc/localtime is touched.
if [ -n "$TZ_NAME" ] && [ -d /usr/share/zoneinfo ] && [ ! -f "/usr/share/zoneinfo/$TZ_NAME" ]; then
  bad_tz "Nothing was changed."
fi

say "Installing packages"
apk add --no-cache python3 tzdata ca-certificates sqlite >/dev/null </dev/null

if [ -n "$TZ_NAME" ]; then
  [ -f "/usr/share/zoneinfo/$TZ_NAME" ] || bad_tz "Only the packages were installed."
  say "Setting time zone to $TZ_NAME"
  rm -f /etc/localtime   # it may be a symlink into zoneinfo: cp would write through it
  cp "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
  echo "$TZ_NAME" > /etc/timezone
fi

say "Creating the unprivileged '$APP_USER' user"
# Stock Alpine already has a 'games' system account (home /usr/games, shell nologin). It is reused as it is,
# never modified, so nothing here relies on its home directory or login shell: every path is explicit.
if id "$APP_USER" >/dev/null 2>&1; then
  echo "Existing '$APP_USER' account reused as is (not modified)."
else
  adduser -D -h "$HOME_DIR" -s /bin/sh "$APP_USER"
fi
mkdir -p "$HOME_DIR"
mkdir -p "$DATA/imports" "$DATA/backups" "$HOME_DIR/logs"
chmod 0700 "$DATA/imports"

say "Installing to $PREFIX"
# Replace the code directories wholesale so files removed from the repository do not linger.
rm -rf "${PREFIX:?}/ps" "${PREFIX:?}/migrations" "${PREFIX:?}/data_static"
install -d -m 0755 "$PREFIX"
install -m 0755 "$SRC/player_select_server.py" "$SRC/backup.sh" "$PREFIX/"
install -m 0644 "$SRC/catalog.html" "$PREFIX/"
for d in ps migrations data_static; do
  (cd "$SRC" && find "$d" -name __pycache__ -prune -o -type d -print) | while read -r p; do
    install -d -m 0755 "$PREFIX/$p"
  done
  (cd "$SRC" && find "$d" -name __pycache__ -prune -o -type f -print) | while read -r p; do
    install -m 0644 "$SRC/$p" "$PREFIX/$p"
  done
done

# The person seed. Written once and never overwritten; the owner edits it (names and colors are personal data).
# The server follows the file on every start, so replacing the placeholder name removes "Player 1" from the picker.
if [ ! -f "$PERSONS" ]; then
  umask 077
  printf '[{"name": "Player 1", "color": "#4c78a8"}]\n' > "$PERSONS"
  umask 022
  echo "Wrote $PERSONS with a placeholder person (replace it with your names, then: rc-service player-select restart)"
fi
chown -R "$APP_USER:$APP_USER" "$HOME_DIR"
chown -R root:root "$PREFIX"

say "Scheduling the nightly backup ($BACKUP_SCHEDULE)"
# Replace only this project's block, so other jobs in the same crontab survive. backup.sh does the dating
# itself: crontab would treat a % in an inline $(date +%F) as a newline.
mkdir -p /etc/crontabs
TAB=/etc/crontabs/$APP_USER
touch "$TAB"
sed -i '/^# BEGIN player-select$/,/^# END player-select$/d' "$TAB"
cat >> "$TAB" <<EOF
# BEGIN player-select
$BACKUP_SCHEDULE  $PREFIX/backup.sh $DATA >> $HOME_DIR/logs/backup.log 2>&1
# END player-select
EOF
chmod 0600 "$TAB"
rc-update add crond default >/dev/null 2>&1 || true
rc-service crond restart >/dev/null 2>&1 || rc-service crond start

say "Installing the catalog service (port $PORT)"
NAV_ARG=""
[ -n "$NAV" ] && NAV_ARG="--nav $NAV"
cat > /etc/init.d/player-select <<EOF
#!/sbin/openrc-run
name="player-select"
description="Player Select console catalog"
command="/usr/bin/python3"
command_args="$PREFIX/player_select_server.py --port $PORT --data $DATA --persons $PERSONS $NAV_ARG"
command_user="$APP_USER:$APP_USER"
supervisor="supervise-daemon"
respawn_delay=5
output_log="$HOME_DIR/logs/server.log"
error_log="$HOME_DIR/logs/server.log"

depend() {
  after net
}
EOF
chmod 0755 /etc/init.d/player-select
touch "$HOME_DIR/logs/server.log" && chown "$APP_USER:$APP_USER" "$HOME_DIR/logs/server.log"
rc-update add player-select default >/dev/null 2>&1 || true
rc-service player-select restart >/dev/null 2>&1 || rc-service player-select start

IP=$(ip -4 -o addr show 2>/dev/null | awk '$2 != "lo" {print $4; exit}' | cut -d/ -f1)
sleep 1
if wget -q -O /dev/null "http://127.0.0.1:$PORT/healthz" 2>/dev/null; then
  echo "Catalog running: http://${IP:-<this-host>}:$PORT/"
else
  echo "WARNING: the catalog didn't answer; check $HOME_DIR/logs/server.log"
fi

cat > "$SAVED" <<EOF
# Written by install/alpine.sh; reused on re-runs. Pass a variable again to change it.
SAVED_PORT='$PORT'
SAVED_NAV='$NAV'
SAVED_TZ_NAME='$TZ_NAME'
SAVED_BACKUP_SCHEDULE='$BACKUP_SCHEDULE'
EOF

say "Done"
echo "Persons: $PERSONS"
echo "Data and backups: $DATA"
echo "Back up now: su -s /bin/sh $APP_USER -c '$PREFIX/backup.sh $DATA'"
echo "Logs: $HOME_DIR/logs/"
