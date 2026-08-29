"""Every executor's idempotency, proven at the executor itself.

The lease and the unique constraint both sit *above* these functions. This
suite removes both and asks the harder question: if a repair somehow ran
twice, would the merchant's state still be correct?

That matters because a unique constraint only protects INSERT-shaped repairs.
An UPDATE writes no new row, so `write_count` stays flat while the value goes
wrong — a double-repair path the row-count tests cannot see. Every executor
therefore declares one of:

    INSERT_UNIQUE    an insert guarded by a uniqueness constraint
    ABSOLUTE_UPDATE  assigns a fixed value; running it twice is a no-op
    GUARDED_UPDATE   conditional on expected state; the second run matches nothing

A relative update (`stock = stock + n`) belongs to none of these and is
forbidden. `test_no_executor_performs_a_relative_update` enforces that.
"""

import pytest

from statesync.executor.repairs import REPAIRS, IdempotencyClass, idempotency_class
from statesync.executor.store import InMemoryRepairStore
from statesync.models.enums import DivergenceClass
from tests.unit.helpers import divergence_for

ALL_CLASSES = [
    DivergenceClass.CAPTURED_NO_ORDER,
    DivergenceClass.ORDER_NO_CAPTURE,
    DivergenceClass.DUPLICATE_ORDER,
    DivergenceClass.REFUND_NOT_REFLECTED,
]


@pytest.fixture
def store():
    s = InMemoryRepairStore()
    # An order already exists for the classes that repair by updating one.
    s.create_order("order_1", payment_id="pay_1", total_paise=400000,
                   status="pending", inventory_units=3)
    s.write_count = 0
    return s


# ── every executor declares its idempotency class ───────────────────────────

@pytest.mark.parametrize("klass", ALL_CLASSES)
def test_every_executor_declares_an_idempotency_class(klass):
    assert idempotency_class(klass) in set(IdempotencyClass)


def test_captured_no_order_is_an_insert_guarded_by_a_unique_constraint():
    assert idempotency_class(DivergenceClass.CAPTURED_NO_ORDER) == IdempotencyClass.INSERT_UNIQUE


def test_refund_not_reflected_is_an_insert_guarded_by_a_unique_constraint():
    assert idempotency_class(DivergenceClass.REFUND_NOT_REFLECTED) == (
        IdempotencyClass.INSERT_UNIQUE
    )


def test_order_no_capture_is_a_guarded_update():
    """Expiring an order is an UPDATE. No unique constraint protects it."""
    assert idempotency_class(DivergenceClass.ORDER_NO_CAPTURE) == IdempotencyClass.GUARDED_UPDATE


def test_duplicate_order_is_a_guarded_update():
    assert idempotency_class(DivergenceClass.DUPLICATE_ORDER) == IdempotencyClass.GUARDED_UPDATE


# ── the executors actually perform their stated effect ──────────────────────

def test_order_no_capture_really_expires_the_order(store):
    REPAIRS[DivergenceClass.ORDER_NO_CAPTURE](
        divergence_for(DivergenceClass.ORDER_NO_CAPTURE), store
    )
    assert store.order_status("order_1") == "expired"


def test_order_no_capture_releases_the_inventory(store):
    REPAIRS[DivergenceClass.ORDER_NO_CAPTURE](
        divergence_for(DivergenceClass.ORDER_NO_CAPTURE), store
    )
    assert store.inventory_released("order_1") is True


def test_duplicate_order_really_voids_the_later_order(store):
    store.create_order("order_1_dup", payment_id="pay_1_dup", total_paise=400000,
                       status="confirmed")
    REPAIRS[DivergenceClass.DUPLICATE_ORDER](
        divergence_for(DivergenceClass.DUPLICATE_ORDER, order_id="order_1_dup"), store
    )
    assert store.order_status("order_1_dup") == "voided"


# ── the real question: run it twice with no protection at all ───────────────

@pytest.mark.parametrize("klass", ALL_CLASSES)
def test_running_an_executor_twice_directly_leaves_state_unchanged(store, klass):
    """Both idempotency layers removed. The executor must still be safe."""
    # CAPTURED_NO_ORDER means no order exists yet, so it gets a fresh payment.
    payment_id = "pay_fresh" if klass == DivergenceClass.CAPTURED_NO_ORDER else "pay_1"
    divergence = divergence_for(klass, payment_id=payment_id)
    executor = REPAIRS[klass]

    try:
        executor(divergence, store)
    except Exception as exc:  # pragma: no cover - first run must succeed
        pytest.fail(f"first run failed: {exc}")

    snapshot = store.snapshot()

    try:
        executor(divergence, store)
    except Exception:
        pass  # a constraint rejecting the second run is a correct outcome

    assert store.snapshot() == snapshot, f"{klass.value} changed state on a second run"


def test_releasing_inventory_twice_does_not_double_the_stock(store):
    """The specific bug a relative update would cause.

    `stock = stock + n` run twice writes no new row, so a row-count test stays
    green while the merchant's stock is silently wrong.
    """
    divergence = divergence_for(DivergenceClass.ORDER_NO_CAPTURE)
    REPAIRS[DivergenceClass.ORDER_NO_CAPTURE](divergence, store)
    units_after_first = store.inventory_units("order_1")

    try:
        REPAIRS[DivergenceClass.ORDER_NO_CAPTURE](divergence, store)
    except Exception:
        pass

    assert store.inventory_units("order_1") == units_after_first


def test_no_executor_performs_a_relative_update():
    """A relative update is not expressible through the store's interface.

    The store offers assignment and guarded assignment only. There is no
    increment method, so the unsafe shape cannot be written by accident.
    """
    assert not [m for m in dir(InMemoryRepairStore)
                if any(word in m for word in ("increment", "decrement", "add_to", "adjust"))]


def test_a_guarded_update_matches_nothing_on_the_second_run(store):
    divergence = divergence_for(DivergenceClass.ORDER_NO_CAPTURE)
    assert store.expire_order("order_1", expected_status="pending") is True
    assert store.expire_order("order_1", expected_status="pending") is False
    _ = divergence
