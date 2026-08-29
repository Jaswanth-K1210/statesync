"""Deterministic classification of the four clean divergence classes.

No model is involved and saying so plainly is the honest framing. The
reconciler's set operations already carry the class; this module is where the
decision is made explicit, validated against the closed vocabulary, and given
a reason code — so that Phase 5 has one seam to plug the propose-verify layer
into rather than a rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass

from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass, ReasonCode

__all__ = ["Classification", "RULE_RESOLVED_CLASSES", "classify"]

RULE_RESOLVED_CLASSES = frozenset({
    DivergenceClass.CAPTURED_NO_ORDER,
    DivergenceClass.ORDER_NO_CAPTURE,
    DivergenceClass.DUPLICATE_ORDER,
    DivergenceClass.REFUND_NOT_REFLECTED,
})
"""The four set operations. AMOUNT_MISMATCH and SETTLEMENT_GAP are the two
genuinely under-determined classes and are routed to Phase 5's verifier."""


@dataclass(frozen=True)
class Classification:
    divergence: Divergence
    klass: DivergenceClass
    reason_code: ReasonCode
    resolved_by: str
    """`rules` here. Phase 5 adds `verified_hypothesis` and `escalated`."""


def classify(divergence: Divergence) -> Classification:
    """Classify one divergence deterministically.

    A class outside the four clean ones is not guessed at — it is returned
    unresolved with an explicit reason code, so the count of "things rules
    could not settle" is a reported number rather than a silent gap.
    """
    if divergence.klass in RULE_RESOLVED_CLASSES:
        return Classification(divergence=divergence, klass=divergence.klass,
                              reason_code=ReasonCode.VERIFIED, resolved_by="rules")
    return Classification(divergence=divergence, klass=divergence.klass,
                          reason_code=ReasonCode.NO_HYPOTHESIS_VERIFIED,
                          resolved_by="escalated")
