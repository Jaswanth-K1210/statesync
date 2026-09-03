#!/usr/bin/env bash
# The video sequence. Six beats, five minutes, no network call.
#
# Ten beats at thirty seconds each is exactly five minutes with no room to
# speak. Setup, the generator invocation and the arm-by-arm walkthrough are cut
# — they belong in the README, where a reviewer reads them at their own pace.
#
# Run it three times. Record the third. Do not start a background job while
# recording: concurrent runs contend for Postgres and Redis, and a smoke
# reading once came back at 929s for exactly that reason.

set -uo pipefail
cd "$(git rev-parse --show-toplevel)"

beat() { printf '\n\033[1m── %s\033[0m  \033[2m%s\033[0m\n\n' "$1" "$2"; }
die()  { printf '\n\033[1;31mPREFLIGHT FAILED: %s\033[0m\n\n' "$1" >&2; exit 1; }

# Everything the beats need is checked and captured ONCE, before any of them
# print. A previous take ran with Docker down: `make eval 2>/dev/null` swallowed
# the error and beats 4 and 5 rendered as blank space on camera. A beat that
# can silently show nothing is worse than one that refuses to start.
printf 'preflight… '
docker info >/dev/null 2>&1 || die "Docker is not running. Start it, then re-run."
docker compose up -d --wait >/dev/null 2>&1 || die "Postgres/Redis would not start."

# The demo gets its own Redis database. A test run or a smoke check sharing
# db 0 flushes claims mid-eval, and beat 5 then shows the row count moving
# while the narration says it does not — which is the one thing on camera that
# must never happen. Isolation beats remembering not to run things.
export STATESYNC_REDIS_URL="redis://localhost:${STATESYNC_REDIS_PORT:-56379}/15"
EVAL_OUT=$(mktemp)
make eval > "$EVAL_OUT" 2>&1 || { cat "$EVAL_OUT" >&2; die "make eval failed."; }
for marker in "earn its place" "the same batch, three times" "rejection rate"; do
  grep -q "$marker" "$EVAL_OUT" || die "the eval output is missing: $marker"
done
[ -s exceptions.csv ] || die "exceptions.csv is empty."

# Beat 5 claims the row count does not move. Check that against the table the
# beat is about to print, rather than trusting it.
ROWS=$(sed -n '/the same batch, three times/,/^$/p' "$EVAL_OUT" \
       | grep -E '^  [123] ·' | awk '{print $NF}' | tr -d ',' | sort -u | wc -l | tr -d ' ')
[ "$ROWS" = "1" ] || die "beat 5 would contradict itself: the row count moves across the three passes.
                 Something else is using Redis. Stop it and re-run."
SETTLEMENT_STATUS=$(.venv/bin/python -c \
  "from statesync.classifier.settlement import settlement_gap_report as r; print(r()['status'])") \
  || die "could not read the settlement status."
printf 'ok\n'
trap 'rm -f "$EVAL_OUT"' EXIT
pause() { [ "${DEMO_PAUSE:-1}" = "1" ] && read -rp $'\n    [enter]' _ || true; }

beat "1 · the problem" "0:00–0:40"
cat <<'TXT'
    Webhooks deliver at-least-once, in no guaranteed order, and give up
    permanently after 24 hours.

    So a merchant who turns them on gets duplicate orders, and one who
    turns them off loses paid ones. razorpay/razorpay-magento#208 is that
    merchant, in Razorpay's own issue tracker. There is no correct setting.

    Either way the money moved and the merchant's database does not know it.

    Today a human notices — usually after a customer complains — and
    reconciles it by hand.
TXT
pause

beat "2 · architecture, and the criticism pre-empted" "0:40–1:40"
cat <<'TXT'
    Three sources disagree: gateway, order store, ledger.

    FOUR OF THE SIX classes are exact set operations. Rules handle them and
    a language model would not beat a set difference at being a set
    difference. Saying that before a reviewer does.

    The model is used in one place: proposing decompositions of an
    unexplained residual. A deterministic verifier accepts one only if it
    reconciles exactly in paise, cites artifacts that exist, and declares
    rates consistent with their own amounts.
TXT
pause

beat "3 · the system refuses to act" "1:40–2:10"
echo "    Case 7 — two legitimate orders, same customer, same amount, 2s apart."
printf '    Rows in the exception list for case 7: %s\n' \
  "$(grep -c 'pay_hc07' exceptions.csv || true)"
echo "        not merged, so there is nothing to escalate"
pause

beat "4 · every guard fired on real model output" "2:10–3:00"
# Say this AS the zero renders, not after it. `llm_calls: 0` beside a README
# claiming a measured generation cost misleads by adjacency — a reviewer who
# does not ask leaves believing no model ever ran. Figures below are read from
# llm_cache/MANIFEST.json; if they ever disagree, the manifest is right.
cat <<'TXT'
    Every model response is cached and committed, so this demo is
    reproducible and needs no API key. The cold run happened once — eight
    calls, 1,493ms mean on openai/gpt-oss-120b — recorded in
    llm_cache/MANIFEST.json with the date and the model. What you are
    watching replays that.
TXT
echo
sed -n '/earn its place/,/^  LIMIT/p' "$EVAL_OUT" | sed '/^  LIMIT/,$d' 
pause

beat "5 · run it again — and again with no cache" "3:00–3:50"
sed -n '/the same batch, three times/,/^$/p' "$EVAL_OUT"
echo "    Row count does not move. Pass 3 wipes Redis entirely:"
echo "    the database constraint is the guarantee, the lease is an optimisation."
pause

beat "6 · fail closed" "3:50–4:20"
echo "    Break the audit chain, then try to repair:"
.venv/bin/python -m eval.tamper; echo "    exit code: $?"
echo
echo "    Zero writes. A reconciler that writes while its own audit trail is"
echo "    compromised is worse than no reconciler."
pause

beat "close · what it cannot do" "4:20–5:00"
cat <<'TXT'
    The verifier is sound but not complete. It refuses anything fabricated;
    it cannot detect an explanation that was never proposed. Candidate sets
    are pooled so the model can add a resolution but never remove an
    ambiguity.

TXT
printf '    Settlement reconciliation ships as %s:\n' "$SETTLEMENT_STATUS"
cat <<'TXT'
    demonstrated against synthetic payout data, never validated against
    real settlement behaviour, and reported separately from everything
    above.

    And the seam bugs: a value computed correctly in one place, then
    ignored or re-derived somewhere else. Every component test passed each
    time. The failures lived between correct components.
TXT
echo
echo "    exceptions.csv, committed:"
cat exceptions.csv
