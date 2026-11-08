#!/bin/sh
# Lint: shellcheck on every shell script, py_compile on every Python file.
set -eu
cd "$(dirname "$0")/../.."

status=0
echo "== shellcheck"
# SC1090/SC1091: the scripts source files whose names are only known at run time (the env file).
# The installer scripts arrive in a later task; until then there is nothing to check.
scripts=$(git ls-files '*.sh')
if [ -n "$scripts" ]; then
  # shellcheck disable=SC2086
  shellcheck --exclude=SC1090,SC1091 $scripts || status=1
else
  echo "no shell scripts yet"
fi

echo "== py_compile"
for f in $(git ls-files '*.py'); do
  python3 -m py_compile "$f" || status=1
done
find . -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

[ "$status" = 0 ] && echo "lint passed"
exit "$status"
