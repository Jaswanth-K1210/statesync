"""An LLM behind the hypothesis-provider seam.

The verifier, the escalation packet and cases 13/14 were built and proven in
Phase 4 against fixtures. This swaps in a model and changes nothing else —
that was the point of inverting the dependency, and it means the verifier was
a known quantity before any generated output reached it.

**What the model is and is not given.** It receives the residual, the
instrument, a component taxonomy, and on the retry pass the hypotheses that
already failed. It does not receive the known fee and tax: those are
subtracted deterministically first, so the model is never handed the chance to
re-explain a number the gateway already reported.

**Cost accounting.** Every response is cached and committed, so on any run
after the first the cache serves everything — calls read zero, throughput
approaches the rules-only arm, and cost per 1,000 records reads zero. Those
are cache hits, not generation. `calls` and `network_calls` are therefore
counted separately and both are reported, cold and warm.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from statesync.classifier.provider import MAX_HYPOTHESES, HypothesisRequest
from statesync.classifier.verifier import Component, Proposal
from statesync.llm.cache import LLMCache

__all__ = ["LLMHypothesisProvider", "build_prompt", "parse_proposals"]

COMPONENT_TAXONOMY = (
    "fees", "refunds", "chargebacks", "reserves", "rounding", "adjustments", "FX",
)


def build_prompt(request: HypothesisRequest) -> str:
    """The exact text sent to the model, and the cache key.

    Deterministic in the request: same residual, same instrument, same
    rejections produce the same prompt and therefore the same cache entry.
    """
    lines = [
        "You are proposing candidate decompositions of an unexplained residual",
        "on a payment reconciliation. Known fees and taxes have ALREADY been",
        "subtracted; explain only the residual below.",
        "",
        f"residual_paise: {request.residual_paise}",
        f"instrument: {request.instrument}",
        # The provider is told which artifact it may cite. Without this it can
        # only guess an id, and every proposal fails ARTIFACT_MISSING for a
        # reason that says nothing about the quality of its reasoning.
        f"payment_id: {request.case_id}",
        f"component_taxonomy: {', '.join(COMPONENT_TAXONOMY)}",
        "",
        "Every component must cite the id of an artifact that exists. Amounts",
        "are integer paise. Do not invent artifact ids: a decomposition citing",
        "an artifact that does not exist is rejected even if the sum is exact.",
        "",
        'Reply with JSON: {"hypotheses": [{"components": [',
        '  {"name": str, "amount_paise": int, "cites": str, "rate_bps": int|null}',
        "]}]}",
    ]
    if request.rejected_summaries:
        lines += [
            "",
            "These candidates were already rejected. Propose different ones:",
            *(f"  - {s}" for s in request.rejected_summaries),
        ]
    return "\n".join(lines)


def parse_proposals(raw: str) -> list[Proposal]:
    """Turn a model response into proposals, discarding anything malformed.

    Every failure mode degrades to "nothing proposed" rather than raising:
    a provider that returns garbage must produce an honest escalation, not a
    crash. Constraint 1 is enforced here against model output rather than
    assumed of it — a float amount is discarded, not rounded.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(payload, dict):
        return []

    proposals: list[Proposal] = []
    for entry in payload.get("hypotheses", [])[:MAX_HYPOTHESES]:
        if not isinstance(entry, dict):
            continue
        components: list[Component] = []
        malformed = False
        for raw_component in entry.get("components", []):
            if not isinstance(raw_component, dict):
                malformed = True
                break
            amount = raw_component.get("amount_paise")
            # A float is discarded rather than rounded. Rounding a model's
            # number into a paise figure is exactly the silent fabrication the
            # verifier exists to prevent.
            if not isinstance(amount, int) or isinstance(amount, bool):
                malformed = True
                break
            rate = raw_component.get("rate_bps")
            components.append(Component(
                name=str(raw_component.get("name", "unnamed")),
                amount_paise=amount,
                cites=raw_component.get("cites") or None,
                rate_bps=rate if isinstance(rate, int) and not isinstance(rate, bool) else None,
            ))
        if malformed:
            continue
        proposals.append(Proposal(components=components))
    return proposals


@dataclass
class LLMHypothesisProvider:
    """Cache-first. The network is only reached on a miss."""

    cache: LLMCache = field(default_factory=LLMCache)
    client: Callable[[str], str] | None = None
    strict: bool = True
    """When False, a client failure degrades to no proposals instead of raising."""

    calls: int = 0
    network_calls: int = 0
    degraded: bool = False

    def propose(self, request: HypothesisRequest) -> list[Proposal]:
        self.calls += 1
        prompt = build_prompt(request)

        if self.cache.get(prompt) is None and self.client is not None:
            self.network_calls += 1

        try:
            raw = self.cache.call(prompt, self.client)
        except Exception:
            if self.strict:
                raise
            # Degraded: the deterministic path still produces an honest
            # escalation, marked so the run can report it.
            self.degraded = True
            return []

        return parse_proposals(raw)

    def as_event(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "network_calls": self.network_calls,
            "cache_hits": self.cache.hits,
            "degraded": self.degraded,
        }
