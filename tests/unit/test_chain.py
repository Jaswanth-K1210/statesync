"""The hash-chained append-only ledger.

The §11 fail-closed invariant depends on this chain being able to detect any
mutation. These tests are the evidence it can.
"""

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from statesync.ledger.chain import GENESIS, Ledger, chain_hash


def fixed_clock():
    """A clock that never moves, so hashes are comparable across runs."""
    return lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def build(n: int = 10) -> Ledger:
    led = Ledger(clock=fixed_clock())
    for i in range(n):
        led.append("E", i=i)
    return led


def test_genesis_is_64_zeros():
    assert GENESIS == "0" * 64


def test_first_entry_chains_from_genesis():
    led = build(1)
    assert led.entries[0].prev_hash == GENESIS


def test_clean_chain_verifies():
    assert build(10).verify() == (True, None)


def test_seq_starts_at_one_and_increments():
    assert [e.seq for e in build(3).entries] == [1, 2, 3]


def test_chain_detects_single_field_tamper():
    led = build(10)
    led.entries[5].payload["i"] = 999
    ok, idx = led.verify()
    assert not ok and idx == 5


def test_chain_detects_deleted_entry():
    led = build(10)
    del led.entries[4]
    assert led.verify()[0] is False


def test_chain_detects_reordered_entries():
    led = build(10)
    led.entries[3], led.entries[7] = led.entries[7], led.entries[3]
    assert led.verify()[0] is False


def test_chain_detects_tampered_event_type():
    led = build(5)
    led.entries[2].event_type = "SOMETHING_ELSE"
    ok, idx = led.verify()
    assert not ok and idx == 2


def test_amounts_are_integer_paise_only():
    with pytest.raises(TypeError):
        Ledger(clock=fixed_clock()).append("E", amount=100.50)


def test_rejected_append_leaves_no_entry_behind():
    """A refused write must not advance the chain."""
    led = build(3)
    head_before = led.head
    with pytest.raises(TypeError):
        led.append("E", amount=1.5)
    assert len(led.entries) == 3
    assert led.head == head_before


def test_chain_hash_is_pure():
    event = {"seq": 1, "x": "y"}
    assert chain_hash(GENESIS, event) == chain_hash(GENESIS, event)


def test_chain_hash_changes_with_prev_hash():
    event = {"seq": 1, "x": "y"}
    assert chain_hash(GENESIS, event) != chain_hash("1" * 64, event)


def test_has_reports_recorded_event_types():
    led = build(2)
    led.append("REPAIR_DEDUPED_BY_DB", key="k")
    assert led.has("REPAIR_DEDUPED_BY_DB")
    assert not led.has("STUCK_REPAIR_DETECTED")


SUBPROCESS_SCRIPT = """
import sys
from datetime import UTC, datetime
from statesync.ledger.chain import Ledger

led = Ledger(clock=lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC))
for i in range(10):
    led.append("E", i=i, note="₹4,000", ok=True)
sys.stdout.write(led.head)
"""


def _head_in_subprocess() -> str:
    out = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SCRIPT],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_chain_stable_across_process_restart():
    """Non-canonical serialisation passes in-process and fails across one."""
    assert _head_in_subprocess() == _head_in_subprocess()
