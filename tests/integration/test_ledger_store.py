"""The ledger's Postgres storage: append-only, enforced at the role level.

A hash chain the application can rewrite is not an audit trail. The chain
detects tampering; the role grant is what makes tampering require a different
credential in the first place. Both, or neither is worth much.
"""

from datetime import UTC, datetime

import psycopg
import pytest

from statesync.config import ADMIN_DSN, APP_DSN
from statesync.ledger.chain import GENESIS, Ledger
from statesync.ledger.store import LedgerStore, apply_migrations

pytestmark = pytest.mark.integration


def clock():
    return lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store():
    apply_migrations(ADMIN_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")
    return LedgerStore(APP_DSN)


def test_append_persists_an_entry(store):
    led = Ledger(clock=clock())
    store.append(led.append("SMOKE", n=1))
    assert len(store.load()) == 1


def test_loaded_chain_verifies(store):
    led = Ledger(clock=clock())
    for i in range(3):
        store.append(led.append("SMOKE", n=i))
    assert store.verify() == (True, None)


def test_loaded_entries_round_trip_exactly(store):
    led = Ledger(clock=clock())
    original = led.append("SMOKE", n=7, note="₹4,000", flag=True)
    store.append(original)
    loaded = store.load()[0]
    assert loaded.hash == original.hash
    assert loaded.payload == original.payload
    assert loaded.prev_hash == GENESIS


def test_tamper_in_the_database_is_detected_by_the_chain(store):
    """Even with a credential that can UPDATE, the chain still catches it."""
    led = Ledger(clock=clock())
    for i in range(5):
        store.append(led.append("SMOKE", n=i))
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("UPDATE ledger_entries SET payload = '{\"n\":999}' WHERE seq = 3")
    ok, idx = store.verify()
    assert not ok and idx == 2


def test_app_role_cannot_update_an_entry(store):
    led = Ledger(clock=clock())
    store.append(led.append("SMOKE", n=1))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            conn.execute("UPDATE ledger_entries SET event_type = 'X'")


def test_app_role_cannot_delete_an_entry(store):
    led = Ledger(clock=clock())
    store.append(led.append("SMOKE", n=1))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            conn.execute("DELETE FROM ledger_entries")


def test_duplicate_seq_is_rejected_by_the_primary_key(store):
    led = Ledger(clock=clock())
    entry = led.append("SMOKE", n=1)
    store.append(entry)
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.append(entry)


def test_migrations_are_idempotent():
    apply_migrations(ADMIN_DSN)
    apply_migrations(ADMIN_DSN)
