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
    """Compare the books against what the gateway says actually moved.

    Exact integer comparison — there is no tolerance, because every amount in
    the system is integer paise and a one-paisa gap is a real gap.
    """
    expected = 0
    for payment in batch.payments:
        if payment.status == PaymentStatus.CAPTURED:
            expected += payment.amount_paise
        elif payment.status == PaymentStatus.REFUNDED:
            expected += 0  # captured then returned: net zero on the books

    actual = sum(entry.amount_paise for entry in batch.ledger_entries)
    return InvariantResult(ok=expected == actual, expected_paise=expected, actual_paise=actual)
