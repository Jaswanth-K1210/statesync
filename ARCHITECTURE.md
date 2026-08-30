# StateSync — architecture and decision log

Companion to `README.md`. This file records **why**, especially where the
obvious choice was wrong.

## The loop

Three systems hold a view of one transaction — the gateway (authoritative on
whether money moved), the merchant's order store (on whether goods were
promised), and the merchant's ledger (on the books). Webhooks synchronise them
with at-least-once delivery, no ordering guarantee, and permanent give-up after
24 hours. They drift.

**Three sources disagree → find the disagreements → explain them → fix or
escalate → report.** One loop, six kinds of disagreement.

## Where the AI sits, and where it does not

| Stage | Deterministic | Model |
|---|---|---|
| Reconciliation, detection | ✅ | |
| Classification — 4 clean classes | ✅ | |
| Classification — ambiguous attribution | verifier | ✅ hypothesis generation |
| Decision to repair, repair execution | ✅ | |
| Audit logging, ops report | ✅ | |

Four classes are set operations. A language model will not beat a set
operation at being a set operation, and the evaluation says so plainly. The
model proposes decompositions of an unexplained residual; a deterministic
verifier accepts one only if it reconciles exactly in paise, cites artifacts
that exist, and declares rates consistent with their own amounts.

---

# Decision log

Each entry is something that cost time to learn and would otherwise be
rediagnosed from scratch.

## 1. The lease must not expire in Redis

The obvious implementation sets `ex=LEASE_SECONDS` on the claim key. That
reintroduces the bug the lease exists to fix: when the lease expires Redis
*deletes the key*, so the next call sees no claim, takes a fresh one, and a
worker that died mid-repair is never surfaced. `STUCK_REPAIR_DETECTED` becomes
unreachable code.

The lease is therefore **logical**: the claim record carries its own timestamp,
is retained far beyond the lease it represents, and expiry is computed on read.
Pinned by `test_the_claim_record_survives_the_lease_so_a_crash_is_detectable`.

`clear_stuck()` is the way out. Without it a single crash poisons that
divergence key for the whole retention window, re-escalating forever.

## 2. Redis contributes no correctness

Mutation testing showed that removing the Redis lease entirely does **not**
produce a double repair — the database unique constraint catches every
duplicate. Redis contributes avoided wasted work and concurrency behaviour.
Say "two layers" only with that distinction attached, because a reviewer
running the same mutation reaches the same conclusion.

## 3. The books record gross revenue and a separate fee expense

Net-revenue booking is a GST filing error, not a simplification: a merchant
owes GST on gross sale value and claims input tax credit on the MDR's GST
separately. Netting them understates output liability.

```
capture  +gross
fee      -(mdr + gst)
------------------------
         = what settled
```

It also makes the residual nameable — the gap between the fee the merchant
booked and the fee the gateway charged, which has causes an ops person can act
on.

## 4. A schedule-derived fee may not assert a discrepancy

The fee schedule is for validation and settlement attribution. It is not a
source of truth about what the gateway charged. Using an *estimated* fee to
flag books that recorded no fee at all marks every unbooked-fee payment as a
divergence — guessing by the back door. A schedule-priced payment raises
`AMOUNT_MISMATCH` only when the merchant actually booked a fee line to
disagree with.

## 5. Range checking a rate is not enough

The verifier originally checked only that a declared rate sat inside a
permitted range. A decomposition could then claim "MDR at 2%" for an amount
nothing like 2% of the payment: the sum reconciles, the artifact exists, the
rate is plausible, and the explanation is fiction. That is exactly the
coincidental fit bounded search is meant to avoid.

`RATE_INCONSISTENT` requires a declared rate to reproduce its own amount, with
GST checked against the MDR rather than the transaction. It fires on real model
output.

## 6. A CDN blocking `Python-urllib` looks exactly like a bad key

Groq returned `HTTP 403` with Cloudflare code `1010` for every request. The
obvious diagnosis is a bad credential, and rotating a perfectly good key burns
an afternoon and teaches nothing.

The cause was the default `User-Agent` of `Python-urllib/3.x`, which the CDN
rejects outright. Sending a real User-Agent fixed it, and the next error was
the genuinely useful one: the configured model did not exist on that account.

**Diagnostic order for a provider 403:** send a real User-Agent first, then
check the model list, then suspect the key. Terminal statuses (401/402/403/404)
disable a link for the run rather than being re-probed on every prompt.

## 7. Adding an API key must not change test behaviour

With keys present, `resolve_client()` returned a live chain *inside the test
suite*, and `make verify` began making hundreds of real API calls with backoff
— slow, billable, and liable to fail on someone else's rate limit. It would
have made "fresh clone → `make verify` green" a lie on any machine with a key.

`tests/conftest.py` pins `STATESYNC_LLM_OFFLINE=1` for the session and
`resolve_client()` honours it before anything else. `test_suite_is_hermetic.py`
asserts both the effect and the mechanism, so deleting the fixture fails
loudly.

## 8. A live failure is corroboration, never proof

A real run showed OpenRouter returning 402 and the chain falling through to
Groq. That is recorded in `llm_cache/OBSERVED_EVENTS.json` as dated evidence —
but the property is proven by `tests/chaos/test_provider_fallback.py`, which
injects the 402 deterministically. Relying on the accident would mean the
chain's most interesting behaviour became unobservable the moment someone
topped up the account.

Same move as `late_arrivals` in the hard-case injector: a metric that can only
be non-zero when the world cooperates is a metric that ships as a permanent
zero.

## 9. Warm runs measure the cache, not the model

Every response is cached and committed, so on any run after the first the
provider is never reached: calls read zero, arm 3 throughput approaches arm 2,
and cost per 1,000 records reads zero. All three measure cache hits.

Cold and warm are counted and reported separately, and
`llm_cache/MANIFEST.json` records which client and **which model** produced the
cache. Wall clock alone is contaminated by failed links and their backoff, so
provider latency and chain overhead are split.

**Determinism rests on the cache, not on the model.**

## 10. A value computed upstream must never be re-derived downstream

Case 13's escalation packet was built, was correct, and was ignored on the way
to `exceptions.csv`, which re-derived the reason code from a classifier
default. Every per-component test passed; the artifact a reviewer opens was
wrong.

This recurred three more times — the per-class rate derived in the template,
hard-case packets built without the base amount the eval passed, and
`warm_cache` probing one client while measuring another.
`tests/integration/test_reporting_consistency.py` asserts agreement *across*
outputs rather than within any one of them. Extend it for every new surface,
the Phase 6 API especially.


## 11. The verifier is sound but not complete

It accepts nothing that fails to reconcile, cite, and price consistently — so
a fabricated explanation cannot get through. **It cannot detect an explanation
that was never proposed.** Ambiguity is only visible among generated
candidates, which means a proposer that misses the second valid decomposition
turns a correctly-ambiguous case into false confidence.

This is not hypothetical. Hard case 13 is built so that two decompositions
reconcile exactly. When the model provider *replaced* the deterministic
candidate set, it found neither, and a correct
`AMBIGUOUS_MULTIPLE_VERIFIED` became `NO_HYPOTHESIS_VERIFIED` — the system went
from knowing it could not resolve the case to wrongly believing it had. A
weaker proposer makes this *more* likely, not less.

Two consequences, both load-bearing:

1. **Candidate sets are pooled, never replaced.** Deterministic hypotheses stay
   in the pool and model hypotheses are added to it, so the model can add a
   resolution but can never remove an ambiguity the deterministic set already
   established. Pooling may push other cases *into* ambiguity that previously
   read as resolved; that is correct behaviour. More escalations with sound
   reasoning beats fewer with unsound ones.

2. **Say the limit out loud.** The claim this project rests on is that the
   system knows when it does not know. That claim is bounded by what was
   proposed, and stating the bound is the difference between it reading as
   sophistication and reading as a hole someone else found.

Before pooling, hc13's designed outcome had **no coverage at all** in the arm
being shipped, because the fixtures that create the ambiguity were displaced.
`test_case_13_stays_ambiguous_in_the_model_arm` closes that.


## 12. Seven bugs, one shape

Entry 10 described this pattern with two examples. By the end of the build it
had recurred five more times, and the canonical list lives in
`docs/seam_bugs.json` — pointed at rather than restated here, because writing
the count in two places is the pattern itself.

**The shape:** a value computed correctly in one place, then re-derived,
misrouted, or ignored somewhere else.

**What makes it hard to see:** every component test passed, every time. The
packet was built correctly. The chain verified correctly. The provider was
constructed correctly. The latency was timed correctly. Each piece did its job,
and the failure lived in the seam between two correct pieces — which is exactly
where unit tests do not look.

**What catches it:** tests that assert agreement *across* outputs rather than
correctness within one. `tests/integration/test_reporting_consistency.py` is
that suite: the CSV's reason codes against the reported tallies, the row count
against the summary line, detected against the per-class sum, the rendered
report against the result it was built from.

The last instance is the one to remember. `Ledger.verify()` was correct,
thoroughly tested, and called by nothing — so a tampered chain would have been
detected and then ignored while repairs kept writing. The most important
guarantee in the design existed as prose for six phases, and no component test
could have told us, because no component was wrong.
