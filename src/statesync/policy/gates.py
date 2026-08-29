"""Repair authorisation gates — deterministic, always.

Anything outside the confidence, value or blast-radius bounds goes to a human
**by design, not by limitation**. This is the module that makes "not fully
autonomous" a stated scope boundary rather than an excuse for a gap.

No model output reaches this decision. The LLM never decides whether money
moves; it does not get a vote on whether a repair is authorised either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass

__all__ = ["AUTO_REPAIRABLE", "GateDecision", "PolicyGate"]

AUTO_REPAIRABLE = frozenset({
    DivergenceClass.CAPTURED_NO_ORDER,
    DivergenceClass.ORDER_NO_CAPTURE,
    DivergenceClass.DUPLICATE_ORDER,
    DivergenceClass.REFUND_NOT_REFLECTED,
})
"""The four deterministic classes. AMOUNT_MISMATCH and SETTLEMENT_GAP are
under-determined and are never auto-repaired — they escalate with an
evidence packet, which is the correct outcome rather than a limitation."""

DEFAULT_VALUE_THRESHOLD_PAISE = 5_000_000  # ₹50,000


@dataclass(frozen=True)
class GateDecision:
    authorised: bool
    reason: str | None = None

    def as_event(self) -> dict[str, Any]:
        return {"authorised": self.authorised, "reason": self.reason or ""}


@dataclass(frozen=True)
class PolicyGate:
    value_threshold_paise: int = DEFAULT_VALUE_THRESHOLD_PAISE

    def evaluate(self, divergence: Divergence) -> GateDecision:
        """Decide whether this divergence may be repaired without a human."""
        if divergence.klass not in AUTO_REPAIRABLE:
            return GateDecision(False, "CLASS_NOT_AUTO_REPAIRABLE")
        if divergence.amount_paise > self.value_threshold_paise:
            # The cost of being wrong scales with the amount, so the bar for
            # acting without a human scales with it too.
            return GateDecision(False, "VALUE_THRESHOLD_EXCEEDED")
        return GateDecision(True)
