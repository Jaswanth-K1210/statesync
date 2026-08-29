"""The 15 hard cases.

A test set of clean single-class divergences produces excellent metrics and
tells you nothing. These are compound, ambiguous, and several of them have
"refuse to act" as the correct answer.

Cases 7, 13 and 14 are the most valuable in the set. They test whether the
system knows when *not* to act, which is harder and more important than acting
correctly.

**Timestamps are relative to `now`, never to a fixed past anchor.** Case 12
depends on being inside the staleness window, and a hard-coded date would put
every case permanently outside it — `transient_filtered` would ship as a
constant zero and the two-run confirmation would look like dead code.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from statesync.classifier.escalation import EscalationPacket, build_packet
from statesync.classifier.provider import FixtureHypothesisProvider, HypothesisRequest
from statesync.classifier.verifier import ArtifactIndex, Component, Proposal
from statesync.config import SEED
from statesync.generator.synthetic import Batch
from statesync.models.domain import Divergence, LedgerEntryRecord, Order, Payment
from statesync.models.enums import DivergenceClass, PaymentStatus

__all__ = ["HARD_CASES", "ExpectedOutcome", "HardCase", "HardCaseBatch", "inject_hard_cases"]


class ExpectedOutcome(StrEnum):
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    NOT_A_DIVERGENCE = "not_a_divergence"
    NOT_MERGED = "not_merged"
    AMBIGUOUS = "ambiguous"
    NO_HYPOTHESIS = "no_hypothesis"


@dataclass(frozen=True)
class HardCase:
    case_id: str
    title: str
    expected: ExpectedOutcome
    payment_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    detail: dict[str, str] = field(default_factory=dict)


HARD_CASES: tuple[tuple[str, str, ExpectedOutcome], ...] = (
    ("case_01", "Partial handler write: order row exists, line items missing",
     ExpectedOutcome.ESCALATED),
    ("case_02", "Refund and a second payment attempt racing on one order",
     ExpectedOutcome.ESCALATED),
    ("case_03", "Capture during a network partition; merchant timed out and retried",
     ExpectedOutcome.RESOLVED),
    ("case_04", "Duplicate webhook, two deliveries with different payloads",
     ExpectedOutcome.RESOLVED),
    ("case_05", "Delta exactly equals MDR + GST", ExpectedOutcome.NOT_A_DIVERGENCE),
    ("case_06", "Delta equals MDR + GST except 60 paise", ExpectedOutcome.ESCALATED),
    ("case_07", "Two legitimate orders, same customer, same amount, 2s apart",
     ExpectedOutcome.NOT_MERGED),
    ("case_08", "Refund issued then reversed by the bank", ExpectedOutcome.RESOLVED),
    ("case_09", "Settlement including a prior-cycle chargeback debit",
     ExpectedOutcome.ESCALATED),
    ("case_10", "refund.processed arrives before payment.captured",
     ExpectedOutcome.RESOLVED),
    ("case_11", "Webhook replay 26 hours later, after manual re-enable",
     ExpectedOutcome.RESOLVED),
    ("case_12", "Order cancelled by the merchant while the payment is in flight",
     ExpectedOutcome.ESCALATED),
    ("case_13", "Two hypotheses both reconcile exactly", ExpectedOutcome.AMBIGUOUS),
    ("case_14", "No hypothesis reconciles", ExpectedOutcome.NO_HYPOTHESIS),
    ("case_15", "Split payment across two instruments, rounding on each",
     ExpectedOutcome.ESCALATED),
)


@dataclass(frozen=True)
class HardCaseBatch:
    batch: Batch
    cases: list[HardCase]
    packets: dict[str, EscalationPacket]
    injected_payment_ids: frozenset[str] = frozenset()
    """Every payment this injector created, numbered case or not.

    Derived from what was built rather than from the case registry: auxiliary
    payments (the late arrival, the unpriceable instrument) belong to no
    numbered case, and keying off the registry silently left them out of the
    exclusion set — where they were then counted as false positives."""

    provider: FixtureHypothesisProvider | None = None
    """Proposals for the ambiguous cases, keyed by payment id.

    The eval routes every AMOUNT_MISMATCH through this, so an exception row's
    reason code comes from the packet that actually ran rather than from a
    classifier default. Phase 5 substitutes an LLM provider here."""

    artifacts: ArtifactIndex | None = None
    late_arrivals: tuple[Order, ...] = ()
    """Records that land *between* reconciliation passes.

    Without these the batch is static, so a divergence seen on pass 1 is still
    there on pass 2 and the staleness window can never be observed doing its
    job — `transient_filtered` would be a permanent zero however correct the
    implementation was. A webhook that arrives late is the commonest transient
    there is, and it is the honest way to measure the filter."""

    @property
    def payment_ids(self) -> set[str]:
        """Every payment belonging to a hard case.

        Hard-case results are reported separately from clean ones: a miss on a
        clean case is a detection failure, while for cases 7, 13 and 14 the
        correct outcome *is* refusal, so they are scored differently.
        """
        return set(self.injected_payment_ids) | {
            pid for case in self.cases for pid in case.payment_ids
        }

    def case(self, case_id: str) -> HardCase:
        return next(c for c in self.cases if c.case_id == case_id)

    def packet_for(self, case_id: str) -> EscalationPacket:
        return self.packets[case_id]


class _Builder:
    """Accumulates mutations against copies of the three views."""

    def __init__(self, source: Batch, now: datetime, rng: random.Random) -> None:
        self.payments = list(source.payments)
        self.orders = list(source.orders)
        self.entries = list(source.ledger_entries)
        self.seed = source.seed
        self.now = now
        self.rng = rng
        self.cases: list[HardCase] = []
        self.created: set[str] = set()
        self.artifacts = ArtifactIndex(
            {p.payment_id for p in source.payments} | {"stl_hard", "rfnd_hard"}
        )

    # ── small helpers ───────────────────────────────────────────────────────

    def payment(self, pid: str, *, amount: int, status: PaymentStatus,
                age: timedelta, fee: int | None = None, tax: int | None = None,
                instrument: str = "card_domestic", order_ref: str | None = None) -> Payment:
        stamp = self.now - age
        payment = Payment(
            payment_id=pid, order_ref=order_ref, amount_paise=amount,
            fee_paise=fee, tax_paise=tax, instrument=instrument, status=status,
            status_changed_at=stamp,
            captured_at=stamp if status in (PaymentStatus.CAPTURED,
                                            PaymentStatus.REFUNDED) else None,
        )
        self.payments.append(payment)
        self.artifacts.add(pid)
        self.created.add(pid)
        return payment

    def order(self, oid: str, pid: str | None, *, total: int, age: timedelta,
              customer: str = "cust_hard", status: str = "confirmed",
              line_items: int = 2) -> Order:
        order = Order(order_id=oid, payment_id=pid, customer_id=customer,
                      total_paise=total, status=status, created_at=self.now - age,
                      line_items_count=line_items)
        self.orders.append(order)
        return order

    def entry(self, eid: str, pid: str, oid: str | None, *, amount: int,
              kind: str, age: timedelta) -> LedgerEntryRecord:
        entry = LedgerEntryRecord(entry_id=eid, order_id=oid, payment_id=pid,
                                  amount_paise=amount, entry_type=kind,
                                  created_at=self.now - age)
        self.entries.append(entry)
        return entry

    def record(self, case_id: str, *, payments: tuple[str, ...] = (),
               orders: tuple[str, ...] = (), detail: dict[str, str] | None = None) -> None:
        _, title, expected = next(c for c in HARD_CASES if c[0] == case_id)
        self.cases.append(HardCase(case_id=case_id, title=title, expected=expected,
                                   payment_ids=payments, order_ids=orders,
                                   detail=detail or {}))


OLD = timedelta(days=2)
"""Well past the staleness window and the order timeout."""


def inject_hard_cases(batch: Batch, seed: int = SEED, now: datetime | None = None
                      ) -> HardCaseBatch:
    """Add all 15 hard cases to `batch`, keeping the clean records intact."""
    now = now or datetime.now(tz=batch.payments[0].status_changed_at.tzinfo)
    rng = random.Random(seed)
    b = _Builder(batch, now, rng)

    # 1 — partial handler write: the order row landed, the line items did not.
    b.payment("pay_hc01", amount=400_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc01", "pay_hc01", total=400_000, age=OLD, line_items=0)
    b.entry("le_hc01", "pay_hc01", "order_hc01", amount=400_000, kind="capture", age=OLD)
    b.record("case_01", payments=("pay_hc01",), orders=("order_hc01",))

    # 2 — a refund and a second payment attempt racing on one order.
    b.payment("pay_hc02a", amount=250_000, status=PaymentStatus.REFUNDED, age=OLD)
    b.payment("pay_hc02b", amount=250_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc02", "pay_hc02a", total=250_000, age=OLD)
    b.entry("le_hc02", "pay_hc02a", "order_hc02", amount=250_000, kind="capture", age=OLD)
    b.record("case_02", payments=("pay_hc02a", "pay_hc02b"), orders=("order_hc02",))

    # 3 — capture during a partition: the merchant retried, the gateway had it.
    b.payment("pay_hc03", amount=180_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc03", "pay_hc03", total=180_000, age=OLD)
    b.entry("le_hc03", "pay_hc03", "order_hc03", amount=180_000, kind="capture", age=OLD)
    b.record("case_03", payments=("pay_hc03",), orders=("order_hc03",),
             detail={"retried": "true"})

    # 4 — the same webhook delivered twice with different payloads.
    b.payment("pay_hc04", amount=320_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc04", "pay_hc04", total=320_000, age=OLD)
    b.entry("le_hc04", "pay_hc04", "order_hc04", amount=320_000, kind="capture", age=OLD)
    b.record("case_04", payments=("pay_hc04",), orders=("order_hc04",),
             detail={"authoritative": "later", "deliveries": "2"})

    # 5 — the delta is exactly MDR + GST, so it is a fee, not a divergence.
    b.payment("pay_hc05", amount=400_000, status=PaymentStatus.CAPTURED, age=OLD,
              fee=8_000, tax=1_440)
    b.order("order_hc05", "pay_hc05", total=400_000, age=OLD)
    b.entry("le_hc05", "pay_hc05", "order_hc05", amount=400_000, kind="capture", age=OLD)
    b.entry("le_hc05f", "pay_hc05", "order_hc05", amount=-9_440, kind="fee", age=OLD)
    b.record("case_05", payments=("pay_hc05",), orders=("order_hc05",))

    # 6 — the same, 60 paise short. That residual is the whole exercise.
    b.payment("pay_hc06", amount=400_000, status=PaymentStatus.CAPTURED, age=OLD,
              fee=8_000, tax=1_440)
    b.order("order_hc06", "pay_hc06", total=400_000, age=OLD)
    b.entry("le_hc06", "pay_hc06", "order_hc06", amount=400_000, kind="capture", age=OLD)
    # The merchant booked 60 paise more fee than the gateway charged.
    b.entry("le_hc06f", "pay_hc06", "order_hc06", amount=-9_500, kind="fee", age=OLD)
    b.record("case_06", payments=("pay_hc06",), orders=("order_hc06",),
             detail={"residual_paise": "60"})

    # 7 — MANDATORY. Two legitimate orders: one customer, one amount, 2s apart,
    #     two *different* payments. A dedupe on customer+amount+time destroys a
    #     real order. Merging happens on exact payment_id, never on similarity.
    b.payment("pay_hc07a", amount=150_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.payment("pay_hc07b", amount=150_000, status=PaymentStatus.CAPTURED,
              age=OLD - timedelta(seconds=2))
    b.order("order_hc07a", "pay_hc07a", total=150_000, age=OLD, customer="cust_twin")
    b.order("order_hc07b", "pay_hc07b", total=150_000, age=OLD - timedelta(seconds=2),
            customer="cust_twin")
    b.entry("le_hc07a", "pay_hc07a", "order_hc07a", amount=150_000, kind="capture", age=OLD)
    b.entry("le_hc07b", "pay_hc07b", "order_hc07b", amount=150_000, kind="capture", age=OLD)
    b.record("case_07", payments=("pay_hc07a", "pay_hc07b"),
             orders=("order_hc07a", "order_hc07b"))

    # 8 — refund issued, then reversed by the bank. Net position must be right.
    b.payment("pay_hc08", amount=200_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc08", "pay_hc08", total=200_000, age=OLD)
    b.entry("le_hc08a", "pay_hc08", "order_hc08", amount=200_000, kind="capture", age=OLD)
    b.entry("le_hc08b", "pay_hc08", "order_hc08", amount=-200_000, kind="refund", age=OLD)
    b.entry("le_hc08c", "pay_hc08", "order_hc08", amount=200_000, kind="refund_reversal",
            age=OLD)
    b.record("case_08", payments=("pay_hc08",), orders=("order_hc08",),
             detail={"expected_net_paise": "200000"})

    # 9 — a settlement carrying a chargeback debit from a prior cycle.
    b.payment("pay_hc09", amount=500_000, status=PaymentStatus.CAPTURED, age=OLD,
              fee=10_000, tax=1_800)
    b.order("order_hc09", "pay_hc09", total=500_000, age=OLD)
    b.entry("le_hc09", "pay_hc09", "order_hc09", amount=500_000, kind="capture", age=OLD)
    b.entry("le_hc09f", "pay_hc09", "order_hc09", amount=-61_800, kind="fee", age=OLD)
    b.record("case_09", payments=("pay_hc09",), orders=("order_hc09",),
             detail={"prior_cycle_chargeback_paise": "50000"})

    # 10 — out-of-order delivery: the refund event arrived before the capture.
    b.payment("pay_hc10", amount=90_000, status=PaymentStatus.REFUNDED, age=OLD)
    b.order("order_hc10", "pay_hc10", total=90_000, age=OLD)
    b.entry("le_hc10a", "pay_hc10", "order_hc10", amount=-90_000, kind="refund",
            age=OLD + timedelta(hours=1))
    b.entry("le_hc10b", "pay_hc10", "order_hc10", amount=90_000, kind="capture", age=OLD)
    b.record("case_10", payments=("pay_hc10",), orders=("order_hc10",))

    # 11 — a replay 26 hours later, past the webhook disable window.
    b.payment("pay_hc11", amount=275_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.order("order_hc11a", "pay_hc11", total=275_000, age=OLD)
    b.order("order_hc11b", "pay_hc11", total=275_000, age=OLD - timedelta(hours=26))
    b.entry("le_hc11", "pay_hc11", "order_hc11a", amount=275_000, kind="capture", age=OLD)
    b.record("case_11", payments=("pay_hc11",), orders=("order_hc11a", "order_hc11b"),
             detail={"replay_delay_hours": "26"})

    # 12 — cancelled by the merchant while the payment is still in flight.
    #      Deliberately recent: this is what makes the staleness window
    #      measurable rather than a permanently unexercised branch.
    b.payment("pay_hc12", amount=310_000, status=PaymentStatus.AUTHORIZED,
              age=timedelta(minutes=3))
    b.order("order_hc12", "pay_hc12", total=310_000, age=timedelta(minutes=4),
            status="cancelled")
    b.record("case_12", payments=("pay_hc12",), orders=("order_hc12",),
             detail={"in_flight": "true"})

    # 13 — MANDATORY. Two hypotheses reconcile exactly. Escalate, never pick.
    b.payment("pay_hc13", amount=400_000, status=PaymentStatus.CAPTURED, age=OLD,
              fee=8_000, tax=1_440)
    b.order("order_hc13", "pay_hc13", total=400_000, age=OLD)
    b.entry("le_hc13", "pay_hc13", "order_hc13", amount=400_000, kind="capture", age=OLD)
    b.entry("le_hc13f", "pay_hc13", "order_hc13", amount=-10_580, kind="fee", age=OLD)
    b.record("case_13", payments=("pay_hc13",), orders=("order_hc13",),
             detail={"residual_paise": "1140"})

    # 14 — MANDATORY. Nothing reconciles. Attach every rejected candidate.
    b.payment("pay_hc14", amount=400_000, status=PaymentStatus.CAPTURED, age=OLD,
              fee=8_000, tax=1_440)
    b.order("order_hc14", "pay_hc14", total=400_000, age=OLD)
    b.entry("le_hc14", "pay_hc14", "order_hc14", amount=400_000, kind="capture", age=OLD)
    b.entry("le_hc14f", "pay_hc14", "order_hc14", amount=-10_580, kind="fee", age=OLD)
    b.record("case_14", payments=("pay_hc14",), orders=("order_hc14",),
             detail={"residual_paise": "1140"})

    # 15 — a split payment over two instruments, rounding on each.
    b.payment("pay_hc15a", amount=125_003, status=PaymentStatus.CAPTURED, age=OLD,
              fee=2_500, tax=450, instrument="upi")
    b.payment("pay_hc15b", amount=125_003, status=PaymentStatus.CAPTURED, age=OLD,
              fee=2_500, tax=450, instrument="card_domestic")
    b.order("order_hc15a", "pay_hc15a", total=125_003, age=OLD)
    b.order("order_hc15b", "pay_hc15b", total=125_003, age=OLD)
    b.entry("le_hc15a", "pay_hc15a", "order_hc15a", amount=125_003, kind="capture", age=OLD)
    b.entry("le_hc15af", "pay_hc15a", "order_hc15a", amount=-2_951, kind="fee", age=OLD)
    b.entry("le_hc15b", "pay_hc15b", "order_hc15b", amount=125_003, kind="capture", age=OLD)
    b.entry("le_hc15bf", "pay_hc15b", "order_hc15b", amount=-2_951, kind="fee", age=OLD)
    b.record("case_15", payments=("pay_hc15a", "pay_hc15b"),
             orders=("order_hc15a", "order_hc15b"),
             detail={"residual_paise": "1"})

    # A late webhook: captured with no order on pass 1, order lands before
    # pass 2. The divergence is real when first seen and gone by the time a
    # repair could be authorised — exactly what two-run confirmation exists to
    # catch, and the only way `transient_filtered` becomes a measured number.
    b.payment("pay_hc16", amount=210_000, status=PaymentStatus.CAPTURED, age=OLD)
    b.entry("le_hc16", "pay_hc16", "order_hc16", amount=210_000, kind="capture", age=OLD)
    late_order = Order(order_id="order_hc16", payment_id="pay_hc16",
                       customer_id="cust_late", total_paise=210_000,
                       status="confirmed", created_at=now - OLD, line_items_count=2)

    # An instrument the fee schedule does not cover and the payment object
    # does not price. The correct outcome is FEE_SCHEDULE_UNKNOWN: the system
    # names the config that would resolve it rather than assuming a rate.
    b.payment("pay_hc17", amount=275_000, status=PaymentStatus.CAPTURED, age=OLD,
              instrument="crypto_voucher")
    b.order("order_hc17", "pay_hc17", total=275_000, age=OLD)
    b.entry("le_hc17", "pay_hc17", "order_hc17", amount=275_000, kind="capture", age=OLD)
    b.entry("le_hc17f", "pay_hc17", "order_hc17", amount=-6_000, kind="fee", age=OLD)

    packets, provider = _build_packets(b)
    return HardCaseBatch(
        batch=Batch(seed=b.seed, payments=b.payments, orders=b.orders,
                    ledger_entries=b.entries),
        cases=b.cases,
        packets=packets,
        late_arrivals=(late_order,),
        injected_payment_ids=frozenset(b.created),
        provider=provider,
        artifacts=b.artifacts,
    )


def _build_packets(
    b: _Builder,
) -> tuple[dict[str, EscalationPacket], FixtureHypothesisProvider]:
    """Escalation packets for the two propose-verify cases.

    The proposals are fixtures, not model output: the verifier is proven here,
    in Phase 4, before anything generated reaches it. Phase 5 swaps the
    provider and these same assertions must still hold.
    """
    residual = 1140

    # One provider for both cases, keyed by payment id so the eval can look up
    # proposals for any AMOUNT_MISMATCH it detects. Phase 5 swaps this for an
    # LLM provider behind the same interface and nothing else changes.
    case_13 = FixtureHypothesisProvider({
        "pay_hc13": [
            # Two genuinely different explanations of the same 1140 paise.
            # Neither declares a rate: these are components of a *residual*,
            # not percentages of the full payment, so a declared rate would be
            # checked against the wrong base and rejected — correctly.
            Proposal(components=[
                Component(name="instant_settlement", amount_paise=966, cites="stl_hard"),
                Component(name="gst", amount_paise=174, cites="stl_hard"),
            ]),
            Proposal(components=[
                Component(name="refund", amount_paise=1000, cites="rfnd_hard"),
                Component(name="rounding", amount_paise=140, cites="pay_hc13"),
            ]),
        ],
    })

    # Case 14 — five candidates, each wrong in a different, informative way.
    case_14 = FixtureHypothesisProvider({
        "pay_hc14": [
            Proposal(components=[
                Component(name="instant_settlement", amount_paise=914, cites="stl_hard",
                          rate_bps=30),
                Component(name="gst", amount_paise=165, cites="stl_hard", rate_bps=1800),
            ]),  # 1079, off by 61
            Proposal(components=[
                Component(name="refund", amount_paise=966, cites="rfnd_ghost"),
                Component(name="mdr", amount_paise=174, cites="pay_hc14", rate_bps=200),
            ]),  # arithmetic exact, cites an artifact that does not exist
            Proposal(components=[
                Component(name="mdr", amount_paise=1140, cites="pay_hc14", rate_bps=1250),
            ]),  # exact, but 12.5% MDR is impossible
            Proposal(components=[
                Component(name="rounding", amount_paise=1079, cites="pay_hc14"),
            ]),  # off by 61
            Proposal(components=[
                Component(name="adjustment", amount_paise=1201, cites="pay_hc14"),
            ]),  # off by 61 the other way
        ],
    })

    divergence_13 = Divergence(klass=DivergenceClass.AMOUNT_MISMATCH,
                               payment_id="pay_hc13", order_id="order_hc13",
                               amount_paise=400_000, observed_at=b.now)
    divergence_14 = Divergence(klass=DivergenceClass.AMOUNT_MISMATCH,
                               payment_id="pay_hc14", order_id="order_hc14",
                               amount_paise=400_000, observed_at=b.now)
    known = [Component(name="fee", amount_paise=8_000, cites="pay_hc13"),
             Component(name="tax", amount_paise=1_440, cites="pay_hc13")]

    combined = FixtureHypothesisProvider({**case_13.fixtures, **case_14.fixtures})

    packets = {
        "case_13": build_packet(
            divergence=divergence_13, known_components=known, residual_paise=residual,
            provider=FixtureHypothesisProvider(combined.fixtures),
            request=HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                                      artifacts=b.artifacts, case_id="pay_hc13"),
            # The same base the eval passes. Building the packet two ways with
            # two different checks is how a value comes out right in one place
            # and wrong in another.
            base_paise=400_000,
        ),
        "case_14": build_packet(
            divergence=divergence_14, known_components=known, residual_paise=residual,
            provider=FixtureHypothesisProvider(combined.fixtures),
            request=HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                                      artifacts=b.artifacts, case_id="pay_hc14"),
            base_paise=400_000,
        ),
    }
    return packets, combined
