#!/usr/bin/env bash
# The reproducibility test, done literally.
#
#   git clone <repo> /tmp/sstest && cd /tmp/sstest && make setup && make verify
#
# Every project fails this the first time — an uncommitted file, a hardcoded
# path, a dependency installed globally months ago. Running it from a script
# means it gets run, rather than being a paragraph in a checklist.
#
# Deliberately clones the COMMITTED state, not the working tree: a stranger
# gets what is in git, and anything only present locally is a bug.
#
# Runs on alternate ports so it does not collide with a development copy. The
# first run of this script failed exactly that way, which is the sort of thing
# a reviewer would hit and read as a broken setup.

set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
DEST="${1:-/tmp/sstest}"

echo "==> cloning committed state into $DEST"
rm -rf "$DEST"
git clone --quiet "$REPO" "$DEST"

cd "$DEST"

# Alternate ports, and DSNs to match, so a running dev copy is no obstacle.
export STATESYNC_PG_PORT="${STATESYNC_PG_PORT:-55433}"
export STATESYNC_REDIS_PORT="${STATESYNC_REDIS_PORT:-56380}"
export STATESYNC_ADMIN_DSN="postgresql://statesync:statesync@localhost:${STATESYNC_PG_PORT}/statesync"
export STATESYNC_APP_DSN="postgresql://statesync_app:statesync_app@localhost:${STATESYNC_PG_PORT}/statesync"
export STATESYNC_REDIS_URL="redis://localhost:${STATESYNC_REDIS_PORT}/0"
trap 'cd "$DEST" && docker compose down -v >/dev/null 2>&1 || true' EXIT
echo "==> a stranger has: $(git ls-files | wc -l | tr -d ' ') files, no .env, no API key"

# Prove the demo path needs no credential.
unset OPENROUTER_API_KEY GROQ_API_KEY ANTHROPIC_API_KEY 2>/dev/null || true

# A previous attempt may have left containers bound to the old ports. Compose
# reuses an existing container rather than recreating it when only the port
# mapping changed, which then fails to connect in a way that looks like a
# broken setup rather than a stale one.
docker compose down -v >/dev/null 2>&1 || true

echo "==> make setup"
make setup

echo "==> make verify"
make verify

echo "==> make eval (must reproduce the committed numbers)"
make eval > /tmp/sstest-eval.txt
echo "==> checking the README still matches regeneration"
.venv/bin/python -m eval.readme
if ! git diff --quiet README.md; then
  echo "FAIL: README.md drifted from regeneration" >&2
  exit 1
fi

echo
echo "reproduced cleanly in $DEST with no API key"
