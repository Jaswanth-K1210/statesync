#!/usr/bin/env bash
# Stop this project's processes. Nothing broader, ever.
#
# A bare `pkill -f "make verify"` took down the Docker daemon twice during this
# build, because the pattern matched more than intended — the same shape as the
# seam bugs in docs/seam_bugs.json: something trusted to be narrow that wasn't.
#
# The fix is the one applied everywhere else in this repo: make the unsafe form
# impossible rather than remembering not to type it. Use this script.
#
#   scripts/stop.sh            stop containers, leave data
#   scripts/stop.sh --hard     stop containers and drop volumes

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ "${1:-}" = "--hard" ]; then
  echo "==> docker compose down -v (this project's stack only)"
  docker compose down -v
else
  echo "==> docker compose stop (this project's stack only)"
  docker compose stop
fi

# Test runs, scoped to this checkout's virtualenv rather than to any pytest
# anywhere on the machine.
pids=$(pgrep -f "$(pwd)/.venv/bin/python -m pytest" 2>/dev/null || true)
if [ -n "$pids" ]; then
  echo "==> stopping this checkout's test runs: $pids"
  # shellcheck disable=SC2086
  kill $pids
else
  echo "==> no test runs from this checkout"
fi

echo "done — the Docker daemon and everything outside this project are untouched"
