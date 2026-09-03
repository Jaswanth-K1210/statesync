"""The settlement view: a payout, and the captures it covers.

Separate from `models/domain.py` because this is a different shape. A payment
is compared against one order; a payout is compared against a *set* of
captures, refunds and fees that the gateway has already netted together.

All amounts are integer paise, as everywhere else.
"""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict

from statesync.models.domain import Paise

__all__ = ["Capture", "Payout"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Capture(_Frozen):
    """One settled capture. `cycle` is the day it was captured, not paid out."""

    capture_id: str
    payment_id: str
    amount_paise: Paise
    refund_paise: Paise
    fee_paise: Paise
    instrument: str
    cycle: int


class Payout(_Frozen):
    """What the gateway says it paid, and what it says that is made of.

    `capture_ids` is authoritative about which captures this payout covers.
    Deriving that set from dates instead invents shortfalls at every cycle
    boundary — a capture made late settles one cycle later, and that is timing
    rather than a gap.
    """

    payout_id: str
    settled_at: AwareDatetime
    cycle: int
    gross_captures_paise: Paise
    total_refunds_paise: Paise
    total_fees_paise: Paise
    reserve_paise: Paise
    net_paise: Paise
    capture_ids: tuple[str, ...]

    @property
    def declared_net_paise(self) -> int:
        """What the payout's own components say the net should be."""
        return (
            self.gross_captures_paise
            - self.total_refunds_paise
            - self.total_fees_paise
            - self.reserve_paise
        )
