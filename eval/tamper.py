"""Break the audit chain on purpose, for the demo.

    python -m eval.tamper

Beat 9 of the video: tamper with the ledger, run the batch, and watch the
system halt with a non-zero exit and zero writes. A reconciler that keeps
writing while its audit trail is compromised is worse than no reconciler, so
this is the beat that proves the guarantee rather than asserting it.
"""

from __future__ import annotations

import sys

from statesync.ledger.chain import Ledger

__all__ = ["main", "tamper_in_memory"]


def tamper_in_memory(ledger: Ledger, index: int = 0) -> Ledger:
    """Alter one entry's payload, leaving its stored hash untouched."""
    entry = ledger.entries[index]
    entry.payload = {**entry.payload, "tampered": True}
    return ledger


def main(argv: list[str] | None = None) -> int:
    from datetime import UTC, datetime

    from statesync.ledger.chain import ChainIntegrityError, verify_or_halt

    ledger = Ledger(clock=lambda: datetime(2026, 9, 5, tzinfo=UTC))
    for i in range(10):
        ledger.append("DEMO", i=i)

    print(f"chain of {len(ledger.entries)} entries verifies: {ledger.verify()[0]}")  # noqa: T201
    tamper_in_memory(ledger, index=4)
    print("tampered with entry index 4")  # noqa: T201

    try:
        verify_or_halt(ledger)
    except ChainIntegrityError as exc:
        print(f"HALTED: {exc}", file=sys.stderr)  # noqa: T201
        return 1

    print("chain still verified — the tamper harness is broken")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
