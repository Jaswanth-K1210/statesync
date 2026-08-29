"""Seeded synthetic transaction generator.

The track explicitly asks for synthetic data, which is what makes ground truth
perfect: **we know the correct answer because we caused the divergence.** This
module produces the *clean* baseline — three views that agree. Phase 2's
injector is what breaks them.

Every value derives from `random.Random(seed)`, and every timestamp derives
from `BASE_TIME`. Nothing reads the wall clock, so two runs at the same seed
are byte-identical.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from statesync.config import BASE_TIME, SEED
from statesync.ledger.canonical import canonical
from statesync.models.domain import LedgerEntryRecord, Order, Payment
from statesync.models.enums import PaymentStatus

__all__ = ["Batch", "generate_batch"]

# (instrument, weight, mdr basis points). GST is 18% of the MDR.
_INSTRUMENTS: list[tuple[str, int, int]] = [
    ("upi", 45, 0),
    ("card_domestic", 35, 200),
    ("card_intl", 8, 300),
    ("emi", 12, 250),
]
_GST_BPS = 1800
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _token(rng: random.Random, n: int = 14) -> str:
    return "".join(rng.choice(_ALPHABET) for _ in range(n))


def _fee_paise(amount_paise: int, mdr_bps: int) -> tuple[int, int]:
    """MDR and GST in integer paise. Floor division throughout — this is
    where the sub-rupee residuals that Phase 5 has to attribute come from."""
    mdr = amount_paise * mdr_bps // 10_000
    gst = mdr * _GST_BPS // 10_000
    return mdr, gst


@dataclass(frozen=True)
class Batch:
    """Three views of the same set of transactions, in agreement."""

    seed: int
    payments: list[Payment]
    orders: list[Order]
    ledger_entries: list[LedgerEntryRecord]

    def as_event(self) -> dict[str, Any]:
        """The whole batch as a canonically serialisable body."""
        return {
            "seed": self.seed,
            "payments": [p.model_dump(mode="python") for p in self.payments],
            "orders": [o.model_dump(mode="python") for o in self.orders],
            "ledger_entries": [e.model_dump(mode="python") for e in self.ledger_entries],
        }

    def digest(self) -> str:
        """SHA-256 of the canonical batch. Two runs at one seed must match."""
        return hashlib.sha256(canonical(self.as_event())).hexdigest()


def generate_batch(seed: int = SEED, n: int = 500) -> Batch:
    """Generate `n` clean transactions. Deterministic in `seed` alone."""
    rng = random.Random(seed)
    weights = [w for _, w, _ in _INSTRUMENTS]

    payments: list[Payment] = []
    orders: list[Order] = []
    entries: list[LedgerEntryRecord] = []

    for i in range(n):
        instrument, _, mdr_bps = rng.choices(_INSTRUMENTS, weights=weights, k=1)[0]
        amount = rng.randrange(10_000, 5_000_000, 100)  # ₹100 – ₹50,000, whole rupees
        created = BASE_TIME + timedelta(seconds=rng.randrange(0, 14 * 24 * 3600))
        captured_at = created + timedelta(seconds=rng.randrange(5, 900))

        # No AUTHORIZED payments in a clean batch. An order whose payment is
        # authorized-but-never-captured is not agreement between the three
        # views — it *is* ORDER_NO_CAPTURE. Emitting it here would mean the
        # baseline shipped with real divergences the injector never recorded,
        # and every false-positive number would be wrong. The injector creates
        # that state deliberately, and Phase 4 covers in-flight races.
        roll = rng.random()
        if roll < 0.86:
            status = PaymentStatus.CAPTURED
        elif roll < 0.94:
            status = PaymentStatus.REFUNDED
        else:
            status = PaymentStatus.FAILED

        # Fee data is present on most payments but not all. Where it is absent
        # Phase 5 must fall back to the schedule, or escalate — never guess.
        mdr, gst = _fee_paise(amount, mdr_bps)
        has_fee = rng.random() < 0.85 and status != PaymentStatus.FAILED

        payment_id = f"pay_{_token(rng)}"
        order_id = f"order_{_token(rng)}"
        customer_id = f"cust_{_token(rng, 10)}"
        terminal = status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED)

        payments.append(
            Payment(
                payment_id=payment_id,
                order_ref=order_id,
                amount_paise=amount,
                fee_paise=mdr if has_fee else None,
                tax_paise=gst if has_fee else None,
                instrument=instrument,
                status=status,
                status_changed_at=captured_at if terminal else created,
                captured_at=captured_at if terminal else None,
            )
        )

        order_status = {
            PaymentStatus.CAPTURED: "confirmed",
            PaymentStatus.REFUNDED: "confirmed",
            PaymentStatus.FAILED: "cancelled",
        }[status]
        orders.append(
            Order(
                order_id=order_id,
                payment_id=payment_id,
                customer_id=customer_id,
                total_paise=amount,
                status=order_status,
                created_at=created,
                line_items_count=rng.randint(1, 5),
            )
        )

        # The books carry the **gross** sale as revenue and the gateway's cut
        # as a separate expense line. Booking net would understate output GST
        # liability — a merchant owes GST on gross sale value and claims input
        # tax credit on the MDR's GST separately, so netting them is a filing
        # error rather than a simplification.
        #
        #     capture  +gross
        #     fee      -(mdr + gst)
        #     -----------------------
        #              = what settled
        #
        # It also makes the residual nameable: the gap between the fee the
        # merchant booked and the fee the gateway actually charged.
        fee_total = mdr + gst if has_fee else 0
        settled = amount - fee_total
        if status in (PaymentStatus.CAPTURED, PaymentStatus.REFUNDED):
            entries.append(
                LedgerEntryRecord(
                    entry_id=f"le_{_token(rng)}",
                    order_id=order_id,
                    payment_id=payment_id,
                    amount_paise=amount,
                    entry_type="capture",
                    created_at=captured_at,
                )
            )
            if has_fee and fee_total > 0:
                # No fee data means no fee line, and a zero fee (UPI at 0% MDR)
                # means no line either — a zero-amount entry is noise in the
                # books and would make "the fee line is missing" ambiguous.
                entries.append(
                    LedgerEntryRecord(
                        entry_id=f"le_{_token(rng)}",
                        order_id=order_id,
                        payment_id=payment_id,
                        amount_paise=-fee_total,
                        entry_type="fee",
                        created_at=captured_at,
                    )
                )
        if status == PaymentStatus.REFUNDED:
            entries.append(
                LedgerEntryRecord(
                    entry_id=f"le_{_token(rng)}",
                    order_id=order_id,
                    payment_id=payment_id,
                    amount_paise=-settled,
                    entry_type="refund",
                    created_at=captured_at + timedelta(seconds=rng.randrange(3600, 86400)),
                )
            )
        _ = i

    return Batch(seed=seed, payments=payments, orders=orders, ledger_entries=entries)
