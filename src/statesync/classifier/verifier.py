"""The deterministic verifier — where the architectural thesis lives.

**The model proposes. The calculator decides.**

A proposed decomposition is accepted only if all three hold:

1. `Σ(components) == residual`, exactly, in integer paise. No tolerance.
2. Every cited artifact actually exists.
3. Every component with a declared rate sits inside its permitted range.

A hallucinated decomposition fails by construction rather than by instruction.
That is the whole guarantee: a model that invents a refund id can always make
the arithmetic work, so arithmetic alone was never going to be enough — check
(2) is what makes the guard load-bearing, and the verifier rejection rate is
reported precisely because it is the evidence.

Built in Phase 4 and proven against fixtures, before any model output reaches
it in Phase 5. By then the verifier is a known quantity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt

__all__ = [
    "ArtifactIndex", "Component", "Hypothesis", "Proposal", "Verdict",
    "COMPONENT_RANGES_BPS", "verify",
]

Paise = Annotated[StrictInt, Field(description="integer paise, never float")]

COMPONENT_RANGES_BPS: dict[str, tuple[int, int]] = {
    "mdr": (0, 400),                 # 0% – 4%
    "gst": (0, 1800),                # GST on the fee, 18% ceiling
    "instant_settlement": (0, 100),  # 0% – 1%
}
"""Permitted rate ranges in basis points. A component claiming a rate outside
its range is rejected however well the arithmetic works — a 12.5% MDR is not a
thing, and accepting one because the sum happened to land is exactly the
coincidental fit the bounded search exists to avoid."""


class Verdict(StrEnum):
    VERIFIED = "VERIFIED"
    ARITHMETIC_FAILED = "ARITHMETIC_FAILED"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    RANGE_VIOLATION = "RANGE_VIOLATION"
    RATE_INCONSISTENT = "RATE_INCONSISTENT"


class Component(BaseModel):
    """One named piece of an explanation, with what it rests on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    amount_paise: Paise
    cites: str | None = None
    rate_bps: int | None = None


@dataclass(frozen=True)
class Proposal:
    """An unverified candidate explanation, as a provider emits it.

    Deliberately carries no verdict: a provider cannot mark its own work
    correct. Only `verify()` produces a `Hypothesis`.
    """

    components: list[Component] = field(default_factory=list)


class Hypothesis(BaseModel):
    """A proposal that has been through the verifier, accepted or not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    components: list[Component]
    sum_paise: int
    residual_paise: int
    matched_residual: bool
    citations: list[str]
    verdict: Verdict
    rejection_reason: str = ""
    pass_no: int = 1
    """Which generation pass produced this candidate. Stored so a reader can
    see whether the retry did any work, rather than inferring it."""

    @property
    def arithmetic_shown(self) -> str:
        """The sum, written out. Every hypothesis in an escalation packet
        shows this — including the rejected ones, which is the point."""
        if not self.components:
            return f"(no components) = {self.sum_paise}"
        terms = " + ".join(str(c.amount_paise) for c in self.components)
        return f"{terms} = {self.sum_paise}"

    def as_packet_field(self, artifacts: ArtifactIndex) -> dict[str, Any]:
        """The stored form the API and the UI render verbatim.

        Every value here is what the verifier decided. Nothing downstream may
        recompute a sum, a match, or a verdict — that is instance eight of the
        pattern in docs/seam_bugs.json.
        """
        return {
            "label": self.label,
            "pass_no": self.pass_no,
            "components": [
                {"name": c.name, "amount_paise": c.amount_paise,
                 "cites": c.cites or "", "rate_bps": c.rate_bps or 0}
                for c in self.components
            ],
            "sum_paise": self.sum_paise,
            "residual_paise": self.residual_paise,
            "matched_residual": self.matched_residual,
            "arithmetic_shown": self.arithmetic_shown,
            "citations": [
                {"artifact": c, "resolves": c in artifacts} for c in self.citations
            ],
            "verdict": self.verdict.value,
            "rejection_reason": self.rejection_reason,
        }

    def as_event(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "sum_paise": self.sum_paise,
            "residual_paise": self.residual_paise,
            "matched_residual": self.matched_residual,
            "verdict": self.verdict.value,
            "rejection_reason": self.rejection_reason,
            "citations": sorted(self.citations),
            "components": [
                {"name": c.name, "amount_paise": c.amount_paise,
                 "cites": c.cites or "", "rate_bps": c.rate_bps or 0}
                for c in self.components
            ],
        }


class ArtifactIndex:
    """Which artifact ids actually exist. Membership is the whole interface."""

    def __init__(self, ids: set[str] | None = None) -> None:
        self._ids = set(ids or ())

    def __contains__(self, artifact_id: object) -> bool:
        return artifact_id in self._ids

    def add(self, artifact_id: str) -> None:
        self._ids.add(artifact_id)


def verify(
    proposal: Proposal,
    residual_paise: int,
    artifacts: ArtifactIndex,
    label: str = "H1",
    base_paise: int | None = None,
    pass_no: int = 1,
) -> Hypothesis:
    """Accept or reject one proposal. Deterministic, total, no I/O.

    Checks run in a fixed order — arithmetic, citations, ranges, then rate
    consistency — so an ops person reading a rejection fixes the sum before
    chasing a citation.

    The last check matters more than it looks. Verifying only that a rate is
    *in range* lets a decomposition claim "MDR at 2%" for an amount that is
    nothing like 2% of the payment: the sum reconciles, the artifact exists,
    the rate is plausible, and the explanation is still fiction. That is the
    coincidental fit bounded search is meant to avoid, and range checking alone
    does not catch it. When `base_paise` is known, a declared rate must
    reproduce its own amount.
    """
    total = sum(c.amount_paise for c in proposal.components)
    matched = total == residual_paise
    citations = [c.cites for c in proposal.components if c.cites]

    def result(verdict: Verdict, reason: str = "") -> Hypothesis:
        return Hypothesis(
            label=label, components=list(proposal.components), sum_paise=total,
            residual_paise=residual_paise, matched_residual=matched,
            citations=citations, verdict=verdict, rejection_reason=reason,
            pass_no=pass_no,
        )

    if not matched:
        delta = total - residual_paise
        return result(
            Verdict.ARITHMETIC_FAILED,
            f"components sum to {total} paise, residual is {residual_paise} "
            f"(off by {abs(delta)})",
        )

    for component in proposal.components:
        if component.cites is None:
            return result(
                Verdict.ARTIFACT_MISSING,
                f"component '{component.name}' cites no artifact",
            )
        if component.cites not in artifacts:
            return result(
                Verdict.ARTIFACT_MISSING,
                f"cited artifact '{component.cites}' does not exist",
            )

    for component in proposal.components:
        if component.rate_bps is None:
            continue
        bounds = COMPONENT_RANGES_BPS.get(component.name)
        if bounds is None:
            continue
        low, high = bounds
        if not low <= component.rate_bps <= high:
            return result(
                Verdict.RANGE_VIOLATION,
                f"component '{component.name}' claims {component.rate_bps / 100:.1f}%, "
                f"permitted range is {low / 100:.1f}%-{high / 100:.1f}%",
            )

    if base_paise is not None:
        by_name = {c.name: c for c in proposal.components}
        for component in proposal.components:
            if component.rate_bps is None:
                continue
            # GST is charged on the fee, not on the transaction, so it is
            # checked against the MDR component when one is present.
            base = base_paise
            if component.name == "gst" and "mdr" in by_name:
                base = by_name["mdr"].amount_paise
            expected = base * component.rate_bps // 10_000
            if component.amount_paise != expected:
                return result(
                    Verdict.RATE_INCONSISTENT,
                    f"component '{component.name}' claims "
                    f"{component.rate_bps / 100:.2f}% of {base} paise, which is "
                    f"{expected} paise, but declares {component.amount_paise}",
                )

    return result(Verdict.VERIFIED)
