# StateSync — Build & Smoke Test Plan

> Companion to `CLAUDE-CODE-PROMPT.md` (the authoritative brief), `statesync-build-plan.md` (7-day schedule) and `statesync-ideation.md` (design rationale).
>
> This document answers one question per phase: **what do I build, and what 30-second command tells me it's alive?**

**Goal:** A payment/order divergence reconciler that runs from a clean clone in one command, proves idempotency, refuses to act on ambiguous cases, and reports honest throughput + match rate + an exception list.

**Architecture:** Three-way reconciler over gateway / order-store / ledger views. Deterministic detection and classification for four clean divergence classes; an LLM proposes decompositions of the *residual only* for the two ambiguous classes, and a deterministic verifier accepts a hypothesis only if it reconciles exactly in paise. All repairs are gated by a policy engine and double-guarded by a DB unique constraint plus a Redis lease. Everything is written to a hash-chained append-only ledger; a broken chain halts all writes.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.x, PostgreSQL, Redis, Pydantic v2, pytest + hypothesis, ruff + mypy --strict. Frontend: Vite + React + TypeScript + Tailwind. Infra: docker-compose + Makefile + GitHub Actions.

**Spec:** `CLAUDE-CODE-PROMPT.md`

---

## Global Constraints

Copied verbatim from the brief. Every phase's exit criteria implicitly include these.

| # | Constraint |
|---|---|
| 1 | **All money is integer paise.** Never float, anywhere, including tests. |
| 2 | **Canonical JSON for anything hashed:** `json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False)` |
| 3 | **Every run is seeded.** `SEED = 20260905` threads through every generator and shuffle. |
| 4 | **Every LLM response is cached to disk**, keyed by SHA-256 of the prompt, committed to the repo. |
| 5 | **The LLM never decides whether money moves.** It generates hypotheses only. |
| 6 | **Two layers of idempotency:** DB unique constraint (the real guarantee) + Redis lease (the optimisation). |
| 7 | **Fail closed.** If the audit chain doesn't verify, halt all repairs and exit non-zero. |
| 8 | **No silent failures.** Every caught exception writes to the ledger. Zero `except: pass` in the codebase. |

**Ledger chain construction** (referenced by every phase):

```
GENESIS = "0" * 64
hash = sha256(prev_hash.encode("ascii") + canonical(event_body)).hexdigest()
```

Full chain walk at the start of every run. Append-only Postgres table, `UPDATE`/`DELETE` revoked at the role level.

---

## The smoke ladder

This is the spine of the plan. **One command, `make smoke`, grows with every phase and must always finish under 30 seconds.** It is what you run before every commit, and it is what tells you in half a minute whether the system is alive.

Smoke is not a test suite. It proves the system *boots and does one real thing* per capability. Each rung is cumulative — Phase 4's smoke runs Phases 1–3's rungs too.

| Rung | Added in | What it proves | Budget |
|---|---|---|---|
| **S0** Infra | Phase 1 | Postgres + Redis reachable, migrations applied | 4s |
| **S1** Chain | Phase 1 | 3 entries written, chain verifies, tamper detected at the right index | 2s |
| **S2** Loop | Phase 2 | 50-record batch runs end to end, non-zero match rate, `exceptions.csv` written | 8s |
| **S3** Idempotency | Phase 3 | Same batch twice → zero second-pass writes; Redis flushed → still zero | 5s |
| **S4** Refusal | Phase 4 | Case 7 not merged; case 13 ambiguous; case 14 escalated with all hypotheses | 3s |
| **S5** AI + degradation | Phase 5 | LLM path served entirely from cache (network off); malformed response → `DEGRADED` | 3s |
| **S6** Fail-closed | Phase 7 | Tampered ledger → run halts, exits non-zero, **zero writes** | 2s |
| | | **Total** | **~27s** |

`make smoke-frontend` is separate (Phase 6) and is *not* in the 30-second budget — it builds and boots the UI.

**Budget discipline.** If a rung pushes the total over 30s, shrink its dataset, never its assertions. S2 uses 50 records, not 500; the 500-record run belongs to `make eval`.

### Test taxonomy

| Layer | What it is | Runtime | Command |
|---|---|---|---|
| Unit | One function, no I/O | <5s | `make test-unit` |
| Property | Invariants under generated input (hypothesis) | <30s | `make test-prop` |
| Integration | Real Postgres + Redis, full pipeline | <2min | `make test-int` |
| Chaos | Injected failures, degradation paths | <1min | `make test-chaos` |
| **Smoke** | **Does it boot and do one real thing?** | **<30s** | **`make smoke`** |
| User journey | Playwright, ops-person scenarios | <2min | `make test-e2e` |
| Eval | The 3-arm measurement run | <5min | `make eval` |

`make verify` = unit + property + integration + chaos + smoke. Green before any commit to `main`.

---

# PHASE 1 — Foundations

**Gate:** 🛑 Stop for review. **Target:** Day 1.

### Build

Repo scaffold, `Makefile`, `docker-compose.yml`, GitHub Actions running `make verify`. Domain models in integer paise. Seeded synthetic generator producing clean `Payment` / `Order` / `LedgerEntry` sets. Hash-chained append-only ledger with canonical serialisation. LLM cache harness — build it now, even though the first LLM call is Phase 5.

### Files

```
Create: Makefile · docker-compose.yml · pyproject.toml · .github/workflows/verify.yml
Create: src/statesync/config.py              SEED, STALENESS_WINDOW, LEASE_SECONDS, STRICT
Create: src/statesync/models/domain.py       Payment, Order, LedgerEntry, Divergence
Create: src/statesync/models/enums.py        PaymentStatus, DivergenceClass, ReasonCode
Create: src/statesync/ledger/canonical.py    canonical(), reject floats
Create: src/statesync/ledger/chain.py        Ledger.append(), Ledger.verify()
Create: src/statesync/ledger/store.py        append-only Postgres table + migration
Create: src/statesync/generator/synthetic.py seeded generator
Create: src/statesync/llm/cache.py           SHA-256-keyed disk cache
Create: tests/unit/test_canonical.py · tests/unit/test_chain.py
Create: tests/smoke/test_smoke.py            rungs S0, S1
```

### Interfaces produced

```python
def canonical(event: dict) -> bytes            # raises TypeError on any float
def chain_hash(prev_hash: str, event: dict) -> str

class Ledger:
    def append(self, event_type: str, **payload) -> LedgerEntry
    def verify(self) -> tuple[bool, int | None]   # (ok, break_index)

def generate_batch(seed: int, n: int) -> Batch    # Batch(payments, orders, ledger_entries)
def llm_call(prompt: str) -> str                  # cache-first, network only on miss
```

### Tests written in this phase

**Unit** — these five are the foundation everything else rests on:

```python
def test_canonical_ignores_key_order():
    a = {"amount": 100, "id": "pay_1", "type": "CAPTURE"}
    b = {"type": "CAPTURE", "id": "pay_1", "amount": 100}
    assert canonical(a) == canonical(b)

def test_float_amount_rejected():
    with pytest.raises(TypeError):
        Ledger().append("E", amount=100.50)

def test_chain_detects_single_field_tamper():
    led = Ledger(); [led.append("E", i=i) for i in range(10)]
    led.entries[5].payload["i"] = 999
    ok, idx = led.verify()
    assert not ok and idx == 5

def test_chain_detects_deleted_entry():
    led = Ledger(); [led.append("E", i=i) for i in range(10)]
    del led.entries[4]
    assert led.verify()[0] is False

def test_chain_stable_across_process_restart():
    assert build_chain_in_subprocess() == build_chain_in_subprocess()
```

The last one is the sleeper. A chain that verifies in-process but not across processes means something non-canonical crept in — a float, a dict-ordering dependency, a timestamp with sub-second precision. Catch it on Day 1, not on camera.

### Smoke — rungs S0, S1

```bash
make smoke
```

| Step | Assertion | Budget |
|---|---|---|
| Boot Postgres + Redis via docker-compose, wait for health | both reachable | 4s |
| Apply migrations | `ledger_entries` table exists, `UPDATE`/`DELETE` revoked | — |
| Write 3 ledger entries | 3 rows, `seq` = 1,2,3, entry 1 `prev_hash == GENESIS` | 1s |
| `Ledger.verify()` | returns `(True, None)` | — |
| Tamper entry 2 in memory, verify again | returns `(False, 1)` | 1s |
| Exit | code 0 | — |

**Exit criteria**

- [ ] `make test` green
- [ ] `make smoke` green, under 30s
- [ ] CI green on push
- [ ] Generator run twice with the same seed → byte-identical output
- [ ] `grep -rn "except.*:\s*pass" src/` returns nothing

---

# PHASE 2 — The loop, end to end

**Gate:** 🛑 Stop for review. **This is shippable state #1** — if the project ends here it still clears the published bar. **Target:** Day 2.

### Build

Divergence injector, clean cases only: drop webhook, duplicate webhook, abandon after order create, refund without ledger write. Three-way reconciler. Ledger invariant. Deterministic classifier for the four clean classes. Staleness window (15 min) + two-run confirmation. Eval harness with arms 1 (no detection) and 2 (rules only). `exceptions.csv` writer. **Throughput instrumentation from the start** — it is the first word of the published bar, and retrofitting it is how it gets forgotten.

### Files

```
Create: src/statesync/injector/clean.py          four injectors, seeded
Create: src/statesync/reconciler/three_way.py    set operations across three views
Create: src/statesync/reconciler/invariants.py   balance == initial − Σ(successful)
Create: src/statesync/reconciler/staleness.py    eligibility + two-run confirmation
Create: src/statesync/classifier/deterministic.py  four clean classes
Create: src/statesync/reporting/exceptions_csv.py
Create: src/statesync/metrics/throughput.py      records/sec, wall clock, p50/p99
Create: eval/harness.py · eval/arms.py
Modify: tests/smoke/test_smoke.py                add rung S2
```

### Interfaces produced

```python
def reconcile(batch: Batch, now: datetime) -> list[Divergence]
def eligible_for_reconciliation(payment: Payment, now: datetime) -> bool
def classify(divergence: Divergence) -> Classification   # .klass, .reason_code, .outcome
def write_exceptions_csv(path: Path, divergences: list[Divergence]) -> int
def run_arm(arm: Literal["none","rules","full"], batch: Batch) -> ArmResult
```

`ArmResult` carries `match_rate`, `per_class_detection`, `records_per_sec`, `wall_clock_s`, `p50_ms`, `p99_ms`, `llm_calls`, `exceptions_count`.

### Key rules to get right here

**Staleness.** No payment is reconciled until it has been terminal for 15 minutes. **Two-run confirmation.** A divergence must be observed on two consecutive passes before any repair is authorised — first observation writes `DIVERGENCE_OBSERVED`, only the second promotes to `DIVERGENCE_CONFIRMED`. Report *transient divergences filtered* as a metric; it is the evidence the window is load-bearing.

### Tests written in this phase

- **Unit:** staleness rejects a payment terminal for 2 minutes; two-run confirmation does not authorise on first observation; ledger invariant catches an injected imbalance; known `fee` + `tax` fully explaining a delta produces `NO_DIVERGENCE` **and does not call the LLM**.
- **Integration:** 500-record batch → per-class detection rate matches injected ground truth.

### Smoke — rung S2 added

| Step | Assertion | Budget |
|---|---|---|
| Generate a 50-record batch, seed 20260905 | 50 records, N injected divergences | 2s |
| Run arm 2 (rules) end to end | completes, no exception | 5s |
| Check match rate | strictly > 0 and ≤ 1.0 | — |
| Check per-class detection | every injected class appears in the result | — |
| Check `exceptions.csv` | file exists, non-empty, has a `reason_code` column | 1s |
| Check throughput | `records_per_sec > 0`, `wall_clock_s` recorded | — |
| Chain verify after the run | `(True, None)` | — |

**Exit criteria**

- [ ] `make eval` prints match rate, per-class detection, and throughput (records/sec, wall clock, p50/p99) for arms 1–2 on 500 records
- [ ] `exceptions.csv` written and non-empty
- [ ] Two identical `make eval` runs → identical output except timestamps
- [ ] All Phase 1 tests still green
- [ ] `make smoke` still under 30s

> **🚩 Gate.** If this is not done on schedule, cut the LLM layer entirely and spend the remaining time polishing a measured deterministic reconciler. Throughput, match rate, exception list — all satisfied without any AI. The AI improves the submission; it is not required by it.

---

# PHASE 3 — Repairs and idempotency

**Gate:** 🛑 Stop for review. **Target:** Day 3.

### Build

One repair executor per divergence class. DB unique constraints on `orders.payment_id` and `ledger_entries.repair_key`. Redis lease state machine with a 300s TTL. Policy engine gates: confidence, value threshold, blast-radius cap.

### Files

```
Create: src/statesync/policy/idempotency.py    the lease state machine
Create: src/statesync/policy/gates.py          confidence, value threshold
Create: src/statesync/policy/blast_radius.py   max N repairs per run
Create: src/statesync/executor/captured_no_order.py
Create: src/statesync/executor/order_no_capture.py
Create: src/statesync/executor/duplicate_order.py
Create: src/statesync/executor/refund_not_reflected.py
Create: migrations/002_unique_constraints.sql
Create: tests/property/test_idempotency.py
Modify: tests/smoke/test_smoke.py              add rung S3
```

### The state machine — five states, five tests

This is where the project's credibility lives. Each row is one unit test.

| Prior state | Behaviour | Never |
|---|---|---|
| `SUCCEEDED` | Return cached result, zero writes | — |
| `FAILED` | Return failure | Silently retry |
| `CLAIMED`, live lease | Return `IN_PROGRESS` | Return `None` |
| `CLAIMED`, expired lease | Write `STUCK_REPAIR_DETECTED`, alert, escalate | Return an empty result |
| `DuplicateKeyError` from DB | Log `REPAIR_DEDUPED_BY_DB`, return `ALREADY_APPLIED` | Swallow the exception |

The expired-lease row is the one that bites. A crash after claiming the key but before writing the result leaves a claim with no result — the naive implementation reads it, finds nothing, and returns silence forever. **A crash mid-execution must surface as an escalation, never as silence.**

### The single most important test in the project

```python
@given(st.lists(st.builds(Divergence), min_size=1, max_size=50),
       st.integers(1, 5))
def test_n_runs_equal_one_run(divs, n):
    for _ in range(n):
        for d in divs:
            repair(d)
    assert store.write_count == len(set(d.key() for d in divs))
```

If everything else has to go, this stays. It is the credibility claim of the whole project.

### Smoke — rung S3 added

| Step | Assertion | Budget |
|---|---|---|
| Run the 50-record batch with repairs enabled | writes recorded, `write_count == W` where `W > 0` | 3s |
| Run the identical batch again | **`write_count` still `W`** — zero second-pass writes | 1s |
| `redis-cli FLUSHALL`, run a third time | **`write_count` still `W`** | 1s |
| Check the ledger | contains `REPAIR_DEDUPED_BY_DB` | — |
| Check blast radius | repairs in any single run ≤ cap | — |

That middle pair of rows is the demo. Everything else in this project is engineering; those two lines are the argument.

**Exit criteria**

- [ ] `test_n_runs_equal_one_run` passes
- [ ] Redis-flush test passes — zero additional writes, `REPAIR_DEDUPED_BY_DB` in the ledger
- [ ] All five state-machine tests pass
- [ ] Blast-radius cap enforced
- [ ] `make smoke` still under 30s

---

# PHASE 4 — Hard cases and escalation

**Gate:** 🛑 Stop for review. **This is shippable state #2.** **Target:** Day 4.

### Build

The 15 ambiguous cases in the injector. Escalation packet structure. Templated ops report — **every number comes from a template populated from the ledger; no model output produces any figure.**

### Files

```
Create: src/statesync/injector/hard_cases.py     the 15
Create: src/statesync/models/escalation.py       EscalationPacket, Hypothesis, Component
Create: src/statesync/reporting/templates.py     fixed suggested_action per reason code
Modify: src/statesync/reporting/exceptions_csv.py  add reason_code column
Create: tests/integration/test_hard_cases.py     one test per case
Modify: tests/smoke/test_smoke.py                add rung S4
```

### The three that matter

Of the 15, three are mandatory and may not be skipped. They test whether the system correctly *refuses* to act — harder and more important than acting correctly.

| # | Scenario | Correct outcome |
|---|---|---|
| **7** | Two legitimate orders, same customer, same amount, 2s apart | **NOT merged. `merge_count == 0`** |
| **13** | Two hypotheses both reconcile exactly | `AMBIGUOUS_MULTIPLE_VERIFIED`, both attached |
| **14** | No hypothesis reconciles | `NO_HYPOTHESIS_VERIFIED`, all rejected attached with reasons |

Merge only on exact `payment_id`. Never on heuristic similarity.

The other twelve: partial handler write · refund racing a second payment · capture during network partition · duplicate webhook with differing payloads · delta exactly MDR + GST · delta off by ₹0.60 · refund reversed by bank · prior-cycle chargeback in settlement · `refund.processed` before `payment.captured` · 26-hour webhook replay · order cancelled while payment in flight · split payment with rounding on each instrument.

### The wording rule

The system may only ever report *"no generated hypothesis verified."* It may never claim *"no explanation exists."* Anything stronger is a claim the architecture cannot support. Enforce it in the template, and add a test that greps the rendered output for the forbidden phrasing.

### Smoke — rung S4 added

| Step | Assertion | Budget |
|---|---|---|
| Inject case 7, run classify | `klass != DUPLICATE_ORDER` and `store.merge_count == 0` | 1s |
| Inject case 13, run classify | `reason_code == AMBIGUOUS_MULTIPLE_VERIFIED`, exactly 2 `VERIFIED` | 1s |
| Inject case 14, run classify | `reason_code == NO_HYPOTHESIS_VERIFIED`, ≥5 hypotheses, all with arithmetic shown | 1s |
| Check `exceptions.csv` | reason codes present for all three | — |

**Exit criteria**

- [ ] All 15 hard-case tests pass
- [ ] Escalation packets contain full arithmetic for every hypothesis, verified and rejected alike
- [ ] `exceptions.csv` includes reason codes
- [ ] `suggested_action` comes from a fixed template — verified by a test that no model output reaches it
- [ ] `make smoke` still under 30s

---

# PHASE 5 — The AI layer

**Gate:** 🛑 Stop for review. **Target:** Day 5.

### Build

Hypothesis generator and deterministic verifier, for `AMOUNT_MISMATCH` and `SETTLEMENT_GAP` **only**. The four clean classes stay purely deterministic — do not route them through the LLM.

### Files

```
Create: src/statesync/classifier/fees.py        fee resolution order
Create: src/statesync/classifier/hypothesis.py  bounded generation
Create: src/statesync/classifier/verifier.py    arithmetic, artifacts, ranges
Create: src/statesync/llm/client.py · src/statesync/llm/fallback.py
Create: config/fee_schedule.yaml                versioned, effective-dated
Create: llm_cache/                              committed to the repo
Create: tests/chaos/test_llm_degradation.py
Modify: eval/arms.py                            add arm 3
Modify: tests/smoke/test_smoke.py               add rung S5
```

### Fee resolution order — deterministic, before any inference

1. `payment.fee_paise` + `payment.tax_paise` if present → subtract, authoritative
2. Configured fee schedule keyed by `(instrument, card_type, mcc, tier)`, versioned with effective dates
3. Neither → escalate as `FEE_SCHEDULE_UNKNOWN`. **Never guess a rate.**

Only the unexplained **residual** goes to the LLM. Report **fee-schedule coverage** as a first-class metric — the percentage of `AMOUNT_MISMATCH` cases where fee data was available at all. A system reporting 95% verification while silently escalating the 40% it couldn't price is not honest.

### Generation bounds — hard

```
Pass 1: up to 5 hypotheses, temperature 0.7
  ↓ none verify
Pass 2: ONE retry; prompt includes the rejected hypotheses, the residual, and the
        component taxonomy [fees, refunds, chargebacks, reserves, rounding,
        adjustments, FX]
  ↓ none verify
STOP → NO_HYPOTHESIS_VERIFIED
```

Hard cap: 2 passes, 10 hypotheses per divergence. No adaptive retry loops — an unbounded search over an under-determined problem eventually fits by coincidence, and a coincidental fit is worse than an escalation.

**Verifier checks, all deterministic:** `Σ(components) == residual` exactly in paise · every cited artifact exists · every component within its permitted range (MDR ≤ 4%).

### Tests written in this phase

- **Unit:** verifier rejects off-by-one-paisa; rejects a missing artifact citation; rejects an out-of-range MDR; the LLM is *not* called when `fee` + `tax` fully explain the delta.
- **Chaos:** invalid class returned → falls back, `mode == DEGRADED`; malformed JSON → falls back; timeout → falls back; generation bounded to ≤2 LLM calls per divergence.
- **Integration:** arm 3 with resolution split (rules / verified / escalated), verifier rejection rate by reason, fee-schedule coverage.

### Smoke — rung S5 added

| Step | Assertion | Budget |
|---|---|---|
| Run one `AMOUNT_MISMATCH` with the network disabled | resolves entirely from `llm_cache/`, zero network calls | 2s |
| Mock the LLM to return `"BANANA"` | `mode == DEGRADED`, `klass` in `VALID_CLASSES` | — |
| Mock the LLM to raise `TimeoutError` | `mode == DEGRADED` | — |
| Count LLM calls for one divergence | ≤ 2 | 1s |

The first row is the one that saves the demo. **The demo must not depend on a network call** — prove it in smoke, every time, not by hoping on the day.

**Exit criteria**

- [ ] Three arms produce comparable numbers
- [ ] Every LLM response cached to disk and committed
- [ ] Chaos tests green
- [ ] Rules-only vs full-agent throughput reported side by side — rules-only will be far faster, and saying so is the AI-judgment criterion
- [ ] `make smoke` still under 30s, with the network off

---

# PHASE 6 — Frontend

**Gate:** 🛑 Stop for review. **Timebox: one day. The demo must work with the frontend switched off.** **Target:** Day 6.

The brief says *"if you are choosing colours, you have gone too far."* That is a warning against spending build time *deciding*. The answer is not an ugly UI — it is a UI whose decisions are already made. §UI below pre-specifies the entire design system so implementation is assembly, not design. **Build the API endpoints first, then the screens.**

### Build — API first

```
POST /api/runs                     start a reconciliation run
GET  /api/runs                     list runs with summary metrics
GET  /api/runs/{id}                run detail: metrics, throughput, arm
GET  /api/runs/{id}/divergences    filterable by class, reason_code, resolved
GET  /api/divergences/{id}         full escalation packet
GET  /api/ledger/verify            walk the chain, return ok + break index
GET  /api/runs/{id}/exceptions.csv download
```

### Files

```
Create: src/statesync/api/routes.py · src/statesync/api/schemas.py
Create: frontend/src/tokens.css              the design system, below
Create: frontend/src/components/MetricCard.tsx
Create: frontend/src/components/VerdictBadge.tsx
Create: frontend/src/components/HypothesisCard.tsx
Create: frontend/src/components/DataTable.tsx
Create: frontend/src/components/Money.tsx     paise → ₹, tabular numerals
Create: frontend/src/screens/RunsList.tsx
Create: frontend/src/screens/RunDetail.tsx
Create: frontend/src/screens/PacketViewer.tsx      ⭐ the screen that matters
Create: frontend/src/screens/LedgerViewer.tsx
Create: frontend/tests/e2e/*.spec.ts               five journeys
```

No component library. No state management library — `useState` and `fetch`.

---

## UI specification

### Design principles

1. **It is a console, not a dashboard.** The audience is one ops person deciding whether to trust a machine's judgement about money. Density over whitespace, evidence over summary.
2. **Money is always monospaced, always right-aligned, always tabular-numeral.** Financial figures that don't line up vertically cannot be scanned, and scanning is the entire job.
3. **The verdict is the loudest thing on screen.** Colour carries meaning here and nowhere else. Chrome is neutral; only verdicts, deltas and chain state are coloured.
4. **Show rejected work.** The rejected hypotheses are the product. A UI that hides them is hiding the thing that makes the system trustworthy.

### Tokens

Decided here so nobody decides them at 2am on Day 6. Dark-first — this is a tool that lives on a second monitor next to a terminal.

```css
:root {
  /* surface — cool neutral, four steps */
  --bg:        #0d1117;   /* page */
  --surface:   #161b22;   /* cards, table rows */
  --surface-2: #1c2129;   /* hover, nested cards */
  --border:    #2a313c;
  --border-strong: #3d4653;

  /* text */
  --text:      #e6edf3;
  --text-dim:  #9aa7b4;
  --text-faint:#6b7684;

  /* semantic — the only colours that carry meaning */
  --verified:  #3fb950;   /* VERIFIED, chain OK, repair succeeded */
  --rejected:  #f85149;   /* chain break, failed repair, range violation */
  --ambiguous: #d29922;   /* AMBIGUOUS_MULTIPLE_VERIFIED, escalated */
  --degraded:  #a371f7;   /* DEGRADED_MODE */
  --accent:    #2f81f7;   /* links, focus rings, primary action */
}
```

Light theme: swap the surface ramp to `#ffffff / #f6f8fa / #eef1f4`, text to `#1f2328 / #59636e`, keep semantic hues, darken them one step for contrast. Every semantic colour must hit **4.5:1** against its background — check `--ambiguous` on light, it is the one that fails.

### Type

| Role | Face | Size / weight |
|---|---|---|
| Screen title | Inter | 20px / 600 |
| Section label | Inter | 11px / 600 / uppercase / 0.06em tracking / `--text-faint` |
| Body, table cells | Inter | 13px / 400 |
| **All money, IDs, hashes** | **JetBrains Mono** | **13px / 450 / `font-variant-numeric: tabular-nums`** |
| Metric card figure | JetBrains Mono | 28px / 500 / tabular-nums |

Two faces, no more. If a third is tempting, the answer is a weight, not a family.

### Components

**`<Money paise={394420} />`** → `₹3,894.20`, monospace, right-aligned, tabular. Negative amounts get `--rejected` and a leading `−` (U+2212, not a hyphen). This component exists so that no screen ever formats currency itself — the integer-paise rule needs exactly one place where it converts to display.

**`<VerdictBadge verdict="VERIFIED" />`** — 11px uppercase, 3px radius, 2px/6px padding, background at 12% of the semantic hue, text at full hue, 1px border at 30%.

| Verdict | Colour |
|---|---|
| `VERIFIED` | `--verified` |
| `ARITHMETIC_FAILED` | `--rejected` |
| `ARTIFACT_MISSING` | `--rejected` |
| `RANGE_VIOLATION` | `--rejected` |
| `AMBIGUOUS_MULTIPLE_VERIFIED` | `--ambiguous` |
| `NO_HYPOTHESIS_VERIFIED` | `--ambiguous` |
| `FEE_SCHEDULE_UNKNOWN` | `--text-dim` |
| `DEGRADED` | `--degraded` |

**`<MetricCard label value unit trend?>`** — 11px uppercase label, 28px mono figure, optional 12px dim sub-line. No sparklines, no icons.

**`<DataTable>`** — 13px, 8px/12px cell padding, 1px `--border` row separators, sticky header, hover row `--surface-2`. Numeric columns right-aligned and monospaced. Zebra striping is off; the border is enough and stripes fight the verdict colours.

**`<HypothesisCard>`** — specified with screen 3 below.

### Screen 1 — Runs list

Single full-width table. Columns: **Started** (relative + absolute on hover) · **Arm** (badge: none / rules / full) · **Records** · **Match rate** (percentage, mono) · **Exceptions** (count, `--ambiguous` if > 0) · **Throughput** (rec/s, mono) · **Chain** (● green or red). Row click → run detail. A single primary button top-right: **Start run**, with an arm selector.

Empty state: *"No runs yet."* plus the exact command — `make eval ARM=rules` — because the reader is likelier to be in a terminal than to click the button.

### Screen 2 — Run detail

Header: run ID (mono), arm badge, timestamp, status. **Download exceptions.csv** as a secondary button.

Row of six metric cards: **Match rate** · **Throughput** (rec/s) · **Wall clock** (s) · **LLM calls** (with per-100-records sub-line) · **Exceptions** · **Fee-schedule coverage**.

Below, two panels side by side:

- **Detection by class** — table: class · injected · detected · rate · misses. `SETTLEMENT_GAP` gets a `SYNTHETIC` badge and a footnote: *demonstrated against synthetic payout data, not validated against real settlement behaviour.* Never merged into the headline figure.
- **Resolution split** — a single horizontal stacked bar: resolved-by-rules / verified-hypothesis / escalated, with counts. One bar, not a pie.

Then the divergences table, filterable by class, reason code, and resolved state. Filters are chips, not dropdowns — the reader needs to see the active filter without opening anything.

### Screen 3 — Escalation packet viewer ⭐

**This is the screen the submission is judged on.** It is the visual form of the project's central claim: the model proposes, the verifier decides, and you can see the arithmetic.

Layout, top to bottom:

**a. Divergence header** — payment ID, order ID, instrument, timestamps, class badge, reason-code badge.

**b. The arithmetic strip.** The single most important element. A monospace right-aligned block, laid out as a subtraction:

```
Order total                                    ₹4,000.00
Known fee          (payment.fee_paise)           −₹80.00
Known tax          (payment.tax_paise)           −₹14.40
                                               ──────────
Expected                                       ₹3,905.60
Observed                                       ₹3,894.20
                                               ──────────
RESIDUAL                                          ₹11.40
```

`RESIDUAL` in `--ambiguous`, 20px, the largest figure on the screen. Each known component labels its source in `--text-faint` — this is the provenance claim, made visible.

**c. Hypothesis cards**, one per hypothesis, **verified first, then rejected**, all of them. Each card:

- Left rail 3px in the verdict colour
- Header: `H1`…`H10` in mono, verdict badge right-aligned
- Component table: name · amount (mono, right) · citation (mono, `--text-faint`; if the artifact is missing, struck through in `--rejected`)
- **Arithmetic line, always shown, verified or not:**
  `Σ = ₹8.40 + ₹3.00 = ₹11.40` then `✓ matches residual` in `--verified`, or `✗ off by ₹0.60` in `--rejected`
- Rejection reason in plain language when not verified: *"Cited refund `rfnd_ghost` does not exist."* / *"MDR of 12.5% exceeds the 4% permitted range."*

**d. Suggested action** — in a bordered box labelled **TEMPLATED**, with a `--text-faint` footnote: *"Generated from a fixed template populated from the audit ledger. No model output produces any figure on this screen."* Say it on the screen, not just in the README. It is the whole architectural thesis, and this is the one place a reviewer will be looking.

For case 13, two `VERIFIED` cards appear side by side with visibly different component breakdowns — the reader distinguishes "MDR + instant settlement" from "partial refund + MDR on remainder" without re-deriving anything. For case 14, ten rejected cards where seven are off by consistent paise teaches a human something a bare "escalated" never conveys.

### Screen 4 — Ledger viewer

Virtualised list, newest first. Each row: `seq` · `event_type` badge · `divergence_key` · actor · timestamp · `hash` truncated to 12 chars with a copy affordance. Expand a row for the full canonical payload, pretty-printed mono.

Top-right: a **Verify chain** button. Three states —

- Idle: neutral outline
- **OK:** solid `--verified`, `✓ Chain verified · 1,284 entries · GENESIS → a3f9…`
- **BROKEN:** solid `--rejected`, `✗ Chain broken at seq 47`, the list scrolls to entry 47, and that row plus everything after it gets a `--rejected` left rail and 40% opacity. The visual says *everything below this point is unreliable*, which is exactly what a fail-closed system means.

### States

Every screen needs three besides the happy path, and they are the first thing to get skipped:

- **Loading:** skeleton rows at the real row height. No spinners — layout shift on a table of figures is disorienting.
- **Empty:** one line of plain text plus the terminal command that would produce data.
- **Error:** the HTTP status, the endpoint, and a Retry button. Never a bare "Something went wrong" — the reader is a developer.

### Accessibility floor

Verdict is never carried by colour alone — the badge always has text. All interactive elements reachable by keyboard with a visible `--accent` focus ring. Tables use real `<th scope>`. The Verify result is announced in an `aria-live="polite"` region. Contrast ≥ 4.5:1 for text, ≥ 3:1 for the borders and rails that carry state.

### What is explicitly out of scope

No charts beyond the one stacked bar. No animations beyond a 120ms colour transition on hover and the verify state change. No dark/light toggle — pick dark, ship. No responsive mobile layout; this is a desktop ops tool and a min-width of 1100px is an honest constraint. No login. **If you find yourself building a component library, stop.**

### Tests written in this phase

- **Component:** packet viewer renders 10 hypotheses with the correct verdict badges; ledger viewer shows red and the break index on a broken chain; `<Money>` renders 394420 as `₹3,894.20` and never uses a float internally.
- **User journeys (Playwright), written as ops scenarios:**
  1. *Reviews an exception* — dashboard → latest run → filter `NO_HYPOTHESIS_VERIFIED` → open → residual and all 10 hypotheses visible with arithmetic and failure reasons.
  2. *Distinguishes an ambiguous case* — filter `AMBIGUOUS_MULTIPLE_VERIFIED` → open → exactly two `VERIFIED` badges with different component breakdowns.
  3. *Verifies integrity* — ledger → Verify → green.
  4. *Detects tampering* — tamper an entry via a test fixture → Verify → red with the break index, rows after it dimmed.
  5. *Exports* — run detail → download exceptions CSV → file non-empty, reason codes present.

### Smoke — `make smoke-frontend`, separate budget

| Step | Assertion | Budget |
|---|---|---|
| `vite build` | exits 0, no type errors | 15s |
| Boot API + preview server | both respond to health | 5s |
| Load runs list | HTTP 200, at least one row rendered | 3s |
| Open latest packet | residual visible, ≥1 hypothesis card rendered | 3s |
| Click Verify on ledger | green state within 2s | 2s |

**Exit criteria**

- [ ] All five user journeys pass
- [ ] `make demo` works with the frontend switched off
- [ ] `make smoke-frontend` green
- [ ] `make smoke` (backend) still under 30s and unaffected
- [ ] No component library and no state library in `package.json`

---

# PHASE 7 — Hardening and reproducibility

**Target:** Day 6–7.

### Build

Remaining chaos tests. Throughput instrumentation confirmed on every arm. Full eval run with results committed to `eval/results/`. README with every number and how to reproduce it. `ARCHITECTURE.md` with the decision log and the cited GitHub issues.

### Remaining chaos tests

```python
def test_api_500_midbatch_resumes_no_double_repair():
    with fail_after(250):
        run_batch(records)
    run_batch(records)
    assert store.write_count == expected_repairs

def test_worker_crash_midrepair_surfaces():
    with crash_during_execute():
        try: repair(d)
        except: pass                       # test scaffolding only, never src/
    advance_clock(LEASE_SECONDS + 1)
    assert repair(d).reason == "STUCK_REPAIR"

def test_concurrent_workers_only_one_proceeds():
    results = run_parallel(lambda: repair(d), n=10)
    assert sum(r.status == "SUCCEEDED" for r in results) == 1
    assert store.write_count == 1

def test_broken_chain_halts_all_repairs():
    ledger.entries[3].payload["x"] = "tampered"
    with pytest.raises(ChainIntegrityError):
        run_batch(records)
    assert store.write_count == 0          # fail CLOSED
```

### Smoke — rung S6 added

| Step | Assertion | Budget |
|---|---|---|
| Tamper one ledger entry via `python -m eval.tamper` | — | 1s |
| Run a small batch | raises `ChainIntegrityError`, process exits non-zero | 1s |
| Check writes | **`write_count == 0`** | — |
| Restore the ledger fixture | chain verifies again, smoke leaves no residue | — |

### The reproducibility test — do this literally

```bash
cd /tmp && rm -rf sstest && git clone <repo> sstest && cd sstest
make setup && make verify
```

Fix whatever breaks. **Every project fails this the first time** — usually an uncommitted file, a hardcoded absolute path, or a dependency that was installed globally months ago. Run it on Day 6, not Day 7.

**Exit criteria**

- [ ] Fresh clone reproduces every README number in one command
- [ ] `make verify` green
- [ ] `grep -rn "except.*:\s*pass" src/` returns nothing
- [ ] Every number in the README is regenerated by `make eval`
- [ ] `make smoke` under 30s with all seven rungs

---

## Makefile — the single entrypoint

```makefile
setup            # deps, docker up, migrate
test-unit test-prop test-int test-chaos test-e2e
smoke            # <30s, all rungs, must always pass
smoke-frontend   # build + boot + load, separate budget
verify           # unit + property + integration + chaos + smoke
eval             # 3-arm run, regenerates README numbers
eval-arm ARM=    # single arm
demo             # the exact video sequence
clean
```

## Demo sequence

Rehearse three times, record the third.

| Beat | Command | On screen |
|---|---|---|
| 1 | `make setup` | Fresh clone, clean install |
| 2 | `python -m eval.generate --seed 20260905 --n 500` | 500 records, 15 hard cases, ground truth known |
| 3 | `make eval ARM=rules` | Arm 2 numbers, throughput |
| 4 | `make eval ARM=full` | Arm 3 numbers, throughput, LLM call count |
| 5 | Packet viewer, case 7 | **Two legit orders. NOT merged.** |
| 6 | Packet viewer, case 14 | Residual, 10 hypotheses, arithmetic for each, why each failed |
| 7 | `make eval ARM=full` again | **Zero additional writes** |
| 8 | `redis-cli FLUSHALL && make eval ARM=full` | **Still zero.** DB constraint held |
| 9 | `python -m eval.tamper`, then Verify in the ledger viewer | Chain break → red at the index, halt, non-zero exit, zero writes |
| 10 | `cat exceptions.csv` | The honest residual |

Beats 5–8 are the submission. Everything before is setup; everything after is honesty.

## Definition of done

- [ ] `git clone` → `make setup && make verify` green on a fresh machine
- [ ] `make eval` reproduces every number in the README
- [ ] Running the same batch twice writes nothing the second time
- [ ] Flushing Redis and re-running writes nothing
- [ ] Tampering with the ledger halts the run with a non-zero exit and zero writes
- [ ] Cases 7, 13, 14 pass — the system refuses to act when it should
- [ ] `exceptions.csv` committed and non-empty
- [ ] Throughput reported for every arm
- [ ] Zero `except: pass` in the codebase
- [ ] `make demo` works with the frontend switched off
- [ ] All five UI user journeys pass

---

## Two notes on sequencing

**Throughput belongs in Phase 2, not Phase 7.** It is the first word of the published bar and it appears nowhere in the original ideation doc. Instrument it the moment the first arm runs, or it becomes a Day 7 retrofit that reports a number nobody trusts.

**Freeze after Phase 5.** No new features. Phases 6 and 7 are UI, hardening, docs and video. Every project that dies dies from a feature added the night before.
