"""The three views of one transaction, plus the disagreement between them.

**All amounts are integer paise.** `Paise` is a strict int, so pydantic will
not quietly coerce 4000.0 into 4000 — a float that reaches an event body makes
the hash chain non-reproducible across environments, and the cheapest place to
stop it is the type.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from statesync.models.enums import DivergenceClass, PaymentStatus

__all__ = ["Divergence", "LedgerEntryRecord", "Order", "Paise", "Payment"]

Paise = Annotated[StrictInt, Field(description="integer paise, never float")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Payment(_Frozen):
    """The gateway's view — authoritative on whether money moved."""

    payment_id: str
    order_ref: str | None
    amount_paise: Paise
    fee_paise: Paise | None
    tax_paise: Paise | None
    instrument: str
    status: PaymentStatus
    status_changed_at: AwareDatetime
    captured_at: AwareDatetime | None


class Order(_Frozen):
    """The merchant's view — authoritative on whether goods were promised."""

    order_id: str
    payment_id: str | None
    customer_id: str
    total_paise: Paise
    status: str
    created_at: AwareDatetime
    line_items_count: int = 0
    """0 signals a partial handler write: the row exists but the write died
    before the line items landed. That is hard case 1, not a clean order."""


class LedgerEntryRecord(_Frozen):
    """The merchant's books. Negative amounts are refunds."""

    entry_id: str
    order_id: str | None
    payment_id: str | None
    amount_paise: Paise
    entry_type: str
    created_at: AwareDatetime


class Divergence(BaseModel):
    """One disagreement between the three views."""

    model_config = ConfigDict(extra="forbid")

    klass: DivergenceClass
    payment_id: str | None
    order_id: str | None
    amount_paise: Paise
    observed_at: datetime
    detail: dict[str, str] = Field(default_factory=dict)

    def deterministic_key(self) -> str:
        """The idempotency key for this divergence.

        Deliberately excludes `observed_at` and `detail`: the same divergence
        seen on pass one and pass two must claim the *same* repair, or the
        two-run confirmation rule would double-write by construction.
        """
        material = f"{self.klass.value}|{self.payment_id or ''}|{self.order_id or ''}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
