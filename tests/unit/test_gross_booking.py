"""Gross revenue booking with a separate fee expense line.

Net-revenue booking is a GST filing error, not a simplification: a merchant
owes GST on the **gross** sale value and claims input tax credit on the MDR's
GST separately. Booking net understates output liability.

The books therefore carry two lines per capture:

    capture  +gross            revenue, what the customer paid
    fee      -(mdr + gst)      what the merchant believes the gateway took

and the invariant is three-term:

    revenue + fee_expense == settled

This also makes the residual nameable. It stops being an abstract gap and
becomes *the difference between the fee the merchant booked and the fee the
gateway actually charged*, which has causes an ops person can act on.
"""

from statesync.config import SEED
from statesync.generator.synthetic import generate_batch
from statesync.models.enums import PaymentStatus
from statesync.reconciler.invariants import check_ledger_invariant


def entries_for(batch, payment_id):
    return [e for e in batch.ledger_entries if e.payment_id == payment_id]


def test_a_capture_books_the_gross_sale_value():
    batch = generate_batch(seed=SEED, n=200)
    payment = next(p for p in batch.payments
                   if p.status == PaymentStatus.CAPTURED and p.fee_paise)
    capture = next(e for e in entries_for(batch, payment.payment_id)
                   if e.entry_type == "capture")
    assert capture.amount_paise == payment.amount_paise


def test_the_fee_is_a_separate_negative_expense_line():
    batch = generate_batch(seed=SEED, n=200)
    payment = next(p for p in batch.payments
                   if p.status == PaymentStatus.CAPTURED and p.fee_paise)
    fee = next(e for e in entries_for(batch, payment.payment_id) if e.entry_type == "fee")
    assert fee.amount_paise == -(payment.fee_paise + (payment.tax_paise or 0))


def test_revenue_plus_fee_expense_equals_what_settled():
    batch = generate_batch(seed=SEED, n=200)
    payment = next(p for p in batch.payments
                   if p.status == PaymentStatus.CAPTURED and p.fee_paise)
    total = sum(e.amount_paise for e in entries_for(batch, payment.payment_id))
    assert total == payment.amount_paise - payment.fee_paise - (payment.tax_paise or 0)


def test_a_payment_with_no_fee_data_books_no_fee_line():
    """Nothing is invented. A missing fee is a missing line, not a zero."""
    batch = generate_batch(seed=SEED, n=300)
    payment = next(p for p in batch.payments
                   if p.status == PaymentStatus.CAPTURED and p.fee_paise is None)
    assert not [e for e in entries_for(batch, payment.payment_id) if e.entry_type == "fee"]


def test_the_invariant_holds_on_a_clean_batch():
    assert check_ledger_invariant(generate_batch(seed=SEED, n=300)).ok


def test_the_invariant_reports_all_three_terms():
    result = check_ledger_invariant(generate_batch(seed=SEED, n=200))
    assert result.revenue_paise > 0
    assert result.fee_expense_paise < 0
    assert result.revenue_paise + result.fee_expense_paise == result.settled_paise


def test_the_invariant_catches_a_wrong_fee_line():
    """The failure gross booking exists to make visible."""
    from statesync.generator.synthetic import Batch

    batch = generate_batch(seed=SEED, n=100)
    fee_entry = next(e for e in batch.ledger_entries if e.entry_type == "fee")
    tampered = [
        e.model_copy(update={"amount_paise": e.amount_paise - 5000})
        if e.entry_id == fee_entry.entry_id else e
        for e in batch.ledger_entries
    ]
    broken = Batch(seed=batch.seed, payments=batch.payments, orders=batch.orders,
                   ledger_entries=tampered)
    result = check_ledger_invariant(broken)
    assert not result.ok
    assert result.delta_paise == -5000


def test_a_refunded_payment_nets_to_zero():
    batch = generate_batch(seed=SEED, n=300)
    refunded = next(p for p in batch.payments if p.status == PaymentStatus.REFUNDED)
    assert sum(e.amount_paise for e in entries_for(batch, refunded.payment_id)) == 0


def test_gross_revenue_is_the_gst_relevant_figure():
    """A merchant owes GST on gross sale value, so gross must be recoverable
    from the books without reconstructing it from a net figure plus a fee."""
    batch = generate_batch(seed=SEED, n=200)
    revenue = sum(e.amount_paise for e in batch.ledger_entries if e.entry_type == "capture")
    gross = sum(p.amount_paise for p in batch.payments
                if p.status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED))
    assert revenue == gross
