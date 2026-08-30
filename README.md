# StateSync

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
make setup && make verify     # lint, types, 599 tests
make eval                     # regenerates every figure in this file
make readme                   # regenerates this file from that output
```

No figure in this README was typed by hand. Each one is templated from
`eval/results/*.json` and `llm_cache/MANIFEST.json`, and a test fails the build
if this file drifts from what regeneration produces.

## Results

Seed `20260905` · 500 synthetic records · 124 injected divergences ·
15 hard cases.

| arm | match rate | detected | missed | false positives | rec/s | LLM calls |
|---|---|---|---|---|---|---|
| 1 · no detection | 0.0% | 0 | 124 | 0 | 171,292 | 0 |
| 2 · rules only | 100.0% | 124 | 0 | 0 | 3,570 | 0 |
| 3 · rules + model | 100.0% | 124 | 0 | 0 | 16,599 | 10 |

**100.0% is the expected floor, not an achievement.** Four of the six
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
| rules only | 0 | 1 | 5 | 1 |
| rules + model | 2 | 1 | 3 | 1 |

Hypotheses — individual candidates the verifier ruled on. One case may produce
several, and two verified hypotheses on *one* case is an ambiguity rather than
two resolutions, which is why these two tables do not add up to each other.

| verdict | rules only | rules + model |
|---|---|---|
| VERIFIED | 2 | 4 |
| ARITHMETIC_FAILED | 3 | 6 |
| ARTIFACT_MISSING | 1 | 4 |
| RANGE_VIOLATION | 1 | 1 |
| RATE_INCONSISTENT | 0 | 1 |
| **total** | 7 | 16 |

**Rejection rate: 75.0%.** The model proposed 16
explanations and 12 were refused, across four distinct checks —
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
| audit chain verifies | True |
| ledger invariant | revenue + fee expense = settled |
| divergences confirmed over two passes | 134 |
| transient divergences filtered | 1 |
| unresolved exceptions | 7 |
| fee-schedule coverage | 85.7% |
| repairs on a second identical run | 0 |
| repairs after flushing Redis | 0 |

Running the same batch twice writes nothing the second time, and flushing Redis
entirely changes nothing — the database unique constraint is the guarantee and
the Redis lease is an optimisation on top of it. Mutation testing confirms the
distinction: removing the lease does not cause a double repair.

`exceptions.csv` is committed. Case 7 — two legitimate orders, same customer,
same amount, seconds apart — produces **no row**, because not merging them is
the correct outcome and there is nothing to escalate.

## Provider cost

Model `openai/gpt-oss-120b`, cold measurement taken `2026-08-30T07:55:48Z`.

| metric | value |
|---|---|
| provider calls | 8 |
| HTTP requests | 8 |
| request time | 11945 ms |
| mean request | 1493 ms |
| retry backoff | 0 ms |
| chain overhead | 96 ms |

Backoff is reported beside request time rather than folded into it; counting
`sleep()` as generation latency inflates the mean by however long a rate limit
happened to last.

Every response is cached and committed, so a warm run reaches the network zero
times. **That is the cache working, not a generation cost of zero** — and
**determinism rests on the cache, not on the model.** The demo needs no API key,
and a smoke test asserts that with no client configured at all.

## SETTLEMENT_GAP

Status: `not_implemented`. The class stays in the taxonomy because it is
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

## What went wrong, 7 times

A value computed correctly in one place, then re-derived, misrouted, or ignored somewhere else. Every component test passed. The failure was always between correct components.

| where | what happened |
|---|---|
| escalation packet -> exceptions.csv | The packet was built and was correct; the CSV re-derived the reason code from a classifier default, so case 13 shipped mislabelled. |
| hard-case packets vs the eval | base_paise was passed on one path and omitted on the other, so the same packet was checked two different ways depending on who built it. |
| arm 3's provider | The model provider was constructed, then the hard-case fixture provider was consumed instead — arm 3 read canned answers and reported zero model calls. |
| warm_cache | One client was probed for the manifest while a different one served the run, so the recorded chain was never the chain that answered. |
| provider latency | Retry backoff was measured inside the timed call, so sleep was reported as generation cost — a measurement taken at the wrong boundary. |
| rejection-rate prose | A sentence said 'two times in three' beside a computed 75%. The figure was restated by hand and drifted. |
| Ledger.verify() | Correct, thoroughly tested, and called by nothing. A tampered chain would have been detected and then ignored while repairs kept writing. |

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
