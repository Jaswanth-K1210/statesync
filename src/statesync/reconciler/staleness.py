"""Two defences against repairing a payment that was merely in flight.

The reconciler treats the gateway as authoritative, but during a state
transition the gateway does not yet *have* a consistent answer — a payment
reported captured by one call can read authorized on another while state
propagates. Reconciling into that window manufactures false divergences, and
a false divergence that triggers a repair makes state worse than doing nothing.

1. **Staleness window.** No payment is reconciled until it has been terminal
   for `STALENESS_WINDOW`.
2. **Two-run confirmation.** A divergence must be seen on two consecutive
   passes, separated by at least that window, before a repair is authorised.

The cost is one cycle of detection latency. StateSync is deliberately not a
real-time system, and `transient_filtered` is reported as a metric because it
is the direct evidence this is doing work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from statesync.config import STALENESS_WINDOW
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence, Payment

__all__ = ["ConfirmationTracker", "eligible_for_reconciliation"]


def eligible_for_reconciliation(payment: Payment, now: datetime) -> bool:
    """True once `payment` has been terminal for longer than the window."""
    if not payment.status.is_terminal:
        return False
    return (now - payment.status_changed_at) > STALENESS_WINDOW


@dataclass
class _Observation:
    first_seen: datetime
    confirmed: bool = False


@dataclass
class ConfirmationTracker:
    """Promotes a divergence from observed to confirmed across two passes."""

    ledger: Ledger | None = None
    _seen: dict[str, _Observation] = field(default_factory=dict)
    transient_filtered: int = 0
    """Divergences observed once that had vanished by the next pass."""

    def observe(self, divergences: list[Divergence], now: datetime) -> list[Divergence]:
        """Record this pass. Returns only the newly *confirmed* divergences."""
        present: dict[str, Divergence] = {d.deterministic_key(): d for d in divergences}
        confirmed: list[Divergence] = []

        for key, divergence in present.items():
            observation = self._seen.get(key)
            if observation is None:
                self._seen[key] = _Observation(first_seen=now)
                self._append("DIVERGENCE_OBSERVED", divergence, key)
                continue
            if observation.confirmed:
                continue  # already authorised; do not re-emit
            if now - observation.first_seen < STALENESS_WINDOW:
                continue  # passes too close together to count as consecutive
            observation.confirmed = True
            self._append("DIVERGENCE_CONFIRMED", divergence, key)
            confirmed.append(divergence)

        # Anything seen once, never confirmed, and now gone was transient.
        for key in list(self._seen):
            if key not in present and not self._seen[key].confirmed:
                self.transient_filtered += 1
                del self._seen[key]

        return confirmed

    def _append(self, event_type: str, divergence: Divergence, key: str) -> None:
        if self.ledger is None:
            return
        self.ledger.append(
            event_type,
            divergence_key=key,
            klass=divergence.klass,
            payment_id=divergence.payment_id,
            order_id=divergence.order_id,
            amount_paise=divergence.amount_paise,
        )
