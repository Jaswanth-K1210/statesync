"""One repair executor per divergence class, behind the policy engine.

Each class needs a different repair and each has a dangerous failure mode if
the class is wrong:

    CAPTURED_NO_ORDER     create the order idempotently  | duplicate on replay
    ORDER_NO_CAPTURE      expire, release inventory      | cancels a paid order
    DUPLICATE_ORDER       merge on payment_id, void      | merges two real orders
    REFUND_NOT_REFLECTED  post a compensating entry      | double refund
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

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def rds():
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture
def runner(rds):
    return RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=100), clock=lambda: NOW,
    )


def divergence(klass, amount=400000, pid="pay_1", oid="order_1"):
    return Divergence(klass=klass, payment_id=pid, order_id=oid,
                      amount_paise=amount, observed_at=NOW)


# ── one executor per class ──────────────────────────────────────────────────

def test_captured_no_order_creates_an_order(runner):
    result = runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER))
    assert result.status == RepairStatus.SUCCEEDED
    assert runner.summary()["succeeded"] == 1
    assert runner.ledger.has("REPAIR_SUCCEEDED")


def test_captured_no_order_writes_the_order_and_the_repair_record(runner):
    """Two rows for one repair: the order itself, and the repair log entry.

    `write_count` counts rows, not repairs. The idempotency claim is about
    rows — a second run must add none of them.
    """
    runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER))
    assert runner.store.write_count == 2


def test_captured_no_order_is_idempotent_on_replay(runner):
    """A webhook that replays later must not create a second order."""
    d = divergence(DivergenceClass.CAPTURED_NO_ORDER)
    runner.run(d)
    rows_after_first = runner.store.write_count
    second = runner.run(d)
    assert second.replayed
    assert runner.store.write_count == rows_after_first, "the replay must write nothing"


def test_order_no_capture_expires_the_order(runner):
    result = runner.run(divergence(DivergenceClass.ORDER_NO_CAPTURE))
    assert result.status == RepairStatus.SUCCEEDED
    assert result.detail["action"] == "expired_order"


def test_duplicate_order_merges_on_payment_id(runner):
    result = runner.run(divergence(DivergenceClass.DUPLICATE_ORDER))
    assert result.detail["action"] == "merged_on_payment_id"
    assert result.detail["merged_on"] == "pay_1"


def test_refund_not_reflected_posts_a_compensating_entry(runner):
    result = runner.run(divergence(DivergenceClass.REFUND_NOT_REFLECTED, amount=250000))
    assert result.detail["action"] == "posted_compensating_entry"
    assert result.detail["amount_paise"] == -250000, "a refund is a negative entry"


def test_every_repair_amount_is_integer_paise(runner):
    for klass in (DivergenceClass.CAPTURED_NO_ORDER, DivergenceClass.REFUND_NOT_REFLECTED):
        detail = runner.run(divergence(klass, pid=f"pay_{klass.value}")).detail
        for value in detail.values():
            assert not isinstance(value, float)


# ── the gates are load-bearing ──────────────────────────────────────────────

def test_an_unrepairable_class_escalates_without_writing(rds):
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(), cap=BlastRadiusCap(max_repairs=100), clock=lambda: NOW,
    )
    result = runner.run(divergence(DivergenceClass.AMOUNT_MISMATCH))
    assert result.status == RepairStatus.ESCALATE
    assert result.reason == "CLASS_NOT_AUTO_REPAIRABLE"
    assert runner.store.write_count == 0


def test_a_high_value_repair_escalates_without_writing(rds):
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=100_000),
        cap=BlastRadiusCap(max_repairs=100), clock=lambda: NOW,
    )
    result = runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER, amount=9_000_000))
    assert result.status == RepairStatus.ESCALATE
    assert result.reason == "VALUE_THRESHOLD_EXCEEDED"
    assert runner.store.write_count == 0


def test_the_blast_radius_cap_stops_the_run(rds):
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=2), clock=lambda: NOW,
    )
    results = [
        runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER, pid=f"pay_{i}"))
        for i in range(5)
    ]
    assert runner.summary()["succeeded"] == 2, "the cap is on repairs, not rows"
    blocked = [r for r in results if r.reason == "BLAST_RADIUS_EXCEEDED"]
    assert len(blocked) == 3


def test_a_blocked_repair_is_recorded_in_the_ledger(rds):
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=0), clock=lambda: NOW,
    )
    runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER))
    assert runner.ledger.has("REPAIR_BLOCKED")


def test_an_escalation_is_recorded_in_the_ledger(rds):
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(), cap=BlastRadiusCap(max_repairs=10), clock=lambda: NOW,
    )
    runner.run(divergence(DivergenceClass.AMOUNT_MISMATCH))
    assert runner.ledger.has("REPAIR_ESCALATED")


def test_gates_run_before_the_lease_is_claimed(rds):
    """A repair the policy refuses must not leave a claim behind, or a later
    run with a raised threshold would see a phantom in-progress repair."""
    runner = RepairRunner(
        redis=rds, store=InMemoryRepairStore(), ledger=Ledger(clock=lambda: NOW),
        gate=PolicyGate(value_threshold_paise=1), cap=BlastRadiusCap(max_repairs=10),
        clock=lambda: NOW,
    )
    d = divergence(DivergenceClass.CAPTURED_NO_ORDER, amount=999_999)
    runner.run(d)
    assert rds.get(f"repair:{d.deterministic_key()}") is None


def test_the_run_is_summarised_for_reporting(runner):
    for i in range(3):
        runner.run(divergence(DivergenceClass.CAPTURED_NO_ORDER, pid=f"pay_{i}"))
    summary = runner.summary()
    assert summary["succeeded"] == 3
    assert summary["writes"] == 6  # each repair writes an order plus a repair record
    assert summary["escalated"] == 0
    assert summary["blast_radius_used"] == 3
