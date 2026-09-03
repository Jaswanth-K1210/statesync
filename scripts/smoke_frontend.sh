#!/usr/bin/env bash
# Does the one screen build, boot and load? Under 10 seconds.
#
# Deliberately NOT in `make smoke`: the backend ladder is what runs before every
# commit and it must stay well inside its 30-second budget. This is a supplement
# and `make demo` does not depend on it.

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
start=$(date +%s)

echo "==> component and journey tests"
(cd frontend && npm test >/dev/null 2>&1) || { echo "FAIL: ui tests" >&2; exit 1; }

echo "==> build"
(cd frontend && npm run build >/dev/null 2>&1)

echo "==> boot the packets API"
.venv/bin/python -m statesync.api.server >/dev/null 2>&1 &
api_pid=$!
# Scoped to the PIDs this script started. Never a broad pattern — see
# ARCHITECTURE.md entry 13.
trap 'kill $api_pid $preview_pid 2>/dev/null || true' EXIT

echo "==> boot the preview server"
(cd frontend && npm run preview >/dev/null 2>&1) &
preview_pid=$!

for _ in $(seq 1 40); do
  if curl -sf http://127.0.0.1:8787/api/divergences >/dev/null 2>&1 \
     && curl -sf http://localhost:5174 >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

echo "==> the list loads"
count=$(curl -sf http://127.0.0.1:8787/api/divergences | grep -o payment_id | wc -l | tr -d ' ')
[ "$count" -gt 0 ] || { echo "FAIL: the API served no escalations" >&2; exit 1; }
echo "    $count escalations"

echo "==> one packet loads whole"
curl -sf http://127.0.0.1:8787/api/divergences/pay_hc13 | grep -q ambiguous_multiple_verified \
  || { echo "FAIL: the ambiguous packet did not serve" >&2; exit 1; }

curl -sf http://localhost:5174 | grep -q '<div id="root">' \
  || { echo "FAIL: the app did not serve" >&2; exit 1; }

elapsed=$(( $(date +%s) - start ))
echo "smoke-frontend: ${elapsed}s of a 10s budget"
[ "$elapsed" -le 10 ] || { echo "OVER BUDGET" >&2; exit 1; }
