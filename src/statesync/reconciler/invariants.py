"""The invariant that anchors everything.

    expected = Σ(money the gateway says moved)
    actual   = Σ(the merchant's ledger entries)

If this doesn't hold, something is wrong regardless of what any individual
record says — it catches divergences the three-way comparison misses, because
it is a statement about the whole batch rather than about any one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from statesync.generator.synthetic import Batch
from statesync.models.enums import PaymentStatus

__all__ = ["InvariantResult", "LedgerInvariantViolation", "check_ledger_invariant"]


class LedgerInvariantViolation(RuntimeError):
    """Raised in strict mode. Never swallowed — see constraint 8."""


@dataclass(frozen=True)
class InvariantResult:
    ok: bool
    expected_paise: int
    actual_paise: int
    revenue_paise: int = 0
    fee_expense_paise: int = 0
    settled_paise: int = 0

    @property
    def delta_paise(self) -> int:
        """Signed: negative means the books are short of the gateway."""
        return self.actual_paise - self.expected_paise

    def raise_if_violated(self) -> None:
        if not self.ok:
            raise LedgerInvariantViolation(
                f"ledger invariant violated: books are {self.delta_paise} paise "
                f"from the gateway (expected {self.expected_paise}, "
                f"actual {self.actual_paise})"
            )


def check_ledger_invariant(batch: Batch) -> InvariantResult:
    """Check `revenue + fee_expense == settled`.

    Three terms rather than two, because the books carry gross revenue and the
    gateway's cut as separate lines. A wrong fee line moves `actual` without
    moving `revenue`, which is exactly the error gross booking exists to make
    visible.

    Exact integer comparison — there is no tolerance, because every amount in
    the system is integer paise and a one-paisa gap is a real gap.
    """
    expected = 0
    for payment in batch.payments:
        if payment.status == PaymentStatus.CAPTURED:
            # What the gateway says should have landed after its own cut.
            expected += (
                payment.amount_paise - (payment.fee_paise or 0) - (payment.tax_paise or 0)
            )
        elif payment.status == PaymentStatus.REFUNDED:
            expected += 0  # captured then returned: net zero on the books

    revenue = sum(e.amount_paise for e in batch.ledger_entries if e.entry_type != "fee")
    fee_expense = sum(e.amount_paise for e in batch.ledger_entries if e.entry_type == "fee")
    actual = revenue + fee_expense

    return InvariantResult(
        ok=expected == actual, expected_paise=expected, actual_paise=actual,
        revenue_paise=revenue, fee_expense_paise=fee_expense, settled_paise=expected,
    )
