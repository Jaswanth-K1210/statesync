"""The repair lease state machine — layer 2 of two.

The naive setnx pattern has a real hole: if execution crashes *after* claiming
the key but *before* writing the result, the next call sees the claim, reads a
missing result, and silently returns nothing — a stuck repair with no alerting
path, forever.

Five states, five tests. The one that matters is the expired lease: a crash
mid-execution must surface as an escalation, never as silence.
"""

from datetime import UTC, datetime, timedelta

import pytest
import redis as redis_lib

from statesync.config import LEASE_SECONDS, REDIS_URL
from statesync.executor.store import DuplicateRepairKey, InMemoryRepairStore
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass
from statesync.policy.idempotency import IdempotentRepairer, RepairStatus

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def rds():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def ledger():
    return Ledger(clock=lambda: NOW)


@pytest.fixture
def store():
    return InMemoryRepairStore()


def divergence(pid="pay_1") -> Divergence:
    return Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id=pid,
                      order_id=None, amount_paise=400000, observed_at=NOW)


def repairer(rds, store, ledger, now=NOW):
    return IdempotentRepairer(redis=rds, store=store, ledger=ledger, clock=lambda: now)


def writing_executor(store):
    """What a real executor does: write through the constrained store."""
    def execute(d):
        store.apply(f"rk_{d.payment_id}", divergence_key=d.deterministic_key(),
                    payload={"amount_paise": d.amount_paise})
        return {"created_order": f"order_for_{d.payment_id}"}
    return execute


# ── state 1: first run ──────────────────────────────────────────────────────

def test_a_fresh_repair_succeeds_and_writes_once(rds, store, ledger):
    result = repairer(rds, store, ledger).repair(divergence(), writing_executor(store))
    assert result.status == RepairStatus.SUCCEEDED
    assert store.write_count == 1
    assert ledger.has("REPAIR_SUCCEEDED")


# ── state 2: SUCCEEDED ──────────────────────────────────────────────────────

def test_a_repeat_call_returns_the_cached_result_with_no_side_effects(rds, store, ledger):
    execute = writing_executor(store)
    r = repairer(rds, store, ledger)
    first = r.repair(divergence(), execute)
    second = r.repair(divergence(), execute)
    assert first.status == second.status == RepairStatus.SUCCEEDED
    assert not first.replayed and second.replayed
    assert store.write_count == 1, "the second call must not write"


def test_a_repeat_call_does_not_re_execute_the_repair(rds, store, ledger):
    calls = []

    def counted(d):
        calls.append(d)
        return {}

    r = repairer(rds, store, ledger)
    r.repair(divergence(), counted)
    r.repair(divergence(), counted)
    assert len(calls) == 1


# ── state 3: FAILED ─────────────────────────────────────────────────────────

def test_a_failed_repair_is_not_silently_retried(rds, store, ledger):
    def boom(_):
        raise RuntimeError("merchant db write failed")

    r = repairer(rds, store, ledger)
    with pytest.raises(RuntimeError):
        r.repair(divergence(), boom)

    second = r.repair(divergence(), writing_executor(store))
    assert second.status == RepairStatus.FAILED
    assert store.write_count == 0


def test_a_failure_is_written_to_the_ledger_not_swallowed(rds, store, ledger):
    def boom(_):
        raise RuntimeError("merchant db write failed")

    with pytest.raises(RuntimeError):
        repairer(rds, store, ledger).repair(divergence(), boom)
    assert ledger.has("REPAIR_FAILED")


# ── state 4: CLAIMED, live lease ────────────────────────────────────────────

def test_a_live_lease_returns_in_progress_never_none(rds, store, ledger):
    """The v1 bug returned nothing here, which a caller reads as success."""
    r = repairer(rds, store, ledger)
    r.claim(divergence())  # another worker holds it

    result = r.repair(divergence(), writing_executor(store))
    assert result is not None
    assert result.status == RepairStatus.IN_PROGRESS
    assert store.write_count == 0


# ── state 5: CLAIMED, expired lease ─────────────────────────────────────────

def test_an_expired_lease_escalates_as_a_stuck_repair(rds, store, ledger):
    """A worker died mid-execution. This must never be silence."""
    repairer(rds, store, ledger).claim(divergence())

    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    result = repairer(rds, store, ledger, now=later).repair(divergence(), writing_executor(store))

    assert result.status == RepairStatus.ESCALATE
    assert result.reason == "STUCK_REPAIR"


def test_a_stuck_repair_is_recorded_in_the_ledger(rds, store, ledger):
    repairer(rds, store, ledger).claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    repairer(rds, store, ledger, now=later).repair(divergence(), writing_executor(store))
    assert ledger.has("STUCK_REPAIR_DETECTED")


def test_a_stuck_repair_does_not_write(rds, store, ledger):
    repairer(rds, store, ledger).claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    repairer(rds, store, ledger, now=later).repair(divergence(), writing_executor(store))
    assert store.write_count == 0


def test_the_claim_record_survives_the_lease_so_a_crash_is_detectable(rds, store, ledger):
    """If the claim key expired with the lease, the stuck repair would vanish
    and the escalation above could never fire."""
    repairer(rds, store, ledger).claim(divergence())
    key = f"repair:{divergence().deterministic_key()}"
    ttl = rds.ttl(key)
    assert ttl == -1 or ttl > LEASE_SECONDS, "the claim must outlive its own lease"


# ── the DB constraint catching what Redis missed ────────────────────────────

def test_a_duplicate_key_error_is_logged_and_returns_already_applied(rds, store, ledger):
    def already_there(_):
        raise DuplicateRepairKey("rk_1")

    result = repairer(rds, store, ledger).repair(divergence(), already_there)
    assert result.status == RepairStatus.ALREADY_APPLIED
    assert ledger.has("REPAIR_DEDUPED_BY_DB")


def test_flushing_redis_does_not_cause_a_double_repair(rds, store, ledger):
    """The proof that Redis is an optimisation, not the guarantee.

    Wipe the cache entirely and re-run every repair. The database constraint
    catches the duplicates, and the catch is logged rather than swallowed.
    """
    def execute(d):
        store.apply(f"rk_{d.payment_id}", divergence_key=d.deterministic_key(), payload={})
        return {}

    r = repairer(rds, store, ledger)
    r.repair(divergence(), execute)
    assert store.write_count == 1

    rds.flushall()

    second = repairer(rds, store, ledger).repair(divergence(), execute)
    assert second.status == RepairStatus.ALREADY_APPLIED
    assert store.write_count == 1, "the DB constraint must hold after a flush"
    assert ledger.has("REPAIR_DEDUPED_BY_DB")


# ── concurrency ─────────────────────────────────────────────────────────────

def test_ten_concurrent_workers_produce_exactly_one_repair(rds, store, ledger):
    from concurrent.futures import ThreadPoolExecutor

    execute = writing_executor(store)

    def attempt(_):
        client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
        return IdempotentRepairer(
            redis=client, store=store, ledger=Ledger(clock=lambda: NOW), clock=lambda: NOW
        ).repair(divergence(), execute)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(attempt, range(10)))

    # All ten report success — that is what idempotency means. Exactly one of
    # them actually executed; the rest are replays of its result.
    fresh = [r for r in results if r.status == RepairStatus.SUCCEEDED and not r.replayed]
    assert len(fresh) == 1
    assert store.write_count == 1
    assert not [r for r in results if r.status in
                (RepairStatus.FAILED, RepairStatus.ESCALATE)]


def test_repair_never_returns_none(rds, store, ledger):
    """Every path returns a Result. Silence is the failure mode this whole
    state machine exists to eliminate."""
    r = repairer(rds, store, ledger)
    execute = writing_executor(store)
    assert r.repair(divergence(), execute) is not None
    assert r.repair(divergence(), execute) is not None


# ── recovering from a stuck repair ──────────────────────────────────────────
# Without a recovery path a single crash poisons the divergence key for the
# whole retention window: every later pass re-escalates the same divergence
# and the repair can never be retried after a human has looked at it.

def test_a_stuck_repair_re_escalates_until_it_is_cleared(rds, store, ledger):
    """The failure mode being fixed: escalation repeats, forever, on its own."""
    repairer(rds, store, ledger).claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    r = repairer(rds, store, ledger, now=later)

    first = r.repair(divergence(), writing_executor(store))
    second = r.repair(divergence(), writing_executor(store))
    assert first.reason == second.reason == "STUCK_REPAIR"


def test_clearing_a_stuck_repair_permits_exactly_one_fresh_attempt(rds, store, ledger):
    repairer(rds, store, ledger).claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    r = repairer(rds, store, ledger, now=later)
    r.repair(divergence(), writing_executor(store))

    assert r.clear_stuck(divergence()) is True

    result = r.repair(divergence(), writing_executor(store))
    assert result.status == RepairStatus.SUCCEEDED
    assert store.write_count == 1


def test_clearing_is_recorded_in_the_ledger_with_the_operator(rds, store, ledger):
    """An operator override is exactly the thing an audit trail is for."""
    repairer(rds, store, ledger).claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    r = repairer(rds, store, ledger, now=later)
    r.repair(divergence(), writing_executor(store))
    r.clear_stuck(divergence(), operator="ops@merchant")

    assert ledger.has("STUCK_REPAIR_CLEARED")
    entry = next(e for e in ledger.entries if e.event_type == "STUCK_REPAIR_CLEARED")
    assert entry.payload["operator"] == "ops@merchant"


def test_clearing_a_repair_that_is_not_stuck_is_refused(rds, store, ledger):
    """Clearing a live or succeeded repair would re-open a double-repair path."""
    r = repairer(rds, store, ledger)
    r.repair(divergence(), writing_executor(store))
    assert r.clear_stuck(divergence()) is False
    assert store.write_count == 1


def test_clearing_an_unknown_repair_is_refused(rds, store, ledger):
    assert repairer(rds, store, ledger).clear_stuck(divergence()) is False


def test_a_cleared_repair_that_crashes_again_becomes_stuck_again(rds, store, ledger):
    """Clearing grants one attempt, not permanent immunity."""
    r0 = repairer(rds, store, ledger)
    r0.claim(divergence())
    later = NOW + timedelta(seconds=LEASE_SECONDS + 1)
    r = repairer(rds, store, ledger, now=later)
    r.repair(divergence(), writing_executor(store))
    r.clear_stuck(divergence())
    r.claim(divergence())  # crashes again

    later_still = later + timedelta(seconds=LEASE_SECONDS + 1)
    result = repairer(rds, store, ledger, now=later_still).repair(
        divergence(), writing_executor(store)
    )
    assert result.reason == "STUCK_REPAIR"
