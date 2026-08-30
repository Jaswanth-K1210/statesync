"""The hash-chained append-only audit ledger.

    GENESIS = "0" * 64
    hash    = sha256(prev_hash.encode("ascii") + canonical(event_body)).hexdigest()

Asserting "hash-chained ledger" is not a design, so the construction is
pinned here: SHA-256 over the previous hash concatenated with the canonical
JSON of the event body. Verification is a full chain walk at the start of
every run — at these volumes it costs milliseconds, and spot-checking would
be a false economy.

On a break the caller halts all repairs and exits non-zero. A reconciler that
keeps writing while its own audit trail is compromised is worse than no
reconciler at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from statesync.ledger.canonical import canonical

__all__ = [
    "GENESIS", "ChainIntegrityError", "Ledger", "LedgerEntry", "chain_hash",
    "verify_or_halt",
]


class ChainIntegrityError(RuntimeError):
    """The audit trail cannot be trusted, so nothing may be written.

    A reconciliation system that keeps writing while its own audit trail is
    compromised is worse than no system at all. This is raised, never caught
    and continued from: the run halts, the process exits non-zero, and zero
    repairs land. Fail closed, never open.
    """

GENESIS = "0" * 64


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class LedgerEntry:
    """One append-only record. Mutating any field breaks `Ledger.verify()`."""

    seq: int
    prev_hash: str
    hash: str
    event_type: str
    actor: str
    divergence_key: str | None
    repair_key: str | None
    created_at: datetime
    payload: dict[str, Any]

    def body(self) -> dict[str, Any]:
        """The exact structure that gets hashed. Order here is irrelevant —
        `canonical()` sorts keys — but the field set is load-bearing."""
        return {
            "seq": self.seq,
            "event_type": self.event_type,
            "actor": self.actor,
            "divergence_key": self.divergence_key,
            "repair_key": self.repair_key,
            "created_at": self.created_at,
            "payload": self.payload,
        }


def chain_hash(prev_hash: str, event_body: Any) -> str:
    return hashlib.sha256(prev_hash.encode("ascii") + canonical(event_body)).hexdigest()


@dataclass
class Ledger:
    """An in-memory chain. `statesync.ledger.store` persists one to Postgres."""

    actor: str = "statesync"
    clock: Callable[[], datetime] = _now
    entries: list[LedgerEntry] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.entries[-1].hash if self.entries else GENESIS

    def append(
        self,
        event_type: str,
        *,
        actor: str | None = None,
        divergence_key: str | None = None,
        repair_key: str | None = None,
        **payload: Any,
    ) -> LedgerEntry:
        """Append one event. Raises TypeError on a float anywhere in `payload`.

        The hash is computed *before* the entry joins the list, so a rejected
        write leaves the chain exactly as it was.
        """
        prev_hash = self.head
        entry = LedgerEntry(
            seq=len(self.entries) + 1,
            prev_hash=prev_hash,
            hash="",
            event_type=event_type,
            actor=actor or self.actor,
            divergence_key=divergence_key,
            repair_key=repair_key,
            created_at=self.clock(),
            payload=payload,
        )
        entry.hash = chain_hash(prev_hash, entry.body())  # may raise; nothing appended yet
        self.entries.append(entry)
        return entry

    def verify(self) -> tuple[bool, int | None]:
        """Walk the whole chain. Returns (ok, index of the first bad entry)."""
        prev = GENESIS
        for idx, entry in enumerate(self.entries):
            if entry.prev_hash != prev:
                return False, idx
            if entry.seq != idx + 1:
                return False, idx
            if chain_hash(prev, entry.body()) != entry.hash:
                return False, idx
            prev = entry.hash
        return True, None

    def has(self, event_type: str) -> bool:
        return any(e.event_type == event_type for e in self.entries)


def verify_or_halt(ledger: Ledger) -> None:
    """Walk the chain and refuse to continue if it does not verify.

    Called before the first repair of a batch, never after — verifying
    afterwards would mean the writes had already happened, which is precisely
    the outcome this exists to prevent.

    An empty chain is intact. Nothing written yet is not the same as something
    rewritten.
    """
    ok, index = ledger.verify()
    if ok:
        return
    raise ChainIntegrityError(
        f"audit chain broken at entry index {index}; halting all repairs. "
        f"No write will be attempted while the trail is compromised."
    )
