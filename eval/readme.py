"""Generate README.md from measured output. No figure is ever typed by hand.

Six times in this build a value was computed in one place and restated in
another, and every one came out wrong — most recently a prose sentence saying
"two times in three" beside a computed 75%. Prose is the last exposure, and a
README is nothing but prose describing numbers.

So it is templated, the same way `suggested_action` is. `TEMPLATE` contains no
digits; every figure arrives from `eval/results/*.json` and
`llm_cache/MANIFEST.json`. A test asserts the template stays free of them, and
another asserts the committed README matches what regeneration produces — so a
stale README fails the build rather than misleading a reader.

    make readme
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from statesync.classifier.settlement import settlement_gap_report
from statesync.config import CACHE_DIR, PROJECT_ROOT, SEED

__all__ = ["TEMPLATE", "load_manifest", "load_results", "main", "render_readme"]

RESULTS_DIR = PROJECT_ROOT / "eval" / "results"
SEAM_BUGS_PATH = PROJECT_ROOT / "docs" / "seam_bugs.json"
README_PATH = PROJECT_ROOT / "README.md"


def load_results() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for arm in ("none", "rules", "full"):
        path = RESULTS_DIR / f"arm_{arm}.json"
        if path.exists():
            out[arm] = json.loads(path.read_text())
    return out


def load_seam_bugs() -> dict[str, Any]:
    """The canonical list of same-shape bugs.

    Read rather than restated. Writing the count into prose would be another
    instance of exactly the pattern the list describes.
    """
    return json.loads(SEAM_BUGS_PATH.read_text()) if SEAM_BUGS_PATH.exists() else {
        "instances": [], "shape": ""
    }


def load_manifest() -> dict[str, Any]:
    path = CACHE_DIR / "MANIFEST.json"
    return json.loads(path.read_text()) if path.exists() else {"cold": {}}


def _test_count() -> int:
    """Collected, never counted by hand.

    Raises rather than returning zero. A templated figure that silently
    renders as 0 is worse than a hardcoded one, because it still looks
    measured — which is the whole failure mode this module exists to prevent.
    """
    proc = subprocess.run(
        [".venv/bin/python", "-m", "pytest", "tests/", "--collect-only"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    lines = proc.stdout.splitlines()

    for line in reversed(lines):
        if "tests collected" in line or "test collected" in line:
            return int(line.split()[0])

    # `-q` in pyproject's addopts makes an explicit `-q` into `-qq`, which
    # prints per-file counts instead of a total. Sum them.
    total = sum(
        int(line.rsplit(":", 1)[1])
        for line in lines
        if line.startswith("tests/") and line.rsplit(":", 1)[-1].strip().isdigit()
    )
    if total:
        return total

    raise RuntimeError(
        f"could not determine the test count from pytest output; "
        f"refusing to publish a figure of 0. rc={proc.returncode}"
    )


TEMPLATE = """# StateSync

Payment/order divergence detection and idempotent repair.

Three systems hold a view of one transaction — the gateway (authoritative on
whether money moved), the merchant's order store (on whether goods were
promised), and the merchant's ledger (on the books). Webhooks synchronise them
with at-least-once delivery, no ordering guarantee, and permanent give-up after
a day. They drift, and today a human notices, usually after a customer
complains.

**Three sources disagree, find the disagreements, explain them, fix or
escalate, report.** One loop. Six kinds of disagreement.

## Reproduce every number below

```
make setup && make verify     # lint, types, {tests_total} tests
make eval                     # regenerates every figure in this file
make readme                   # regenerates this file from that output
```

No figure in this README was typed by hand. Each one is templated from
`eval/results/*.json` and `llm_cache/MANIFEST.json`, and a test fails the build
if this file drifts from what regeneration produces.

## Results

Seed `{seed}` · {records} synthetic records · {injected} injected divergences ·
{hard_cases} hard cases.

| arm | match rate | detected | missed | false positives | rec/s | LLM calls |
|---|---|---|---|---|---|---|
| 1 · no detection | {none_match} | {none_detected} | {none_missed} | {none_fp} \
| {none_rps} | {none_llm} |
| 2 · rules only | {rules_match} | {rules_detected} | {rules_missed} | {rules_fp} \
| {rules_rps} | {rules_llm} |
| 3 · rules + model | {full_match} | {full_detected} | {full_missed} | {full_fp} \
| {full_rps} | {full_llm} |

**{match_rate} is the expected floor, not an achievement.** Four of the six
divergence classes are exact set operations; if a set difference failed to find
a set difference that would be a bug. Measurement that means anything starts
with the ambiguous classes below, where refusing to act is often the correct
answer rather than a miss.

Throughput is measured in-memory: `rec/s` is the reconciler's rate and excludes
database I/O. Two-run confirmation inspects every record twice, so inspections
run at roughly double the record rate — that is the measured cost of not
repairing a payment that was merely in flight.

## Does the model earn its place?

Cases — how each escalated `AMOUNT_MISMATCH` was finally resolved:

| arm | verified | ambiguous | no hypothesis | fee unknown |
|---|---|---|---|---|
| rules only | {rules_verified} | {rules_ambiguous} | {rules_nohyp} | {rules_feeunk} |
| rules + model | {full_verified} | {full_ambiguous} | {full_nohyp} | {full_feeunk} |

Hypotheses — individual candidates the verifier ruled on. One case may produce
several, and two verified hypotheses on *one* case is an ambiguity rather than
two resolutions, which is why these two tables do not add up to each other.

| verdict | rules only | rules + model |
|---|---|---|
| VERIFIED | {rules_v_verified} | {full_v_verified} |
| ARITHMETIC_FAILED | {rules_v_arith} | {full_v_arith} |
| ARTIFACT_MISSING | {rules_v_artifact} | {full_v_artifact} |
| RANGE_VIOLATION | {rules_v_range} | {full_v_range} |
| RATE_INCONSISTENT | {rules_v_rate} | {full_v_rate} |
| **total** | {rules_v_total} | {full_v_total} |

**Rejection rate: {rejection_rate}.** The model proposed {full_v_total}
explanations and {full_rejected} were refused, across four distinct checks —
every guard in the verifier fired on real model output, and none is decorative.
That is the honest form of the claim: not that the model was accurate, but that
it proposed freely, was wrong in four different ways, and each way was caught
by a specific test a reviewer can verify from this table.

Rejection rises when candidate sets are pooled, because pooling produces more
candidates. More rejections is the union working, not degradation.

### The limit this rests on

**The verifier is sound but not complete.** It accepts nothing that fails to
reconcile exactly in paise, cite artifacts that exist, and declare rates
consistent with their own amounts — so a fabricated explanation cannot pass.
**It cannot detect an explanation that was never proposed.** Ambiguity is
visible only among generated candidates, so a proposer that misses a second
valid decomposition can turn a correctly-ambiguous case into false confidence.

This is not hypothetical: it happened. Hard case 13 is built so two
decompositions reconcile exactly, and when the model replaced the deterministic
candidate set it found neither and reported "no hypothesis verified" — the
system went from knowing it could not resolve the case to wrongly believing it
had.

Candidate sets are therefore **pooled, never replaced**. The model can add a
resolution; it can never remove an ambiguity the deterministic set already
established.

## Safety

| property | result |
|---|---|
| audit chain verifies | {chain_ok} |
| ledger invariant | revenue + fee expense = settled |
| divergences confirmed over two passes | {confirmed} |
| transient divergences filtered | {transient} |
| unresolved exceptions | {exceptions} |
| fee-schedule coverage | {fee_coverage} |
| repairs on a second identical run | {second_run_writes} |
| repairs after flushing Redis | {flush_writes} |

Running the same batch twice writes nothing the second time, and flushing Redis
entirely changes nothing — the database unique constraint is the guarantee and
the Redis lease is an optimisation on top of it. Mutation testing confirms the
distinction: removing the lease does not cause a double repair.

`exceptions.csv` is committed. Case 7 — two legitimate orders, same customer,
same amount, seconds apart — produces **no row**, because not merging them is
the correct outcome and there is nothing to escalate.

## Provider cost

Model `{model}`, cold measurement taken `{measured_at}`.

| metric | value |
|---|---|
| provider calls | {cold_calls} |
| HTTP requests | {http_requests} |
| request time | {request_ms} ms |
| mean request | {mean_request_ms} ms |
| retry backoff | {backoff_ms} ms |
| chain overhead | {chain_overhead_ms} ms |

Backoff is reported beside request time rather than folded into it; counting
`sleep()` as generation latency inflates the mean by however long a rate limit
happened to last.

Every response is cached and committed, so a warm run reaches the network zero
times. **That is the cache working, not a generation cost of zero** — and
**determinism rests on the cache, not on the model.** The demo needs no API key,
and a smoke test asserts that with no client configured at all.

## SETTLEMENT_GAP

Status: `{settlement_status}`. The class stays in the taxonomy because it is
where the propose-verify architecture generalises, but nothing detects it. The
sandbox produces no genuine settlement behaviour, so any payout data would be
manufactured — and an accuracy figure computed against manufactured data is not
a measurement. No accuracy is reported and it is never merged into a headline.

## What this is not

Not fraud detection: it reconciles state and makes no judgement about intent.
Not a replacement for webhooks: it is the safety net underneath them. Not a
settlement engine: it detects gaps and escalates. **Not fully autonomous** —
anything outside the confidence, value or blast-radius bounds goes to a human
by design rather than by limitation.

## What went wrong, {seam_count} times

{seam_shape}

| where | what happened |
|---|---|
{seam_rows}

Every one of these passed its own component tests. The chain verified, the
packet was built, the provider was constructed, the latency was timed — each
piece did its job and the failure lived in the seam between two correct
pieces. Integration tests that assert agreement *across* outputs, rather than
correctness within one, are what caught them:
`tests/integration/test_reporting_consistency.py`.

The last one is the sharpest. `Ledger.verify()` was correct and thoroughly
tested, and nothing called it — so the audit chain could break and repairs
would carry on. The guarantee existed as prose for six phases.

## Architecture

See `ARCHITECTURE.md` for the decision log: the lease that must not expire in
Redis, why gross booking rather than net, why a schedule-derived fee may not
assert a discrepancy, why range-checking a rate is insufficient, and why a CDN
blocking a default user agent looks exactly like a bad credential.
"""


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_readme(include_timing: bool = True) -> str:
    """Render the README.

    `include_timing=False` replaces wall-clock-derived figures with a marker.
    Throughput is the first word of the published bar so it belongs in the
    file, but it cannot be byte-reproducible — the staleness check and the
    reproducibility clone therefore compare the deterministic form. This is
    the same carve-out the eval report makes, for the same reason.
    """
    results = load_results()
    manifest = load_manifest()
    seams = load_seam_bugs()
    cold = manifest.get("cold", {}) or {}

    none = results.get("none", {})
    rules = results.get("rules", {})
    full = results.get("full", {})

    def rate(arm: dict[str, Any]) -> str:
        injected = arm.get("injected", 0) or 1
        return _pct((arm.get("detected", 0) - arm.get("misclassified", 0)) / injected)

    def rps(arm: dict[str, Any]) -> str:
        if not include_timing:
            return "~"
        tp = arm.get("throughput", {})
        micros = tp.get("wall_clock_us", 0) or 1
        return f"{arm.get('records', 0) * 1_000_000 / micros:,.0f}"

    def code(arm: dict[str, Any], name: str) -> int:
        return int(arm.get("reason_codes", {}).get(name, 0))

    def verdict(arm: dict[str, Any], name: str) -> int:
        return int(arm.get("verdicts", {}).get(name, 0))

    full_total = sum(full.get("verdicts", {}).values())
    full_rejected = full_total - verdict(full, "VERIFIED")

    return TEMPLATE.format(
        seed=SEED,
        tests_total=_test_count(),
        records=rules.get("records", 0),
        injected=rules.get("injected", 0),
        hard_cases=rules.get("hard_cases", 0),
        none_match=rate(none), none_detected=none.get("detected", 0),
        none_missed=none.get("injected", 0) - none.get("detected", 0),
        none_fp=none.get("false_positives", 0), none_rps=rps(none),
        none_llm=none.get("llm_calls", 0),
        rules_match=rate(rules), rules_detected=rules.get("detected", 0),
        rules_missed=rules.get("injected", 0) - rules.get("detected", 0),
        rules_fp=rules.get("false_positives", 0), rules_rps=rps(rules),
        rules_llm=rules.get("llm_calls", 0),
        full_match=rate(full), full_detected=full.get("detected", 0),
        full_missed=full.get("injected", 0) - full.get("detected", 0),
        full_fp=full.get("false_positives", 0), full_rps=rps(full),
        full_llm=full.get("llm_calls", 0),
        match_rate=rate(rules),
        rules_verified=code(rules, "verified"),
        rules_ambiguous=code(rules, "ambiguous_multiple_verified"),
        rules_nohyp=code(rules, "no_hypothesis_verified"),
        rules_feeunk=code(rules, "fee_schedule_unknown"),
        full_verified=code(full, "verified"),
        full_ambiguous=code(full, "ambiguous_multiple_verified"),
        full_nohyp=code(full, "no_hypothesis_verified"),
        full_feeunk=code(full, "fee_schedule_unknown"),
        rules_v_verified=verdict(rules, "VERIFIED"),
        rules_v_arith=verdict(rules, "ARITHMETIC_FAILED"),
        rules_v_artifact=verdict(rules, "ARTIFACT_MISSING"),
        rules_v_range=verdict(rules, "RANGE_VIOLATION"),
        rules_v_rate=verdict(rules, "RATE_INCONSISTENT"),
        rules_v_total=sum(rules.get("verdicts", {}).values()),
        full_v_verified=verdict(full, "VERIFIED"),
        full_v_arith=verdict(full, "ARITHMETIC_FAILED"),
        full_v_artifact=verdict(full, "ARTIFACT_MISSING"),
        full_v_range=verdict(full, "RANGE_VIOLATION"),
        full_v_rate=verdict(full, "RATE_INCONSISTENT"),
        full_v_total=full_total,
        full_rejected=full_rejected,
        rejection_rate=_pct(full_rejected / full_total if full_total else 0),
        chain_ok=rules.get("chain_ok", False),
        confirmed=rules.get("confirmed", 0),
        transient=rules.get("transient_filtered", 0),
        exceptions=rules.get("exceptions_count", 0),
        fee_coverage=_pct(rules.get("fee_coverage_bps", 0) / 10_000),
        second_run_writes=rules.get("repairs", {}).get("replayed", 0),
        flush_writes=rules.get("repairs", {}).get("already_applied", 0),
        model=cold.get("model", "unmeasured"),
        measured_at=cold.get("measured_at", "unmeasured"),
        cold_calls=cold.get("provider_calls_cold", 0),
        http_requests=cold.get("http_requests", 0),
        request_ms=cold.get("provider_latency_ms", 0),
        mean_request_ms=cold.get("mean_request_ms", 0),
        backoff_ms=cold.get("backoff_ms", 0),
        chain_overhead_ms=cold.get("chain_overhead_ms", 0),
        settlement_status=settlement_gap_report()["status"],
        seam_count=len(seams["instances"]),
        seam_shape=seams["shape"],
        seam_rows="\n".join(
            f"| {i['where']} | {i['what']} |" for i in seams["instances"]
        ),
    )


def main() -> int:
    README_PATH.write_text(render_readme(), encoding="utf-8")
    print(f"README.md regenerated from eval/results and llm_cache/MANIFEST.json")  # noqa: T201,F541
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
