"""Escalation packets and the templated ops report.

An escalation that says only "ambiguous" hands the human back the same
under-determined problem the system just declined. Every packet carries the
residual, the components already deterministically subtracted, and **every**
hypothesis — verified and rejected alike — with its arithmetic written out and
the reason it failed.

Case 13 (two verify): both are attached with full breakdowns, so the ops
person distinguishes "MDR + instant settlement" from "partial refund + MDR on
remainder" — which need different follow-ups — rather than re-deriving them.

Case 14 (none verify): all of them are attached with the reason each failed.
A human seeing that seven were close but off by consistent paise learns
something a bare "escalated" never conveys.

**The wording rule.** The system may report *"no generated hypothesis
verified."* It may never claim *"no explanation exists."* The second is a
claim the architecture cannot support, and `FORBIDDEN_PHRASES` plus a test
over every reason code is what enforces it.

**Every figure is templated.** An unfaithful summary of a financial
reconciliation is a liability, not a UX bug: if a report says "3 divergences
repaired" when there were 5, an ops person closes the ticket without
investigating. Counts, amounts and ids come from the record. No model output
produces any figure here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from statesync.classifier.provider import (
    MAX_HYPOTHESES,
    MAX_PASSES,
    HypothesisProvider,
    HypothesisRequest,
)
from statesync.classifier.verifier import Component, Hypothesis, Verdict, verify
from statesync.ledger.chain import Ledger
from statesync.models.domain import Divergence
from statesync.models.enums import ReasonCode

__all__ = ["EscalationPacket", "FORBIDDEN_PHRASES", "build_packet", "suggested_action"]

FORBIDDEN_PHRASES = (
    "no explanation exists",
    "there is no explanation",
    "cannot be explained",
    "unexplainable",
)
"""Claims stronger than the architecture supports. A test asserts no template
produces any of them, for any reason code."""

_TEMPLATES: dict[ReasonCode, str] = {
    ReasonCode.VERIFIED: (
        "Residual of {residual} paise on {payment_id} is fully attributed by one "
        "verified hypothesis. No action required."
    ),
    ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED: (
        "Two or more hypotheses reconcile the {residual} paise residual on "
        "{payment_id} exactly. Both are attached with their component "
        "breakdowns; confirm which applies before any adjustment is posted."
    ),
    ReasonCode.NO_HYPOTHESIS_VERIFIED: (
        "No generated hypothesis verified for the {residual} paise residual on "
        "{payment_id}. All candidates are attached with their arithmetic and "
        "the reason each was rejected. Check the settlement report for the "
        "relevant cycle."
    ),
    ReasonCode.FEE_SCHEDULE_UNKNOWN: (
        "Fee data is unavailable for {payment_id}, leaving {residual} paise "
        "unattributed. Add the instrument to the fee schedule; no rate has "
        "been assumed."
    ),
    ReasonCode.STUCK_REPAIR: (
        "A repair for {payment_id} was claimed and never completed, leaving "
        "{residual} paise unresolved. Review, then clear the claim to permit "
        "one fresh attempt."
    ),
}


def suggested_action(reason: ReasonCode, residual_paise: int, payment_id: str) -> str:
    """A fixed template populated from the record. Never model-generated."""
    return _TEMPLATES[reason].format(residual=residual_paise, payment_id=payment_id)


@dataclass(frozen=True)
class EscalationPacket:
    divergence: Divergence
    known_components: list[Component]
    residual_paise: int
    hypotheses: list[Hypothesis]
    reason_code: ReasonCode
    suggested_action: str
    passes_used: int = 1
    known_components_source: str = "payment.fee_paise + payment.tax_paise"

    @property
    def verified(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.verdict == Verdict.VERIFIED]

    @property
    def rejected(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.verdict != Verdict.VERIFIED]

    def as_event(self) -> dict[str, Any]:
        return {
            "divergence_key": self.divergence.deterministic_key(),
            "klass": self.divergence.klass.value,
            "payment_id": self.divergence.payment_id or "",
            "residual_paise": self.residual_paise,
            "reason_code": self.reason_code.value,
            "suggested_action": self.suggested_action,
            "passes_used": self.passes_used,
            "known_components_source": self.known_components_source,
            "known_components": [
                {"name": c.name, "amount_paise": c.amount_paise} for c in self.known_components
            ],
            "hypotheses": [h.as_event() for h in self.hypotheses],
        }


def build_packet(
    divergence: Divergence,
    known_components: list[Component],
    residual_paise: int,
    provider: HypothesisProvider,
    request: HypothesisRequest,
    ledger: Ledger | None = None,
) -> EscalationPacket:
    """Run the bounded propose-verify loop and package the outcome.

        Pass 1: propose, verify each
          none verify -> Pass 2: retry ONCE with the rejections in hand
            none verify -> STOP, NO_HYPOTHESIS_VERIFIED

    Hard cap: `MAX_PASSES` passes, `MAX_HYPOTHESES` hypotheses. No adaptive
    retry loop — an unbounded search over an under-determined problem will
    eventually fit by coincidence, and that is worse than an escalation.

    **Every pass is written to the ledger.** An unlogged retry is a provider
    call nobody can audit, and the bound is only meaningful if the count is
    reconstructable after the fact.
    """
    hypotheses: list[Hypothesis] = []
    passes_used = 0
    divergence_key = divergence.deterministic_key()

    for pass_no in range(MAX_PASSES):
        if pass_no > 0:
            request = request.with_rejections(
                [f"{h.label} {h.verdict.value}: {h.rejection_reason}" for h in hypotheses]
            )
        passes_used = pass_no + 1

        proposals = provider.propose(request)
        accepted = 0
        for proposal in proposals:
            if len(hypotheses) >= MAX_HYPOTHESES:
                break
            hypotheses.append(
                verify(proposal, residual_paise=residual_paise,
                       artifacts=request.artifacts, label=f"H{len(hypotheses) + 1}")
            )
            accepted += 1

        if ledger is not None:
            ledger.append("HYPOTHESIS_PASS", divergence_key=divergence_key,
                          pass_no=passes_used, proposed=len(proposals),
                          verified_so_far=sum(
                              1 for h in hypotheses if h.verdict == Verdict.VERIFIED
                          ), accepted=accepted)

        if any(h.verdict == Verdict.VERIFIED for h in hypotheses):
            break

    verified = [h for h in hypotheses if h.verdict == Verdict.VERIFIED]
    if len(verified) == 1:
        reason = ReasonCode.VERIFIED
    elif len(verified) > 1:
        reason = ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    else:
        reason = ReasonCode.NO_HYPOTHESIS_VERIFIED

    if ledger is not None:
        ledger.append(
            "HYPOTHESIS_VERIFIED" if verified else "HYPOTHESIS_EXHAUSTED",
            divergence_key=divergence_key, passes_used=passes_used,
            hypotheses=len(hypotheses), verified=len(verified),
            reason_code=reason.value,
        )

    return EscalationPacket(
        divergence=divergence,
        known_components=list(known_components),
        residual_paise=residual_paise,
        hypotheses=hypotheses,
        reason_code=reason,
        suggested_action=suggested_action(reason, residual_paise,
                                          divergence.payment_id or ""),
        passes_used=passes_used,
        known_components_source=(
            "payment.fee_paise + payment.tax_paise" if known_components else "unavailable"
        ),
    )


