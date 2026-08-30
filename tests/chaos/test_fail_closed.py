"""Fail closed on a compromised audit trail.

*"A reconciliation system that keeps writing while its own audit trail is
compromised is worse than no system at all."* That is the most important line
in the design, and until now it was only a line: `Ledger.verify()` existed and
was well tested, but nothing consumed the result. The chain could break and
repairs would carry on.

The guarantee is: **halt every repair, alert, exit non-zero, write nothing.**
Fail closed, never open.
"""

from datetime import UTC, datetime

import pytest

from statesync.executor.store import InMemoryRepairStore
from statesync.ledger.chain import ChainIntegrityError, Ledger, verify_or_halt
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass

pytestmark = pytest.mark.chaos

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def a_chain(n: int = 10) -> Ledger:
    led = Ledger(clock=lambda: NOW)
    for i in range(n):
        led.append("E", i=i)
    return led


def divergence(pid: str = "pay_1") -> Divergence:
    return Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id=pid,
                      order_id=None, amount_paise=400_000, observed_at=NOW)


# ── the guard itself ────────────────────────────────────────────────────────

def test_an_intact_chain_passes_the_guard():
    verify_or_halt(a_chain())


def test_a_tampered_chain_raises_chain_integrity_error():
    led = a_chain()
    led.entries[3].payload["i"] = 999
    with pytest.raises(ChainIntegrityError):
        verify_or_halt(led)


def test_the_error_names_the_break_index():
    """An ops person needs to know where the trail stopped being trustworthy."""
    led = a_chain()
    led.entries[3].payload["i"] = 999
    with pytest.raises(ChainIntegrityError, match="3"):
        verify_or_halt(led)


def test_a_deleted_entry_is_caught_by_the_guard():
    led = a_chain()
    del led.entries[4]
    with pytest.raises(ChainIntegrityError):
        verify_or_halt(led)


def test_an_empty_chain_is_intact_not_broken():
    """Nothing written yet is not the same as something rewritten."""
    verify_or_halt(Ledger(clock=lambda: NOW))


# ── nothing is written after a break ────────────────────────────────────────

def test_a_broken_chain_halts_the_run_before_any_repair():
    import redis as redis_lib

    from statesync.config import REDIS_URL
    from statesync.executor.runner import RepairRunner
    from statesync.policy.blast_radius import BlastRadiusCap
    from statesync.policy.gates import PolicyGate

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()
    led = a_chain()
    led.entries[2].payload["i"] = 777

    runner = RepairRunner(
        redis=client, store=store, ledger=led,
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=50), clock=lambda: NOW,
    )

    with pytest.raises(ChainIntegrityError):
        runner.run_batch([divergence("pay_1"), divergence("pay_2")])

    assert store.write_count == 0, "a repair landed while the audit trail was broken"
    client.flushdb()


def test_an_intact_chain_permits_the_batch():
    import redis as redis_lib

    from statesync.config import REDIS_URL
    from statesync.executor.runner import RepairRunner
    from statesync.policy.blast_radius import BlastRadiusCap
    from statesync.policy.gates import PolicyGate

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()
    runner = RepairRunner(
        redis=client, store=store, ledger=a_chain(),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=50), clock=lambda: NOW,
    )

    results = runner.run_batch([divergence("pay_1")])
    assert len(results) == 1
    assert store.write_count > 0
    client.flushdb()


def test_the_check_runs_before_the_first_repair_not_after():
    """Verifying afterwards would mean the writes already happened."""
    import redis as redis_lib

    from statesync.config import REDIS_URL
    from statesync.executor.runner import RepairRunner
    from statesync.policy.blast_radius import BlastRadiusCap
    from statesync.policy.gates import PolicyGate

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    store = InMemoryRepairStore()
    led = a_chain()
    led.entries[0].payload["i"] = 555

    runner = RepairRunner(
        redis=client, store=store, ledger=led,
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=50), clock=lambda: NOW,
    )
    with pytest.raises(ChainIntegrityError):
        runner.run_batch([divergence(f"pay_{i}") for i in range(20)])

    assert store.write_count == 0
    assert runner.cap.used == 0, "the blast-radius budget was spent before the check"
    client.flushdb()


# ── the process exits non-zero ──────────────────────────────────────────────

def test_the_tamper_harness_breaks_a_persisted_chain():
    """`python -m eval.tamper` is the demo beat. It must actually break it."""
    from eval.tamper import tamper_in_memory

    led = a_chain()
    assert led.verify()[0] is True
    tamper_in_memory(led, index=5)
    assert led.verify() == (False, 5)


def test_a_run_over_a_broken_chain_exits_non_zero():
    import subprocess
    import sys

    script = (
        "import sys;"
        "from datetime import UTC, datetime;"
        "from statesync.ledger.chain import Ledger, verify_or_halt, ChainIntegrityError;"
        "led = Ledger(clock=lambda: datetime(2026,9,5,tzinfo=UTC));"
        "[led.append('E', i=i) for i in range(5)];"
        "led.entries[2].payload['i'] = 999;"
        "verify_or_halt(led)"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode != 0
    assert "ChainIntegrityError" in proc.stderr
