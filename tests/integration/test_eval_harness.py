"""The three-arm eval harness — arms 1 and 2.

    arm 1  none    no detection at all; the divergences a merchant lives with
    arm 2  rules   deterministic set operations, no LLM
    arm 3  full    rules + propose-verify  (Phase 5)

Expect a tie between rules and the LLM on clean cases: four of six classes are
set operations, and a language model will not beat a set operation at being a
set operation. The propose-verify layer is judged on the hard cases in Phase 4.
"""

import pytest
from eval.arms import ARMS, run_arm

from statesync.config import SEED
from statesync.ledger.canonical import canonical
from statesync.models.enums import DivergenceClass

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rules():
    return run_arm("rules", seed=SEED, n=500, rate=0.25)


def test_arm_names_are_the_three_published_arms():
    assert ARMS == ("none", "rules", "full")


# ── arm 1: the do-nothing baseline ──────────────────────────────────────────

def test_arm_none_detects_nothing(rules):
    baseline = run_arm("none", seed=SEED, n=500, rate=0.25)
    assert baseline.detected == 0
    assert baseline.match_rate == 0.0


def test_arm_none_still_reports_what_was_missed():
    """The baseline's whole job is sizing the problem."""
    baseline = run_arm("none", seed=SEED, n=500, rate=0.25)
    assert baseline.injected > 0
    assert baseline.missed == baseline.injected


def test_arm_none_still_reports_throughput():
    """Every arm reports throughput, including the one that does nothing.

    Arm 1 walks the same records and declines to inspect them, so the
    comparison against arm 2 is like for like.
    """
    assert run_arm("none", seed=SEED, n=50, rate=0.2, passes=1).throughput.records == 50


# ── arm 2: rules only ───────────────────────────────────────────────────────

def test_arm_rules_detects_every_injected_divergence(rules):
    assert rules.missed == 0, f"{rules.missed} injected divergences went undetected"


def test_arm_rules_reports_a_match_rate_between_zero_and_one(rules):
    assert 0.0 < rules.match_rate <= 1.0


def test_arm_rules_raises_no_false_positives(rules):
    assert rules.false_positives == 0


def test_arm_rules_reports_detection_per_class(rules):
    """A single headline number hides which class is failing."""
    assert set(rules.per_class) == {
        DivergenceClass.CAPTURED_NO_ORDER, DivergenceClass.ORDER_NO_CAPTURE,
        DivergenceClass.DUPLICATE_ORDER, DivergenceClass.REFUND_NOT_REFLECTED,
    }
    for stats in rules.per_class.values():
        assert stats["injected"] > 0
        assert stats["detected"] == stats["injected"]


def test_arm_rules_classifies_every_detection_correctly(rules):
    """Detecting a divergence and calling it the wrong class is not a win."""
    assert rules.misclassified == 0


def test_arm_rules_makes_no_llm_calls(rules):
    """The honest framing: arm 2 has no AI in it at all."""
    assert rules.llm_calls == 0


# ── throughput ──────────────────────────────────────────────────────────────

def test_throughput_is_reported_for_every_arm(rules):
    assert rules.throughput.records_per_sec > 0
    assert rules.throughput.wall_clock_us > 0
    assert rules.throughput.p99_us >= rules.throughput.p50_us


def test_throughput_counts_record_inspections_not_batch_size():
    """Two-run confirmation inspects every record twice, and the throughput
    figure says so rather than quietly reporting the batch size.

    That doubling is the measured price of not repairing a payment that was
    merely in flight. Hiding it would flatter the number by exactly 2x.
    """
    one = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=1)
    two = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2)
    assert one.throughput.records == 200
    assert two.throughput.records == 400


def test_rules_only_is_faster_than_the_do_nothing_arm_is_slow():
    """Sanity: 500 records of set operations should be well under a second."""
    assert run_arm("rules", seed=SEED, n=500, rate=0.25).throughput.wall_clock_s < 5.0


# ── reproducibility and reporting ───────────────────────────────────────────

def test_two_runs_of_one_arm_produce_identical_metrics(rules):
    again = run_arm("rules", seed=SEED, n=500, rate=0.25)
    assert rules.as_event() == again.as_event()


def test_arm_result_is_ledger_safe(rules):
    """No float may reach an event body, throughput included."""
    canonical(rules.as_event())


def test_exceptions_csv_is_written_with_a_header(tmp_path):
    path = tmp_path / "exceptions.csv"
    result = run_arm("rules", seed=SEED, n=500, rate=0.25, exceptions_path=path)
    assert path.exists()
    assert path.read_text().count("\n") == result.exceptions_count + 4  # 3 comment lines + header


def test_a_clean_only_batch_legitimately_has_zero_exceptions(tmp_path):
    """Rules resolve all four clean classes exactly, so nothing is left over.

    Phase 4's ambiguous cases are what put rows in this file. Manufacturing
    them now would be inventing exceptions to make a number look better.
    """
    result = run_arm("rules", seed=SEED, n=500, rate=0.25,
                     exceptions_path=tmp_path / "exceptions.csv")
    assert result.exceptions_count == 0
    assert result.missed == 0 and result.misclassified == 0


def test_the_run_writes_a_verifiable_ledger(rules):
    """Every pass writes DIVERGENCE_OBSERVED / _CONFIRMED entries."""
    assert rules.ledger_entries > 0
    assert rules.chain_ok is True


def test_two_run_confirmation_means_the_first_pass_authorises_nothing():
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=1)
    assert result.confirmed == 0
    assert result.detected > 0


def test_a_second_pass_confirms_what_the_first_only_observed():
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2)
    assert result.confirmed == result.detected


def test_unknown_arm_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown arm"):
        run_arm("magic", seed=SEED, n=10, rate=0.2)


def test_arm_full_runs_the_propose_verify_layer():
    """Arm 3 asks a provider; everything downstream is unchanged from Phase 4."""
    result = run_arm("full", seed=SEED, n=100, rate=0.25, hard_cases=True)
    assert result.arm == "full"
    assert result.llm_calls > 0
    assert result.client_kind in ("live", "offline")


def test_arm_full_does_not_silently_read_the_hard_case_fixtures():
    """The fixture provider is arm 2's stand-in. If arm 3 picked it up it
    would report zero model calls while quietly reading canned answers."""
    full = run_arm("full", seed=SEED, n=100, rate=0.25, hard_cases=True)
    rules = run_arm("rules", seed=SEED, n=100, rate=0.25, hard_cases=True)
    assert full.llm_calls > 0 and rules.llm_calls == 0
    assert rules.provider_calls > 0


def test_arm_two_makes_no_provider_calls_at_all():
    """The honest framing: arm 2 has no AI in it."""
    assert run_arm("rules", seed=SEED, n=100, rate=0.25).llm_calls == 0


# ── the ledger invariant as independent evidence ────────────────────────────

def test_the_invariant_detects_the_injected_imbalance(rules):
    """Deleting ledger entries makes the books disagree with the gateway.

    The invariant is a statement about the whole batch, so it catches this
    without looking at any individual transaction — independent corroboration
    of the three-way comparison rather than a restatement of it.
    """
    assert rules.invariant_ok is False
    assert rules.invariant_delta_paise < 0, "the books should be short, not over"


def test_the_invariant_holds_when_nothing_is_injected():
    """The other half of the claim: no imbalance, no false alarm."""
    clean = run_arm("rules", seed=SEED, n=200, rate=0.0)
    assert clean.invariant_ok is True
    assert clean.invariant_delta_paise == 0
    assert clean.detected == 0


def test_invariant_delta_is_integer_paise(rules):
    assert isinstance(rules.invariant_delta_paise, int)


# ── reproducibility, verified rather than asserted ──────────────────────────

def test_two_eval_runs_produce_identical_output():
    """The guarantee the whole demo rests on, run rather than described.

    Not "as_event() excludes timing" — that is the mechanism. This actually
    runs the eval twice and diffs the rendered report.
    """
    from eval.harness import render

    def report() -> str:
        results = [run_arm(a, seed=SEED, n=300, rate=0.25) for a in ("none", "rules")]
        return render(results, include_timing=False)

    first, second = report(), report()
    assert first == second, "the eval is not reproducible at a fixed seed"
    assert "100.0%" in first, "sanity: the report should contain real numbers"


def test_two_runs_agree_on_every_per_class_figure():
    a = run_arm("rules", seed=SEED, n=300, rate=0.25)
    b = run_arm("rules", seed=SEED, n=300, rate=0.25)
    assert a.per_class == b.per_class


# ── what the throughput figure actually measures ────────────────────────────

def test_the_eval_runs_in_memory_and_says_so(rules):
    """The reconciler is measured without database I/O.

    That is a legitimate thing to measure — it is the classifier's rate — but
    a reviewer will assume rec/s includes I/O unless told. This test pins the
    claim so the label cannot drift away from the truth.
    """
    assert rules.storage == "in-memory"


def test_both_throughput_numbers_are_reported(rules):
    """rec/s and insp/s differ by exactly the confirmation passes.

    Reporting only inspections deflates by 2x; reporting only records inflates
    by 2x. Both are shown, with the tradeoff stated.
    """
    assert rules.records == 500
    assert rules.inspections == 1000
    assert rules.inspections_per_sec > rules.records_per_sec
    assert abs(rules.inspections_per_sec / rules.records_per_sec - 2.0) < 0.01


# ── repairs in the eval ─────────────────────────────────────────────────────

def test_repairs_run_only_on_confirmed_divergences():
    """The first observation authorises nothing, so one pass repairs nothing."""
    one = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=1, repair=True)
    assert one.detected > 0
    assert one.repairs["succeeded"] == 0


def test_a_second_pass_authorises_the_repairs():
    two = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2, repair=True)
    assert two.repairs["succeeded"] == two.confirmed


def test_running_the_same_batch_twice_writes_nothing_the_second_time():
    """The headline idempotency claim, at eval scale.

    Both runs share one repair pipeline, so the second one meets state that
    already contains every repair — exactly what re-running the demo does.
    """
    from eval.arms import make_repair_runner

    runner = make_repair_runner(flush=True)
    first = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2,
                    repair=True, runner=runner)
    writes_after_first = first.repairs["writes"]

    second = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2,
                     repair=True, runner=runner)

    assert second.repairs["writes"] == writes_after_first, "the second run wrote"
    assert second.repairs["replayed"] >= first.repairs["succeeded"]


def test_flushing_redis_between_eval_runs_still_writes_nothing():
    """Redis is a cache. Wipe it and the DB constraint must still hold."""
    from eval.arms import make_repair_runner

    runner = make_repair_runner(flush=True)
    first = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2,
                    repair=True, runner=runner)
    writes_after_first = first.repairs["writes"]

    runner.redis.flushall()

    second = run_arm("rules", seed=SEED, n=200, rate=0.25, passes=2,
                     repair=True, runner=runner)
    assert second.repairs["writes"] == writes_after_first
    assert second.repairs["already_applied"] > 0
    assert runner.ledger.has("REPAIR_DEDUPED_BY_DB")


def test_the_blast_radius_cap_is_enforced_in_the_eval():
    capped = run_arm("rules", seed=SEED, n=500, rate=0.25, passes=2,
                     repair=True, blast_radius=10)
    assert capped.repairs["succeeded"] == 10
    assert capped.repairs["blocked"] > 0


def test_repair_counts_are_ledger_safe():
    result = run_arm("rules", seed=SEED, n=100, rate=0.25, passes=2, repair=True)
    canonical(result.as_event())


def test_the_three_pass_proof_populates_every_column():
    """Pass 3 must show DB-dedupes, not blocks.

    The blast-radius cap bounds one run. If it accumulated across runs, pass 3
    would be entirely blocked and `DB-deduped` would print zero — the number
    that proves the two-layer claim, sitting empty for the wrong reason.
    """
    from eval.arms import make_repair_runner

    runner = make_repair_runner(flush=True)
    first = run_arm("rules", seed=SEED, n=200, rate=0.25, repair=True, runner=runner)
    second = run_arm("rules", seed=SEED, n=200, rate=0.25, repair=True, runner=runner)
    runner.redis.flushall()
    third = run_arm("rules", seed=SEED, n=200, rate=0.25, repair=True, runner=runner)

    assert second.repairs["replayed"] == first.repairs["succeeded"]
    assert third.repairs["already_applied"] == first.repairs["succeeded"]
    assert third.repairs["blocked"] == 0, "pass 3 was blocked, not deduped"
    assert third.repairs["writes"] == first.repairs["writes"]


# ── hard cases in the eval ──────────────────────────────────────────────────

def test_hard_cases_are_not_counted_as_false_positives():
    """Hard-case detections are expected. Several have refusal as the correct
    outcome, so merging them into the clean false-positive number would
    misreport both."""
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True)
    assert result.hard_cases == 15
    assert result.false_positives == 0


def test_the_staleness_window_filters_a_transient_in_the_eval():
    """`transient_filtered` is the direct evidence two-run confirmation works.

    A late webhook lands between passes: the divergence is real when first
    seen and gone before a repair could be authorised. Without a batch that
    moves between passes this metric is a permanent zero however correct the
    implementation is.
    """
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True)
    assert result.transient_filtered > 0


def test_every_confirmed_divergence_is_repaired_or_escalated_never_dropped():
    """Nothing confirmed may vanish silently.

    The gap between confirmed and repaired is exactly the count the policy
    gate refused — under-determined classes escalate rather than guess, and
    that difference must be accounted for rather than merely unexplained.
    """
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True, repair=True)
    accounted = result.repairs["succeeded"] + result.repairs["escalated"]
    assert accounted == result.confirmed
    assert result.repairs["escalated"] > 0, "AMOUNT_MISMATCH must not be auto-repaired"


def test_hard_cases_put_rows_in_the_exception_list(tmp_path):
    """The clean batch legitimately has zero exceptions. This is what fills it."""
    path = tmp_path / "exceptions.csv"
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True,
                     exceptions_path=path)
    assert result.exceptions_count > 0
    assert "no_hypothesis_verified" in path.read_text()


def test_amount_mismatch_is_never_auto_repaired():
    """Under-determined classes escalate rather than guess."""
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True, repair=True)
    assert result.repairs["escalated"] > 0


# ── the committed cache must be complete ────────────────────────────────────

def test_the_committed_cache_covers_every_eval_prompt():
    """Zero cache misses when the eval runs with no client configured.

    Without this, "fresh clone -> make setup && make verify green" silently
    requires a network call and an API key, and that is discovered on the day
    of the demo rather than now.
    """
    from statesync.classifier.llm_provider import LLMHypothesisProvider
    from statesync.config import CACHE_DIR
    from statesync.llm.cache import LLMCache

    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=CACHE_DIR), client=None)
    result = run_arm("full", seed=SEED, n=500, rate=0.25, hard_cases=True,
                     provider=provider)

    assert provider.network_calls == 0, "the committed cache is incomplete"
    assert result.llm_calls > 0, "sanity: the provider should have been consulted"


def test_the_cache_manifest_records_which_client_filled_it():
    """A cost figure measured against the offline client is not a provider
    cost, and the file must say which one it was."""
    import json

    from statesync.config import CACHE_DIR

    manifest = json.loads((CACHE_DIR / "MANIFEST.json").read_text())
    assert manifest["client_kind"] in ("live", "offline")
    assert manifest["entries"] > 0
    if manifest["client_kind"] == "offline":
        assert "NOT a model" in manifest["note"]


def test_arm_three_reports_cold_and_warm_separately():
    """Warm runs read zero provider calls. That is the cache working, not a
    generation cost of zero, and the two must not be confused."""
    from statesync.classifier.llm_provider import LLMHypothesisProvider
    from statesync.config import CACHE_DIR
    from statesync.llm.cache import LLMCache

    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=CACHE_DIR), client=None)
    warm = run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True,
                   provider=provider)
    assert warm.llm_calls > 0
    assert warm.network_calls == 0


# ── the reproduction claim, asserted ────────────────────────────────────────

def test_two_evals_write_byte_identical_arm_results(tmp_path):
    """`make eval` twice must produce identical files.

    Throughput used to be written into these, so a reviewer running
    `make eval && sha256sum eval/results/*.json` twice saw a difference and
    concluded determinism was broken. Wall clock now lives in its own dated
    snapshot; the arm results carry only the deterministic record.
    """
    import hashlib

    from eval.harness import main

    def digest() -> dict[str, str]:
        # Both outputs redirected: calling main() with defaults writes the
        # COMMITTED exceptions.csv, which is instance 8 recurring inside the
        # very test added to prevent it.
        main([
            "--seed", str(SEED), "--n", "200",
            "--results-dir", str(tmp_path),
            "--exceptions-path", str(tmp_path / "exceptions.csv"),
        ])
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(tmp_path.glob("arm_*.json"))
        }

    assert digest() == digest()


def test_the_arm_results_carry_no_wall_clock():
    """Anything derived from wall clock cannot be byte-stable, so it does not
    belong in the file the reproduction check diffs."""
    import json

    from statesync.config import PROJECT_ROOT

    for arm in ("none", "rules", "full"):
        payload = json.loads(
            (PROJECT_ROOT / "eval" / "results" / f"arm_{arm}.json").read_text()
        )
        assert "throughput" not in payload
        assert not [k for k in payload if "wall_clock" in k or "_us" in k]


def test_the_throughput_snapshot_is_dated_and_separate():
    import json

    from statesync.config import PROJECT_ROOT

    snapshot = json.loads(
        (PROJECT_ROOT / "eval" / "results" / "throughput.json").read_text()
    )
    assert snapshot["measured_at"].endswith("Z")
    assert set(snapshot["arms"]) == {"none", "rules", "full"}
