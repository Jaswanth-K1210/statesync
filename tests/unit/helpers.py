"""Shared test fixtures. Test-only code lives here, never in production."""

from datetime import UTC, datetime

from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def divergence_for(klass: DivergenceClass, order_id: str = "order_1",
                   payment_id: str = "pay_1", amount_paise: int = 400000) -> Divergence:
    return Divergence(klass=klass, payment_id=payment_id, order_id=order_id,
                      amount_paise=amount_paise, observed_at=NOW)
