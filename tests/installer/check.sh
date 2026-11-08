#!/bin/sh
# tests/installer/check.sh - run install/alpine.sh three times with the system commands stubbed, and check
# what a re-run must keep and must not duplicate. The installed server is then started for real.
#
# It changes /etc, /opt and /home, so it only runs where that is safe: inside the python:3.12-alpine CI
# container, or anywhere else when INSTALLER_TEST_OK=1 (a throwaway VM or container). On a machine that
# isn't Alpine, set FAKE_USERS=1 as well: adduser and chown are stubbed, since there is no BusyBox adduser,
# and the health check then runs the server as the current user instead of as 'games'.
#
#   docker run --rm -v "$PWD":/src:ro python:3.12-alpine sh /src/tests/installer/check.sh
set -eu

SRC=$(cd "$(dirname "$0")/../.." && pwd)
if [ "${INSTALLER_TEST_OK:-}" != 1 ] && [ ! -f /.dockerenv ] && [ ! -f /etc/alpine-release ]; then
  echo "Refusing to run: this changes /etc, /opt and /home. Use a container, or set INSTALLER_TEST_OK=1." >&2
  exit 2
fi

STUBS=$(mktemp -d)
CALLS=$STUBS/calls.log
: > "$CALLS"
made_alpine_release=0
made_zoneinfo=0
tz_saved=""
server_pid=""
fail=0

# shellcheck disable=SC2317,SC2329  # runs from the EXIT trap (older shellcheck says SC2317, newer SC2329)
cleanup() {
  if [ -n "$server_pid" ]; then kill "$server_pid" 2>/dev/null || true; fi
  [ "$made_alpine_release" = 1 ] && rm -f /etc/alpine-release
  [ "$made_zoneinfo" = 1 ] && rm -f /usr/share/zoneinfo/Etc/UTC
  if [ -n "$tz_saved" ]; then # put the host's time zone files back
    rm -f /etc/localtime /etc/timezone
    cp -P "$tz_saved/localtime" /etc/localtime 2>/dev/null || true
    cp -P "$tz_saved/timezone" /etc/timezone 2>/dev/null || true
  fi
  rm -rf "$STUBS"
}
trap cleanup EXIT

ok()  { printf 'ok    %s\n' "$1"; }
bad() { printf 'FAIL  %s\n' "$1" >&2; fail=1; }
check() { # check "description" command...
  desc=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$desc"; else bad "$desc"; fi
}
count() { grep -c "$1" "$2" || true; }

# ---- stubs: record the call, do nothing
for cmd in apk rc-service rc-update wget ip; do
  cat > "$STUBS/$cmd" <<EOF
#!/bin/sh
echo "$cmd \$*" >> "$CALLS"
exit 0
EOF
  chmod +x "$STUBS/$cmd"
done
if [ "${FAKE_USERS:-}" = 1 ]; then
  for cmd in adduser chown; do
    printf '#!/bin/sh\necho "%s $*" >> "%s"\nexit 0\n' "$cmd" "$CALLS" > "$STUBS/$cmd"
    chmod +x "$STUBS/$cmd"
  done
  # shellcheck disable=SC2016  # the stub's own $1 and $@ must stay literal
  printf '#!/bin/sh\n[ "${1:-}" = games ] && exit 1\nexec /usr/bin/id "$@"\n' > "$STUBS/id"
  chmod +x "$STUBS/id"
fi
PATH="$STUBS:$PATH"
export PATH

[ -f /etc/alpine-release ] || { echo "3.20.0" > /etc/alpine-release; made_alpine_release=1; }
# The installer copies the zone file when TZ_NAME is given.
if [ ! -f /usr/share/zoneinfo/Etc/UTC ]; then
  mkdir -p /usr/share/zoneinfo/Etc
  : > /usr/share/zoneinfo/Etc/UTC
  made_zoneinfo=1
fi

# The installer rewrites these when TZ_NAME is given; restore them on exit.
tz_saved=$STUBS/tz
mkdir -p "$tz_saved"
cp -P /etc/localtime "$tz_saved/localtime" 2>/dev/null || true
cp -P /etc/timezone "$tz_saved/timezone" 2>/dev/null || true

PREFIX=/opt/player-select
HOME_DIR=/home/games
DATA=$HOME_DIR/data
PERSONS=$HOME_DIR/persons.json
TAB=/etc/crontabs/games
SAVED=/etc/player-select.conf
INIT=/etc/init.d/player-select
# Start from a machine that has never had the installer (matters when this is re-run in the same VM).
rm -f "$SAVED" "$INIT" "$PERSONS"
rm -rf "$PREFIX" "$DATA"
mkdir -p /etc/crontabs /etc/init.d "$HOME_DIR"

# An unrelated job, and a block from an older install that must be replaced rather than duplicated.
cat > "$TAB" <<'EOF'
0 3 * * * /usr/local/bin/other-job.sh
# BEGIN player-select
1 1 * * 1  /old/path/backup.sh >> /old.log 2>&1
# END player-select
30 6 * * * /usr/local/bin/another-job.sh
EOF

run() { # run label [VAR=value ...]
  label=$1; shift
  printf '\n--- %s\n' "$label"
  env "$@" sh "$SRC/install/alpine.sh" > "$STUBS/out.$label" 2>&1 || { cat "$STUBS/out.$label" >&2; bad "$label: installer exited non-zero"; return 1; }
  ok "$label: installer exited 0"
}

# shellcheck disable=SC2317,SC2329  # called through check(), which older shellcheck cannot follow
# Fetch a URL with the real interpreter (wget is stubbed in PATH).
http_get() { python3 -c 'import sys, urllib.request; sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())' "$1"; }

# shellcheck disable=SC2317,SC2329  # called through check()
# Start the installed server with the installed --data and --persons, ask /healthz, stop it.
service_healthy() {
  port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  pidfile=/tmp/player-select-check.$$.pid
  rm -f "$pidfile"
  if [ "${FAKE_USERS:-}" = 1 ]; then
    sh -c 'echo $$ > "$1"; shift; exec "$@"' sh "$pidfile" python3 "$PREFIX/player_select_server.py" \
      --port "$port" --bind 127.0.0.1 --data "$DATA" --persons "$PERSONS" > "$STUBS/server.log" 2>&1 &
  else
    su games -s /bin/sh -c "echo \$\$ > '$pidfile'; exec python3 '$PREFIX/player_select_server.py' --port $port --bind 127.0.0.1 --data '$DATA' --persons '$PERSONS'" \
      > "$STUBS/server.log" 2>&1 &
  fi
  server_pid=$!
  answer=""
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    answer=$(http_get "http://127.0.0.1:$port/healthz" 2>/dev/null) && break
    sleep 0.5
  done
  inner=$(cat "$pidfile" 2>/dev/null || true)
  if [ -n "$inner" ]; then kill "$inner" 2>/dev/null || true; fi
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
  server_pid=""
  rm -f "$pidfile"
  [ "$answer" = ok ] || { cat "$STUBS/server.log" >&2; return 1; }
}

# ================================================================== first install
run first PORT=8586 'NAV=Steam=http://steam.example.com:8484/' TZ_NAME=Etc/UTC 'BACKUP_SCHEDULE=0 4 * * *'

check "saves the port" grep -q "SAVED_PORT='8586'" "$SAVED"
check "saves the nav link" grep -q "SAVED_NAV='Steam=http://steam.example.com:8484/'" "$SAVED"
check "saves the time zone name" grep -q "SAVED_TZ_NAME='Etc/UTC'" "$SAVED"
check "saves the backup schedule" grep -q "SAVED_BACKUP_SCHEDULE='0 4 \* \* \*'" "$SAVED"
check "init script uses the port, persons file, data dir and nav link" \
  grep -q -- "$PREFIX/player_select_server.py --port 8586 --data $DATA --persons $PERSONS --nav Steam=http://steam.example.com:8484/" "$INIT"
check "init script is executable" test -x "$INIT"
check "unrelated cron line before the block is kept" grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' "$TAB"
check "unrelated cron line after the block is kept" grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' "$TAB"
check "the old block's schedule is gone" sh -c "! grep -q '/old/path' '$TAB'"
check "the new schedule runs backup.sh with the data dir" grep -qF "0 4 * * *  $PREFIX/backup.sh $DATA" "$TAB"
check "the cron line has no percent sign" sh -c "! grep -q '%' '$TAB'"
check "exactly one BEGIN" test "$(count '^# BEGIN player-select$' "$TAB")" = 1
check "exactly one END" test "$(count '^# END player-select$' "$TAB")" = 1
check "installed the executables, mode 755" sh -c "for f in player_select_server.py backup.sh; do [ \"\$(stat -c %a '$PREFIX'/\$f)\" = 755 ] || exit 1; done"
check "installed catalog.html, mode 644" test "$(stat -c %a "$PREFIX/catalog.html")" = 644
check "installed the ps package, migrations and data_static" \
  test -f "$PREFIX/ps/importer.py" -a -f "$PREFIX/migrations/001_init.sql" -a -f "$PREFIX/data_static/gameeye_id_maps.json"
check "installed files are mode 644 and directories 755" sh -c "
  [ \"\$(stat -c %a '$PREFIX/ps/importer.py')\" = 644 ] && [ \"\$(stat -c %a '$PREFIX/ps')\" = 755 ] &&
  [ \"\$(stat -c %a '$PREFIX/migrations')\" = 755 ] && [ \"\$(stat -c %a '$PREFIX/data_static/gameeye_id_maps.json')\" = 644 ]"
check "no bytecode caches were installed" sh -c "! find '$PREFIX' -name __pycache__ | grep -q ."
check "installs the sqlite package" grep -q '^apk add .*sqlite' "$CALLS"
check "enabled and restarted the service" grep -q '^rc-service player-select restart' "$CALLS"
check "sets the time zone" grep -qx 'Etc/UTC' /etc/timezone
check "creates the data directory" test -d "$DATA"
check "imports/ is mode 700" test "$(stat -c %a "$DATA/imports")" = 700
check "backups/ exists" test -d "$DATA/backups"
check "persons.json holds the placeholder person" \
  grep -qx '\[{"name": "Player 1", "color": "#4c78a8"}\]' "$PERSONS"
check "persons.json is mode 600" test "$(stat -c %a "$PERSONS")" = 600
check "the installed server and its modules import (rc-service is stubbed, so nothing started it)" \
  sh -c "cd '$PREFIX' && python3 -c 'import player_select_server, ps.importer'"
check "service healthy: the installed server answers /healthz with ok" service_healthy

cp "$TAB" "$STUBS/tab.first"
# The owner edits persons.json between runs; later runs must never touch it.
echo '[{"name": "Player B", "color": "#e45756"}]' > "$PERSONS"
chmod 600 "$PERSONS"
cp "$PERSONS" "$STUBS/persons.before"

# ================================================================== re-run: nothing given
run second

check "keeps persons.json byte for byte" cmp -s "$PERSONS" "$STUBS/persons.before"
check "keeps persons.json mode 600" test "$(stat -c %a "$PERSONS")" = 600
check "remembers PORT" grep -q -- "--port 8586 " "$INIT"
check "remembers NAV" grep -q -- "--nav Steam=http://steam.example.com:8484/" "$INIT"
check "remembers TZ_NAME" grep -q "SAVED_TZ_NAME='Etc/UTC'" "$SAVED"
check "remembers BACKUP_SCHEDULE" grep -qF "0 4 * * *  $PREFIX/backup.sh $DATA" "$TAB"
check "still exactly one BEGIN" test "$(count '^# BEGIN player-select$' "$TAB")" = 1
check "still exactly one END" test "$(count '^# END player-select$' "$TAB")" = 1
check "crontab is identical to after the first run" cmp -s "$TAB" "$STUBS/tab.first"
check "unrelated cron lines still there" sh -c "grep -qx '0 3 \* \* \* /usr/local/bin/other-job.sh' '$TAB' && grep -qx '30 6 \* \* \* /usr/local/bin/another-job.sh' '$TAB'"

# ================================================================== re-run: change one option, clear another
run third PORT=8686 NAV=

check "a given option replaces the saved one" grep -q -- "--port 8686 " "$INIT"
check "an empty NAV removes the link" sh -c "! grep -q -- '--nav' '$INIT'"
check "the other saved option is kept" grep -qF "0 4 * * *  $PREFIX/backup.sh" "$TAB"
check "still exactly one block after the third run" test "$(count '^# BEGIN player-select$' "$TAB")" = 1
check "persons.json still untouched after the third run" cmp -s "$PERSONS" "$STUBS/persons.before"
check "the service still comes up healthy after the third run" service_healthy

# ================================================================== refusals
printf '\n--- refusals\n'
if env 'NAV=bad nav' sh "$SRC/install/alpine.sh" >/dev/null 2>&1; then bad "NAV with a space is refused"; else ok "NAV with a space is refused"; fi

echo
if [ "$fail" = 0 ]; then echo "installer checks passed"; else echo "installer checks FAILED" >&2; fi
exit "$fail"
