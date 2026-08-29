"""One repair per divergence class.

Each returns a plain dict describing what it did — integer paise only, so the
description is safe to write straight to the audit ledger. None of them decide
*whether* to act; that is the policy engine's job, and it has already run by
the time any of these are called.

    CAPTURED_NO_ORDER     create the order idempotently  | duplicate on replay
    ORDER_NO_CAPTURE      expire, release inventory      | cancels a paid order
    DUPLICATE_ORDER       merge on payment_id, void      | merges two real orders
    REFUND_NOT_REFLECTED  post a compensating entry      | double refund

The right-hand column is why each one writes through the constrained store
rather than doing its own bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from statesync.executor.store import RepairStore
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass

__all__ = ["REPAIRS", "repair_for"]


def _repair_key(divergence: Divergence) -> str:
    return divergence.deterministic_key()


def captured_no_order(divergence: Divergence, store: RepairStore) -> dict[str, Any]:
    """Money moved and no order exists. Create one, keyed on the payment.

    The order is created through the `payment_id UNIQUE` constraint, so a
    webhook that replays six hours later cannot produce a second one.
    """
    order_id = f"order_repair_{divergence.payment_id}"
    store.create_order(order_id, payment_id=divergence.payment_id or "",
                       total_paise=divergence.amount_paise)
    store.apply(_repair_key(divergence), divergence_key=divergence.deterministic_key(),
                payload={"action": "created_order", "order_id": order_id,
                         "amount_paise": divergence.amount_paise})
    return {"action": "created_order", "order_id": order_id,
            "amount_paise": divergence.amount_paise}


def order_no_capture(divergence: Divergence, store: RepairStore) -> dict[str, Any]:
    """The order was never paid. Expire it and release the inventory.

    The dangerous failure here is cancelling an order that *was* paid, which
    is why this class is gated behind the staleness window and the two-run
    confirmation rather than acting on a single observation.
    """
    store.apply(_repair_key(divergence), divergence_key=divergence.deterministic_key(),
                payload={"action": "expired_order", "order_id": divergence.order_id or "",
                         "amount_paise": divergence.amount_paise})
    return {"action": "expired_order", "order_id": divergence.order_id or "",
            "amount_paise": divergence.amount_paise}


def duplicate_order(divergence: Divergence, store: RepairStore) -> dict[str, Any]:
    """Two orders, one payment. Merge on the payment id, void the later row.

    **Merged on exact payment_id only, never on heuristic similarity.** Two
    legitimate orders from one customer for one amount seconds apart must not
    merge, and that is a test case rather than a hope.
    """
    store.apply(_repair_key(divergence), divergence_key=divergence.deterministic_key(),
                payload={"action": "merged_on_payment_id",
                         "merged_on": divergence.payment_id or "",
                         "amount_paise": divergence.amount_paise})
    return {"action": "merged_on_payment_id", "merged_on": divergence.payment_id or "",
            "amount_paise": divergence.amount_paise}


def refund_not_reflected(divergence: Divergence, store: RepairStore) -> dict[str, Any]:
    """The gateway refunded and the books never heard. Post the counter-entry.

    Negative amount: a refund reduces the books. Getting the sign wrong here
    is a double refund, which is real money out the door.
    """
    amount = -divergence.amount_paise
    store.apply(_repair_key(divergence), divergence_key=divergence.deterministic_key(),
                payload={"action": "posted_compensating_entry", "amount_paise": amount})
    return {"action": "posted_compensating_entry", "amount_paise": amount,
            "payment_id": divergence.payment_id or ""}


REPAIRS: dict[DivergenceClass, Callable[[Divergence, RepairStore], dict[str, Any]]] = {
    DivergenceClass.CAPTURED_NO_ORDER: captured_no_order,
    DivergenceClass.ORDER_NO_CAPTURE: order_no_capture,
    DivergenceClass.DUPLICATE_ORDER: duplicate_order,
    DivergenceClass.REFUND_NOT_REFLECTED: refund_not_reflected,
}


def repair_for(klass: DivergenceClass) -> Callable[[Divergence, RepairStore], dict[str, Any]]:
    """Look up the executor. A missing class is an error, never a no-op."""
    try:
        return REPAIRS[klass]
    except KeyError as exc:
        raise KeyError(f"no repair executor for {klass.value}; it must escalate") from exc
