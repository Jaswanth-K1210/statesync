"""Three-way reconciliation: gateway vs order store vs ledger.

Four of the six divergence classes fall out of set operations here. No model
is involved and claiming otherwise would be dishonest — §6 of the design doc
says so out loud, and this module is the evidence.

Detection only. Classification of the ambiguous residual (AMOUNT_MISMATCH,
SETTLEMENT_GAP) is Phase 5, and repair is Phase 3.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from statesync.config import ORDER_TIMEOUT
from statesync.generator.synthetic import Batch
from statesync.models.domain import Divergence, Order, Payment
from statesync.models.enums import DivergenceClass, PaymentStatus
from statesync.reconciler.staleness import eligible_for_reconciliation

__all__ = ["ReconcileIndex", "build_index", "reconcile", "reconcile_payment"]


@dataclass(frozen=True)
class ReconcileIndex:
    """The order-store and ledger views, indexed by payment.

    Built once per pass so that `reconcile_payment` is genuinely O(1) per
    record — which is what makes the per-record p50/p99 a real measurement
    rather than an artefact of rebuilding a dict 500 times.
    """

    orders_by_payment: dict[str, list[Order]]
    entry_types: dict[str, set[str]]
    booked_paise: dict[str, int]
    """revenue + fee_expense per payment — what the books say actually landed."""


def build_index(batch: Batch) -> ReconcileIndex:
    orders_by_payment: dict[str, list[Order]] = defaultdict(list)
    for order in batch.orders:
        if order.payment_id is not None:
            orders_by_payment[order.payment_id].append(order)

    entry_types: dict[str, set[str]] = defaultdict(set)
    for entry in batch.ledger_entries:
        if entry.payment_id is not None:
            entry_types[entry.payment_id].add(entry.entry_type)

    booked: dict[str, int] = defaultdict(int)
    for entry in batch.ledger_entries:
        if entry.payment_id is not None and entry.entry_type in ("capture", "fee"):
            booked[entry.payment_id] += entry.amount_paise

    return ReconcileIndex(orders_by_payment=dict(orders_by_payment),
                          entry_types=dict(entry_types), booked_paise=dict(booked))


def reconcile(batch: Batch, now: datetime) -> list[Divergence]:
    """Return every divergence visible between the three views.

    Deterministic in input order, so two runs produce identical output.
    """
    index = build_index(batch)
    found: list[Divergence] = []
    for payment in batch.payments:
        found.extend(reconcile_payment(payment, index, now))
    return found


def reconcile_payment(
    payment: Payment, index: ReconcileIndex, now: datetime
) -> list[Divergence]:
    """Reconcile exactly one payment against the other two views."""
    found: list[Divergence] = []
    pid = payment.payment_id
    orders = index.orders_by_payment.get(pid, [])

    # ORDER_NO_CAPTURE — the order exists but money never moved. Gated on the
    # order's age rather than the staleness window: an abandoned payment never
    # reaches a terminal state, so it would otherwise never become eligible.
    #
    # The clock runs from the *most recent* evidence of activity, not from the
    # order alone. An authorised-but-uncaptured payment inside its window is
    # not a divergence, it is a payment in progress — and a retry authorised
    # two minutes ago against a six-hour-old order is the same thing. Gating on
    # order age alone would flag it, which is a false positive on a live
    # payment: precisely the failure the staleness design exists to prevent.
    if orders and not payment.status.is_terminal:
        last_activity = max(
            min(o.created_at for o in orders),
            payment.status_changed_at,
        )
        if now - last_activity > ORDER_TIMEOUT:
            found.append(
                _divergence(DivergenceClass.ORDER_NO_CAPTURE, payment, orders[0].order_id, now)
            )
        return found

    if not eligible_for_reconciliation(payment, now):
        return found  # mid-flight, or too fresh to trust

    # DUPLICATE_ORDER — at-least-once delivery handled twice. Matched on exact
    # payment_id only, never on heuristic similarity: two legitimate orders
    # from one customer for one amount seconds apart must never merge.
    if len(orders) > 1:
        found.append(
            _divergence(DivergenceClass.DUPLICATE_ORDER, payment, orders[0].order_id, now)
        )

    # CAPTURED_NO_ORDER — money moved, the webhook never landed.
    if payment.status == PaymentStatus.CAPTURED and not orders:
        found.append(_divergence(DivergenceClass.CAPTURED_NO_ORDER, payment, None, now))

    # AMOUNT_MISMATCH — the books disagree with the gateway by more than the
    # fee explains. Known fee and tax are subtracted **deterministically and
    # first**; only the residual left over is genuinely ambiguous, and only
    # that residual is ever handed to the propose-verify layer.
    #
    # With gross booking the residual has a name: it is the gap between the
    # fee the merchant booked and the fee the gateway actually charged, which
    # has causes an ops person can act on rather than an abstract shortfall.
    if payment.status == PaymentStatus.CAPTURED and orders:
        known = (payment.fee_paise or 0) + (payment.tax_paise or 0)
        expected_net = orders[0].total_paise - known
        residual = expected_net - index.booked_paise.get(pid, 0)
        if residual != 0:
            divergence = _divergence(DivergenceClass.AMOUNT_MISMATCH, payment,
                                     orders[0].order_id, now)
            divergence.detail["residual_paise"] = str(residual)
            divergence.detail["known_paise"] = str(known)
            found.append(divergence)

    # REFUND_NOT_REFLECTED — the gateway refunded, the books never heard.
    if payment.status == PaymentStatus.REFUNDED and "refund" not in index.entry_types.get(
        pid, set()
    ):
        found.append(
            _divergence(DivergenceClass.REFUND_NOT_REFLECTED, payment,
                        orders[0].order_id if orders else None, now)
        )

    return found


def _divergence(
    klass: DivergenceClass, payment: object, order_id: str | None, now: datetime
) -> Divergence:
    return Divergence(
        klass=klass,
        payment_id=payment.payment_id,  # type: ignore[attr-defined]
        order_id=order_id,
        amount_paise=payment.amount_paise,  # type: ignore[attr-defined]
        observed_at=now,
        detail={"instrument": payment.instrument},  # type: ignore[attr-defined]
    )
