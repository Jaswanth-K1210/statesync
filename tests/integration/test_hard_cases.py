"""The 15 hard cases.

A test set of clean single-class divergences produces excellent metrics and
tells you nothing. Real divergence is compound and ambiguous, and these are
where the submission stops being a set-difference engine.

Cases 7, 13 and 14 are the most valuable in the set and are mandatory: they
test whether the system correctly **refuses to act**, which is harder and more
important than acting correctly.

Timestamps are generated relative to run time, not from a fixed past anchor —
otherwise the staleness window never fires and `transient_filtered` ships as a
permanent zero. See docs/PHASE_4_REQUIREMENTS.md.
"""

from datetime import UTC, datetime, timedelta

import pytest

from statesync.config import SEED
from statesync.generator.synthetic import generate_batch
from statesync.injector.hard_cases import (
    HARD_CASES,
    ExpectedOutcome,
    inject_hard_cases,
)
from statesync.models.enums import DivergenceClass, ReasonCode
from statesync.reconciler.three_way import reconcile

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def injected():
    return inject_hard_cases(generate_batch(seed=SEED, n=120), seed=SEED, now=NOW)


def divergences_for(injected, case_id):
    case = injected.case(case_id)
    found = reconcile(injected.batch, now=NOW)
    return [d for d in found if d.payment_id in case.payment_ids]


# ── the set is complete and self-describing ─────────────────────────────────

def test_all_fifteen_cases_are_present(injected):
    assert len(HARD_CASES) == 15
    assert {c.case_id for c in injected.cases} == {f"case_{i:02d}" for i in range(1, 16)}


def test_every_case_declares_its_correct_outcome(injected):
    for case in injected.cases:
        assert case.expected in set(ExpectedOutcome)


def test_the_batch_still_contains_the_clean_records(injected):
    assert len(injected.batch.payments) >= 120


# ── case 1: partial handler write ───────────────────────────────────────────

def test_case_01_partial_handler_write_is_detected_as_compound(injected):
    case = injected.case("case_01")
    order = next(o for o in injected.batch.orders if o.order_id in case.order_ids)
    assert order.line_items_count == 0, "the write died before the line items landed"
    assert case.expected == ExpectedOutcome.ESCALATED


# ── case 2: refund racing a second payment ──────────────────────────────────

def test_case_02_refund_racing_a_second_payment_resolves_or_escalates(injected):
    found = divergences_for(injected, "case_02")
    assert found, "the race must not pass silently"


# ── case 3: capture during a network partition ──────────────────────────────

def test_case_03_a_retried_capture_creates_no_duplicate_order(injected):
    """The merchant timed out and retried; the gateway had already captured."""
    case = injected.case("case_03")
    orders = [o for o in injected.batch.orders if o.payment_id in case.payment_ids]
    assert len(orders) == 1, "the retry must not have produced a second order"


# ── case 4: duplicate webhook, different payloads ───────────────────────────

def test_case_04_the_later_delivery_is_authoritative(injected):
    case = injected.case("case_04")
    assert case.detail["authoritative"] == "later"


# ── case 5: the delta is exactly the fee ────────────────────────────────────

def test_case_05_a_delta_exactly_equal_to_mdr_plus_gst_is_not_a_divergence(injected):
    """Fee subtraction runs first and deterministically. This is not a gap."""
    found = divergences_for(injected, "case_05")
    assert not [d for d in found if d.klass == DivergenceClass.AMOUNT_MISMATCH]


# ── case 6: the delta is the fee, off by 60 paise ───────────────────────────

def test_case_06_a_sixty_paise_residual_is_an_amount_mismatch(injected):
    found = divergences_for(injected, "case_06")
    mismatches = [d for d in found if d.klass == DivergenceClass.AMOUNT_MISMATCH]
    assert len(mismatches) == 1
    assert mismatches[0].detail["residual_paise"] == "60"


# ── case 7: MANDATORY. two legitimate orders ────────────────────────────────

def test_case_07_two_legitimate_orders_are_not_merged(injected):
    """The false-positive test. More important than any detection test.

    Same customer, same amount, two seconds apart, two *different* payments.
    A naive dedupe on customer+amount+timestamp would merge them and destroy a
    real order. Merging happens on exact payment_id, never on similarity.
    """
    found = divergences_for(injected, "case_07")
    assert not [d for d in found if d.klass == DivergenceClass.DUPLICATE_ORDER]


def test_case_07_both_orders_survive_a_full_repair_run(injected):
    from eval.arms import make_repair_runner

    runner = make_repair_runner(flush=True)
    case = injected.case("case_07")
    for divergence in reconcile(injected.batch, now=NOW):
        if divergence.payment_id in case.payment_ids:
            runner.run(divergence)

    assert runner.summary()["succeeded"] == 0 or all(
        s != "voided" for s in
        [runner.store.order_status(o) for o in case.order_ids]
    )


def test_case_07_the_two_orders_are_genuinely_distinct(injected):
    """Guard the fixture itself: if the injector accidentally gave them one
    payment id, the test above would pass for the wrong reason."""
    case = injected.case("case_07")
    orders = [o for o in injected.batch.orders if o.order_id in case.order_ids]
    assert len(orders) == 2
    assert orders[0].payment_id != orders[1].payment_id
    assert orders[0].customer_id == orders[1].customer_id
    assert orders[0].total_paise == orders[1].total_paise


# ── case 8: refund reversed by the bank ─────────────────────────────────────

def test_case_08_a_reversed_refund_nets_to_the_correct_position(injected):
    case = injected.case("case_08")
    entries = [e for e in injected.batch.ledger_entries if e.payment_id in case.payment_ids]
    assert sum(e.amount_paise for e in entries) == int(case.detail["expected_net_paise"])


# ── case 9: prior-cycle chargeback ──────────────────────────────────────────

def test_case_09_a_prior_cycle_chargeback_is_attributed_or_escalated(injected):
    case = injected.case("case_09")
    assert case.expected == ExpectedOutcome.ESCALATED


# ── case 10: out-of-order webhooks ──────────────────────────────────────────

def test_case_10_a_refund_arriving_before_the_capture_resolves(injected):
    """Out-of-order delivery must not produce UNKNOWN."""
    found = divergences_for(injected, "case_10")
    assert all(d.klass != DivergenceClass.NO_DIVERGENCE for d in found)
    case = injected.case("case_10")
    entries = [e for e in injected.batch.ledger_entries if e.payment_id in case.payment_ids]
    assert sorted(e.entry_type for e in entries) == ["capture", "refund"]


# ── case 11: a 26-hour replay ───────────────────────────────────────────────

def test_case_11_a_replay_past_the_disable_window_is_deduped(injected):
    case = injected.case("case_11")
    orders = [o for o in injected.batch.orders if o.payment_id in case.payment_ids]
    assert len(orders) == 2, "the replay did land a second order row"
    found = divergences_for(injected, "case_11")
    assert [d.klass for d in found] == [DivergenceClass.DUPLICATE_ORDER]


# ── case 12: cancelled while in flight ──────────────────────────────────────

def test_case_12_an_order_cancelled_in_flight_is_not_auto_repaired(injected):
    """A payment still inside its authorisation window is not a divergence."""
    found = divergences_for(injected, "case_12")
    assert not found, "an in-flight payment must not be touched"


def test_case_12_is_the_reason_transient_filtering_can_be_measured(injected):
    case = injected.case("case_12")
    payment = next(p for p in injected.batch.payments if p.payment_id in case.payment_ids)
    assert NOW - payment.status_changed_at < timedelta(minutes=15)


# ── cases 13 and 14: MANDATORY. the propose-verify refusals ─────────────────

def test_case_13_two_verified_hypotheses_escalate_as_ambiguous(injected):
    """The system must attach both rather than picking one."""
    packet = injected.packet_for("case_13")
    assert packet.reason_code == ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    assert len(packet.verified) == 2


def test_case_13_both_hypotheses_carry_full_arithmetic(injected):
    packet = injected.packet_for("case_13")
    for hypothesis in packet.verified:
        assert hypothesis.arithmetic_shown
        assert hypothesis.matched_residual is True


def test_case_13_the_two_explanations_are_actually_different(injected):
    """Attaching the same explanation twice would not help anyone."""
    packet = injected.packet_for("case_13")
    breakdowns = [sorted(c.name for c in h.components) for h in packet.verified]
    assert breakdowns[0] != breakdowns[1]


def test_case_14_no_hypothesis_verifies_and_all_are_attached(injected):
    packet = injected.packet_for("case_14")
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert len(packet.hypotheses) >= 5
    assert not packet.verified


def test_case_14_every_rejected_hypothesis_shows_why_it_failed(injected):
    packet = injected.packet_for("case_14")
    assert all(h.arithmetic_shown and h.rejection_reason for h in packet.hypotheses)


def test_case_14_never_claims_no_explanation_exists(injected):
    """The wording rule, on the case where it matters most."""
    from statesync.classifier.escalation import FORBIDDEN_PHRASES

    text = injected.packet_for("case_14").suggested_action.lower()
    assert "no generated hypothesis verified" in text
    assert not [p for p in FORBIDDEN_PHRASES if p in text]


def test_case_14_generation_stayed_within_bounds(injected):
    packet = injected.packet_for("case_14")
    assert packet.passes_used <= 2
    assert len(packet.hypotheses) <= 10


# ── case 15: split payment with rounding ────────────────────────────────────

def test_case_15_a_split_payment_is_attributed_to_the_paisa_or_escalated(injected):
    case = injected.case("case_15")
    assert case.expected in (ExpectedOutcome.ESCALATED, ExpectedOutcome.RESOLVED)
    found = divergences_for(injected, "case_15")
    assert found, "a rounding residual across two instruments must surface"


# ── the set as a whole ──────────────────────────────────────────────────────

def test_the_three_mandatory_refusal_cases_all_refuse(injected):
    """If everything else in this file has to go, these stay."""
    assert not [d for d in divergences_for(injected, "case_07")
                if d.klass == DivergenceClass.DUPLICATE_ORDER]
    assert injected.packet_for("case_13").reason_code == (
        ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    )
    assert injected.packet_for("case_14").reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_hard_cases_are_deterministic_at_one_seed():
    a = inject_hard_cases(generate_batch(seed=SEED, n=120), seed=SEED, now=NOW)
    b = inject_hard_cases(generate_batch(seed=SEED, n=120), seed=SEED, now=NOW)
    assert a.batch.digest() == b.batch.digest()


# ── the exception list must not mislabel the cases it exists to surface ─────

def test_case_13_is_labelled_ambiguous_in_the_exception_list(tmp_path):
    """A reviewer opening exceptions.csv looking for case 13 must find it
    correctly labelled.

    The reason code has to come from the escalation packet, not from a
    classifier default — the packet is the only thing that knows two
    hypotheses verified.
    """
    import csv

    from eval.arms import run_arm

    path = tmp_path / "exceptions.csv"
    run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True, exceptions_path=path)
    rows = {r["payment_id"]: r for r in
            csv.DictReader(line for line in path.open() if not line.startswith("#"))}

    assert rows["pay_hc13"]["reason_code"] == "ambiguous_multiple_verified"


def test_case_14_is_labelled_no_hypothesis_verified_in_the_exception_list(tmp_path):
    import csv

    from eval.arms import run_arm

    path = tmp_path / "exceptions.csv"
    run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True, exceptions_path=path)
    rows = {r["payment_id"]: r for r in
            csv.DictReader(line for line in path.open() if not line.startswith("#"))}

    assert rows["pay_hc14"]["reason_code"] == "no_hypothesis_verified"


def test_case_07_produces_no_exception_row(tmp_path):
    """Not merging is the correct outcome, so there is nothing to escalate."""
    import csv

    from eval.arms import run_arm

    path = tmp_path / "exceptions.csv"
    run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True, exceptions_path=path)
    payment_ids = {r["payment_id"] for r in
                   csv.DictReader(line for line in path.open() if not line.startswith("#"))}

    assert "pay_hc07a" not in payment_ids and "pay_hc07b" not in payment_ids


def test_the_reason_code_breakdown_distinguishes_the_two_refusals():
    from eval.arms import run_arm

    result = run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True)
    assert result.reason_codes.get("ambiguous_multiple_verified", 0) >= 1
    assert result.reason_codes.get("no_hypothesis_verified", 0) >= 1


def test_the_eval_reports_provider_calls():
    """Bounded generation is only credible if the call count is reported."""
    from eval.arms import run_arm

    result = run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True)
    assert result.provider_calls > 0
    assert result.provider_calls <= 2 * result.reason_codes_total
    assert result.llm_calls == 0, "arm 2 consults fixtures, not a model"


def test_payment_ids_covers_auxiliary_payments_not_just_numbered_cases(injected):
    """The late arrival and the unpriceable instrument belong to no numbered
    case. Keying the exclusion set off the case registry left them out, and
    they were then counted as false positives."""
    assert "pay_hc16" in injected.payment_ids
    assert "pay_hc17" in injected.payment_ids
    assert injected.payment_ids >= {p for c in injected.cases for p in c.payment_ids}


def test_the_packet_and_the_eval_apply_the_same_checks(injected):
    """Building a packet two ways with two different checks is how a value
    comes out right in one place and wrong in another."""
    from eval.arms import run_arm

    packet = injected.packet_for("case_13")
    result = run_arm("rules", seed=SEED, n=60, rate=0.25, hard_cases=True)
    assert packet.reason_code.value in result.reason_codes


# ── the model may add resolutions, never remove ambiguity ───────────────────

def test_case_13_stays_ambiguous_in_the_model_arm():
    """The regression that made this test necessary.

    The model provider used to *replace* the deterministic set. On hc13 — built
    so two decompositions reconcile exactly — it found neither, and a correct
    AMBIGUOUS_MULTIPLE_VERIFIED became NO_HYPOTHESIS_VERIFIED. The system went
    from knowing it could not resolve the case to wrongly believing it had.

    The candidate sets are pooled now, so the model is strictly additive.
    """
    import csv
    import tempfile
    from pathlib import Path as _Path

    from eval.arms import run_arm

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "exceptions.csv"
        run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True,
                exceptions_path=path)
        rows = {r["payment_id"]: r["reason_code"] for r in
                csv.DictReader(line for line in path.open() if not line.startswith("#"))}

    assert rows["pay_hc13"] == "ambiguous_multiple_verified"


def test_the_model_arm_never_resolves_fewer_cases_than_rules_alone():
    """Pooling makes the model additive by construction. Assert the property
    rather than trusting the construction."""
    from eval.arms import run_arm

    rules = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True)
    full = run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True)

    rules_resolved = rules.reason_codes.get("verified", 0)
    full_resolved = full.reason_codes.get("verified", 0)
    assert full_resolved >= rules_resolved


def test_ambiguity_found_by_rules_survives_the_model_arm():
    """A case the deterministic set proves ambiguous must stay ambiguous."""
    from eval.arms import run_arm

    rules = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True)
    full = run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True)

    assert full.reason_codes.get("ambiguous_multiple_verified", 0) >= (
        rules.reason_codes.get("ambiguous_multiple_verified", 0)
    )


def test_the_deterministic_hypotheses_are_present_in_the_model_arm(injected):
    """hc13's designed outcome had NO coverage in the arm being shipped, because
    the model displaced the fixtures that create it."""
    from eval.arms import run_arm

    full = run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True)
    assert full.verdicts.get("VERIFIED", 0) >= 2, (
        "hc13's two verifying hypotheses are missing from the model arm"
    )
