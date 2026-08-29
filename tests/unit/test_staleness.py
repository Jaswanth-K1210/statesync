"""Staleness window and two-run confirmation.

During a state transition the gateway does not yet have a consistent answer,
and a false divergence that triggers a repair makes state worse than leaving
it alone. Two cheap defences, both tested here.

StateSync is deliberately not real-time. It trades minutes of detection
latency for never repairing a payment that was merely in flight.
"""

from datetime import UTC, datetime, timedelta

from statesync.config import STALENESS_WINDOW
from statesync.models.domain import Divergence, Payment
from statesync.models.enums import DivergenceClass, PaymentStatus
from statesync.reconciler.staleness import ConfirmationTracker, eligible_for_reconciliation

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def payment(status=PaymentStatus.CAPTURED, age=timedelta(hours=1)) -> Payment:
    return Payment(
        payment_id="pay_1", order_ref="order_1", amount_paise=400000,
        fee_paise=8000, tax_paise=1440, instrument="upi", status=status,
        status_changed_at=NOW - age, captured_at=NOW - age,
    )


def divergence(pid="pay_1") -> Divergence:
    return Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id=pid,
                      order_id=None, amount_paise=400000, observed_at=NOW)


# ── staleness window ────────────────────────────────────────────────────────

def test_terminal_payment_past_the_window_is_eligible():
    assert eligible_for_reconciliation(payment(age=timedelta(hours=1)), NOW)


def test_staleness_window_skips_a_payment_terminal_for_two_minutes():
    assert not eligible_for_reconciliation(payment(age=timedelta(minutes=2)), NOW)


def test_boundary_is_exclusive_at_exactly_the_window():
    assert not eligible_for_reconciliation(payment(age=STALENESS_WINDOW), NOW)


def test_one_second_past_the_window_is_eligible():
    assert eligible_for_reconciliation(payment(age=STALENESS_WINDOW + timedelta(seconds=1)), NOW)


def test_non_terminal_payment_is_never_eligible_however_old():
    assert not eligible_for_reconciliation(
        payment(status=PaymentStatus.AUTHORIZED, age=timedelta(days=30)), NOW
    )


def test_created_payment_is_never_eligible():
    assert not eligible_for_reconciliation(payment(status=PaymentStatus.CREATED), NOW)


def test_refunded_and_failed_are_terminal_and_eligible():
    assert eligible_for_reconciliation(payment(status=PaymentStatus.REFUNDED), NOW)
    assert eligible_for_reconciliation(payment(status=PaymentStatus.FAILED), NOW)


# ── two-run confirmation ────────────────────────────────────────────────────

def test_first_observation_authorises_nothing():
    tracker = ConfirmationTracker()
    assert tracker.observe([divergence()], now=NOW) == []


def test_second_observation_confirms():
    tracker = ConfirmationTracker()
    tracker.observe([divergence()], now=NOW)
    confirmed = tracker.observe([divergence()], now=NOW + STALENESS_WINDOW * 2)
    assert [d.payment_id for d in confirmed] == ["pay_1"]


def test_two_passes_closer_than_the_window_do_not_confirm():
    """Passes must be separated by at least the staleness window."""
    tracker = ConfirmationTracker()
    tracker.observe([divergence()], now=NOW)
    assert tracker.observe([divergence()], now=NOW + timedelta(minutes=1)) == []


def test_a_divergence_that_disappears_is_not_confirmed():
    """It resolved itself between passes — a transient. Do not repair it."""
    tracker = ConfirmationTracker()
    tracker.observe([divergence()], now=NOW)
    assert tracker.observe([], now=NOW + STALENESS_WINDOW * 2) == []


def test_transient_divergences_filtered_is_counted():
    """Direct evidence the window is load-bearing — reported as a metric."""
    tracker = ConfirmationTracker()
    tracker.observe([divergence("pay_1"), divergence("pay_2")], now=NOW)
    tracker.observe([divergence("pay_1")], now=NOW + STALENESS_WINDOW * 2)
    assert tracker.transient_filtered == 1


def test_confirming_twice_does_not_re_authorise():
    """A confirmed divergence stays confirmed; it is not re-emitted."""
    tracker = ConfirmationTracker()
    tracker.observe([divergence()], now=NOW)
    tracker.observe([divergence()], now=NOW + STALENESS_WINDOW * 2)
    third = tracker.observe([divergence()], now=NOW + STALENESS_WINDOW * 4)
    assert third == []


def test_observation_writes_a_ledger_entry_on_the_first_pass():
    from statesync.ledger.chain import Ledger
    led = Ledger(clock=lambda: NOW)
    tracker = ConfirmationTracker(ledger=led)
    tracker.observe([divergence()], now=NOW)
    assert led.has("DIVERGENCE_OBSERVED")
    assert not led.has("DIVERGENCE_CONFIRMED")


def test_confirmation_writes_a_ledger_entry_on_the_second_pass():
    from statesync.ledger.chain import Ledger
    led = Ledger(clock=lambda: NOW)
    tracker = ConfirmationTracker(ledger=led)
    tracker.observe([divergence()], now=NOW)
    tracker.observe([divergence()], now=NOW + STALENESS_WINDOW * 2)
    assert led.has("DIVERGENCE_CONFIRMED")
