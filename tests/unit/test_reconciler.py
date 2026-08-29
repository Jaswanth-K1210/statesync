"""The three-way reconciler and the ledger invariant.

Four of the six divergence classes are set operations across three views.
Saying so plainly is the honest framing — a language model would not beat a
set operation at being a set operation.
"""

from datetime import UTC, datetime, timedelta

import pytest

from statesync.config import SEED
from statesync.generator.synthetic import Batch, generate_batch
from statesync.injector.clean import DivergenceInjector, inject_clean
from statesync.models.enums import DivergenceClass, PaymentStatus
from statesync.reconciler.invariants import LedgerInvariantViolation, check_ledger_invariant
from statesync.reconciler.three_way import reconcile

LATER = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def batch():
    return generate_batch(seed=SEED, n=120)


def captured_with_order(batch):
    ordered = {o.payment_id for o in batch.orders}
    return next(p for p in batch.payments
                if p.status == PaymentStatus.CAPTURED and p.payment_id in ordered)


def classes_for(divergences, payment_id):
    return {d.klass for d in divergences if d.payment_id == payment_id}


# ── detection, one class at a time ──────────────────────────────────────────

def test_clean_batch_yields_no_divergences(batch):
    """The generator produces three views that agree. Nothing to find."""
    assert reconcile(batch, now=LATER) == []


def test_detects_captured_no_order(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    found = reconcile(inj.result().batch, now=LATER)
    assert classes_for(found, pid) == {DivergenceClass.CAPTURED_NO_ORDER}


def test_detects_duplicate_order(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.duplicate_webhook(pid)
    found = reconcile(inj.result().batch, now=LATER)
    assert DivergenceClass.DUPLICATE_ORDER in classes_for(found, pid)


def test_detects_order_no_capture(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.abandon_after_order_create(pid)
    found = reconcile(inj.result().batch, now=LATER)
    assert classes_for(found, pid) == {DivergenceClass.ORDER_NO_CAPTURE}


def test_detects_refund_not_reflected(batch):
    refunded = next(p for p in batch.payments if p.status == PaymentStatus.REFUNDED)
    inj = DivergenceInjector(batch)
    inj.refund_without_ledger_write(refunded.payment_id)
    found = reconcile(inj.result().batch, now=LATER)
    assert classes_for(found, refunded.payment_id) == {DivergenceClass.REFUND_NOT_REFLECTED}


# ── the staleness window is load-bearing in the reconciler ──────────────────

def test_a_fresh_divergence_is_not_reported_yet(batch):
    """Terminal for two minutes is inside the window: skip, retry next pass."""
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    result = inj.result()
    payment = next(p for p in result.batch.payments if p.payment_id == pid)
    just_after = payment.status_changed_at + timedelta(minutes=2)
    assert classes_for(reconcile(result.batch, now=just_after), pid) == set()


def test_the_same_divergence_is_reported_once_the_window_passes(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    assert classes_for(reconcile(inj.result().batch, now=LATER), pid)


def test_failed_payments_do_not_produce_captured_no_order(batch):
    """A failed payment has no order by design. That is not a divergence."""
    found = reconcile(batch, now=LATER)
    failed = {p.payment_id for p in batch.payments if p.status == PaymentStatus.FAILED}
    assert not {d.payment_id for d in found} & failed


# ── shape of the output ─────────────────────────────────────────────────────

def test_every_divergence_carries_a_deterministic_key(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    for d in reconcile(inj.result().batch, now=LATER):
        assert len(d.deterministic_key()) == 32


def test_reconcile_is_deterministic_in_order(batch):
    injected = inject_clean(batch, seed=SEED, rate=0.25).batch
    a = [d.deterministic_key() for d in reconcile(injected, now=LATER)]
    b = [d.deterministic_key() for d in reconcile(injected, now=LATER)]
    assert a == b


def test_detection_finds_every_injected_divergence_at_scale():
    """The headline claim of arm 2, asserted against known ground truth."""
    batch = generate_batch(seed=SEED, n=500)
    injected = inject_clean(batch, seed=SEED, rate=0.25)
    found = {d.deterministic_key() for d in reconcile(injected.batch, now=LATER)}
    missed = set(injected.truth) - found
    assert not missed, f"{len(missed)} injected divergences went undetected"


def test_detection_raises_no_false_positives_on_clean_records():
    """More important than the detection number: it must not invent work."""
    batch = generate_batch(seed=SEED, n=500)
    injected = inject_clean(batch, seed=SEED, rate=0.25)
    found = {d.deterministic_key() for d in reconcile(injected.batch, now=LATER)}
    assert not found - set(injected.truth)


# ── the ledger invariant ────────────────────────────────────────────────────

def test_invariant_holds_on_a_clean_batch(batch):
    assert check_ledger_invariant(batch).ok


def test_invariant_catches_an_injected_imbalance(batch):
    """Catches divergences the three-way comparison misses."""
    broken = Batch(seed=batch.seed, payments=batch.payments, orders=batch.orders,
                   ledger_entries=batch.ledger_entries[:-1])
    assert not check_ledger_invariant(broken).ok


def test_invariant_reports_the_signed_gap(batch):
    dropped = batch.ledger_entries[-1]
    broken = Batch(seed=batch.seed, payments=batch.payments, orders=batch.orders,
                   ledger_entries=batch.ledger_entries[:-1])
    assert check_ledger_invariant(broken).delta_paise == -dropped.amount_paise


def test_invariant_violation_can_be_raised_for_strict_mode(batch):
    broken = Batch(seed=batch.seed, payments=batch.payments, orders=batch.orders,
                   ledger_entries=batch.ledger_entries[:-1])
    with pytest.raises(LedgerInvariantViolation):
        check_ledger_invariant(broken).raise_if_violated()


# ── the authorisation window ────────────────────────────────────────────────
# An authorised-but-uncaptured payment inside its auth window is not a
# divergence at all — it is a payment in progress. It becomes ORDER_NO_CAPTURE
# only once the window has elapsed with no capture.

def _authorized_pair(batch, order_age, auth_age, now):
    """An order plus an authorised payment, each aged independently."""
    from statesync.generator.synthetic import Batch as _Batch
    pid = captured_with_order(batch).payment_id
    payment = next(p for p in batch.payments if p.payment_id == pid)
    order = next(o for o in batch.orders if o.payment_id == pid)
    return _Batch(
        seed=batch.seed,
        payments=[payment.model_copy(update={
            "status": PaymentStatus.AUTHORIZED,
            "captured_at": None,
            "status_changed_at": now - auth_age,
        })],
        orders=[order.model_copy(update={"created_at": now - order_age})],
        ledger_entries=[],
    )


def test_authorized_within_auth_window_is_not_order_no_capture(batch):
    """The normal case: money is authorised and capture is still pending."""
    now = LATER
    fresh = _authorized_pair(batch, order_age=timedelta(minutes=5),
                             auth_age=timedelta(minutes=5), now=now)
    assert reconcile(fresh, now=now) == []


def test_authorized_past_the_window_is_order_no_capture(batch):
    now = LATER
    stale = _authorized_pair(batch, order_age=timedelta(hours=6),
                             auth_age=timedelta(hours=6), now=now)
    assert [d.klass for d in reconcile(stale, now=now)] == [DivergenceClass.ORDER_NO_CAPTURE]


def test_a_recent_authorisation_on_an_old_order_is_not_a_divergence(batch):
    """A retry: the order is hours old but the payment was authorised just now.

    Gating on the order's age alone would flag this, and it is exactly the
    false positive the staleness design exists to prevent.
    """
    now = LATER
    retried = _authorized_pair(batch, order_age=timedelta(hours=6),
                               auth_age=timedelta(minutes=3), now=now)
    assert reconcile(retried, now=now) == []


def test_the_window_boundary_is_exclusive(batch):
    from statesync.config import ORDER_TIMEOUT
    now = LATER
    exact = _authorized_pair(batch, order_age=ORDER_TIMEOUT, auth_age=ORDER_TIMEOUT, now=now)
    assert reconcile(exact, now=now) == []
