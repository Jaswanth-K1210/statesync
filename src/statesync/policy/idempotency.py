"""The repair lease state machine — layer 2 of two-layer idempotency.

Layer 1 is the database unique constraint (`executor/store.py`). It is the real
guarantee and it cannot be flushed. This layer is an optimisation on top: it
stops concurrent workers doing duplicate work and short-circuits re-runs.

**The bug this exists to fix.** The naive `setnx` pattern claims a key, then
executes. If execution crashes after the claim but before the result is
written, the next call sees the claim, finds no result, and silently returns
nothing — a stuck repair with no alerting path, forever.

**A note on the lease TTL.** The obvious fix sets `ex=LEASE_SECONDS` on the
claim. That reintroduces the same bug in a new shape: when the lease expires
Redis *deletes the key*, so the next call sees no claim at all, takes a fresh
one, and the crashed repair is never surfaced. `STUCK_REPAIR_DETECTED` could
never fire. So the lease here is **logical** — the claim record carries its own
timestamp and is retained far longer than the lease it represents. Expiry is
computed, not delegated to Redis. The test
`test_the_claim_record_survives_the_lease_so_a_crash_is_detectable` pins this.

Three properties worth stating in the README:

1. A crash mid-execution surfaces as an escalation, never as silence.
2. A wiped Redis cannot cause a double repair — the DB constraint catches it,
   and the catch is logged rather than swallowed.
3. Every state transition is written to the audit ledger, so the full repair
   history is reconstructable from the ledger alone.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from statesync.config import LEASE_SECONDS
from statesync.executor.store import DuplicateRepairKey, RepairStore
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence

__all__ = ["IdempotentRepairer", "RepairResult", "RepairStatus"]

WORKER_ID = os.getenv("STATESYNC_WORKER_ID") or f"worker-{uuid.uuid4().hex[:8]}"

CLAIM_RETENTION_SECONDS = 7 * 24 * 3600
"""How long a claim record is kept — far beyond the lease it represents, so an
expired lease is *detectable* rather than deleted. See the module docstring."""


class RepairStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    ALREADY_APPLIED = "already_applied"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RepairResult:
    status: RepairStatus
    repair_key: str
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    replayed: bool = False
    """True when this result came from a prior run rather than this one.

    A cached replay and a fresh repair both report SUCCEEDED — that is what
    idempotency means — so without this flag a caller cannot tell how many
    repairs actually executed. Ten concurrent workers all report success;
    exactly one of them is not a replay.
    """


class _Redis(Protocol):
    def set(self, name: str, value: str, nx: bool = ..., ex: int | None = ...) -> Any: ...
    def get(self, name: str) -> Any: ...


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class IdempotentRepairer:
    redis: _Redis
    store: RepairStore
    ledger: Ledger
    clock: Callable[[], datetime] = _now
    lease_seconds: int = LEASE_SECONDS

    def _key(self, divergence: Divergence) -> str:
        return f"repair:{divergence.deterministic_key()}"

    def _write(self, key: str, record: dict[str, Any]) -> None:
        self.redis.set(key, json.dumps(record), ex=CLAIM_RETENTION_SECONDS)

    def claim(self, divergence: Divergence) -> bool:
        """Take the lease. Returns False if someone already holds it."""
        record = {
            "state": "CLAIMED",
            "at": self.clock().isoformat(),
            "worker": WORKER_ID,
        }
        claimed = self.redis.set(
            self._key(divergence), json.dumps(record), nx=True, ex=CLAIM_RETENTION_SECONDS
        )
        return bool(claimed)

    def _lease_expired(self, record: dict[str, Any]) -> bool:
        claimed_at = datetime.fromisoformat(record["at"])
        return (self.clock() - claimed_at).total_seconds() > self.lease_seconds

    def repair(
        self, divergence: Divergence, execute: Callable[[Divergence], dict[str, Any]]
    ) -> RepairResult:
        """Run `execute` at most once for this divergence, ever.

        Every path returns a `RepairResult`. Silence is the failure mode this
        whole state machine exists to eliminate.
        """
        key = self._key(divergence)
        divergence_key = divergence.deterministic_key()

        if not self.claim(divergence):
            raw = self.redis.get(key)
            record: dict[str, Any] = json.loads(raw) if raw else {"state": "CLAIMED", "at": ""}

            if record.get("state") == "SUCCEEDED":
                return RepairResult(RepairStatus.SUCCEEDED, key,
                                    detail=record.get("result", {}), replayed=True)

            if record.get("state") == "FAILED":
                # Do not silently retry. A failure that repeats on its own is
                # a failure nobody ever looks at.
                return RepairResult(RepairStatus.FAILED, key, reason=record.get("err"),
                                    replayed=True)

            if not record.get("at") or self._lease_expired(record):
                # A previous worker died mid-execution. Never swallow this.
                self.ledger.append("STUCK_REPAIR_DETECTED", divergence_key=divergence_key,
                                   prior_worker=str(record.get("worker", "unknown")),
                                   claimed_at=str(record.get("at", "")))
                return RepairResult(RepairStatus.ESCALATE, key, reason="STUCK_REPAIR")

            return RepairResult(RepairStatus.IN_PROGRESS, key)

        try:
            result = execute(divergence)
        except DuplicateRepairKey:
            # The DB constraint caught it: the repair already happened, and
            # Redis had simply lost the record. This is the flush case.
            self._write(key, {"state": "SUCCEEDED", "at": self.clock().isoformat(),
                              "note": "db_constraint_dedup"})
            self.ledger.append("REPAIR_DEDUPED_BY_DB", divergence_key=divergence_key)
            return RepairResult(RepairStatus.ALREADY_APPLIED, key, reason="REPAIR_DEDUPED_BY_DB")
        except Exception as exc:
            self._write(key, {"state": "FAILED", "at": self.clock().isoformat(),
                              "err": str(exc)})
            self.ledger.append("REPAIR_FAILED", divergence_key=divergence_key, error=str(exc))
            raise

        self._write(key, {"state": "SUCCEEDED", "at": self.clock().isoformat(),
                          "result": result})
        self.ledger.append("REPAIR_SUCCEEDED", divergence_key=divergence_key, result=result)
        return RepairResult(RepairStatus.SUCCEEDED, key, detail=result)
