"""Closed vocabularies. Anything outside these is a bug, not a new case."""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DivergenceClass", "PaymentStatus", "ReasonCode"]


class PaymentStatus(StrEnum):
    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Only terminal payments are eligible for reconciliation at all.

        A payment mid-transition does not have a consistent answer yet, and
        reconciling into that window manufactures false divergences.
        """
        return self in _TERMINAL


_TERMINAL = frozenset({PaymentStatus.CAPTURED, PaymentStatus.REFUNDED, PaymentStatus.FAILED})


class DivergenceClass(StrEnum):
    CAPTURED_NO_ORDER = "captured_no_order"
    ORDER_NO_CAPTURE = "order_no_capture"
    DUPLICATE_ORDER = "duplicate_order"
    REFUND_NOT_REFLECTED = "refund_not_reflected"
    AMOUNT_MISMATCH = "amount_mismatch"
    SETTLEMENT_GAP = "settlement_gap"
    NO_DIVERGENCE = "no_divergence"


class ReasonCode(StrEnum):
    VERIFIED = "verified"
    AMBIGUOUS_MULTIPLE_VERIFIED = "ambiguous_multiple_verified"
    NO_HYPOTHESIS_VERIFIED = "no_hypothesis_verified"
    FEE_SCHEDULE_UNKNOWN = "fee_schedule_unknown"
    STUCK_REPAIR = "stuck_repair"
