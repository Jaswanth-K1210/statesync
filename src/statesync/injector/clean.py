"""Deliberate divergence injection — the four clean classes.

Most buildathon entries fabricate a dataset and a panel discounts it. The way
around that is to break known-good data on purpose: **ground truth is perfect
because we caused the divergence.** Every injector records what it did, keyed
exactly the way detection keys its findings, so scoring is a dict comparison
rather than a labelling exercise.

    drop_webhook                -> CAPTURED_NO_ORDER
    duplicate_webhook           -> DUPLICATE_ORDER
    abandon_after_order_create  -> ORDER_NO_CAPTURE
    refund_without_ledger_write -> REFUND_NOT_REFLECTED

The 15 ambiguous cases live in Phase 4's `hard_cases.py`.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from statesync.config import BASE_TIME, SEED
from statesync.generator.synthetic import Batch
from statesync.models.domain import Divergence, LedgerEntryRecord, Order, Payment
from statesync.models.enums import DivergenceClass, PaymentStatus

__all__ = ["DivergenceInjector", "Injection", "InjectedBatch", "inject_clean"]


@dataclass(frozen=True)
class Injection:
    """One divergence we caused, and therefore know the correct answer to."""

    klass: DivergenceClass
    payment_id: str | None
    order_id: str | None
    amount_paise: int

    def key(self) -> str:
        """Keyed identically to a detected `Divergence`, so truth and
        detection can be compared directly."""
        return Divergence(
            klass=self.klass, payment_id=self.payment_id, order_id=self.order_id,
            amount_paise=max(self.amount_paise, 1), observed_at=BASE_TIME,
        ).deterministic_key()


@dataclass(frozen=True)
class InjectedBatch:
    batch: Batch
    injections: list[Injection]

    @property
    def truth(self) -> dict[str, DivergenceClass]:
        return {i.key(): i.klass for i in self.injections}


@dataclass
class DivergenceInjector:
    """Mutates a copy of a clean batch. The original is never touched."""

    source: Batch
    payments: list[Payment] = field(init=False)
    orders: list[Order] = field(init=False)
    entries: list[LedgerEntryRecord] = field(init=False)
    injections: list[Injection] = field(init=False)

    def __post_init__(self) -> None:
        self.payments = list(self.source.payments)
        self.orders = list(self.source.orders)
        self.entries = list(self.source.ledger_entries)
        self.injections = []

    # ── the four clean injectors ────────────────────────────────────────────

    def drop_webhook(self, payment_id: str) -> Injection:
        """The webhook never arrived: money moved, no order row exists."""
        payment = self._payment(payment_id)
        self.orders = [o for o in self.orders if o.payment_id != payment_id]
        self.entries = [e for e in self.entries if e.payment_id != payment_id]
        return self._record(DivergenceClass.CAPTURED_NO_ORDER, payment_id, None,
                            payment.amount_paise)

    def duplicate_webhook(self, payment_id: str) -> Injection:
        """At-least-once delivery: the same event handled twice."""
        original = next(o for o in self.orders if o.payment_id == payment_id)
        self.orders.append(
            original.model_copy(update={"order_id": f"{original.order_id}_dup"})
        )
        return self._record(DivergenceClass.DUPLICATE_ORDER, payment_id,
                            original.order_id, original.total_paise)

    def abandon_after_order_create(self, payment_id: str) -> Injection:
        """Order created client-side, payment never completed."""
        payment = self._payment(payment_id)
        self._replace_payment(
            payment.model_copy(update={
                "status": PaymentStatus.AUTHORIZED,
                "captured_at": None,
                "status_changed_at": payment.status_changed_at,
            })
        )
        self.entries = [e for e in self.entries if e.payment_id != payment_id]
        order = next((o for o in self.orders if o.payment_id == payment_id), None)
        return self._record(DivergenceClass.ORDER_NO_CAPTURE, payment_id,
                            order.order_id if order else None, payment.amount_paise)

    def refund_without_ledger_write(self, payment_id: str) -> Injection:
        """The gateway refunded; the books never heard. Real money out."""
        payment = self._payment(payment_id)
        self.entries = [
            e for e in self.entries
            if not (e.payment_id == payment_id and e.entry_type == "refund")
        ]
        return self._record(DivergenceClass.REFUND_NOT_REFLECTED, payment_id,
                            payment.order_ref, payment.amount_paise)

    # ── plumbing ────────────────────────────────────────────────────────────

    def _payment(self, payment_id: str) -> Payment:
        return next(p for p in self.payments if p.payment_id == payment_id)

    def _replace_payment(self, payment: Payment) -> None:
        self.payments = [
            payment if p.payment_id == payment.payment_id else p for p in self.payments
        ]

    def _record(
        self, klass: DivergenceClass, payment_id: str, order_id: str | None, amount: int
    ) -> Injection:
        injection = Injection(klass=klass, payment_id=payment_id, order_id=order_id,
                              amount_paise=amount)
        self.injections.append(injection)
        return injection

    def result(self) -> InjectedBatch:
        return InjectedBatch(
            batch=Batch(seed=self.source.seed, payments=self.payments,
                        orders=self.orders, ledger_entries=self.entries),
            injections=list(self.injections),
        )


def inject_clean(batch: Batch, seed: int = SEED, rate: float = 0.2) -> InjectedBatch:
    """Break `rate` of the batch, choosing candidates deterministically.

    `rate` is a proportion of records rather than a money amount, so it is the
    one float in the system — it never reaches an event body.
    """
    rng = random.Random(seed)
    injector = DivergenceInjector(batch)
    ordered = {o.payment_id for o in batch.orders}
    used: set[str] = set()

    captured = [p.payment_id for p in batch.payments
                if p.status == PaymentStatus.CAPTURED and p.payment_id in ordered]
    refunded = [p.payment_id for p in batch.payments
                if p.status == PaymentStatus.REFUNDED and p.payment_id in ordered]

    budget = int(len(batch.payments) * rate)
    if budget == 0:
        return injector.result()

    # A quarter of the budget to each class, refunds capped by availability.
    per_class = max(budget // 4, 1)
    plan: list[tuple[str, list[str]]] = [
        ("drop_webhook", captured),
        ("duplicate_webhook", captured),
        ("abandon_after_order_create", captured),
        ("refund_without_ledger_write", refunded),
    ]

    for method, pool in plan:
        available = [pid for pid in pool if pid not in used]
        rng.shuffle(available)
        for payment_id in available[:per_class]:
            getattr(injector, method)(payment_id)
            used.add(payment_id)

    return injector.result()
