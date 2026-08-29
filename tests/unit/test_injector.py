"""The clean divergence injector.

This is the data strategy in one module: **we know the correct answer because
we caused the divergence.** Every injection records its own ground truth, so
there is no labelling step and no "precision against what?" problem.

Phase 2 covers the four clean classes. The 15 ambiguous cases are Phase 4.
"""

import pytest

from statesync.config import SEED
from statesync.generator.synthetic import generate_batch
from statesync.injector.clean import DivergenceInjector, inject_clean
from statesync.models.enums import DivergenceClass, PaymentStatus


@pytest.fixture
def batch():
    return generate_batch(seed=SEED, n=120)


def captured_with_order(batch):
    ordered = {o.payment_id for o in batch.orders}
    return next(p for p in batch.payments
                if p.status == PaymentStatus.CAPTURED and p.payment_id in ordered)


# ── individual injectors ────────────────────────────────────────────────────

def test_drop_webhook_removes_the_order_and_records_captured_no_order(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    result = inj.result()

    assert not [o for o in result.batch.orders if o.payment_id == pid]
    assert result.truth[_key(DivergenceClass.CAPTURED_NO_ORDER, pid, None)] == (
        DivergenceClass.CAPTURED_NO_ORDER
    )


def test_drop_webhook_leaves_the_payment_intact(batch):
    """The gateway still says money moved. That is the whole point."""
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    kept = [p for p in inj.result().batch.payments if p.payment_id == pid]
    assert len(kept) == 1 and kept[0].status == PaymentStatus.CAPTURED


def test_duplicate_webhook_creates_a_second_order_on_one_payment(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.duplicate_webhook(pid)
    dupes = [o for o in inj.result().batch.orders if o.payment_id == pid]
    assert len(dupes) == 2
    assert dupes[0].order_id != dupes[1].order_id


def test_duplicate_webhook_copies_the_amount_exactly(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.duplicate_webhook(pid)
    dupes = [o for o in inj.result().batch.orders if o.payment_id == pid]
    assert dupes[0].total_paise == dupes[1].total_paise


def test_abandon_after_order_create_leaves_an_uncaptured_payment(batch):
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.abandon_after_order_create(pid)
    result = inj.result()
    payment = next(p for p in result.batch.payments if p.payment_id == pid)

    assert payment.status == PaymentStatus.AUTHORIZED
    assert payment.captured_at is None
    assert [o for o in result.batch.orders if o.payment_id == pid], "the order survives"
    assert not [e for e in result.batch.ledger_entries if e.payment_id == pid]


def test_refund_without_ledger_write_removes_only_the_refund_entry(batch):
    refunded = next(p for p in batch.payments if p.status == PaymentStatus.REFUNDED)
    inj = DivergenceInjector(batch)
    inj.refund_without_ledger_write(refunded.payment_id)
    entries = [e for e in inj.result().batch.ledger_entries
               if e.payment_id == refunded.payment_id]

    assert "refund" not in [e.entry_type for e in entries]
    assert "capture" in [e.entry_type for e in entries], "the capture entry stays"


# ── ground truth ────────────────────────────────────────────────────────────

def _key(klass, payment_id, order_id):
    from datetime import UTC, datetime

    from statesync.models.domain import Divergence
    return Divergence(klass=klass, payment_id=payment_id, order_id=order_id,
                      amount_paise=1, observed_at=datetime(2026, 1, 1, tzinfo=UTC)
                      ).deterministic_key()


def test_truth_keys_match_the_divergence_deterministic_key(batch):
    """Ground truth is keyed the same way detection is, or nothing lines up."""
    pid = captured_with_order(batch).payment_id
    inj = DivergenceInjector(batch)
    inj.drop_webhook(pid)
    assert _key(DivergenceClass.CAPTURED_NO_ORDER, pid, None) in inj.result().truth


def test_injections_are_recorded_with_their_amount(batch):
    payment = captured_with_order(batch)
    inj = DivergenceInjector(batch)
    inj.drop_webhook(payment.payment_id)
    assert inj.result().injections[0].amount_paise == payment.amount_paise


# ── the seeded driver ───────────────────────────────────────────────────────

def test_inject_clean_is_deterministic_at_one_seed(batch):
    a = inject_clean(batch, seed=SEED, rate=0.2)
    b = inject_clean(batch, seed=SEED, rate=0.2)
    assert a.batch.digest() == b.batch.digest()
    assert a.truth == b.truth


def test_inject_clean_differs_at_a_different_seed(batch):
    a = inject_clean(batch, seed=SEED, rate=0.2)
    b = inject_clean(batch, seed=SEED + 1, rate=0.2)
    assert a.batch.digest() != b.batch.digest()


def test_inject_clean_produces_all_four_clean_classes_at_scale():
    batch = generate_batch(seed=SEED, n=500)
    truth = inject_clean(batch, seed=SEED, rate=0.25).truth
    assert set(truth.values()) == {
        DivergenceClass.CAPTURED_NO_ORDER,
        DivergenceClass.ORDER_NO_CAPTURE,
        DivergenceClass.DUPLICATE_ORDER,
        DivergenceClass.REFUND_NOT_REFLECTED,
    }


def test_inject_clean_injects_roughly_the_requested_rate():
    batch = generate_batch(seed=SEED, n=500)
    injected = inject_clean(batch, seed=SEED, rate=0.20)
    assert 60 <= len(injected.injections) <= 140


def test_inject_clean_never_injects_twice_on_one_payment():
    batch = generate_batch(seed=SEED, n=500)
    injections = inject_clean(batch, seed=SEED, rate=0.25).injections
    touched = [i.payment_id for i in injections]
    assert len(touched) == len(set(touched))


def test_uninjected_records_are_left_exactly_as_generated():
    batch = generate_batch(seed=SEED, n=300)
    result = inject_clean(batch, seed=SEED, rate=0.2)
    touched = {i.payment_id for i in result.injections}
    before = {p.payment_id: p for p in batch.payments if p.payment_id not in touched}
    after = {p.payment_id: p for p in result.batch.payments if p.payment_id not in touched}
    assert before == after


def test_a_zero_rate_injects_nothing(batch):
    result = inject_clean(batch, seed=SEED, rate=0.0)
    assert result.injections == [] and result.batch.digest() == batch.digest()
