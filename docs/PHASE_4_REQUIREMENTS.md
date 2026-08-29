# Carried into Phase 4

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

**Test to add:** `test_transient_divergences_are_filtered_in_the_eval` —
assert `transient_filtered > 0` on a batch containing fresh in-flight records.

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
