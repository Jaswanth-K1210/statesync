"""Where candidate explanations come from — the seam the LLM plugs into.

The verifier decides; a provider only proposes. Keeping that boundary explicit
means Phase 4 can build and prove the whole escalation path — verifier,
packet, cases 13 and 14 — against hand-written fixtures with no model and no
network, and Phase 5 becomes a swap rather than a rewrite.

`FixtureHypothesisProvider` is production code, not a test double: it is what
the eval's deterministic arms use, and it is what makes the demo reproducible
without a provider key.

**Generation bounds are enforced here, not in the caller.** Two passes, ten
hypotheses per divergence, hard. An unbounded search over an under-determined
problem will eventually produce something that reconciles by coincidence, and
a coincidental fit is worse than an escalation.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol

from statesync.classifier.verifier import ArtifactIndex, Proposal

__all__ = [
    "FixtureHypothesisProvider", "HypothesisProvider", "HypothesisRequest",
    "MAX_HYPOTHESES", "MAX_PASSES",
]

MAX_PASSES = 2
"""Pass 1 proposes; pass 2 retries once with the rejections in hand. Then stop."""

MAX_HYPOTHESES = 10
"""Hard cap per divergence, across both passes."""


@dataclass(frozen=True)
class HypothesisRequest:
    """Everything a provider is allowed to see.

    Note what is absent: the provider never receives the *known* components.
    Fee and tax are subtracted deterministically before this is built, so only
    the unexplained residual is ever up for explanation.
    """

    residual_paise: int
    instrument: str
    artifacts: ArtifactIndex
    case_id: str
    rejected_summaries: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.residual_paise, int) or isinstance(self.residual_paise, bool):
            raise TypeError(
                f"residual_paise must be integer paise, got {type(self.residual_paise).__name__}"
            )

    def with_rejections(self, summaries: list[str]) -> HypothesisRequest:
        """The pass-2 request: same residual, plus what already failed."""
        return replace(self, rejected_summaries=list(summaries))


class HypothesisProvider(Protocol):
    """Proposes candidate decompositions of a residual. Never verifies them."""

    calls: int

    def propose(self, request: HypothesisRequest) -> list[Proposal]: ...


@dataclass
class FixtureHypothesisProvider:
    """Hand-written proposal sets, keyed by case id.

    Deterministic and offline. An unknown case yields nothing rather than
    inventing one — silence beats a fabricated explanation, and "no generated
    hypothesis verified" is a claim the architecture can actually support.
    """

    fixtures: dict[str, list[Proposal]] = field(default_factory=dict)
    calls: int = 0

    def propose(self, request: HypothesisRequest) -> list[Proposal]:
        """Pass 1 reads `case_id`; pass 2 reads `case_id:retry`.

        The retry is a *different* proposal set, which is what a real provider
        emits when told what already failed. Re-offering the same candidates
        would double every hypothesis in the packet and inflate the count with
        duplicates rather than new thinking. A case with no retry fixture
        proposes nothing on pass 2, which is an honest "I have no more ideas".
        """
        self.calls += 1
        key = f"{request.case_id}:retry" if request.rejected_summaries else request.case_id
        return list(self.fixtures.get(key, []))[:MAX_HYPOTHESES]
