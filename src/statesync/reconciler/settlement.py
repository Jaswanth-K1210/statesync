"""Batch-level reconciliation: one payout against the set it covers.

`reconciler/three_way.py` compares one payment against one order. This is a
different shape and gets its own module rather than bending that one: a payout
nets a *set* of captures, refunds and fees, and it settles two cycles after the
captures were made.

**The boundary that matters.** `payout.capture_ids` is authoritative about what
the payout covers. Deriving that set from dates instead invents a shortfall
every time a late capture slips into the next cycle — timing read as loss. The
`next_cycle_capture` gap exists to prove this does not happen here.

Attribution runs through the existing propose-verify path with nothing changed:
known components subtracted first, only the residual proposed against, verified
to the exact paise, same verdicts, same two-pass bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from statesync.classifier.escalation import EscalationPacket, build_packet
from statesync.classifier.provider import FixtureHypothesisProvider, HypothesisRequest
from statesync.classifier.verifier import Component
from statesync.config import BASE_TIME
from statesync.generator.payouts import InjectedGap, PayoutBatch
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass
from statesync.models.settlement import Payout

__all__ = ["SettlementGap", "attribute", "check_payout", "reconcile_payouts"]


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    residual_paise: int
    declared_net_paise: int
    actual_net_paise: int
    covered_gross_paise: int


@dataclass(frozen=True)
class SettlementGap:
    payout_id: str
    cycle: int
    residual_paise: int
    detail: dict[str, str] = field(default_factory=dict)


def check_payout(payout: Payout, batch: PayoutBatch) -> CheckResult:
    """Two checks, both deterministic.

    First that the payout's declared components reconcile to its own net, and
    second that its declared gross matches the captures it says it covers. A
    residual is what the components do not account for.
    """
    covered = [c for c in batch.captures if c.capture_id in payout.capture_ids]
    covered_gross = sum(c.amount_paise for c in covered)

    residual = payout.declared_net_paise - payout.net_paise
    coverage_gap = payout.gross_captures_paise - covered_gross

    return CheckResult(
        ok=residual == 0 and coverage_gap == 0,
        residual_paise=residual + coverage_gap,
        declared_net_paise=payout.declared_net_paise,
        actual_net_paise=payout.net_paise,
        covered_gross_paise=covered_gross,
    )


def reconcile_payouts(batch: PayoutBatch) -> list[SettlementGap]:
    """Every payout whose components do not account for its net."""
    gaps: list[SettlementGap] = []
    for payout in batch.payouts:
        result = check_payout(payout, batch)
        if not result.ok:
            gaps.append(SettlementGap(
                payout_id=payout.payout_id, cycle=payout.cycle,
                residual_paise=result.residual_paise,
                detail={"declared_net_paise": str(result.declared_net_paise),
                        "actual_net_paise": str(result.actual_net_paise)},
            ))
    return gaps


def attribute(gap: SettlementGap | InjectedGap, batch: PayoutBatch) -> EscalationPacket:
    """Explain one gap's residual through the unchanged propose-verify path.

    Takes a detected gap or an injected one: both carry a payout id and a
    residual, and scoring wants to attribute either.
    """
    payout_id = gap.payout_id
    residual = gap.residual_paise
    payout = next(p for p in batch.payouts if p.payout_id == payout_id)

    divergence = Divergence(
        klass=DivergenceClass.SETTLEMENT_GAP, payment_id=payout_id,
        order_id=None, amount_paise=payout.gross_captures_paise,
        observed_at=BASE_TIME,
    )
    # Everything the payout already declares is subtracted before anything is
    # proposed. Only what is left over is ever handed to a provider.
    known = [
        Component(name="declared_fees", amount_paise=payout.total_fees_paise, cites=payout_id),
        Component(name="declared_refunds", amount_paise=payout.total_refunds_paise,
                  cites=payout_id),
    ]

    return build_packet(
        divergence=divergence, known_components=known, residual_paise=residual,
        provider=FixtureHypothesisProvider(
            {payout_id: batch.proposals.get(payout_id, [])}
        ),
        request=HypothesisRequest(
            residual_paise=residual, instrument="settlement",
            artifacts=batch.artifacts, case_id=payout_id,
        ),
    )
