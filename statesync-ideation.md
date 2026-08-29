# StateSync

**Payment/order divergence detection and idempotent repair for Razorpay merchants**

Razorpay AI Buildathon · Track 04 — AI Finance Controller
Jaswanth Koppisetty

*Version 3 — revised after two rounds of technical review.*
*v2: AI role narrowed and justified (§6), idempotency corrected (§7.3), evidence hierarchy reordered (§3), evaluation split by difficulty (§10), schedule gated (§13).*
*v3: fee provenance specified (§6.1), hypothesis bounds and escalation packets specified (§6.7), hash chain construction specified (§7.6), staleness window and two-run confirmation added (§7.1), `SETTLEMENT_GAP` scope decided up front (§6.5), ₹-at-risk metric given a denominator (§10).*

---

## 1. One line

When money moves at Razorpay but the merchant's database doesn't hear about it, StateSync finds the gap, works out why, fixes what is provably safe to fix, and hands a human the rest — with a proof that it never fixed the same thing twice.

---

## 2. Explaining it to someone with no technical background

### The restaurant

Imagine a restaurant where the **cash counter** and the **kitchen** are in two separate buildings, connected by a runner who carries paper slips.

A customer pays at the counter. The counter writes a slip. The runner takes it to the kitchen. The kitchen cooks the food.

Almost always this works. But:

- **Sometimes the runner drops a slip.** The customer paid. The kitchen never heard. The customer waits, gets nothing, and eventually shouts.
- **Sometimes the runner delivers the same slip twice.** The kitchen cooks two plates for one payment.
- **Sometimes the runner has a bad day** and stops running entirely. Nobody notices for hours, because the counter keeps taking money quite happily.
- **Sometimes a refund is given at the counter** and nobody tells the accounts book, so the books say the restaurant earned money it actually gave back.
- **And sometimes the numbers just don't line up** — the counter says ₹4,000, the accounts book says ₹3,894.20 — and nobody can say whether the difference is the card company's commission, a partial refund, or money that has genuinely gone missing.

Right now, restaurants handle all of this by waiting for a customer to complain, then having a staff member walk between the two buildings comparing the cash drawer to the kitchen's list, by hand, one order at a time.

**StateSync is a manager who does that walk automatically, every few minutes.**

It compares three records — what the counter says, what the kitchen says, and what the accounts book says — finds every mismatch, works out which of the problems above occurred, fixes the ones that are provably safe to fix, and puts the genuinely confusing ones on a list for a human with a note explaining what it saw.

It keeps a tamper-proof logbook of everything it did, so if anyone asks "why did you cook that extra plate," there's an answer.

### Where the intelligence actually is

Four of those five problems are simple bookkeeping. You compare two lists and see what's missing. Any careful clerk can do it, and so can ordinary software.

**The fifth one is different.** When the counter says ₹4,000 and the book says ₹3,894.20, that ₹105.80 gap could be several different things — commission plus tax on the commission, a partial refund, a rounding difference across a split payment, a fee for instant settlement. Several of those explanations produce *almost the same number*. Working out which one it actually was means proposing an explanation and then checking whether the arithmetic works out exactly.

That is the one place StateSync uses AI: **generating candidate explanations for an ambiguous gap.** And critically, it never trusts them — a separate, ordinary piece of arithmetic checks whether the proposed explanation adds up to the paisa. If it doesn't, the explanation is thrown away and a human gets the case.

The AI suggests. The calculator decides.

### The one-sentence version

> "Payment companies and online shops keep separate records of the same transaction, those records drift apart more often than you'd think, and today a human has to notice and fix it. StateSync notices and fixes it instead — and when the difference is genuinely ambiguous, it proposes explanations but only accepts the one that adds up exactly."

---

## 3. The problem, with evidence

Every failure mode below is publicly documented, most of them in **Razorpay's own GitHub repositories**.

### 3.1 Primary evidence — the lose-lose fork

[razorpay/razorpay-magento issue #208](https://github.com/razorpay/razorpay-magento/issues/208)

A merchant enables webhooks on `payment.authorized`, `payment.captured` and `order.paid`. Result: some orders are created twice as duplicates.

So they disable webhooks. Result: orders go missing despite payment being made.

**There is no correct setting.** The merchant is choosing between double-charging customers and losing paid orders. This is the problem in its purest form, reported by a real merchant, in Razorpay's own issue tracker. Lead every conversation about this project with it.

### 3.2 Webhooks that switch themselves off

[razorpay/razorpay-python issue #286](https://github.com/razorpay/razorpay-python/issues/286)

Repeated webhook failures cause the webhook to be automatically disabled, and the only way to re-enable it is manually, from the dashboard. The developer's objection is exactly right: one problematic customer causing failures shouldn't take the webhook down for every other customer.

[Razorpay's webhook documentation](https://razorpay.com/docs/webhooks/faqs/) confirms the design: retries on exponential backoff for 24 hours, then disabled pending manual re-enable. **Bulk replay of missed events is not supported** — recovery means a support ticket per event.

**What that means operationally:** your webhook endpoint has a bad deploy at 2am and is dead for six hours. Every payment event in that window is gone. You cannot bulk-replay them. Meanwhile money moved and your database has no idea.

That six-hour window is exactly what StateSync closes.

### 3.3 The handler itself crashes

[razorpay/razorpay-woocommerce issue #544](https://github.com/razorpay/razorpay-woocommerce/issues/544)

A fatal error inside the webhook handler, plus 24-hour delivery-failure emails. The failure isn't always the network — sometimes it's the merchant's own code dying mid-write, leaving partial state. This is the origin of the hardest divergence class.

### 3.4 Secondary evidence — manual reconciliation is a staffed function

[Razorpay job posting — Analyst, Financial Operations, Bangalore, entry level](https://builtin.com/job/analyst-financial-operations/7422098)

The role covers resolving merchant, customer and bank queries on settlement, refunds and transactions, and working with the Finance team on reconciliation.

**State the scope honestly.** This posting is primarily about *gateway-side* reconciliation — Razorpay against banks — and only partly about merchant-raised queries. It does not directly prove the merchant-side order-store divergence StateSync addresses. What it does establish is that reconciliation exception handling in this ecosystem is a manual, staffed function rather than a solved one.

Use it as supporting context, never as the headline claim. A reviewer who knows the org chart will notice if it's oversold, and overselling your strongest-sounding evidence is a worse outcome than not using it.

---

## 4. Why the problem exists (technical root)

Three independent systems hold a view of the same transaction:

1. **The gateway** (Razorpay) — authoritative for whether money moved
2. **The merchant's order store** — authoritative for whether goods were promised
3. **The merchant's ledger** — authoritative for what the books say

They're synchronised by webhooks: HTTP calls over an unreliable network into merchant code of unknown quality.

Webhooks provide **at-least-once delivery, no ordering guarantee, and eventual give-up.** Therefore:

- At-least-once → duplicates are expected, not exceptional
- No ordering → `payment.captured` can arrive before `order.paid`
- Eventual give-up → after 24 hours of failure, events are simply lost

Any system built on those three properties **will** drift. The question isn't whether divergence happens — it's whether anyone notices before a customer does.

---

## 5. The divergence taxonomy

Six classes. Each needs a different repair, and each has a dangerous failure mode if the class is wrong.

| Class | What happened | Correct repair | Danger if misclassified | Detectable by rules? |
|---|---|---|---|---|
| `CAPTURED_NO_ORDER` | Money captured, webhook dropped, no order | Create order idempotently from payment record | Duplicate if webhook later replays | **Yes** — set difference |
| `ORDER_NO_CAPTURE` | Order created client-side, payment never completed | Expire order, release inventory | Cancels an order that was paid | **Yes** — set difference + timeout |
| `DUPLICATE_ORDER` | Same webhook delivered twice | Merge on `razorpay_payment_id`, void later record | Merges two genuinely separate orders | **Mostly** — exact match, but see §6.3 |
| `REFUND_NOT_REFLECTED` | Refund at gateway, ledger unchanged | Post compensating ledger entry | Double refund — real money out | **Yes** — join |
| `AMOUNT_MISMATCH` | Order total ≠ settled amount | Attribute the delta, then reconcile or escalate | Silently accepts a wrong amount | **No** — see §6 |
| `SETTLEMENT_GAP` ⚠️ | Payout ≠ Σ(captures − refunds − fees) | Attribute components, escalate remainder | Masks a genuine shortfall | **No** — see §6 |

⚠️ **`SETTLEMENT_GAP` is tested against synthetic payout data only.** Razorpay's sandbox does not produce real settlement behaviour. See §6.5 — this is stated up front rather than discovered at demo time.

**Read the last two columns together.** Four classes are deterministic set operations — no model required, and claiming otherwise would be dishonest. Two are genuinely hard. That asymmetry is not a weakness in the design; it *is* the design, and §6 is where the project earns its keep.

---

## 6. Where AI actually earns its place

This section exists because the obvious criticism of a reconciliation project is: *you used a language model to do string comparison.* That criticism is fair for four of the six classes. Here is the honest answer for the other two.

### 6.1 Where fee data comes from — specified, not assumed

The ambiguity in `AMOUNT_MISMATCH` is only real if you're honest about what the system does and doesn't know about fees. Three sources, in priority order:

**Source 1 — the payment entity itself (primary).** Razorpay's Payment object carries `fee` and `tax` fields for the individual transaction. Where present, these are authoritative and require no inference at all: subtract them deterministically before anything else runs.

**Source 2 — merchant-configured fee schedule (fallback and validation).** A versioned config keyed by `(instrument, card_type, mcc, tier)` with effective-from dates:

```yaml
fee_schedule:
  version: 3
  effective_from: 2026-04-01
  rates:
    upi:            { mdr_pct: 0.00, gst_pct: 18 }
    card_domestic:  { mdr_pct: 2.00, gst_pct: 18 }
    card_intl:      { mdr_pct: 3.00, gst_pct: 18 }
    emi:            { mdr_pct: 2.50, gst_pct: 18 }
  addons:
    instant_settlement: { pct: 0.30, gst_pct: 18 }
```

**Source 3 — unknown.** Some instrument, MCC, or negotiated rate isn't covered.

**What happens when fee data is missing.** The verifier does not guess, and it does not silently reject. It emits a distinct outcome:

```
FEE_SCHEDULE_UNKNOWN → escalate with reason code, attach the instrument
                       and what config would be needed to resolve it
```

This is reported as a first-class metric: **fee-schedule coverage** — the percentage of `AMOUNT_MISMATCH` cases where fee data was available at all. A system that verifies 95% of cases at 90% coverage is honest. A system that reports 95% verification while silently escalating the 40% it couldn't price is not.

**A correction worth stating plainly**, because a reviewer may raise the fee-schedule problem as fatal: it isn't, because per-payment `fee` and `tax` come off the payment object rather than requiring a schedule. The schedule is needed for *validating* those figures and for settlement-level attribution, not for the base case. **Day 1 task: confirm whether test mode populates `fee` and `tax`.** If it doesn't, Source 2 becomes primary and coverage must be reported prominently rather than as a footnote.

### 6.2 Why `AMOUNT_MISMATCH` is genuinely hard

Deterministic subtraction runs first. Order total ₹4,000.00, known `fee` + `tax` = ₹94.40, amount landed ₹3,894.20.

```
4000.00 − 94.40 = 3905.60 expected
                  3894.20 observed
                  ─────────
                    11.40 residual, unexplained
```

**The AI's job is the residual, never the known part.** Candidate explanations for ₹11.40:

| Hypothesis | Arithmetic |
|---|---|
| Instant-settlement fee, 0.30% + GST | 11.70 × ... ≈ ₹11.40 depending on base |
| Rounding across a split payment on two instruments | paisa-level, varies |
| Partial refund of ₹9.66 + MDR adjustment on the remainder | ≈ ₹11.40 |
| A prior-cycle adjustment netted into this payout | unbounded |
| Genuine shortfall | unexplained |

Several land within paise of each other. **The system is under-determined** — multiple combinations of fees, refunds, rounding and adjustments reconcile to nearly the same residual, and the correct decomposition depends on the merchant's settlement configuration and what else happened on that transaction.

A rules-only approach must enumerate every combination in advance. The space is combinatorial and merchant-specific, which is exactly where hand-written rules rot.

### 6.3 The propose-verify architecture

**The LLM does hypothesis generation over an under-determined attribution problem. A deterministic verifier accepts only hypotheses that reconcile exactly.**

```
Divergence + residual + known fee/tax + fee schedule
+ refund history + adjacent transactions
        ↓
Deterministic pre-subtraction of everything known
        ↓
LLM proposes candidate decompositions of the RESIDUAL ONLY,
each citing which artifacts support it
        ↓
Deterministic verifier:
    Σ(claimed components) == residual, to the paisa?
    every cited artifact actually exists?
    every component within its permitted range?
        ↓
Exactly one verifies    →  classify, repair, log
Zero verify             →  escalate, all rejected hypotheses attached
More than one verifies  →  escalate as ambiguous, both attached
Fee data missing        →  escalate as FEE_SCHEDULE_UNKNOWN
```

The model performs **search over an explanation space**, which is a real inference task. It does not do arithmetic, string matching, or decide whether money moves — the verifier does all of that deterministically and rejects anything that doesn't add up to the paisa.

A hallucinated decomposition fails verification by construction. That's a structural guarantee, not a prompt instruction.

### 6.4 Honest note on `DUPLICATE_ORDER`

Exact `razorpay_payment_id` matching handles the common case, but a genuine adversarial case exists: two legitimate orders from the same customer, same amount, seconds apart, where a naive dedupe on customer+amount+timestamp would wrongly merge them. The injector includes this as a false-positive test. **Merge only on payment ID, never on heuristic similarity.**

### 6.5 `SETTLEMENT_GAP` — scope decided up front

Razorpay's test mode does not produce realistic settlement behaviour. There is no genuine T+2 cycle, no rolling reserve, no payout webhook carrying real fee deductions. Any settlement data used here is **manufactured by me, not observed from the sandbox.**

Rather than leave this as a day-8 cliff-edge, the decision is made now:

- `SETTLEMENT_GAP` **stays in the taxonomy**, because it's where the propose-verify architecture generalises and the marginal cost is low.
- It is **labelled synthetic-only** in §5, in the README, and in the results table.
- Its metrics are **reported separately** from classes tested against real sandbox behaviour, and never merged into a headline accuracy figure.
- The README states: *"Settlement-level attribution is demonstrated against synthetic payout data. The architecture extends to it via the same propose-verify path, but it is not validated against real Razorpay settlement behaviour, which the sandbox does not expose."*

Demonstrated, not validated. Say both words.

### 6.6 What this means for the metrics

Report classification performance **split by case type**:

- Clean single-class divergences → expect rules and LLM to tie. Say so.
- Ambiguous attribution cases → where the propose-verify layer earns its place or doesn't.

AI-layer-specific metrics:

- **Fee-schedule coverage** — % of cases where fee data was available at all
- **Hypothesis verification rate** — % of proposals surviving the verifier
- **Resolution split** — % of `AMOUNT_MISMATCH` resolved by rules / by verified hypothesis / escalated
- **Verifier rejection rate** — how often the model proposed something that didn't add up. Report it; it's the evidence the guard is load-bearing.

### 6.7 Hypothesis bounds and escalation packets

**The epistemic point first, because it changes what the system is allowed to claim.** The system can never conclude "no explanation exists." It can only conclude "**no generated hypothesis verified.**" Every escalation message uses that wording. Anything stronger is a claim the architecture cannot support.

**Generation strategy — bounded, with exactly one retry:**

```
Pass 1: generate up to N=5 hypotheses, temperature 0.7 for diversity
        ↓ if none verify
Pass 2: retry ONCE with the rejected hypotheses and the component
        taxonomy in the prompt — "none of these verified; the residual
        is ₹X; consider components you did not use: [fees, refunds,
        chargebacks, reserves, rounding, adjustments, FX]"
        ↓ if none verify
STOP. Escalate as NO_HYPOTHESIS_VERIFIED.
```

Hard cap: **two passes, ten hypotheses, per divergence.** No adaptive retry loops. An unbounded search over an under-determined problem will always eventually produce something that reconciles by coincidence, and a coincidental fit is worse than an escalation.

**Escalation packet contents.** An escalation that says only "ambiguous" hands the human back the same under-determined problem the system just declined. Every packet carries:

| Field | Content |
|---|---|
| `divergence` | IDs, amounts, timestamps, instrument |
| `known_components` | Fee/tax deterministically subtracted, with source |
| `residual` | The exact unexplained amount |
| `hypotheses[]` | **Every** hypothesis generated, verified and rejected alike |
| — `components` | Named component + amount for each |
| — `arithmetic` | The sum, shown, and whether it matched to the paisa |
| — `citations` | Which artifacts each component claims to rest on |
| — `verdict` | `VERIFIED` / `ARITHMETIC_FAILED` / `ARTIFACT_MISSING` / `RANGE_VIOLATION` |
| `reason_code` | `AMBIGUOUS_MULTIPLE_VERIFIED` / `NO_HYPOTHESIS_VERIFIED` / `FEE_SCHEDULE_UNKNOWN` |
| `suggested_action` | Per reason code, from a fixed template — not model-generated |

**Case 13 (two hypotheses both verify):** both are attached with full arithmetic, so the ops person distinguishes "MDR + instant settlement" from "partial refund + MDR on remainder" — which need different follow-ups — rather than re-deriving them.

**Case 14 (nothing verifies):** all ten rejected hypotheses are attached with the reason each failed. A human seeing that seven were close but off by consistent paise learns something a bare "escalated" never conveys.

## 7. Architecture

```
Razorpay test-mode API ──┐
Merchant order store ────┼──→  Three-Way Reconciler
Internal ledger ─────────┘            │
                                      ↓
                            Divergence Detector
                        (set operations + invariants)
                                      ↓
                    ┌─────────────────┴─────────────────┐
        Deterministic classifier              Ambiguous cases only
        (4 clean classes)                     (AMOUNT_MISMATCH,
                    │                          SETTLEMENT_GAP)
                    │                                   ↓
                    │                     LLM hypothesis generator
                    │                                   ↓
                    │                     Deterministic verifier
                    │                     (exact arithmetic, artifact
                    │                      existence, range checks)
                    └─────────────────┬─────────────────┘
                                      ↓
                            Repair Policy Engine
                              (DETERMINISTIC)
                     confidence gate · blast-radius cap
                     value threshold · idempotency claim
                                      ↓
                    ┌─────────────────┴─────────────────┐
              Bounded Repair                      Exception Queue
        (idempotency key + DB constraint)   (with reason + rejected
                                             hypotheses attached)
                                      ↓
                          Hash-Chained Audit Ledger
                                      ↓
                    Templated Ops Report (§7.6)
```

### 7.1 Staleness window and two-run confirmation

The reconciler treats the gateway as authoritative, but **during a state transition the gateway does not yet have a consistent answer.** A payment reported `captured` by one call can read `authorized` on another within a short window while state propagates asynchronously. Reconciling into that window generates false divergences, and a false divergence that triggers a repair makes state worse than leaving it alone.

Two defences, both cheap:

**1. Staleness window.** No payment is reconciled until it has been terminal for `SETTLEMENT_STALENESS = 15 minutes`. Anything more recent is skipped and picked up on the next pass.

```python
def eligible_for_reconciliation(payment, now):
    if payment.status not in TERMINAL_STATES:
        return False
    return (now - payment.status_changed_at) > STALENESS_WINDOW
```

**2. Two-run confirmation before any repair.** A divergence must be observed on **two consecutive reconciliation passes**, separated by at least the staleness window, before the policy engine will authorise a repair. First observation writes a `DIVERGENCE_OBSERVED` ledger entry; only the second promotes it to `DIVERGENCE_CONFIRMED`.

This costs one cycle of latency and eliminates the entire class of transient-state false positives. The tradeoff is stated explicitly rather than hidden: **StateSync is not a real-time system, deliberately.** It trades minutes of detection latency for the guarantee that it doesn't repair a payment that was merely mid-flight.

Report **transient divergences filtered** as a metric. It's direct evidence the window is doing work.

### 7.2 The invariant that anchors everything

```python
def ledger_invariant(wallet):
    expected = wallet.initial - sum(t.amount for t in wallet.txns if t.ok)
    return abs(wallet.balance - expected) < TOLERANCE
```

If this doesn't hold, something is wrong regardless of what any individual record says. It catches divergences the three-way comparison misses.

### 7.3 Two layers of idempotency, not one

Redis is a cache. It can be flushed, evicted, or partitioned. **It must never be the only thing standing between you and a double repair.**

**Layer 1 — database constraint (the real guarantee).**

```sql
ALTER TABLE orders
  ADD CONSTRAINT uniq_rzp_payment UNIQUE (razorpay_payment_id);

ALTER TABLE ledger_entries
  ADD CONSTRAINT uniq_repair_key UNIQUE (repair_key);
```

Every repair writes with a deterministic key. If Redis is wiped and every repair is attempted again, the database rejects the duplicates. The guarantee lives in the schema, where it can't be lost.

**Layer 2 — Redis lease (the optimisation).** Prevents concurrent workers from doing wasted work and gives fast short-circuit on re-runs.

### 7.4 Corrected idempotency implementation

The naive `setnx` pattern has a real hole: if execution crashes *after* claiming the key but *before* writing the result, the next call sees the claim, reads a missing result, and silently returns nothing — a stuck repair with no alerting path, forever.

The fix is an explicit state machine plus a lease with expiry:

```python
LEASE_SECONDS = 300

def repair(divergence):
    key = f"repair:{divergence.deterministic_key()}"

    claimed = redis.set(
        key,
        json.dumps({"state": "CLAIMED", "at": now(), "worker": WORKER_ID}),
        nx=True, ex=LEASE_SECONDS,
    )

    if not claimed:
        rec = json.loads(redis.get(key))

        if rec["state"] == "SUCCEEDED":
            return Result.from_record(rec)          # true idempotent replay

        if rec["state"] == "FAILED":
            return Result.failed(rec)               # do not silently retry

        # state == CLAIMED
        if lease_expired(rec, LEASE_SECONDS):
            # a previous worker died mid-execution — never swallow this
            ledger.append(STUCK_REPAIR_DETECTED, key=key, prior=rec)
            alert("stuck repair", key=key)
            return Result.escalate("STUCK_REPAIR")

        return Result.in_progress()                 # live worker holds the lease

    try:
        res = execute_repair(divergence)            # writes carry the DB constraint
        redis.set(key, json.dumps({
            "state": "SUCCEEDED", "at": now(), "result": res.serialize()
        }))                                          # no TTL — permanent record
        ledger.append(REPAIR_SUCCEEDED, key=key, result=res)
        return res

    except DuplicateKeyError:
        # DB constraint caught it — the repair already happened
        redis.set(key, json.dumps({"state": "SUCCEEDED", "at": now(),
                                   "note": "db_constraint_dedup"}))
        ledger.append(REPAIR_DEDUPED_BY_DB, key=key)
        return Result.already_applied()

    except Exception as e:
        redis.set(key, json.dumps({"state": "FAILED", "at": now(), "err": str(e)}))
        ledger.append(REPAIR_FAILED, key=key, error=str(e))
        raise
```

Three properties worth stating explicitly in the README:

1. **A crash mid-execution surfaces as an escalation, never as silence.** The expired lease is detected and alerted.
2. **A wiped Redis cannot cause a double repair.** The database constraint catches it, and the catch is logged rather than swallowed.
3. **Every state transition is written to the audit ledger**, so the history is reconstructable from the ledger alone.

### 7.5 Where the AI sits, and where it doesn't

| Stage | Deterministic | AI |
|---|---|---|
| Reconciliation | ✅ | |
| Detection | ✅ | |
| Classification — 4 clean classes | ✅ | |
| Classification — ambiguous attribution | verifier | ✅ **hypothesis generation** |
| Decision to repair | ✅ | |
| Repair execution | ✅ | |
| Audit logging | ✅ | |
| Ops report | ✅ (templated) | ✅ (prose only, §7.6) |

The model proposes explanations for genuinely under-determined gaps. It never decides whether money moves, and it never produces a number that reaches a human unverified.

### 7.6 The ops report — numbers are templated, never generated

An unfaithful summary of a financial reconciliation run is a liability, not a UX bug. If a report says "3 divergences repaired" when there were 5, an ops person closes the ticket without investigating.

Therefore:

- **All counts, amounts, IDs, and statuses come from templates populated directly from the audit ledger.** No model output is involved in producing any figure.
- **The LLM writes prose only**, and only for individual escalated exceptions — a short "what likely happened and what to check" note.
- **A guard runs on that prose**: any numeral, currency amount, or ID appearing in the generated text is checked against the source record. If it isn't present there, the text is rejected and the exception ships with the template alone.

The failure mode is bounded by construction: the worst case is a missing explanatory note, never a wrong number.

### 7.7 The audit ledger — chain construction specified

Asserting "hash-chained ledger" is not a design. The construction is specified here because the §11 fail-closed invariant depends on it, and because non-canonical serialisation produces spurious chain breaks that pass in testing (you control everything) and fail in the demo (you serialise slightly differently somewhere).

**Chain construction:**

```python
GENESIS = "0" * 64

def canonical(event: dict) -> bytes:
    # sort_keys is the whole point — dict insertion order must not
    # affect the hash. Fixed separators, explicit UTF-8, no NaN.
    return json.dumps(
        event, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")

def chain_hash(prev_hash: str, event: dict) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonical(event)).hexdigest()
```

**Decisions, stated:**

| Question | Answer |
|---|---|
| Hash function | SHA-256 |
| Chained input | `prev_hash` (ascii) ‖ canonical JSON of the event body |
| Serialisation | `sort_keys=True`, `separators=(",",":")`, UTF-8, `allow_nan=False` |
| Amount representation | **Integer paise, never float.** Floats are not canonically serialisable |
| Timestamps | ISO-8601 UTC with explicit `Z`, second precision |
| Storage | Dedicated append-only table in the same Postgres instance, `INSERT`-only; `UPDATE`/`DELETE` revoked at the role level |
| Genesis | 64 zeros |
| Verification | **Full chain walk at the start of every run.** At these volumes it's milliseconds — spot-checking would be a false economy |
| On break | Halt all repairs, alert, exit non-zero. Fail closed (§11) |

**What goes in an entry:** `seq`, `prev_hash`, `hash`, `event_type`, `divergence_key`, `repair_key`, `actor`, `payload`, `created_at`. Every state transition in §7.4 writes one, so the full repair history is reconstructable from the ledger alone.

**Why the float rule matters more than it looks.** `0.1 + 0.2` does not serialise identically everywhere, and a single float amount anywhere in an event body makes the chain non-reproducible across environments. All money is integer paise throughout the system, converted for display only.

---

## 8. Data strategy

Most buildathon entries will fabricate their dataset and a panel will discount it. StateSync has a way around that.

**Layer 1 — real Razorpay test-mode transactions.** Create orders, capture payments, issue refunds against the actual sandbox. Real API responses, real webhook payloads, real edge cases.

**Layer 2 — deliberately injected divergence.** Drop webhooks, deliver them twice, deliver out of order, kill the handler mid-write.

**This is the key insight: you don't need labelled data because you caused the divergence.** Ground truth is perfect and honest — you know exactly what you broke, so you know the correct classification.

That single choice sidesteps the "precision and recall against what ground truth?" problem that sinks most submissions in the risk track.

**Layer 3 — synthetic volume.** Scale to 500+ records with realistic distributions.

### 8.1 Clean injectors

```python
class DivergenceInjector:
    """Ground truth is known because we caused it."""
    def drop_webhook(self, payment_id):            ...  # CAPTURED_NO_ORDER
    def duplicate_webhook(self, payment_id, n=2):  ...  # DUPLICATE_ORDER
    def abandon_after_order_create(self, order_id):...  # ORDER_NO_CAPTURE
    def refund_without_ledger_write(self, pid):    ...  # REFUND_NOT_REFLECTED
```

### 8.2 Hard cases — required, not optional

A test set of clean single-class divergences will produce excellent metrics and tell you nothing. Real divergence is compound and ambiguous. **At least 15 of these must be in the batch**, and results must be reported separately for them.

| # | Scenario | Tests |
|---|---|---|
| 1 | Partial handler write — order row created, line items missing, payment captured | Compound state, not any single class |
| 2 | Refund and second payment attempt racing on the same order ID | Ordering sensitivity |
| 3 | Capture during network partition — gateway captured, merchant timed out and retried | Uncertain gateway state |
| 4 | Duplicate webhook where the two deliveries carry *different* payloads | Which delivery is authoritative? |
| 5 | Delta exactly explained by MDR + GST | Should classify as fee, **not** a divergence |
| 6 | Delta explained by MDR + GST except for ₹0.60 | Rounding or genuine shortfall? |
| 7 | Two legitimate orders, same customer, same amount, 2s apart | **False-positive test** — must NOT merge |
| 8 | Refund issued, then reversed by the bank | Direction reversal |
| 9 | Settlement including a chargeback debit from a prior cycle | Cross-cycle attribution |
| 10 | `refund.processed` arrives before `payment.captured` | Out-of-order delivery |
| 11 | Webhook replay 26 hours later, after manual re-enable | Past the disable window |
| 12 | Order cancelled by merchant while payment in flight | Race with merchant action |
| 13 | Two hypotheses both reconcile exactly to the delta | Must escalate as ambiguous, not pick one |
| 14 | Delta where no hypothesis reconciles | Must escalate, not force a fit |
| 15 | Split payment, two instruments, rounding on each | Paisa-level attribution |

Cases 7, 13 and 14 are the most valuable in the set. They test whether the system correctly refuses to act — which is harder and more important than acting correctly.

---

## 9. What StateSync is not

Scope boundaries stated explicitly, in the README.

- **Not a fraud detection system.** It reconciles state; it makes no judgement about intent.
- **Not a replacement for webhooks.** It's the safety net underneath them.
- **Not a settlement engine.** It detects settlement gaps and escalates; it doesn't move money between banks.
- **Not fully autonomous.** Anything outside the confidence, value, or blast-radius bounds goes to a human by design, not by limitation.

---

## 10. Evaluation

Three arms, same batch: 500+ transactions, of which at least 15 are the hard cases from §8.2.

| Arm | Description |
|---|---|
| **Manual baseline** | No detection. Count divergences that would go unnoticed; estimate analyst-minutes |
| **Rules-only** | Deterministic matching and classification, no LLM |
| **Full agent** | Rules + propose-verify layer for ambiguous attribution |

### Metrics, reported split by case difficulty

**On clean cases:**
- Detection rate by class
- Classification accuracy by class
- Repair success rate by class

**On hard cases (§8.2) — reported separately:**
- Classification accuracy
- **Fee-schedule coverage** — % of cases where fee data was available at all
- Hypothesis verification rate (and mean hypotheses generated per case)
- Resolution split: rules / verified hypothesis / escalated
- Verifier rejection rate, broken out by rejection reason
- **Correct-refusal rate** on cases 7, 13, 14 — did it decline to act when it should?

**Safety metrics, across the whole batch:**
- **Zero double-repairs on re-run** — the idempotency proof
- **Zero double-repairs after a Redis flush** — the DB constraint proof
- **False repair rate** — repairs that made state worse
- **Stuck-repair detection rate** — crashes surfaced, not swallowed
- **Transient divergences filtered** by the staleness window (§7.1) — evidence it's load-bearing

**Business metrics — with denominators, split by difficulty:**

A single "₹ at risk missed" figure is meaningless, because the injector controls what exists. 400 easy divergences plus 15 hard ones, missing 10 of the hard ones, looks like a 2.4% miss on total injected value while hiding a 67% miss rate on the cases that matter. So report it as a matrix:

| | Injected (₹) | Detected (₹) | Missed (₹) | Detection % |
|---|---|---|---|---|
| Clean cases | — | — | — | — |
| Hard cases (§8.2) | — | — | — | — |
| `SETTLEMENT_GAP` (synthetic) | — | — | — | — |
| **Total** | — | — | — | — |

- A miss on a **clean** case is a detection failure. It should be near zero.
- A miss on a **hard** case is different — for cases 7, 13 and 14, escalation *is* the correct outcome, so those are scored on correct-refusal, not detection.
- Never merge the synthetic settlement row into the headline.
- Exception list with reasons, committed to the repo
- Analyst-minutes saved

### On reporting the false repair rate

Report it even though it hurts. A submission that volunteers where its system caused harm reads as senior. One claiming 100% success reads as untested. Razorpay scores honest metrics explicitly — most applicants will fail that criterion by omission.

### 10.1 Contingency: what if rules-only wins?

**Expect a tie on clean cases.** Four of six classes are set operations; a language model will not beat a set operation at being a set operation. Say this in the README before anyone else says it to you.

The propose-verify layer is judged only on §8.2 hard cases. Three outcomes, all publishable:

**Outcome A — the layer resolves ambiguous cases rules cannot.** The AI earned its place. Report the resolution split.

**Outcome B — it resolves some, but the verifier rejects most proposals.** Also a good result: report the rejection rate and argue that the guard is load-bearing — the system safely escalates rather than guessing. That is a stronger safety story than a higher accuracy number.

**Outcome C — rules match or beat it everywhere.** Then say so plainly: *"I tested whether a language model adds value to reconciliation classification. On this dataset it did not beat deterministic rules, and here is the evidence. I have kept it scoped to hypothesis generation on attribution, where it resolved N cases, and removed it everywhere else."*

Outcome C is not a failure. Razorpay's rubric asks whether AI was applied appropriately **rather than forced**. A candidate who measured, found the honest answer, and cut scope accordingly is demonstrating exactly that judgement. The failure mode is keeping the LLM in the pipeline with no evidence it helps.

---

## 11. Failure recovery

One of four scored criteria. Most submissions will pretend nothing broke. Instrument these and demonstrate at least three live:

| Failure | Response |
|---|---|
| LLM returns a class outside the enum | Rejected by validation → deterministic classifier, logged `DEGRADED_MODE` |
| LLM hypothesis fails arithmetic verification | Rejected → next hypothesis → after 2 passes / 10 hypotheses, escalate as `NO_HYPOTHESIS_VERIFIED` |
| Fee schedule missing for an instrument | Escalate as `FEE_SCHEDULE_UNKNOWN` with the config needed — never guess a rate |
| Gateway state mid-transition | Staleness window skips it; two-run confirmation prevents repair on a transient |
| Razorpay API returns 5xx mid-batch | Resume from ledger position, no re-repair of completed items |
| Worker crashes mid-repair | Lease expires → `STUCK_REPAIR_DETECTED` → alert + escalate |
| Redis flushed entirely | DB constraint catches duplicates, logged as `REPAIR_DEDUPED_BY_DB` |
| Merchant DB write fails after gateway read | Compensating entry, mark `REPAIR_FAILED`, escalate |
| Two reconciler instances run concurrently | Lease → exactly one proceeds |
| Ledger hash chain breaks | **Halt all repairs. Alert. Fail closed, never open.** |

That last line is the most important in the document. A reconciliation system that keeps writing while its own audit trail is compromised is worse than no system at all.

---

## 12. Repository structure

```
statesync/
├── README.md              # architecture + how to reproduce every number
├── ARCHITECTURE.md        # diagrams, decision log, cited GitHub issues
├── Makefile               # `make eval` regenerates all README numbers
├── src/
│   ├── connectors/        # razorpay_client, order_store, ledger
│   ├── reconciler/        # three_way, invariants
│   ├── classifier/
│   │   ├── deterministic.py   # 4 clean classes
│   │   ├── hypothesis.py      # LLM proposal generation
│   │   └── verifier.py        # exact arithmetic verification
│   ├── policy/            # repair_policy, blast_radius, idempotency
│   ├── executor/          # one repair module per divergence class
│   ├── ledger/            # hash_chain
│   └── reporting/         # templates + prose guard
├── eval/
│   ├── injector.py        # clean cases
│   ├── hard_cases.py      # the 15 from §8.2
│   ├── harness.py         # three arms
│   └── results/           # committed output, dated, reproducible
└── exceptions.csv         # the honest residual, committed
```

**`make eval` regenerating the README numbers is the single highest-signal artefact in the repo.** It turns every claim from an assertion into something a reviewer verifies in one command.

---

## 13. Build plan with decision gates

Compressed from the original schedule. The connector and mock-store work in days 1–2 is not hard; the complexity lives in days 3–8, and the schedule now reflects that.

| Day | Deliverable |
|---|---|
| 1 | Razorpay sandbox connector, mock order store, ledger, hash chain. **First task: confirm whether test mode populates `fee` and `tax` on the payment object (§6.1)** — the answer changes how §6 is framed |
| 2 | Clean injectors, reconciler, invariants, deterministic classifier → **arms 1–2 producing numbers** |
| 3 | Repair executors, DB constraints, Redis lease state machine |
| 4 | **Idempotency proof: re-run batch → zero writes. Flush Redis → zero writes.** Blast-radius and value gates. Staleness window + two-run confirmation |
| 5 | The 15 hard cases from §8.2 |
| 6–7 | LLM hypothesis generator + deterministic verifier for `AMOUNT_MISMATCH` |
| 8 | `SETTLEMENT_GAP` — go/no-go |
| 9 | Failure injection, degradation paths, stuck-repair handling |
| 10 | Freeze. Full evaluation run. README + ARCHITECTURE |
| 11 | Record video |
| 12 | Buffer |

### Gate 1 — end of day 2

Arms 1–2 must be producing real numbers on real injected divergences. If not, cut the LLM layer entirely and ship a measured deterministic reconciler. That is still a complete, honest, defensible submission.

### Gate 2 — end of day 4

Both idempotency proofs must pass. If they don't, stop adding features and fix this. **Idempotency is the credibility claim of the entire project** — a system that double-repairs is worse than one that does nothing, and a reviewer will test it.

### Gate 3 — end of day 8

The *existence* decision for `SETTLEMENT_GAP` is already made (§6.5): it ships, labelled synthetic-only, with separately reported metrics. Day 8 is a **quality** gate, not an existence gate:

- **Synthetic settlement attribution verifying at a usable rate** → ship it, clearly labelled.
- **Not verifying** → keep the class in the taxonomy but report it as `NOT_IMPLEMENTED` with the reason, rather than reporting a misleading accuracy figure on data you manufactured.

Either way the README says the same thing: demonstrated, not validated.

**General rule, more important than the schedule: never let the interesting part block the shippable part.**

---

## 14. Five-minute video script

| Time | Content |
|---|---|
| 0:00–0:30 | Magento issue #208 on screen. The lose-lose fork: duplicate orders, or lost orders. No correct setting exists. |
| 0:30–1:15 | Webhook FAQ on screen — 24h then disabled, no bulk replay. "Six hours of dead endpoint means six hours of lost events and no recovery path." |
| 1:15–2:15 | Architecture. Four classes are set operations and rules handle them — say this out loud. Then the ₹105.80 example and why it's genuinely ambiguous. Propose-verify. |
| 2:15–3:15 | Live run: inject 50 divergences including hard cases. Show detection, repair, and one case correctly **escalated rather than guessed**. |
| 3:15–4:15 | **Run the identical batch again — zero writes. Then flush Redis and run again — still zero writes.** Then one escalation packet on screen: the residual, all ten hypotheses, the arithmetic, why each failed. |
| 4:15–5:00 | Results across three arms, split clean vs hard vs synthetic-settlement. Fee-schedule coverage. Verifier rejection rate. False-repair rate. Exception list. "Here's what it can't solve, and why." |

The strongest 45 seconds is 3:15–4:00. Anyone can demo a system doing something. Demonstrating it correctly does **nothing** the second time — and still nothing after you delete its cache — is what proves you understand payments.

The second strongest moment is at 2:15, admitting rules handle four of six classes before a reviewer can point it out. Pre-empting your own weakest point is worth more than hiding it.

---

## 15. Anticipated objections

**"Why not just make webhooks reliable?"**
You can't, from the merchant side. At-least-once delivery with eventual give-up is a property of the protocol. StateSync assumes webhooks fail and works anyway.

**"Doesn't Razorpay already reconcile?"**
Razorpay reconciles its own books. It cannot see inside the merchant's order store or ledger — which is exactly where this divergence lives.

**"You used an LLM to do string comparison."**
No — four classes are handled by deterministic set operations and the evaluation reports that plainly. The model is used only for hypothesis generation on under-determined attribution (§6), where its output is accepted only if it reconciles exactly. See §10.1 for what happens if that layer doesn't earn its place.

**"How do I know your repairs are safe?"**
Six independent answers: the DB unique constraint, the Redis lease state machine, the blast-radius cap, the value threshold, the hash-chained ledger, and the reported false-repair rate. Plus two live proofs in the video.

**"What if the LLM hallucinates a decomposition?"**
It fails arithmetic verification and is discarded. This is structural, not a prompt instruction. The verifier rejection rate is reported as a metric precisely because it demonstrates the guard is doing work.

**"Where does your fee schedule come from, and does it vary by instrument?"**
Per-payment `fee` and `tax` come off the Razorpay payment object and are subtracted deterministically before any inference runs. A versioned merchant config keyed by instrument, card type, MCC and tier handles validation and settlement-level attribution. When neither covers a case, the system escalates as `FEE_SCHEDULE_UNKNOWN` rather than guessing, and fee-schedule coverage is reported as a metric. See §6.1.

**"What if the Razorpay API is mid-transition and gives you an inconsistent read?"**
Two defences (§7.1): a 15-minute staleness window before a payment is eligible for reconciliation at all, and a requirement that a divergence be observed on two consecutive passes before any repair is authorised. StateSync is deliberately not real-time — it trades minutes of detection latency for not repairing payments that were merely in flight. Transient divergences filtered is reported as a metric.

**"Your settlement data is fake."**
Correct, and it says so in §6.5, in the README, and in the results table. The sandbox does not produce real settlement behaviour. That class is demonstrated, not validated, and its metrics are never merged into the headline.

---

## 16. Why this fits the rubric

| Scored criterion | How StateSync answers it |
|---|---|
| **Problem taste** | Failure modes cited from Razorpay's own open issues, with the strongest evidence used first and the weaker evidence honestly scoped |
| **Build quality** | Two-layer idempotency, lease state machine with stuck-repair detection, staleness window and two-run confirmation, canonically-serialised hash chain, invariant checking, one-command reproducible eval |
| **AI judgment** | AI scoped to hypothesis generation on genuinely under-determined attribution, gated by exact verification, with a stated contingency to remove it if measurement says it doesn't help |
| **Failure recovery** | Eleven instrumented degradation paths, including fail-closed on audit compromise, crash-mid-repair surfacing as an escalation rather than silence, and bounded hypothesis search that stops rather than fitting by coincidence |

The practical advantage: reconciliation is unglamorous enough that submission volume in this track should be low, while the bar — a 50+ record batch with a match rate and an honest exception list — is mechanically achievable within the time available.
