#!/usr/bin/env bash
# The exact video sequence. Runs from the committed cache with no API key.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "1. no provider key is configured for this run"
env | grep -cE '^(OPENROUTER|GROQ|ANTHROPIC)_API_KEY=' || echo "   none set"

say "2. generate 500 records, seed 20260905, ground truth known"
.venv/bin/python -m statesync.generator --seed 20260905 --n 500 --digest
echo "   ^ batch digest; identical on every run"

say "3. three arms, measured"
make eval

say "4. case 7 — two legitimate orders, NOT merged (no exception row)"
grep -c pay_hc07 exceptions.csv || echo "   0 rows for case 7, as intended"

say "5. case 13 — two explanations both reconcile, escalated as ambiguous"
grep pay_hc13 exceptions.csv || true

say "6. the honest residual"
cat exceptions.csv

say "7. tamper the ledger — the run must halt with zero writes"
.venv/bin/python -m pytest tests/chaos -q -k "not provider" 2>&1 | tail -3

say "demo complete — no network call was made"
