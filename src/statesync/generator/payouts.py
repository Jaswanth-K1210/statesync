"""Seeded synthetic payouts on a T+2 cycle, with deliberately injected gaps.

Separate from the 500-record batch in `generator/synthetic.py` and sharing
nothing with it: the record-level evaluation must not move because settlement
work happened.

**Why T+2 matters.** Captures made on cycle N settle in the payout for cycle
N+2, so a payout legitimately covers captures from a prior day. A reconciler
that sums today's captures against today's payout is wrong by design, not by
accident, and the `next_cycle_capture` gap exists to prove this implementation
is not making that mistake.

Ground truth is exact because the gaps are caused here, the same way the
record-level injector works.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from statesync.classifier.verifier import ArtifactIndex, Component, Proposal
from statesync.config import BASE_TIME, SEED
from statesync.ledger.canonical import canonical
from statesync.models.settlement import Capture, Payout

__all__ = ["GAP_KINDS", "InjectedGap", "PayoutBatch", "generate_payouts"]

GAP_KINDS: tuple[str, ...] = (
    "undisclosed_reserve",
    "prior_cycle_chargeback",
    "instant_settlement",
    "next_cycle_capture",
    "ambiguous",
    "unexplained",
)

CYCLES = 10
CAPTURES_PER_CYCLE = 12
SETTLEMENT_LAG = 2

_INSTRUMENTS = (("upi", 0), ("card_domestic", 200), ("card_intl", 300), ("emi", 250))
_GST_BPS = 1800


@dataclass(frozen=True)
class InjectedGap:
    """A gap we caused, so the correct answer is known exactly."""

    kind: str
    payout_id: str
    residual_paise: int
    detail: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PayoutBatch:
    seed: int
    captures: list[Capture]
    payouts: list[Payout]
    gaps: list[InjectedGap]
    proposals: dict[str, list[Proposal]] = field(default_factory=dict)
    """Candidate attributions, keyed by payout id.

    Built here beside the ground truth that determines them, the same way
    `injector/hard_cases.py` builds its fixtures. Deterministic and offline:
    settlement adds no LLM calls and does not touch the committed cache.
    """

    @property
    def artifacts(self) -> ArtifactIndex:
        return ArtifactIndex(
            {p.payout_id for p in self.payouts} | {c.capture_id for c in self.captures}
        )

    @property
    def gap_payout_ids(self) -> set[str]:
        """Payouts carrying an injected gap that *should* be flagged.

        `next_cycle_capture` is excluded deliberately: it is a timing boundary
        and the correct outcome is that nothing is flagged at all.
        """
        return {g.payout_id for g in self.gaps if g.kind != "next_cycle_capture"}

    def capture(self, capture_id: str) -> Capture:
        return next(c for c in self.captures if c.capture_id == capture_id)

    def as_event(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "captures": [c.model_dump(mode="python") for c in self.captures],
            "payouts": [p.model_dump(mode="python") for p in self.payouts],
        }

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.as_event())).hexdigest()


def _token(rng: random.Random, n: int = 12) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(rng.choice(alphabet) for _ in range(n))


def generate_payouts(seed: int = SEED) -> PayoutBatch:
    """Ten cycles of captures, their payouts two cycles later, and six gaps."""
    rng = random.Random(seed)
    captures: list[Capture] = []

    for cycle in range(CYCLES):
        for _ in range(CAPTURES_PER_CYCLE):
            instrument, mdr_bps = rng.choice(_INSTRUMENTS)
            amount = rng.randrange(50_000, 2_000_000, 100)
            mdr = amount * mdr_bps // 10_000
            fee = mdr + mdr * _GST_BPS // 10_000
            refund = amount if rng.random() < 0.08 else 0
            captures.append(Capture(
                capture_id=f"cap_{_token(rng)}", payment_id=f"pay_{_token(rng)}",
                amount_paise=amount, refund_paise=refund, fee_paise=fee,
                instrument=instrument, cycle=cycle,
            ))

    by_cycle: dict[int, list[Capture]] = {}
    for capture in captures:
        by_cycle.setdefault(capture.cycle, []).append(capture)

    # One gap kind per payout, assigned to the last cycles so the earlier ones
    # stay clean and the false-positive count is meaningful.
    settling = [c for c in range(CYCLES) if c + SETTLEMENT_LAG < CYCLES + SETTLEMENT_LAG]
    assignments = dict(zip(settling[-len(GAP_KINDS):], GAP_KINDS, strict=False))

    payouts: list[Payout] = []
    gaps: list[InjectedGap] = []

    for source_cycle in settling:
        covered = list(by_cycle.get(source_cycle, []))
        kind = assignments.get(source_cycle)
        payout_id = f"pout_{_token(rng)}"

        # The T+2 boundary. One capture slips into the NEXT payout, and this
        # payout says so by leaving it out of capture_ids. Correct behaviour is
        # to notice nothing: the declared components still reconcile.
        if kind == "next_cycle_capture" and len(covered) > 1:
            covered = covered[:-1]

        gross = sum(c.amount_paise for c in covered)
        refunds = sum(c.refund_paise for c in covered)
        fees = sum(c.fee_paise for c in covered)
        reserve = 0
        residual = 0
        detail: dict[str, str] = {}

        if kind == "undisclosed_reserve":
            # Withheld but not declared: net is short and no field explains it.
            residual = gross * 50 // 10_000
        elif kind == "prior_cycle_chargeback":
            residual = 125_000
            detail = {"cross_cycle": "true", "from_cycle": str(source_cycle - 1)}
        elif kind == "instant_settlement":
            subset = covered[: max(len(covered) // 3, 1)]
            residual = sum(c.amount_paise for c in subset) * 30 // 10_000
            detail = {"captures": str(len(subset))}
        elif kind == "ambiguous":
            # A figure two different decompositions both reach exactly.
            residual = 60_000
        elif kind == "unexplained":
            residual = 43_717

        net = gross - refunds - fees - reserve - residual
        payouts.append(Payout(
            payout_id=payout_id,
            settled_at=BASE_TIME + timedelta(days=source_cycle + SETTLEMENT_LAG),
            cycle=source_cycle + SETTLEMENT_LAG,
            gross_captures_paise=gross, total_refunds_paise=refunds,
            total_fees_paise=fees, reserve_paise=reserve, net_paise=net,
            capture_ids=tuple(c.capture_id for c in covered),
        ))
        if kind:
            gaps.append(InjectedGap(kind=kind, payout_id=payout_id,
                                    residual_paise=residual, detail=detail))

    return PayoutBatch(seed=seed, captures=captures, payouts=payouts, gaps=gaps,
                       proposals=_proposals(gaps))


def _proposals(gaps: list[InjectedGap]) -> dict[str, list[Proposal]]:
    """One candidate set per gap, keyed by payout id.

    No component declares a rate: these are components of a *residual*, not
    percentages of one base, so a declared rate would be checked against the
    wrong denominator and correctly rejected.
    """
    out: dict[str, list[Proposal]] = {}
    for gap in gaps:
        cite = gap.payout_id
        r = gap.residual_paise

        if gap.kind == "undisclosed_reserve":
            out[cite] = [Proposal(components=[
                Component(name="reserve", amount_paise=r, cites=cite)])]
        elif gap.kind == "prior_cycle_chargeback":
            out[cite] = [Proposal(components=[
                Component(name="chargeback", amount_paise=r, cites=cite)])]
        elif gap.kind == "instant_settlement":
            out[cite] = [Proposal(components=[
                Component(name="instant_settlement_fee", amount_paise=r, cites=cite)])]
        elif gap.kind == "ambiguous":
            # Two decompositions that both reconcile exactly. The system must
            # attach both rather than pick one.
            out[cite] = [
                Proposal(components=[
                    Component(name="reserve", amount_paise=r * 2 // 3, cites=cite),
                    Component(name="rounding", amount_paise=r - r * 2 // 3, cites=cite)]),
                Proposal(components=[
                    Component(name="chargeback", amount_paise=r // 2, cites=cite),
                    Component(name="adjustment", amount_paise=r - r // 2, cites=cite)]),
            ]
        elif gap.kind == "unexplained":
            # Each wrong in a different, informative way. None verify.
            out[cite] = [
                Proposal(components=[
                    Component(name="reserve", amount_paise=r - 500, cites=cite)]),
                Proposal(components=[
                    Component(name="chargeback", amount_paise=r, cites="cap_nonexistent")]),
                Proposal(components=[
                    Component(name="fx", amount_paise=r + 1200, cites=cite)]),
            ]
    return out
