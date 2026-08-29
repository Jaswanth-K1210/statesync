"""The repair store's unique constraint — layer 1 of two.

Redis is a cache. It can be flushed, evicted or partitioned, and it must never
be the only thing standing between the system and a double repair. The real
guarantee lives in a uniqueness constraint, where it cannot be lost.

Two production implementations, tested against the same contract: the
in-memory store the eval runs against, and the Postgres store that carries the
constraint in the schema. `test_postgres_store.py` runs this same suite
against real Postgres.
"""

import pytest

from statesync.executor.store import DuplicateRepairKey, InMemoryRepairStore


@pytest.fixture
def store():
    return InMemoryRepairStore()


def test_a_repair_is_recorded(store):
    store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 100})
    assert store.has("rk_1")
    assert store.write_count == 1


def test_a_second_repair_on_the_same_key_is_rejected(store):
    store.apply("rk_1", divergence_key="dk_1", payload={})
    with pytest.raises(DuplicateRepairKey):
        store.apply("rk_1", divergence_key="dk_1", payload={})


def test_a_rejected_duplicate_does_not_count_as_a_write(store):
    store.apply("rk_1", divergence_key="dk_1", payload={})
    with pytest.raises(DuplicateRepairKey):
        store.apply("rk_1", divergence_key="dk_1", payload={})
    assert store.write_count == 1


def test_different_keys_are_independent(store):
    store.apply("rk_1", divergence_key="dk_1", payload={})
    store.apply("rk_2", divergence_key="dk_2", payload={})
    assert store.write_count == 2


def test_payload_round_trips(store):
    store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 400000})
    assert store.get("rk_1")["payload"] == {"amount_paise": 400000}


def test_float_amounts_are_refused_at_the_store_boundary(store):
    """Constraint 1 holds all the way to the write."""
    with pytest.raises(TypeError):
        store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 100.5})


def test_orders_are_unique_on_payment_id(store):
    """The brief's constraint: orders.payment_id UNIQUE."""
    store.create_order("order_1", payment_id="pay_1", total_paise=400000)
    with pytest.raises(DuplicateRepairKey):
        store.create_order("order_2", payment_id="pay_1", total_paise=400000)


def test_creating_an_order_counts_as_a_write(store):
    store.create_order("order_1", payment_id="pay_1", total_paise=400000)
    assert store.write_count == 1


def test_a_rejected_order_does_not_count_as_a_write(store):
    store.create_order("order_1", payment_id="pay_1", total_paise=400000)
    with pytest.raises(DuplicateRepairKey):
        store.create_order("order_2", payment_id="pay_1", total_paise=400000)
    assert store.write_count == 1
