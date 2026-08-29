"""The seeded synthetic generator.

Constraint 3: every run is seeded. `make eval` twice must produce identical
output. If it doesn't, that is a bug, and it is found here on Day 1 rather
than on camera.
"""

import pytest

from statesync.config import SEED
from statesync.generator.synthetic import Batch, generate_batch
from statesync.ledger.canonical import canonical
from statesync.models.enums import PaymentStatus


def test_generates_the_requested_number_of_payments():
    assert len(generate_batch(seed=SEED, n=50).payments) == 50


def test_same_seed_produces_byte_identical_output():
    assert generate_batch(seed=SEED, n=100).digest() == generate_batch(seed=SEED, n=100).digest()


def test_different_seed_produces_different_output():
    assert generate_batch(seed=SEED, n=100).digest() != (
        generate_batch(seed=SEED + 1, n=100).digest()
    )


def test_batch_contains_no_floats_anywhere():
    """The whole batch must survive canonical serialisation."""
    canonical(generate_batch(seed=SEED, n=100).as_event())  # raises TypeError on any float


def test_amounts_are_positive_integers():
    for p in generate_batch(seed=SEED, n=100).payments:
        assert isinstance(p.amount_paise, int) and not isinstance(p.amount_paise, bool)
        assert p.amount_paise > 0


def test_timestamps_are_deterministic_not_wall_clock():
    a = generate_batch(seed=SEED, n=20).payments
    b = generate_batch(seed=SEED, n=20).payments
    assert [p.status_changed_at for p in a] == [p.status_changed_at for p in b]


def test_clean_batch_has_an_order_for_every_captured_payment():
    """Phase 1 generates clean data. Divergence is injected in Phase 2."""
    batch = generate_batch(seed=SEED, n=200)
    ordered = {o.payment_id for o in batch.orders}
    captured = {p.payment_id for p in batch.payments if p.status == PaymentStatus.CAPTURED}
    assert captured <= ordered


def test_every_payment_whose_money_moved_has_a_capture_entry():
    """Refunded payments were captured first, so they are booked too."""
    batch = generate_batch(seed=SEED, n=200)
    booked = {e.payment_id for e in batch.ledger_entries if e.entry_type == "capture"}
    moved = {p.payment_id for p in batch.payments
             if p.status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED)}
    assert moved == booked


def test_refunded_payments_are_booked_twice_capture_then_refund():
    batch = generate_batch(seed=SEED, n=300)
    refunded = {p.payment_id for p in batch.payments if p.status == PaymentStatus.REFUNDED}
    assert refunded, "the generator should produce refunds at n=300"
    for pid in refunded:
        kinds = [e.entry_type for e in batch.ledger_entries if e.payment_id == pid]
        assert sorted(kinds) == ["capture", "refund"]


def test_failed_payments_are_never_booked():
    batch = generate_batch(seed=SEED, n=300)
    failed = {p.payment_id for p in batch.payments if p.status == PaymentStatus.FAILED}
    booked = {e.payment_id for e in batch.ledger_entries}
    assert failed and not (failed & booked)


def test_refunds_are_booked_as_negative_entries():
    batch = generate_batch(seed=SEED, n=300)
    refunds = [e for e in batch.ledger_entries if e.entry_type == "refund"]
    assert refunds, "the generator should produce some refunds at n=300"
    assert all(e.amount_paise < 0 for e in refunds)


def test_payment_ids_are_unique():
    ids = [p.payment_id for p in generate_batch(seed=SEED, n=500).payments]
    assert len(ids) == len(set(ids))


def test_generator_covers_every_instrument_at_scale():
    instruments = {p.instrument for p in generate_batch(seed=SEED, n=500).payments}
    assert instruments == {"upi", "card_domestic", "card_intl", "emi"}


def test_fee_and_tax_are_absent_on_some_payments():
    """Fee data is not always present. Phase 5 escalates those as
    FEE_SCHEDULE_UNKNOWN rather than guessing a rate."""
    batch = generate_batch(seed=SEED, n=300)
    assert any(p.fee_paise is None for p in batch.payments)
    assert any(p.fee_paise is not None for p in batch.payments)


def test_batch_roundtrips_through_a_digest_that_is_a_sha256():
    d = generate_batch(seed=SEED, n=10).digest()
    assert len(d) == 64 and int(d, 16) >= 0


@pytest.mark.parametrize("n", [1, 2, 50])
def test_small_batches_are_well_formed(n):
    batch = generate_batch(seed=SEED, n=n)
    assert isinstance(batch, Batch)
    assert len(batch.payments) == n


def test_clean_batch_contains_no_in_flight_payments():
    """An authorized-but-uncaptured payment with an order IS a divergence.

    Emitting one in the baseline would mean the batch ships with divergences
    the injector never recorded, making every false-positive number wrong.
    """
    statuses = {p.status for p in generate_batch(seed=SEED, n=500).payments}
    assert PaymentStatus.AUTHORIZED not in statuses
    assert PaymentStatus.CREATED not in statuses
