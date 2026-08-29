"""Domain models. Constraint 1 is enforced at the type boundary."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from statesync.models.domain import Divergence, LedgerEntryRecord, Order, Payment
from statesync.models.enums import DivergenceClass, PaymentStatus, ReasonCode

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def a_payment(**kw) -> Payment:
    base = dict(
        payment_id="pay_1", order_ref="order_1", amount_paise=400000,
        fee_paise=8000, tax_paise=1440, instrument="card_domestic",
        status=PaymentStatus.CAPTURED, status_changed_at=NOW, captured_at=NOW,
    )
    return Payment(**{**base, **kw})


def test_payment_accepts_integer_paise():
    assert a_payment().amount_paise == 400000


def test_payment_rejects_fractional_float_amount():
    with pytest.raises(ValidationError):
        a_payment(amount_paise=100.50)


def test_payment_rejects_whole_float_amount():
    """4000.0 is still a float. Coercion would smuggle one past the chain."""
    with pytest.raises(ValidationError):
        a_payment(amount_paise=4000.0)


def test_payment_rejects_float_fee():
    with pytest.raises(ValidationError):
        a_payment(fee_paise=94.40)


def test_payment_fee_and_tax_may_be_absent():
    p = a_payment(fee_paise=None, tax_paise=None)
    assert p.fee_paise is None and p.tax_paise is None


def test_payment_rejects_naive_timestamp():
    with pytest.raises(ValidationError):
        a_payment(status_changed_at=datetime(2026, 9, 5, 12, 0, 0))  # noqa: DTZ001


def test_terminal_states_are_captured_refunded_failed():
    assert PaymentStatus.CAPTURED.is_terminal
    assert PaymentStatus.REFUNDED.is_terminal
    assert PaymentStatus.FAILED.is_terminal
    assert not PaymentStatus.CREATED.is_terminal
    assert not PaymentStatus.AUTHORIZED.is_terminal


def test_order_rejects_float_total():
    with pytest.raises(ValidationError):
        Order(order_id="o1", payment_id="pay_1", customer_id="c1",
              total_paise=4000.0, status="confirmed", created_at=NOW,
              line_items_count=2)


def test_ledger_entry_allows_negative_amount_for_refunds():
    e = LedgerEntryRecord(entry_id="le_1", order_id="o1", payment_id="pay_1",
                          amount_paise=-100000, entry_type="refund", created_at=NOW)
    assert e.amount_paise == -100000


def test_all_six_divergence_classes_plus_no_divergence_exist():
    assert {c.value for c in DivergenceClass} == {
        "captured_no_order", "order_no_capture", "duplicate_order",
        "refund_not_reflected", "amount_mismatch", "settlement_gap",
        "no_divergence",
    }


def test_reason_codes_cover_the_refusal_outcomes():
    assert {r.value for r in ReasonCode} >= {
        "verified", "ambiguous_multiple_verified", "no_hypothesis_verified",
        "fee_schedule_unknown", "stuck_repair",
    }


def a_divergence(**kw) -> Divergence:
    base = dict(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id="pay_1",
                order_id=None, amount_paise=400000, observed_at=NOW)
    return Divergence(**{**base, **kw})


def test_divergence_key_is_stable_across_instances():
    assert a_divergence().deterministic_key() == a_divergence().deterministic_key()


def test_divergence_key_differs_by_class():
    other = a_divergence(klass=DivergenceClass.AMOUNT_MISMATCH)
    assert a_divergence().deterministic_key() != other.deterministic_key()


def test_divergence_key_differs_by_payment():
    other = a_divergence(payment_id="pay_2")
    assert a_divergence().deterministic_key() != other.deterministic_key()


def test_divergence_key_ignores_observation_time():
    """The same divergence seen on two passes must claim the same repair key."""
    later = a_divergence(observed_at=datetime(2026, 9, 5, 18, 0, 0, tzinfo=UTC))
    assert a_divergence().deterministic_key() == later.deterministic_key()
