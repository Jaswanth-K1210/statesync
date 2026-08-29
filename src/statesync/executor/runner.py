"""The repair pipeline: gate, cap, lease, execute, record.

Order matters and is deliberate:

1. **Policy gate** — may this class be repaired at all, at this value?
2. **Blast-radius cap** — has this run already changed enough?
3. **Lease + DB constraint** — has this exact repair already happened?
4. **Execute** — the class-specific repair.

Gates run *before* the lease is claimed. A repair the policy refuses must not
leave a claim behind, or a later run with a raised threshold would find a
phantom in-progress repair and escalate a stuck repair that never existed.

Every outcome — success, escalation, block, failure — writes to the audit
ledger. There is no path through this module that produces silence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from statesync.executor.repairs import repair_for
from statesync.executor.store import RepairStore
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence
from statesync.policy.blast_radius import BlastRadiusCap
from statesync.policy.gates import PolicyGate
from statesync.policy.idempotency import IdempotentRepairer, RepairResult, RepairStatus

__all__ = ["RepairRunner"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class RepairRunner:
    redis: Any
    store: RepairStore
    ledger: Ledger
    gate: PolicyGate = field(default_factory=PolicyGate)
    cap: BlastRadiusCap = field(default_factory=lambda: BlastRadiusCap(max_repairs=50))
    clock: Callable[[], datetime] = _now

    def __post_init__(self) -> None:
        self._repairer = IdempotentRepairer(
            redis=self.redis, store=self.store, ledger=self.ledger, clock=self.clock
        )
        self._counts: dict[str, int] = {
            "succeeded": 0, "replayed": 0, "escalated": 0,
            "blocked": 0, "failed": 0, "already_applied": 0,
        }

    def run(self, divergence: Divergence) -> RepairResult:
        key = divergence.deterministic_key()

        decision = self.gate.evaluate(divergence)
        if not decision.authorised:
            self.ledger.append("REPAIR_ESCALATED", divergence_key=key,
                               reason=decision.reason or "", klass=divergence.klass)
            self._counts["escalated"] += 1
            return RepairResult(RepairStatus.ESCALATE, key, reason=decision.reason)

        if not self.cap.allow():
            self.ledger.append("REPAIR_BLOCKED", divergence_key=key,
                               reason="BLAST_RADIUS_EXCEEDED", used=self.cap.used)
            self._counts["blocked"] += 1
            return RepairResult(RepairStatus.ESCALATE, key, reason="BLAST_RADIUS_EXCEEDED")

        execute = repair_for(divergence.klass)
        result = self._repairer.repair(divergence, lambda d: execute(d, self.store))

        if result.status == RepairStatus.SUCCEEDED and not result.replayed:
            self._counts["succeeded"] += 1
        elif result.replayed:
            self._counts["replayed"] += 1
        elif result.status == RepairStatus.ALREADY_APPLIED:
            self._counts["already_applied"] += 1
        elif result.status == RepairStatus.FAILED:
            self._counts["failed"] += 1
        elif result.status == RepairStatus.ESCALATE:
            self._counts["escalated"] += 1
        return result

    def summary(self) -> dict[str, int]:
        """Integers only — safe to write to the ledger and to a report."""
        return dict(self._counts) | {
            "writes": self.store.write_count,
            "blast_radius_used": self.cap.used,
            "blast_radius_blocked": self.cap.blocked,
        }
