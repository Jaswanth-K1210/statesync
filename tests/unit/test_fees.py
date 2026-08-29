"""Fee resolution — deterministic, and before any inference runs.

Three sources, in priority order:

    1. payment.fee_paise + payment.tax_paise   authoritative, no inference
    2. a versioned schedule keyed by instrument, with effective dates
    3. neither -> FEE_SCHEDULE_UNKNOWN

**Never guess a rate.** A system that reports 95% verification while silently
escalating the 40% it could not price is not honest, which is why coverage is
reported as a first-class metric rather than a footnote.
"""

from datetime import UTC, datetime

import pytest

from statesync.classifier.fees import (
    FeeResolution,
    FeeSchedule,
    FeeSource,
    load_fee_schedule,
    resolve_fee,
)
from statesync.models.domain import Payment
from statesync.models.enums import PaymentStatus

AT = datetime(2026, 9, 5, tzinfo=UTC)


def payment(fee=None, tax=None, instrument="card_domestic", amount=400_000) -> Payment:
    return Payment(payment_id="pay_1", order_ref="order_1", amount_paise=amount,
                   fee_paise=fee, tax_paise=tax, instrument=instrument,
                   status=PaymentStatus.CAPTURED, status_changed_at=AT, captured_at=AT)


@pytest.fixture
def schedule():
    return load_fee_schedule()


# ── source 1: the payment object itself ─────────────────────────────────────

def test_fee_and_tax_on_the_payment_are_authoritative(schedule):
    result = resolve_fee(payment(fee=8_000, tax=1_440), schedule, at=AT)
    assert result.source == FeeSource.PAYMENT_OBJECT
    assert result.total_paise == 9_440


def test_the_payment_object_wins_even_when_the_schedule_disagrees(schedule):
    """No inference is needed when the gateway told us the number."""
    result = resolve_fee(payment(fee=1, tax=1), schedule, at=AT)
    assert result.source == FeeSource.PAYMENT_OBJECT
    assert result.total_paise == 2


def test_a_zero_fee_on_the_payment_is_still_authoritative(schedule):
    """UPI at 0% MDR. Zero is an answer, not a missing value."""
    result = resolve_fee(payment(fee=0, tax=0, instrument="upi"), schedule, at=AT)
    assert result.source == FeeSource.PAYMENT_OBJECT
    assert result.total_paise == 0


# ── source 2: the configured schedule ───────────────────────────────────────

def test_a_missing_fee_falls_back_to_the_schedule(schedule):
    result = resolve_fee(payment(instrument="card_domestic"), schedule, at=AT)
    assert result.source == FeeSource.SCHEDULE
    assert result.total_paise == 9_440  # 2% of 400000, plus 18% GST on that


def test_the_schedule_computes_in_integer_paise(schedule):
    result = resolve_fee(payment(instrument="card_intl", amount=333_333), schedule, at=AT)
    assert isinstance(result.total_paise, int)


def test_each_instrument_has_its_own_rate(schedule):
    upi = resolve_fee(payment(instrument="upi"), schedule, at=AT)
    card = resolve_fee(payment(instrument="card_domestic"), schedule, at=AT)
    assert upi.total_paise < card.total_paise


def test_the_schedule_reports_the_rate_it_used(schedule):
    result = resolve_fee(payment(instrument="card_domestic"), schedule, at=AT)
    assert result.mdr_bps == 200
    assert result.gst_bps == 1800


def test_a_schedule_version_is_recorded_for_audit(schedule):
    result = resolve_fee(payment(instrument="card_domestic"), schedule, at=AT)
    assert result.schedule_version > 0


def test_a_schedule_not_yet_effective_is_not_used(schedule):
    """Effective dates are honoured; a future rate is not applied to the past."""
    early = datetime(2020, 1, 1, tzinfo=UTC)
    result = resolve_fee(payment(instrument="card_domestic"), schedule, at=early)
    assert result.source == FeeSource.UNKNOWN


# ── source 3: nothing ───────────────────────────────────────────────────────

def test_an_unknown_instrument_escalates_rather_than_guessing(schedule):
    result = resolve_fee(payment(instrument="crypto_voucher"), schedule, at=AT)
    assert result.source == FeeSource.UNKNOWN
    assert result.total_paise is None


def test_the_unknown_result_names_what_config_would_resolve_it(schedule):
    result = resolve_fee(payment(instrument="crypto_voucher"), schedule, at=AT)
    assert "crypto_voucher" in result.needed_config


def test_an_unknown_fee_is_never_silently_treated_as_zero(schedule):
    """Treating a missing fee as zero manufactures a residual out of nothing."""
    result = resolve_fee(payment(instrument="crypto_voucher"), schedule, at=AT)
    assert result.total_paise is not None or result.source == FeeSource.UNKNOWN
    assert result.total_paise != 0


# ── coverage ────────────────────────────────────────────────────────────────

def test_coverage_counts_resolvable_payments(schedule):
    payments = [payment(fee=1, tax=1), payment(instrument="card_domestic"),
                payment(instrument="crypto_voucher")]
    resolutions = [resolve_fee(p, schedule, at=AT) for p in payments]
    covered = [r for r in resolutions if r.source != FeeSource.UNKNOWN]
    assert len(covered) == 2


def test_a_resolution_is_ledger_safe(schedule):
    from statesync.ledger.canonical import canonical
    canonical(resolve_fee(payment(fee=1, tax=1), schedule, at=AT).as_event())


def test_the_schedule_loads_from_committed_config():
    loaded = load_fee_schedule()
    assert isinstance(loaded, FeeSchedule)
    assert loaded.version > 0


def test_resolution_type(schedule):
    assert isinstance(resolve_fee(payment(fee=1, tax=1), schedule, at=AT), FeeResolution)
