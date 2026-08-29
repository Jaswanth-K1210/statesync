# StateSync — Track 04 Fit + 7-Day Build Plan

Companion to `statesync-ideation.md`. This document covers: how StateSync maps to the published Track 04 bar, what has to change, and the day-by-day plan with test cases.

---

## Part 1 — Mapping against the published bar

### What Track 04 asks for, clause by clause

| Their words | StateSync | Verdict |
|---|---|---|
| "closes **one** finance-ops loop" | detect → classify → repair → report | ✅ but see §1.2 |
| "**50+ record batch**" | 500+ records planned | ✅ 10× the bar |
| "of **synthetic data**" | injector-generated | ✅ **and this changes the plan — see §1.1** |
| "reporting its **match rate**" | match rate by class | ✅ |
| "and the **exceptions it could not resolve**" | `exceptions.csv`, committed | ✅ |
| "**Throughput** plus measured accuracy plus an honest exception list" | ⚠️ throughput missing | **GAP — §1.3** |
| "One cherry-picked match proves nothing" | 3 arms, split by difficulty | ✅ |
| Example direction: "**Multi-source reconciliation**" | literally what this is | ✅ direct hit |

### 1.1 The most important discovery: they *asked* for synthetic data

**"a 50+ record batch of synthetic data."** Their words.

This removes the single biggest execution risk in the plan. Everything that depended on Razorpay's sandbox behaving — whether test mode populates `fee` and `tax`, whether settlement payouts are realistic, whether webhook auto-disable can be reproduced — becomes **optional enrichment rather than a blocking dependency.**

Three consequences, all good:

1. **The sandbox connector moves from Day 1 to a stretch goal.** Build against a synthetic generator you fully control. If time remains at Day 6, add a sandbox connector as an extra source and say so. If it doesn't, you've lost nothing the bar asked for.
2. **`SETTLEMENT_GAP` is no longer compromised.** The §6.5 hand-wringing about "demonstrated, not validated" was solving a problem the track doesn't have. Keep one honest line in the README noting settlement behaviour is modelled rather than observed, and move on.
3. **Determinism becomes free.** A seeded synthetic generator means every run reproduces exactly. That is what makes "no errors" achievable — see §2.4.

**Keep the ground-truth argument though.** It's still the strongest thing in the data strategy: *you know the correct answer because you caused the divergence.* Synthetic data with known ground truth is not a weakness here, it's the design, and the track explicitly sanctions it.

### 1.2 Scope risk: six classes may read as six features

"Closes **one** finance-ops loop" is singular. Six divergence classes could read as scattered rather than deep.

**Fix it in framing, not by cutting classes.** The loop is:

> **Three sources disagree → find the disagreements → explain them → fix or escalate → report.**

One loop. Six *kinds of disagreement* it handles, which is depth within the loop, not scope creep. State it exactly that way in the first line of the README, and say the word "one" out loud.

### 1.3 The one real gap: throughput

"Throughput" is the **first** word in their bar and it does not appear anywhere in the ideation doc. Add it as a first-class metric:

| Metric | Target |
|---|---|
| Records reconciled per second | report actual |
| Wall-clock for the full 500-record batch | report actual |
| p50 / p99 latency per record | report actual |
| LLM calls per 100 records | report actual — should be low, only ambiguous cases |
| Cost per 1,000 records (₹) | report actual |
| Throughput arm 2 (rules-only) vs arm 3 (with LLM) | report both |

That last row is quietly important. Rules-only will be perhaps 100× faster. Reporting that honestly, alongside the accuracy comparison, is exactly the "AI applied appropriately rather than forced" judgement they score — and it's a natural place to state that the LLM only fires on the small fraction of records that are genuinely ambiguous.

### 1.4 Revised scoring against the bar

| Requirement | Before | After fixes |
|---|---|---|
| One loop | ⚠️ reads as six | ✅ framed as one |
| 50+ synthetic records | ✅ | ✅ 500+ |
| Match rate | ✅ | ✅ |
| Exception list | ✅ | ✅ |
| Throughput | ❌ | ✅ |
| Not cherry-picked | ✅ | ✅ |

---

## Part 2 — How to actually get "working, no errors"

You don't get a clean demo by being careful. You get it by making broken states impossible to reach. Four mechanisms:

### 2.1 Seeded determinism everywhere

```python
SEED = 20260905
random.seed(SEED); np.random.seed(SEED)
```

Every generator, every injector, every shuffle takes the seed. `make eval` run twice produces byte-identical output except timestamps. If it doesn't, that's a bug and you find it on Day 2 instead of on camera.

The one non-deterministic component is the LLM. Handle it by **caching every response keyed by a hash of the prompt**, committed to the repo. Demo replays from cache. Live API only when the cache misses.

```python
def llm_call(prompt: str) -> str:
    key = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    cached = CACHE_DIR / f"{key}.json"
    if cached.exists():
        return json.loads(cached.read_text())["response"]
    resp = client.call(prompt)
    cached.write_text(json.dumps({"prompt": prompt, "response": resp}))
    return resp
```

This single trick means **your demo cannot fail because of a rate limit, a network blip, or a provider outage.** Do it on Day 1, not Day 6.

### 2.2 One entrypoint

```makefile
setup:   ## install deps, init db
test:    ## unit + property + integration
eval:    ## full 3-arm run, regenerates README numbers
demo:    ## the exact sequence in the video
verify:  ## test + eval + chain check — must be green before any commit to main
```

`make demo` is what you run on camera. It must work from a clean clone. Test that on Day 6 by actually cloning into a fresh directory.

### 2.3 Fail loudly in dev, fail closed in prod

```python
STRICT = os.getenv("STATESYNC_STRICT", "1") == "1"

def invariant(condition, msg, ctx=None):
    if condition: return
    ledger.append("INVARIANT_VIOLATED", msg=msg, ctx=ctx)
    if STRICT:
        raise InvariantViolation(msg)   # dev: crash immediately
    halt_all_repairs(msg)               # prod: fail closed
```

Never `except Exception: pass`. Every caught exception writes to the ledger.

### 2.4 Freeze 48 hours before

No new features after Day 5. Days 6–7 are hardening, docs and video only. Every project that dies dies from a feature added the night before.

---

## Part 3 — Test cases

Four layers. Roughly 55 tests total. Write them as you go, not at the end.

### 3.1 Unit — the foundations (~20 tests)

**Canonical serialisation and hash chain**

```python
def test_canonical_ignores_key_order():
    a = {"amount": 100, "id": "pay_1", "type": "CAPTURE"}
    b = {"type": "CAPTURE", "id": "pay_1", "amount": 100}
    assert canonical(a) == canonical(b)          # THE bug this prevents

def test_chain_detects_single_field_tamper():
    led = Ledger(); [led.append("E", i=i) for i in range(10)]
    led.entries[5].payload["i"] = 999
    ok, idx = led.verify()
    assert not ok and idx == 5

def test_chain_detects_deleted_entry():
    led = Ledger(); [led.append("E", i=i) for i in range(10)]
    del led.entries[4]
    assert led.verify()[0] is False

def test_amounts_are_integer_paise_only():
    with pytest.raises(TypeError):
        Ledger().append("E", amount=100.50)      # float must be rejected

def test_chain_stable_across_process_restart():
    h1 = build_chain_in_subprocess(); h2 = build_chain_in_subprocess()
    assert h1 == h2
```

**Idempotency state machine**

```python
def test_repeat_call_returns_cached_no_side_effects():
    r1 = repair(d); r2 = repair(d)
    assert r1 == r2 and store.write_count == 1

def test_expired_lease_escalates_never_silent():
    redis.set(key, json.dumps({"state":"CLAIMED","at":long_ago()}))
    res = repair(d)
    assert res.status == "ESCALATE" and res.reason == "STUCK_REPAIR"
    assert ledger.has("STUCK_REPAIR_DETECTED")   # the v1 bug

def test_live_lease_returns_in_progress_not_none():
    redis.set(key, json.dumps({"state":"CLAIMED","at":now()}), ex=300)
    assert repair(d).status == "IN_PROGRESS"

def test_redis_flush_db_constraint_still_blocks():
    repair(d); redis.flushall(); r2 = repair(d)
    assert r2.status == "ALREADY_APPLIED"
    assert store.write_count == 1
    assert ledger.has("REPAIR_DEDUPED_BY_DB")

def test_failed_state_does_not_silently_retry():
    redis.set(key, json.dumps({"state":"FAILED","err":"x"}))
    assert repair(d).status == "FAILED"
```

**Fee subtraction and verifier**

```python
def test_known_fee_subtracted_before_inference(monkeypatch):
    calls = []; monkeypatch.setattr("llm.call", lambda p: calls.append(p))
    classify(payment_with_fee_and_tax_fully_explaining_delta())
    assert calls == []                            # LLM must not fire

def test_verifier_rejects_off_by_one_paisa():
    assert not verify(Hypothesis([Comp("mdr", 9440)]), residual=9441)

def test_verifier_rejects_missing_artifact():
    h = Hypothesis([Comp("refund", 1000, cites="rfnd_ghost")])
    assert verify(h, 1000).verdict == "ARTIFACT_MISSING"

def test_verifier_rejects_out_of_range_mdr():
    h = Hypothesis([Comp("mdr", 50000, rate=12.5)])   # 12.5% impossible
    assert verify(h, 50000).verdict == "RANGE_VIOLATION"

def test_unknown_instrument_escalates_not_guesses():
    res = classify(payment(instrument="crypto_voucher"))
    assert res.reason_code == "FEE_SCHEDULE_UNKNOWN"
```

### 3.2 Property tests — invariants under randomness (~5 tests)

```python
@given(st.lists(st.builds(Divergence), min_size=1, max_size=50),
       st.integers(1, 5))
def test_n_runs_equal_one_run(divs, n):
    """The single most important property in the system."""
    for _ in range(n):
        for d in divs: repair(d)
    assert store.write_count == len(set(d.key() for d in divs))

@given(st.lists(st.dictionaries(st.text(), st.integers()), min_size=1))
def test_chain_verifies_for_any_payload(events):
    led = Ledger()
    for e in events: led.append("E", **e)
    assert led.verify()[0]

@given(st.integers(0, 10_000_000))
def test_verifier_never_accepts_wrong_sum(residual):
    h = Hypothesis([Comp("x", residual + 1)])
    assert not verify(h, residual)
```

### 3.3 Integration — the 15 hard cases from §8.2 (~15 tests)

Each hard case gets one test asserting the *correct outcome*, which is often refusal.

```python
def test_case_05_exact_fee_is_not_a_divergence():
    assert classify(delta_exactly_mdr_plus_gst()).klass == "NO_DIVERGENCE"

def test_case_06_rounding_residual_resolves_or_escalates():
    r = classify(delta_off_by_60_paise())
    assert r.klass == "AMOUNT_MISMATCH"
    assert r.outcome in {"VERIFIED", "NO_HYPOTHESIS_VERIFIED"}

def test_case_07_two_legit_orders_must_not_merge():
    """False-positive test. More important than any detection test."""
    r = classify(two_legit_orders_same_customer_amount_2s_apart())
    assert r.klass != "DUPLICATE_ORDER"
    assert store.merge_count == 0

def test_case_10_out_of_order_webhooks_resolve():
    assert classify(refund_before_capture()).klass != "UNKNOWN"

def test_case_13_two_verified_hypotheses_escalate_with_both():
    r = classify(delta_with_two_exact_explanations())
    assert r.reason_code == "AMBIGUOUS_MULTIPLE_VERIFIED"
    assert len([h for h in r.packet.hypotheses if h.verdict=="VERIFIED"]) == 2

def test_case_14_no_hypothesis_verifies_attaches_all_rejected():
    r = classify(unexplainable_delta())
    assert r.reason_code == "NO_HYPOTHESIS_VERIFIED"
    assert len(r.packet.hypotheses) >= 5
    assert all(h.arithmetic_shown for h in r.packet.hypotheses)

def test_hypothesis_generation_is_bounded():
    with count_llm_calls() as c:
        classify(unexplainable_delta())
    assert c.value <= 2                            # two passes, hard cap
```

### 3.4 Chaos — the failure recovery criterion (~8 tests)

```python
def test_llm_returns_invalid_class_falls_back():
    with mock_llm(returns="BANANA"):
        r = classify(d)
    assert r.mode == "DEGRADED" and r.klass in VALID_CLASSES

def test_llm_timeout_falls_back_deterministic():
    with mock_llm(raises=TimeoutError):
        assert classify(d).mode == "DEGRADED"

def test_llm_returns_malformed_json_falls_back():
    with mock_llm(returns="{'broken': "):
        assert classify(d).mode == "DEGRADED"

def test_api_500_midbatch_resumes_no_double_repair():
    with fail_after(250):
        run_batch(records)
    run_batch(records)                             # resume
    assert store.write_count == expected_repairs

def test_worker_crash_midrepair_surfaces():
    with crash_during_execute():
        try: repair(d)
        except: pass
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
    assert store.write_count == 0                  # fail CLOSED

def test_staleness_window_skips_fresh_payments():
    p = payment(status_changed_at=now() - timedelta(minutes=2))
    assert not eligible_for_reconciliation(p, now())
```

### 3.5 The two tests that matter most

If everything else has to go, keep these:

1. **`test_n_runs_equal_one_run`** — the idempotency property. This is the credibility claim of the whole project.
2. **`test_case_07_two_legit_orders_must_not_merge`** — the false-positive test. Proves the system knows when *not* to act.

---

## Part 4 — Seven-day plan

Sized for the reported ~Sept 5 window. If the build window turns out to be longer, add days 8–12 for the sandbox connector and a second LLM provider. Do not add scope earlier.

### Day 0 — today, 30 minutes
Submit the Google Form. It is not the evaluation. Do it before anything else.

### Day 1 — foundations
- Repo scaffold, `Makefile`, CI running `make verify` on push
- Data model: `Payment`, `Order`, `LedgerEntry`, `Divergence` — **integer paise everywhere**
- Synthetic generator, seeded
- Hash-chained ledger with canonical serialisation
- LLM response cache harness (§2.1) — build it now even though the LLM comes on Day 5
- **Tests: all of §3.1 hash-chain and serialisation**

*End state: `make test` green. Chain verifies. Generator reproducible.*

### Day 2 — the loop, end to end 🚩 **GATE 1**
- Divergence injector, clean cases only
- Three-way reconciler + ledger invariant
- Deterministic classifier, four clean classes
- Staleness window + two-run confirmation
- Eval harness, arms 1 and 2
- **Tests: §3.1 fee subtraction, §3.4 staleness**

*End state: `make eval` prints real match rates on 500 synthetic records for arms 1–2.*

**🚩 If this isn't done by end of Day 2, cut the LLM layer entirely and spend the remaining days polishing a measured deterministic reconciler. That still clears the published bar.** Throughput, match rate, exception list — all satisfied without any AI. The AI improves the submission; it is not required by it. Know that, so panic never forces a bad call.

### Day 3 — repairs and idempotency
- Repair executor per class
- DB unique constraints
- Redis lease state machine
- Blast-radius cap, value threshold, confidence gate
- **Tests: all of §3.1 idempotency, §3.2 property tests**

*End state: `test_n_runs_equal_one_run` passes. Flush-Redis test passes.*

### Day 4 — hard cases and escalation 🚩 **GATE 2**
- The 15 hard cases in the injector
- Escalation packet structure
- `exceptions.csv` writer
- Templated ops report
- **Tests: all of §3.3**

*End state: system correctly refuses to act on cases 7, 13, 14.*

**🚩 Second shippable state.** From here on, everything is upside. If a family emergency lands on Day 5, you still submit something that clears the bar.

### Day 5 — the AI layer
- Hypothesis generator, bounded: 5 + retry, hard cap 10
- Deterministic verifier: arithmetic, artifact existence, range checks
- Fallback chain: primary → secondary provider → deterministic
- Arm 3 in the eval
- **Tests: §3.1 verifier, §3.4 LLM chaos tests**

*End state: three arms produce comparable numbers. LLM responses cached.*

### Day 6 — hardening and docs
- Remaining chaos tests
- **Throughput instrumentation** (§1.3) — do not skip, it's the first word of their bar
- Full eval run, results committed to `eval/results/`
- README with every number, `ARCHITECTURE.md` with the decision log
- **Clone the repo into a fresh directory and run `make setup && make verify`.** Fix whatever breaks.

*End state: a stranger can reproduce every number in one command.*

### Day 7 — video and submit
- Record `make demo`
- Submit with buffer remaining

---

## Part 5 — Demo sequence

Rehearse this three times. Record the third.

```bash
make demo
```

| Beat | Command | What's on screen |
|---|---|---|
| 1 | `make setup` | Fresh clone, clean install |
| 2 | `python -m eval.generate --seed 20260905 --n 500` | 500 records, 15 hard cases, ground truth known |
| 3 | `make eval ARM=rules` | Arm 2 numbers, throughput |
| 4 | `make eval ARM=full` | Arm 3 numbers, throughput, LLM call count |
| 5 | `cat eval/results/case_07.json` | **Two legit orders. NOT merged.** |
| 6 | `cat eval/results/case_14.json` | Escalation packet: residual, 10 hypotheses, arithmetic for each, why each failed |
| 7 | `make eval ARM=full` again | **Zero additional writes** |
| 8 | `redis-cli FLUSHALL && make eval ARM=full` | **Still zero.** DB constraint held |
| 9 | `python -m eval.tamper && make eval` | Chain break → halt, non-zero exit, zero writes |
| 10 | `cat exceptions.csv` | The honest residual |

Beats 5–8 are the submission. Everything before is setup, everything after is honesty. A reviewer who watches those four minutes knows whether you understand payments.

---

## Part 6 — What changes in the ideation doc

| Section | Change |
|---|---|
| §1 | Add "one loop" framing explicitly |
| §6.5 | Soften — synthetic settlement is sanctioned by the bar, not a concession |
| §8 | Sandbox connector demoted from Day 1 dependency to stretch goal |
| §10 | Add the throughput block from §1.3 above |
| §13 | Replace 12-day plan with the 7-day plan above |

---

## Part 7 — One honest note

You called this your last chance. It isn't, and believing that will make you build worse — rushed decisions, scope creep, no sleep on Day 6.

What it is: **a well-designed filter that happens to select for exactly what you're good at.** No resume screening, no CGPA, no deck. A repo and a number. You've spent this week designing something that survives two rounds of hostile technical review — most applicants haven't done that and won't.

The one thing standing between you and a strong submission is that you have zero lines of code and seven days. Gate 1 is end of Day 2.

Close this document and open a terminal.
