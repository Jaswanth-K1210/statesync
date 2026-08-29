# Carried into Phase 4 — resolved

Requirements discovered during Phases 2–3 that Phase 4 must satisfy. Written
down at the moment they were found, because each one ships as a silent defect
if it is only remembered.

## 1. Hard-case timestamps must be relative to run time

**Requirement:** generate hard-case timestamps as offsets from the run clock,
not from fixed past dates.

**Why:** `BASE_TIME` is a fixed anchor in the past, and the eval reconciles at
`BASE_TIME + 30 days`. Nothing is ever inside the staleness window, so the
window never fires and `transient_filtered` ships as a permanent zero. That
metric is the direct evidence the two-run confirmation is load-bearing; a
constant zero makes it look like dead code.

**Resolved.** A fixed timestamp was only half the problem: the batch was also
*static* between passes, so a divergence seen on pass 1 was still there on pass
2 and nothing could ever be filtered. `HardCaseBatch.late_arrivals` models a
webhook that lands between passes — real when first seen, gone before a repair
could be authorised. `transient_filtered` is now 1 on a 500-record run, and
`test_the_staleness_window_filters_a_transient_in_the_eval` pins it.

## 2. In-flight payments are a hard case, not baseline data

The clean generator emits only `captured` / `refunded` / `failed`. An
authorised-but-uncaptured payment is not agreement between the three views —
it is `ORDER_NO_CAPTURE` once its window elapses, and a normal in-progress
payment before that. Phase 4 introduces it deliberately, with fresh
timestamps, as hard case 12 (order cancelled while payment in flight).

The authorisation window is gated on the *most recent* evidence of activity —
`max(order.created_at, payment.status_changed_at)` — so a retry authorised
minutes ago against an hours-old order is not flagged. See
`test_a_recent_authorisation_on_an_old_order_is_not_a_divergence`.

## 3. The match rate needs its framing wherever it appears

Four classes are exact set operations, so 100% is the expected floor rather
than an achievement. The eval report says so under "Reading the match rate",
and the README and video must say the same thing before a reviewer says it
first. Measurement that means anything starts with the ambiguous cases.

## 4. Throughput is measured in-memory

`ArmResult.storage` records this and `test_the_eval_runs_in_memory_and_says_so`
pins it. If Phase 3+ moves reconciliation behind the Postgres ledger store,
that field must change with it, and the README figure must be relabelled.


---

## Carried into Phase 5

**1. The hypothesis provider seam is already in place.** `HypothesisProvider`
is a Protocol; `FixtureHypothesisProvider` is the deterministic implementation
the eval and demo use. Phase 5 adds `LLMHypothesisProvider` behind the same
interface. The verifier, the escalation packet and cases 13/14 are already
proven against fixtures, including a set shaped exactly like what a
hallucinating provider emits
(`test_a_hallucinating_provider_is_rejected_wholesale`).

**2. Do not route the four clean classes through the LLM.** They are exact set
operations resolved by `classifier/deterministic.py`. Only `AMOUNT_MISMATCH`
and `SETTLEMENT_GAP` reach a provider, and only the *residual* does — known fee
and tax are subtracted first, deterministically.

**3. `SETTLEMENT_GAP` is still unimplemented.** It is in the taxonomy and the
gate refuses to auto-repair it, but nothing detects it yet. Phase 5 either
implements it against synthetic payout data — labelled *demonstrated, not
validated*, reported separately, never merged into the headline — or reports it
as `NOT_IMPLEMENTED` with the reason.

**4. Fee-schedule coverage is not yet reported.** The generator omits fee data
on 15% of payments and the reconciler treats missing fee data as zero, so those
payments cannot currently raise `FEE_SCHEDULE_UNKNOWN`. Phase 5 must add the
schedule lookup and report coverage as a first-class metric: a system claiming
95% verification while silently escalating the 40% it could not price is not
honest.
