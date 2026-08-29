"""The smoke ladder — rungs S0 and S1.

Not a test suite. This proves the system boots and does one real thing per
capability, in under thirty seconds, against real Postgres and real Redis.
It is what you run before every commit.

Rungs are added one per phase and the total budget never moves:

    S0 infra        Phase 1   4s
    S1 chain        Phase 1   2s
    S2 loop         Phase 2   8s     <- not yet built
    S3 idempotency  Phase 3   5s
    S4 refusal      Phase 4   3s
    S5 degradation  Phase 5   3s
    S6 fail-closed  Phase 7   2s
                              ---
                              27s of a 30s cap
"""

from datetime import UTC, datetime

import psycopg
import pytest
import redis

from statesync.config import ADMIN_DSN, APP_DSN, REDIS_URL
from statesync.ledger.chain import GENESIS, Ledger
from statesync.ledger.store import LedgerStore, apply_migrations

pytestmark = pytest.mark.smoke


@pytest.fixture(scope="module")
def clean_ledger():
    """Leave no residue: smoke starts and ends with an empty chain."""
    apply_migrations(ADMIN_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")
    yield LedgerStore(APP_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")


# ── S0 · infra ──────────────────────────────────────────────────────────────

def test_s0_postgres_is_reachable():
    with psycopg.connect(APP_DSN) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_s0_redis_is_reachable():
    assert redis.Redis.from_url(REDIS_URL).ping() is True


def test_s0_migrations_applied_and_ledger_table_exists(clean_ledger):
    with psycopg.connect(APP_DSN) as conn:
        row = conn.execute("SELECT to_regclass('public.ledger_entries')").fetchone()
    assert row is not None and row[0] == "ledger_entries"


def test_s0_app_role_has_no_update_or_delete_grant():
    """Constraint 7 leans on this: the audit trail is append-only by grant."""
    with psycopg.connect(APP_DSN) as conn:
        rows = conn.execute(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_name = 'ledger_entries' AND grantee = 'statesync_app'"
        ).fetchall()
    granted = {r[0] for r in rows}
    assert granted == {"SELECT", "INSERT"}


# ── S1 · chain ──────────────────────────────────────────────────────────────

def test_s1_writes_three_entries_and_the_chain_verifies(clean_ledger):
    led = Ledger(clock=lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC))
    for i in range(3):
        clean_ledger.append(led.append("SMOKE", n=i))

    assert clean_ledger.count() == 3
    entries = clean_ledger.load()
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS
    assert clean_ledger.verify() == (True, None)


def test_s1_tampering_is_detected_at_the_right_index(clean_ledger):
    """The fail-closed guarantee is only as good as this assertion."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("UPDATE ledger_entries SET payload = '{\"n\":999}' WHERE seq = 2")

    ok, idx = clean_ledger.verify()
    assert not ok and idx == 1
