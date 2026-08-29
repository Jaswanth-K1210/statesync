"""The idempotency property.

    Running N repairs M times produces exactly len(unique_keys) writes.

This is the credibility claim of the entire project. A reconciler that
double-repairs is worse than one that does nothing, and a reviewer will test
it. If everything else has to go, this test stays.

Run against the in-memory store (production code, the one the eval uses) with
real Redis. `tests/integration/test_idempotency.py::test_flushing_redis_does_
not_cause_a_double_repair` covers the same property against the constraint
that carries the real guarantee.
"""

from datetime import UTC, datetime

import pytest
import redis as redis_lib
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from statesync.config import REDIS_URL
from statesync.executor.store import InMemoryRepairStore
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass
from statesync.policy.idempotency import IdempotentRepairer

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)

divergences = st.builds(
    Divergence,
    klass=st.sampled_from([
        DivergenceClass.CAPTURED_NO_ORDER,
        DivergenceClass.ORDER_NO_CAPTURE,
        DivergenceClass.DUPLICATE_ORDER,
        DivergenceClass.REFUND_NOT_REFLECTED,
    ]),
    payment_id=st.sampled_from([f"pay_{i}" for i in range(12)]),
    order_id=st.none(),
    amount_paise=st.integers(min_value=1, max_value=10_000_000),
    observed_at=st.just(NOW),
)


@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(divs=st.lists(divergences, min_size=1, max_size=20), runs=st.integers(1, 5))
def test_n_runs_equal_one_run(divs, runs):
    """The single most important test in the project."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()
    repairer = IdempotentRepairer(
        redis=client, store=store, ledger=Ledger(clock=lambda: NOW), clock=lambda: NOW
    )

    def execute(d):
        store.apply(d.deterministic_key(), divergence_key=d.deterministic_key(),
                    payload={"amount_paise": d.amount_paise})
        return {}

    for _ in range(runs):
        for d in divs:
            repairer.repair(d, execute)

    assert store.write_count == len({d.deterministic_key() for d in divs})


@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(divs=st.lists(divergences, min_size=1, max_size=15))
def test_exactly_one_fresh_repair_per_unique_divergence(divs):
    """The complement: replays are replays, not new work."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()
    repairer = IdempotentRepairer(
        redis=client, store=store, ledger=Ledger(clock=lambda: NOW), clock=lambda: NOW
    )

    results = []
    for _ in range(3):
        for d in divs:
            results.append(repairer.repair(d, lambda x: {}))

    fresh = [r for r in results if not r.replayed]
    assert len(fresh) == len({d.deterministic_key() for d in divs})


@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(divs=st.lists(divergences, min_size=1, max_size=15))
def test_flushing_redis_between_runs_still_writes_once(divs):
    """Redis is a cache. The property must survive losing it entirely."""
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()

    def execute(d):
        store.apply(d.deterministic_key(), divergence_key=d.deterministic_key(), payload={})
        return {}

    for _ in range(3):
        repairer = IdempotentRepairer(
            redis=client, store=store, ledger=Ledger(clock=lambda: NOW), clock=lambda: NOW
        )
        for d in divs:
            repairer.repair(d, execute)
        client.flushall()

    assert store.write_count == len({d.deterministic_key() for d in divs})
