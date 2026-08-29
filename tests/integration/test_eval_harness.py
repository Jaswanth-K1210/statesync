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
    assert path.read_text().count("\n") == result.exceptions_count + 1


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


def test_arm_full_is_not_available_until_phase_5():
    with pytest.raises(NotImplementedError, match="Phase 5"):
        run_arm("full", seed=SEED, n=10, rate=0.2)


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
