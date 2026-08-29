"""SETTLEMENT_GAP — in the taxonomy, not implemented, and said so plainly.

The class describes a real failure mode: a payout that does not equal
Σ(captures − refunds − fees). It is where the propose-verify architecture
generalises, and removing it from the taxonomy would hide a gap the design
actually covers.

It is **not detected**. The sandbox produces no genuine settlement behaviour —
no real T+2 cycle, no rolling reserve, no payout webhook carrying real fee
deductions — so any payout data here would be manufactured, and an accuracy
figure computed against data manufactured for the purpose is not a
measurement. The design's own gate says: keep the class, report
NOT_IMPLEMENTED with the reason, rather than publishing a misleading number.

That is what this module does. It reports a status and a reason, never a
percentage, and it is never merged into a headline figure.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = ["SETTLEMENT_GAP_STATUS", "SettlementStatus", "settlement_gap_report"]


class SettlementStatus(StrEnum):
    NOT_IMPLEMENTED = "not_implemented"
    DEMONSTRATED_SYNTHETIC = "demonstrated_synthetic"
    VALIDATED = "validated"


SETTLEMENT_GAP_STATUS = SettlementStatus.NOT_IMPLEMENTED


def settlement_gap_report() -> dict[str, Any]:
    """A status and a reason. Deliberately no figures."""
    return {
        "klass": "settlement_gap",
        "status": SETTLEMENT_GAP_STATUS.value,
        "accuracy": "not reported",
        "merged_into_headline": False,
        "reason": (
            "The gateway sandbox produces no genuine settlement behaviour — no real "
            "T+2 cycle, no rolling reserve, no payout webhook carrying real fee "
            "deductions. Any payout data would be manufactured here, and an accuracy "
            "figure computed against manufactured data is not a measurement."
        ),
        "needed": (
            "Real payout records with per-transaction fee breakdowns, or a sandbox "
            "that emits settlement webhooks. The propose-verify path extends to this "
            "class unchanged once such data exists."
        ),
    }
