"""`python -m statesync.generator` — the byte-identical-output exit criterion.

Phase 1 exits on "running the generator twice with the same seed produces
byte-identical output". That claim is made at the shell, so it is tested at
the shell.
"""

import subprocess
import sys


def run(*args: str) -> bytes:
    out = subprocess.run(
        [sys.executable, "-m", "statesync.generator", *args],
        capture_output=True, check=True,
    )
    return out.stdout


def test_same_seed_twice_is_byte_identical():
    assert run("--seed", "20260905", "--n", "50") == run("--seed", "20260905", "--n", "50")


def test_different_seed_differs():
    assert run("--seed", "1", "--n", "50") != run("--seed", "2", "--n", "50")


def test_output_is_canonical_json_with_sorted_keys():
    out = run("--seed", "20260905", "--n", "2")
    assert out.startswith(b'{"ledger_entries"')  # sorted before payments/orders/seed


def test_digest_flag_prints_only_the_digest():
    out = run("--seed", "20260905", "--n", "10", "--digest").strip()
    assert len(out) == 64
