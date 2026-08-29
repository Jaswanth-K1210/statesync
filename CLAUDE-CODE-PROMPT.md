# StateSync — Build Brief for Claude Code

Paste this whole file as your first message, or save it as `CLAUDE.md` in the repo root.

---

## 0. How to work on this

Read this section before writing any code.

1. **Work phase by phase. Stop at every phase gate and wait for my review.** Do not start Phase N+1 until I say go. Phases have hard exit criteria; report them as a checklist.
2. **Tests are written inside the phase, not after.** A phase is not complete until its tests pass. If you finish the code and the tests fail, the phase is not done.
3. **Never `except Exception: pass`.** Every caught exception writes a ledger entry and either re-raises (dev) or halts (prod).
4. **Do not gold-plate the frontend.** It is Phase 6, it is four screens, and the demo must work without it. If you find yourself building a component library, stop.
5. **Ask before adding a dependency, changing the data model, or adding a divergence class.** Do not "improve" the scope.
6. **If something in this brief is wrong or impossible, say so immediately** rather than working around it silently. I would rather revise the spec than discover a workaround at demo time.
7. Commit at every green phase gate with the phase number in the message.

---

## 1. What we're building and why

**StateSync** — a payment/order divergence detection and idempotent repair agent for e-commerce merchants using a payment gateway.

**The problem.** Three systems hold a view of the same transaction: the payment gateway (authoritative on whether money moved), the merchant's order store (authoritative on whether goods were promised), and the merchant's ledger (authoritative on the books). They're synchronised by webhooks, which deliver at-least-once, in no guaranteed order, and give up permanently after 24 hours. So they drift. Today a human notices, usually after a customer complains.

**The loop we close.** *Three sources disagree → find the disagreements → explain them → fix or escalate → report.* One loop. Six kinds of disagreement.

This is a submission for a hackathon whose bar is: **throughput + measured accuracy + an honest exception list, over a 50+ record batch of synthetic data.** We will do 500+ records. Judged on problem taste, build quality, appropriate use of AI, and how failures were handled at runtime.

**What matters most, in order:**
1. It runs from a clean clone with one command
2. Idempotency is provable — running the same batch twice writes nothing the second time
3. The system correctly *refuses* to act on ambiguous cases
4. Honest metrics, including where it did badly

---

## 2. Non-negotiable constraints

These are correctness requirements, not style preferences. Violating any of them is a bug.

| # | Constraint | Why |
|---|---|---|
| 1 | **All money is integer paise.** Never float, anywhere, including tests | Floats are not canonically serialisable; one float breaks the hash chain across environments |
| 2 | **Canonical JSON for anything hashed**: `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)` | Dict ordering must not affect hashes |
| 3 | **Every run is seeded.** `SEED = 20260905` threads through every generator and shuffle | `make eval` twice must produce identical output |
| 4 | **Every LLM response is cached to disk**, keyed by SHA-256 of the prompt, committed to the repo | The demo must not depend on a network call |
| 5 | **The LLM never decides whether money moves.** It generates hypotheses only; a deterministic verifier accepts or rejects them | This is the architectural thesis of the project |
| 6 | **Two layers of idempotency**: a DB unique constraint (the real guarantee) plus a Redis lease (the optimisation) | Redis is a cache and can be flushed |
| 7 | **Fail closed.** If the audit chain doesn't verify, halt all repairs and exit non-zero | A reconciler that writes while its audit trail is compromised is worse than nothing |
| 8 | **No silent failures.** Every caught exception writes to the ledger | This is a scored criterion |

---

## 3. Stack

**Backend:** Python 3.11+, FastAPI, SQLAlchemy 2.x, PostgreSQL, Redis, Pydantic v2, `pytest` + `hypothesis`, `ruff` + `mypy --strict`.

**Frontend (Phase 6 only):** Vite + React + TypeScript + Tailwind. No component library. No state management library — `useState` and `fetch`.

**LLM:** one primary provider, one fallback, then a deterministic classifier. All responses cached.

**Infra:** `docker-compose` for Postgres + Redis. `Makefile` as the single entrypoint. GitHub Actions running `make verify` on push.

---

## 4. Domain model

```python
# All amounts are integer PAISE.

class PaymentStatus(StrEnum):
    CREATED = "created"; AUTHORIZED = "authorized"
    CAPTURED = "captured"; REFUNDED = "refunded"; FAILED = "failed"

class Payment(BaseModel):          # gateway's view
    payment_id: str                # "pay_xxx"
    order_ref: str | None
    amount_paise: int
    fee_paise: int | None          # gateway-reported, may be absent
    tax_paise: int | None          # GST on the fee, may be absent
    instrument: str                # upi | card_domestic | card_intl | emi
    status: PaymentStatus
    status_changed_at: datetime
    captured_at: datetime | None

class Order(BaseModel):            # merchant's view
    order_id: str
    payment_id: str | None
    customer_id: str
    total_paise: int
    status: str                    # pending | confirmed | cancelled | expired
    created_at: datetime
    line_items_count: int          # 0 signals a partial handler write

class LedgerEntry(BaseModel):      # merchant's books
    entry_id: str
    order_id: str | None
    payment_id: str | None
    amount_paise: int              # negative for refunds
    entry_type: str
    created_at: datetime

class DivergenceClass(StrEnum):
    CAPTURED_NO_ORDER = "captured_no_order"
    ORDER_NO_CAPTURE = "order_no_capture"
    DUPLICATE_ORDER = "duplicate_order"
    REFUND_NOT_REFLECTED = "refund_not_reflected"
    AMOUNT_MISMATCH = "amount_mismatch"
    SETTLEMENT_GAP = "settlement_gap"
    NO_DIVERGENCE = "no_divergence"

class ReasonCode(StrEnum):
    VERIFIED = "verified"
    AMBIGUOUS_MULTIPLE_VERIFIED = "ambiguous_multiple_verified"
    NO_HYPOTHESIS_VERIFIED = "no_hypothesis_verified"
    FEE_SCHEDULE_UNKNOWN = "fee_schedule_unknown"
    STUCK_REPAIR = "stuck_repair"
```

### The six divergence classes

| Class | Detection | Repair | Danger if wrong |
|---|---|---|---|
| `CAPTURED_NO_ORDER` | Payment captured, no order row | Create order idempotently | Duplicate on webhook replay |
| `ORDER_NO_CAPTURE` | Order exists, no capture past timeout | Expire, release inventory | Cancels a paid order |
| `DUPLICATE_ORDER` | Two orders, same `payment_id` | Merge on payment_id, void later | Merges two real orders |
| `REFUND_NOT_REFLECTED` | Gateway refund, no ledger entry | Post compensating entry | Double refund |
| `AMOUNT_MISMATCH` | Order total ≠ settled, residual unexplained | Attribute residual, then reconcile | Accepts a wrong amount |
| `SETTLEMENT_GAP` | Payout ≠ Σ(captures − refunds − fees) | Attribute components | Masks a real shortfall |

**Merge only on exact `payment_id`. Never on heuristic similarity.** Two legitimate orders from the same customer for the same amount seconds apart must not merge — this is a test case.

---

## 5. Phases

### PHASE 1 — Foundations

**Build:** repo scaffold, Makefile, docker-compose, CI. Domain models. Seeded synthetic generator producing clean `Payment`/`Order`/`LedgerEntry` sets. Hash-chained append-only ledger with canonical serialisation. LLM cache harness (build it now even though no LLM calls happen until Phase 5).

**Ledger spec:**
```
GENESIS = "0" * 64
hash = sha256(prev_hash.encode("ascii") + canonical(event_body)).hexdigest()
```
Stored in a dedicated append-only Postgres table, INSERT-only. Full chain walk at the start of every run.

**Tests:**
- Unit: `canonical()` ignores key order; float amounts rejected with `TypeError`; chain detects a single tampered field and returns its index; chain detects a deleted entry; chain hash identical across two separate subprocess runs.
- Smoke: `make smoke` boots Postgres + Redis, writes 3 ledger entries, verifies the chain, exits 0 in under 30 seconds.

**Exit criteria:** `make test` green · `make smoke` green · CI green · running the generator twice with the same seed produces byte-identical output.

**🛑 STOP for review.**

---

### PHASE 2 — The loop, end to end

This phase is the whole submission in miniature. Everything after it is improvement.

**Build:** divergence injector (clean cases only — drop webhook, duplicate webhook, abandon after order create, refund without ledger write). Three-way reconciler. Ledger invariant `balance == initial − Σ(successful)`. Deterministic classifier for the four clean classes. Staleness window (15 min) and two-run confirmation. Eval harness with arms 1 (no detection) and 2 (rules only). `exceptions.csv` writer.

**Staleness rule:** no payment is reconciled until it has been terminal for 15 minutes. A divergence must be observed on two consecutive passes before any repair is authorised. This prevents repairing a payment that was merely mid-flight.

**Tests:**
- Unit: staleness window rejects a payment terminal for 2 minutes; two-run confirmation does not authorise on first observation; ledger invariant catches an injected imbalance.
- Integration: 500-record batch → detection rate per class matches injected ground truth.
- Smoke: `make smoke` now also runs a 50-record batch end to end and asserts a non-zero match rate.

**Exit criteria:** `make eval` prints match rate, per-class detection, and throughput (records/sec, wall clock) for arms 1–2 on 500 records · `exceptions.csv` written · all Phase 1 tests still green.

**🛑 STOP for review. This is shippable state #1.** If the project has to end here, it still clears the published bar.

---

### PHASE 3 — Repairs and idempotency

**Build:** one repair executor per class. DB unique constraints on `orders.payment_id` and `ledger_entries.repair_key`. Redis lease state machine: `CLAIMED → SUCCEEDED | FAILED`, lease TTL 300s. Policy engine gates: confidence, value threshold, blast-radius cap (max N repairs per run).

**The state machine must handle these correctly:**
- `SUCCEEDED` → return cached result, no writes
- `FAILED` → return failure, do not silently retry
- `CLAIMED` with live lease → return `IN_PROGRESS`
- `CLAIMED` with expired lease → **write `STUCK_REPAIR_DETECTED`, alert, escalate.** Never return an empty result.
- `DuplicateKeyError` from the DB → log `REPAIR_DEDUPED_BY_DB`, return `ALREADY_APPLIED`

**Tests:**
- Unit: each of the five states above, one test each.
- Property (hypothesis): for any list of divergences and any N in 1..5, running all repairs N times produces exactly `len(unique_keys)` writes. **This is the single most important test in the project.**
- Integration: run batch → flush Redis entirely → run batch again → zero additional writes, `REPAIR_DEDUPED_BY_DB` in the ledger.
- Smoke: extended to run a small batch twice and assert zero second-pass writes.

**Exit criteria:** the property test passes · the Redis-flush test passes · blast-radius cap enforced.

**🛑 STOP for review.**

---

### PHASE 4 — Hard cases and escalation

**Build:** the 15 ambiguous cases in the injector. Escalation packet structure. Templated ops report — **every number comes from a template populated from the ledger; no model output produces any figure.**

**The 15 hard cases:**

| # | Scenario | Correct outcome |
|---|---|---|
| 1 | Partial handler write — order row exists, `line_items_count == 0`, payment captured | Detected, classified as compound, escalated |
| 2 | Refund and second payment attempt racing on one order | Resolved by ordering rules or escalated |
| 3 | Capture during network partition — gateway captured, merchant timed out and retried | No duplicate created |
| 4 | Duplicate webhook, two deliveries with *different* payloads | Later delivery authoritative, logged |
| 5 | Delta exactly equals MDR + GST | `NO_DIVERGENCE` |
| 6 | Delta equals MDR + GST except ₹0.60 | `AMOUNT_MISMATCH`, verified or escalated |
| 7 | **Two legitimate orders, same customer, same amount, 2s apart** | **NOT merged. `merge_count == 0`** |
| 8 | Refund issued then reversed by bank | Net position correct |
| 9 | Settlement including a prior-cycle chargeback debit | Attributed or escalated |
| 10 | `refund.processed` arrives before `payment.captured` | Resolved, not `UNKNOWN` |
| 11 | Webhook replay 26 hours later | Deduped, not double-applied |
| 12 | Order cancelled by merchant while payment in flight | Escalated, not auto-repaired |
| 13 | **Two hypotheses both reconcile exactly** | `AMBIGUOUS_MULTIPLE_VERIFIED`, both attached |
| 14 | **No hypothesis reconciles** | `NO_HYPOTHESIS_VERIFIED`, all rejected attached |
| 15 | Split payment, two instruments, rounding on each | Paisa-level attribution or escalation |

**Escalation packet:**
```python
class EscalationPacket(BaseModel):
    divergence: Divergence
    known_components: list[Component]   # fee/tax subtracted, with source
    residual_paise: int
    hypotheses: list[Hypothesis]        # ALL of them, verified and rejected
    reason_code: ReasonCode
    suggested_action: str               # from a fixed template, NOT model-generated

class Hypothesis(BaseModel):
    components: list[Component]
    sum_paise: int
    matched_residual: bool
    citations: list[str]                # artifact IDs claimed
    verdict: Literal["VERIFIED","ARITHMETIC_FAILED","ARTIFACT_MISSING","RANGE_VIOLATION"]
```

Cases 13 and 14 are the most valuable in the set: they test whether the system correctly refuses to act. Case 7 is the false-positive test.

**Tests:** one integration test per hard case, asserting the correct outcome. Cases 7, 13, 14 are mandatory and may not be skipped. Smoke extended to include case 7.

**Exit criteria:** all 15 hard-case tests pass · escalation packets contain full arithmetic for every hypothesis · `exceptions.csv` includes reason codes.

**🛑 STOP for review. This is shippable state #2.**

---

### PHASE 5 — The AI layer

**Build:** hypothesis generator and deterministic verifier for `AMOUNT_MISMATCH` and `SETTLEMENT_GAP` only. The four clean classes stay purely deterministic — do not route them through the LLM.

**Fee resolution order (deterministic, before any inference):**
1. `payment.fee_paise` + `payment.tax_paise` if present → subtract, authoritative
2. Configured fee schedule keyed by `(instrument, card_type, mcc, tier)`, versioned with effective dates
3. Neither → escalate as `FEE_SCHEDULE_UNKNOWN`. **Never guess a rate.**

Only the unexplained **residual** goes to the LLM.

**Generation bounds — hard:**
```
Pass 1: up to 5 hypotheses, temperature 0.7
  ↓ none verify
Pass 2: ONE retry, prompt includes the rejected hypotheses, the residual,
        and the component taxonomy [fees, refunds, chargebacks, reserves,
        rounding, adjustments, FX]
  ↓ none verify
STOP → NO_HYPOTHESIS_VERIFIED
```
Hard cap: 2 passes, 10 hypotheses per divergence. No adaptive retry loops — an unbounded search over an under-determined problem eventually fits by coincidence, and a coincidental fit is worse than an escalation.

**Verifier checks, all deterministic:** `Σ(components) == residual` exactly in paise; every cited artifact exists; every component within its permitted range (e.g. MDR ≤ 4%).

**Wording rule:** the system may only ever report *"no generated hypothesis verified."* It may never claim *"no explanation exists."*

**Fallback chain:** primary provider → secondary provider → deterministic classifier, marked `DEGRADED_MODE`.

**Tests:**
- Unit: verifier rejects off-by-one-paisa; rejects a missing artifact citation; rejects an out-of-range MDR; LLM is *not* called when `fee`+`tax` fully explain the delta.
- Chaos: LLM returns an invalid class → falls back; returns malformed JSON → falls back; times out → falls back; hypothesis generation is bounded to ≤2 LLM calls per divergence.
- Integration: arm 3 in the eval, with resolution split (rules / verified / escalated), verifier rejection rate by reason, and fee-schedule coverage.

**Exit criteria:** three arms produce comparable numbers · every LLM response cached to disk and committed · chaos tests green.

**🛑 STOP for review.**

---

### PHASE 6 — Frontend

**Timebox: one day. Four screens. The demo must work without it.** Build the API endpoints first, then the thinnest UI that renders them. If you are choosing colours, you have gone too far.

**API (FastAPI):**
```
POST /api/runs                     start a reconciliation run
GET  /api/runs                     list runs with summary metrics
GET  /api/runs/{id}                run detail: metrics, throughput, arm
GET  /api/runs/{id}/divergences    filterable by class, reason_code, resolved
GET  /api/divergences/{id}         full escalation packet
GET  /api/ledger/verify            walk the chain, return ok + break index
GET  /api/runs/{id}/exceptions.csv download
```

**Screens:**

1. **Runs list** — table of runs: timestamp, arm, records, match rate, exceptions, throughput. Click through.
2. **Run detail** — metric cards (match rate, throughput, LLM calls, exceptions), per-class detection table, link to exceptions CSV.
3. **Escalation packet viewer** ⭐ — *the screen that matters.* For one divergence: the residual, known components already subtracted, then every hypothesis as a card showing its components, the arithmetic sum, whether it matched, its citations, and its verdict badge. Rejected hypotheses shown too, with why they failed.
4. **Ledger viewer** — the chain, with a Verify button that turns green or shows the break index in red.

**Tests:**
- Component: escalation packet viewer renders 10 hypotheses with correct verdict badges; ledger viewer shows red on a broken chain.
- **User journey tests (Playwright), written as scenarios:**
  - *Ops reviews an exception:* open dashboard → latest run → filter to `NO_HYPOTHESIS_VERIFIED` → open one → see residual and all 10 hypotheses with arithmetic and failure reasons.
  - *Ops distinguishes an ambiguous case:* filter to `AMBIGUOUS_MULTIPLE_VERIFIED` → open → see exactly two `VERIFIED` badges with different component breakdowns.
  - *Ops verifies integrity:* ledger page → Verify → green.
  - *Ops detects tampering:* tamper an entry via a test fixture → Verify → red with the break index.
  - *Ops exports:* run detail → download exceptions CSV → file non-empty with reason codes.
- Smoke: `make smoke-frontend` builds, boots, loads the runs list, exits 0.

**Exit criteria:** all five user journeys pass · `make demo` works with the frontend off.

**🛑 STOP for review.**

---

### PHASE 7 — Hardening and reproducibility

**Build:** remaining chaos tests. Throughput instrumentation on every arm. Full eval run with results committed to `eval/results/`. README with every number and how to reproduce it. `ARCHITECTURE.md` with the decision log.

**Remaining chaos tests:**
- API 500 mid-batch → resume → no double repair
- Worker crash mid-repair → lease expires → `STUCK_REPAIR` escalation
- Ten concurrent workers on one divergence → exactly one succeeds, one write
- Tampered ledger → run halts, exits non-zero, **zero writes**

**The reproducibility test — do this literally:**
```bash
cd /tmp && rm -rf sstest && git clone <repo> sstest && cd sstest
make setup && make verify
```
Fix whatever breaks. Every project fails this the first time.

**Exit criteria:** fresh clone reproduces every README number in one command · `make verify` green · no `except: pass` anywhere in the codebase.

---

## 6. Test taxonomy

| Layer | What it is | Runtime | Command |
|---|---|---|---|
| **Unit** | One function, no I/O | <5s total | `make test-unit` |
| **Property** | Invariants under generated input (hypothesis) | <30s | `make test-prop` |
| **Integration** | Real Postgres + Redis, full pipeline | <2min | `make test-int` |
| **Chaos** | Injected failures, degradation paths | <1min | `make test-chaos` |
| **Smoke** | Does it boot and do one real thing? | **<30s** | `make smoke` |
| **User journey** | Playwright, ops-person scenarios | <2min | `make test-e2e` |
| **Eval** | The 3-arm measurement run | <5min | `make eval` |

**Smoke tests grow with every phase** and must always stay under 30 seconds. They're what you run before every commit and what tells you in half a minute whether the system is alive.

**`make verify` = unit + property + integration + chaos + smoke.** Must be green before any commit to `main`.

---

## 7. Repo layout

```
statesync/
├── CLAUDE.md · README.md · ARCHITECTURE.md
├── Makefile · docker-compose.yml · pyproject.toml
├── src/statesync/
│   ├── models/          domain models, integer paise
│   ├── generator/       seeded synthetic data
│   ├── injector/        clean.py, hard_cases.py
│   ├── reconciler/      three_way.py, invariants.py, staleness.py
│   ├── classifier/      deterministic.py, hypothesis.py, verifier.py, fees.py
│   ├── policy/          gates.py, idempotency.py, blast_radius.py
│   ├── executor/        one module per divergence class
│   ├── ledger/          chain.py, canonical.py
│   ├── reporting/       templates.py, exceptions_csv.py
│   ├── llm/             client.py, cache.py, fallback.py
│   └── api/             FastAPI routes
├── frontend/            Vite + React, Phase 6
├── tests/               unit/ property/ integration/ chaos/ smoke/ e2e/
├── eval/                harness.py, arms.py, results/
└── llm_cache/           committed
```

---

## 8. Makefile targets

```makefile
setup          # deps, docker up, migrate
test-unit test-prop test-int test-chaos test-e2e
smoke          # <30s, must always pass
verify         # everything except e2e and eval
eval           # 3-arm run, regenerates README numbers
eval-arm ARM=  # single arm
demo           # the exact video sequence
clean
```

---

## 9. Definition of done

- [ ] `git clone` → `make setup && make verify` green on a fresh machine
- [ ] `make eval` reproduces every number in the README
- [ ] Running the same batch twice writes nothing the second time
- [ ] Flushing Redis and re-running writes nothing
- [ ] Tampering with the ledger halts the run with a non-zero exit and zero writes
- [ ] Cases 7, 13, 14 pass — the system refuses to act when it should
- [ ] `exceptions.csv` is committed and non-empty
- [ ] Throughput reported for every arm
- [ ] Zero `except: pass` in the codebase
- [ ] `make demo` works with the frontend switched off

---

## 10. Start here

Begin with **Phase 1 only**. Before writing code, reply with:

1. Your reading of the phase's scope in your own words
2. The file list you intend to create
3. Anything in this brief you think is wrong, ambiguous, or a bad idea

Then wait for my go-ahead.
