"""Repair policy gates.

Anything outside the confidence, value or blast-radius bounds goes to a human
**by design, not by limitation**. These gates are what make "not fully
autonomous" a stated scope boundary rather than an excuse.
"""

from datetime import UTC, datetime

import pytest

from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass
from statesync.policy.blast_radius import BlastRadiusCap
from statesync.policy.gates import GateDecision, PolicyGate

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def divergence(amount=400000, klass=DivergenceClass.CAPTURED_NO_ORDER, pid="pay_1"):
    return Divergence(klass=klass, payment_id=pid, order_id=None,
                      amount_paise=amount, observed_at=NOW)


# ── value threshold ─────────────────────────────────────────────────────────

def test_a_small_repair_is_authorised():
    gate = PolicyGate(value_threshold_paise=1_000_000)
    assert gate.evaluate(divergence(amount=400_000)).authorised


def test_a_repair_above_the_value_threshold_escalates():
    """High-value repairs go to a human. The cost of being wrong scales."""
    gate = PolicyGate(value_threshold_paise=1_000_000)
    decision = gate.evaluate(divergence(amount=5_000_000))
    assert not decision.authorised
    assert decision.reason == "VALUE_THRESHOLD_EXCEEDED"


def test_the_value_threshold_boundary_is_inclusive():
    gate = PolicyGate(value_threshold_paise=1_000_000)
    assert gate.evaluate(divergence(amount=1_000_000)).authorised


def test_the_threshold_is_compared_in_integer_paise():
    gate = PolicyGate(value_threshold_paise=1_000_000)
    assert isinstance(gate.value_threshold_paise, int)


# ── class confidence ────────────────────────────────────────────────────────

def test_a_class_outside_the_auto_repairable_set_escalates():
    """AMOUNT_MISMATCH is under-determined. It is never auto-repaired."""
    gate = PolicyGate()
    decision = gate.evaluate(divergence(klass=DivergenceClass.AMOUNT_MISMATCH))
    assert not decision.authorised
    assert decision.reason == "CLASS_NOT_AUTO_REPAIRABLE"


def test_settlement_gap_is_never_auto_repaired():
    gate = PolicyGate()
    assert not gate.evaluate(divergence(klass=DivergenceClass.SETTLEMENT_GAP)).authorised


def test_all_four_clean_classes_are_auto_repairable():
    gate = PolicyGate(value_threshold_paise=10_000_000)
    for klass in (DivergenceClass.CAPTURED_NO_ORDER, DivergenceClass.ORDER_NO_CAPTURE,
                  DivergenceClass.DUPLICATE_ORDER, DivergenceClass.REFUND_NOT_REFLECTED):
        assert gate.evaluate(divergence(klass=klass)).authorised, klass


def test_no_divergence_is_never_repaired():
    gate = PolicyGate()
    assert not gate.evaluate(divergence(klass=DivergenceClass.NO_DIVERGENCE)).authorised


# ── blast radius ────────────────────────────────────────────────────────────

def test_repairs_below_the_cap_are_allowed():
    cap = BlastRadiusCap(max_repairs=3)
    assert all(cap.allow() for _ in range(3))


def test_the_cap_blocks_the_repair_after_the_limit():
    """A reconciler that repairs 400 records in one run because of one bad
    rule is worse than one that stops at 50 and asks."""
    cap = BlastRadiusCap(max_repairs=3)
    for _ in range(3):
        cap.allow()
    assert not cap.allow()


def test_the_cap_counts_only_permitted_repairs():
    cap = BlastRadiusCap(max_repairs=2)
    cap.allow()
    cap.allow()
    cap.allow()
    cap.allow()
    assert cap.used == 2


def test_the_cap_reports_how_many_it_blocked():
    cap = BlastRadiusCap(max_repairs=1)
    cap.allow()
    cap.allow()
    cap.allow()
    assert cap.blocked == 2


def test_a_zero_cap_blocks_everything():
    """A kill switch that needs no code change."""
    assert not BlastRadiusCap(max_repairs=0).allow()


def test_a_negative_cap_is_rejected_rather_than_silently_treated_as_zero():
    with pytest.raises(ValueError, match="max_repairs"):
        BlastRadiusCap(max_repairs=-1)


# ── the decision carries its reason ─────────────────────────────────────────

def test_an_authorised_decision_has_no_reason():
    assert PolicyGate().evaluate(divergence()).reason is None


def test_a_decision_is_ledger_safe():
    from statesync.ledger.canonical import canonical
    canonical(PolicyGate().evaluate(divergence(amount=99_000_000)).as_event())


def test_gate_decisions_are_frozen():
    """A decision that can be edited after the fact is not a decision."""
    import dataclasses

    decision = PolicyGate().evaluate(divergence())
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.authorised = False  # type: ignore[misc]


def test_gate_decision_type():
    assert isinstance(PolicyGate().evaluate(divergence()), GateDecision)
