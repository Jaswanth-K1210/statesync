"""A batch that dies partway through must resume without repairing twice.

The gateway returns 500 at record 250 of 500. The process stops, someone
restarts it, and the batch runs again from the top. Everything already
repaired must stay repaired exactly once — resumption is ordinary here, not a
special path, because the idempotency key is derived from the divergence
rather than from position in a run.
"""

from datetime import UTC, datetime

import pytest
import redis as redis_lib

from statesync.config import REDIS_URL
from statesync.executor.runner import RepairRunner
from statesync.executor.store import InMemoryRepairStore
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass
from statesync.policy.blast_radius import BlastRadiusCap
from statesync.policy.gates import PolicyGate
from statesync.policy.idempotency import RepairStatus

pytestmark = pytest.mark.chaos

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


class GatewayError(RuntimeError):
    """A 5xx from the gateway, mid-batch."""


def divergences(n: int) -> list[Divergence]:
    return [
        Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id=f"pay_{i}",
                   order_id=None, amount_paise=1_000 + i, observed_at=NOW)
        for i in range(n)
    ]


@pytest.fixture
def rds():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()


def a_runner(rds, store) -> RepairRunner:
    return RepairRunner(
        redis=rds, store=store, ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=500), clock=lambda: NOW,
    )


def test_a_batch_that_fails_midway_resumes_without_double_repairing(rds):
    store = InMemoryRepairStore()
    runner = a_runner(rds, store)
    batch = divergences(20)

    # The gateway dies at record 10.
    with pytest.raises(GatewayError):
        for i, divergence in enumerate(batch):
            if i == 10:
                raise GatewayError("gateway returned 500")
            runner.run(divergence)

    writes_before = store.write_count
    assert writes_before > 0, "the first half should have repaired something"

    # Restart: the whole batch runs again from the top.
    resumed = a_runner(rds, store)
    for divergence in batch:
        resumed.run(divergence)

    assert store.write_count == len(batch) * 2, (
        "each divergence writes an order and a repair record, exactly once"
    )


def test_the_records_repaired_before_the_failure_are_replayed_not_rewritten(rds):
    store = InMemoryRepairStore()
    runner = a_runner(rds, store)
    batch = divergences(10)

    for divergence in batch[:5]:
        runner.run(divergence)
    writes_after_partial = store.write_count

    resumed = a_runner(rds, store)
    results = [resumed.run(divergence) for divergence in batch]

    replayed = [r for r in results if r.replayed]
    assert len(replayed) == 5, "the first five should replay, not re-execute"
    assert store.write_count == writes_after_partial + 10, "only the new five wrote"


def test_resuming_after_a_redis_loss_still_writes_once(rds):
    """The restart lost the cache as well as the process."""
    store = InMemoryRepairStore()
    runner = a_runner(rds, store)
    batch = divergences(10)

    for divergence in batch[:5]:
        runner.run(divergence)
    writes_after_partial = store.write_count

    rds.flushall()

    resumed = a_runner(rds, store)
    results = [resumed.run(divergence) for divergence in batch]

    assert store.write_count == writes_after_partial + 10
    assert [r for r in results if r.status == RepairStatus.ALREADY_APPLIED], (
        "the DB constraint should have caught the first five"
    )


def test_a_failure_during_a_repair_is_recorded_before_it_propagates(rds):
    """The exception escapes, but not before the ledger knows about it."""
    store = InMemoryRepairStore()
    runner = a_runner(rds, store)

    def failing(_):
        raise GatewayError("gateway returned 500 during execute")

    with pytest.raises(GatewayError):
        runner._repairer.repair(divergences(1)[0], failing)

    assert runner.ledger.has("REPAIR_FAILED")


def test_the_ledger_still_verifies_after_a_midbatch_failure(rds):
    store = InMemoryRepairStore()
    runner = a_runner(rds, store)
    batch = divergences(10)

    with pytest.raises(GatewayError):
        for i, divergence in enumerate(batch):
            if i == 5:
                raise GatewayError("gateway returned 500")
            runner.run(divergence)

    assert runner.ledger.verify() == (True, None)


def test_resumption_needs_no_bookkeeping_of_where_it_stopped(rds):
    """The idempotency key comes from the divergence, not from position in a
    run, so nothing has to remember how far the previous attempt got."""
    store = InMemoryRepairStore()
    batch = divergences(12)

    for cut in (3, 7, 12):
        runner = a_runner(rds, store)
        for divergence in batch[:cut]:
            runner.run(divergence)

    assert store.write_count == len(batch) * 2
