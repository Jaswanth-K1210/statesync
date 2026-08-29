"""The same store contract, against real Postgres.

`InMemoryRepairStore` is what the eval runs against and it is fast. This suite
is what stops it drifting from the implementation that carries the actual
guarantee — the constraint in the schema.
"""

import psycopg
import pytest

from statesync.config import ADMIN_DSN, APP_DSN
from statesync.executor.store import DuplicateRepairKey, PostgresRepairStore
from statesync.ledger.store import apply_migrations

pytestmark = pytest.mark.integration


@pytest.fixture
def store():
    apply_migrations(ADMIN_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE repairs, orders")
    return PostgresRepairStore(APP_DSN)


def test_a_repair_is_recorded(store):
    store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 100})
    assert store.has("rk_1") and store.write_count == 1


def test_the_unique_constraint_rejects_a_second_repair(store):
    """The guarantee, exercised against the real schema."""
    store.apply("rk_1", divergence_key="dk_1", payload={})
    with pytest.raises(DuplicateRepairKey):
        store.apply("rk_1", divergence_key="dk_1", payload={})
    assert store.write_count == 1
    assert store.count() == 1


def test_orders_are_unique_on_payment_id(store):
    store.create_order("order_1", payment_id="pay_1", total_paise=400000)
    with pytest.raises(DuplicateRepairKey):
        store.create_order("order_2", payment_id="pay_1", total_paise=400000)


def test_a_duplicate_order_leaves_exactly_one_row(store):
    store.create_order("order_1", payment_id="pay_1", total_paise=400000)
    with pytest.raises(DuplicateRepairKey):
        store.create_order("order_2", payment_id="pay_1", total_paise=400000)
    with psycopg.connect(APP_DSN) as conn:
        rows = conn.execute("SELECT count(*) FROM orders WHERE payment_id = 'pay_1'").fetchone()
    assert rows is not None and rows[0] == 1


def test_payload_round_trips_through_jsonb(store):
    store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 400000, "k": "v"})
    assert store.get("rk_1")["payload"] == {"amount_paise": 400000, "k": "v"}


def test_float_amounts_are_refused_before_reaching_the_database(store):
    with pytest.raises(TypeError):
        store.apply("rk_1", divergence_key="dk_1", payload={"amount_paise": 100.5})
    assert store.count() == 0


def test_the_app_role_cannot_update_or_delete_a_repair(store):
    store.apply("rk_1", divergence_key="dk_1", payload={})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            conn.execute("DELETE FROM repairs")


def test_the_ledger_enforces_one_entry_per_repair_key(store):
    """The audit trail carries the same constraint the domain tables do."""
    from datetime import UTC, datetime

    from statesync.ledger.chain import Ledger
    from statesync.ledger.store import LedgerStore

    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")
    ledger_store = LedgerStore(APP_DSN)
    led = Ledger(clock=lambda: datetime(2026, 9, 5, tzinfo=UTC))

    ledger_store.append(led.append("REPAIR_SUCCEEDED", repair_key="rk_1"))
    with pytest.raises(psycopg.errors.UniqueViolation):
        ledger_store.append(led.append("REPAIR_SUCCEEDED", repair_key="rk_1"))
