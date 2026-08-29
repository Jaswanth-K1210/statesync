"""SETTLEMENT_GAP — reported as NOT_IMPLEMENTED, deliberately.

The class stays in the taxonomy because it is where the propose-verify
architecture generalises. It is not detected, and the honest thing is to say
so rather than report an accuracy figure computed against payout data that was
manufactured for the purpose.

The design doc's own gate: *"Not verifying -> keep the class in the taxonomy
but report it as NOT_IMPLEMENTED with the reason, rather than reporting a
misleading accuracy figure on data you manufactured."* That is this.

Demonstrated, not validated. Say both words — or in this case, neither, and
say NOT_IMPLEMENTED instead.
"""

from statesync.classifier.settlement import (
    SETTLEMENT_GAP_STATUS,
    SettlementStatus,
    settlement_gap_report,
)
from statesync.models.enums import DivergenceClass
from statesync.policy.gates import AUTO_REPAIRABLE


def test_the_class_remains_in_the_taxonomy():
    """Removing it would hide a real failure mode the architecture covers."""
    assert DivergenceClass.SETTLEMENT_GAP in set(DivergenceClass)


def test_it_is_reported_as_not_implemented():
    assert SETTLEMENT_GAP_STATUS == SettlementStatus.NOT_IMPLEMENTED


def test_it_is_never_auto_repaired():
    """Whatever its status, the policy engine will not act on it."""
    assert DivergenceClass.SETTLEMENT_GAP not in AUTO_REPAIRABLE


def test_the_report_states_the_reason_rather_than_a_number():
    report = settlement_gap_report()
    assert report["status"] == "not_implemented"
    assert report["accuracy"] == "not reported"
    assert "sandbox" in report["reason"].lower()


def test_the_report_never_quotes_an_accuracy_figure():
    """A percentage computed against data we manufactured is not a measurement."""
    for value in settlement_gap_report().values():
        assert "%" not in str(value)


def test_the_report_says_what_would_change_the_answer():
    assert "payout" in settlement_gap_report()["needed"].lower()


def test_the_report_is_ledger_safe():
    from statesync.ledger.canonical import canonical

    canonical(settlement_gap_report())


def test_it_is_excluded_from_the_headline_metrics():
    """Never merged into a headline figure — the design doc is explicit."""
    assert settlement_gap_report()["merged_into_headline"] is False
