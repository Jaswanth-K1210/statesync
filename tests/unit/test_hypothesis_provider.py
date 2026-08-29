"""The hypothesis provider seam.

Cases 13 and 14 are defined by hypothesis verification, but generation is
Phase 5. Inverting the dependency lets Phase 4 build and prove the verifier,
the escalation packet and both refusal cases against hand-written fixtures —
deterministic, no model, no network.

Phase 5 swaps in an LLM provider behind the same interface. By then the
verifier is already proven against known-good and known-bad proposal sets,
including one shaped exactly like what a hallucinating provider emits.
"""

import pytest

from statesync.classifier.provider import (
    MAX_HYPOTHESES,
    MAX_PASSES,
    FixtureHypothesisProvider,
    HypothesisRequest,
)
from statesync.classifier.verifier import ArtifactIndex, Component, Proposal

ARTIFACTS = ArtifactIndex({"pay_1", "stl_4471", "rfnd_real"})


def request(residual=1140, case="default"):
    return HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                             artifacts=ARTIFACTS, case_id=case)


def test_a_provider_returns_proposals_not_verdicts():
    """A provider cannot mark its own work correct."""
    proposals = FixtureHypothesisProvider({}).propose(request())
    assert all(isinstance(p, Proposal) for p in proposals)


def test_fixtures_are_returned_for_a_known_case():
    fixture = [Proposal(components=[Component(name="mdr", amount_paise=1140, cites="pay_1")])]
    provider = FixtureHypothesisProvider({"case_06": fixture})
    assert provider.propose(request(case="case_06")) == fixture


def test_an_unknown_case_yields_nothing_rather_than_inventing_one():
    """Silence beats a fabricated explanation."""
    assert FixtureHypothesisProvider({}).propose(request(case="case_99")) == []


def test_generation_is_bounded_to_ten_hypotheses():
    """An unbounded search over an under-determined problem eventually fits by
    coincidence, and a coincidental fit is worse than an escalation."""
    many = [Proposal(components=[Component(name="x", amount_paise=i, cites="pay_1")])
            for i in range(25)]
    provider = FixtureHypothesisProvider({"big": many})
    assert len(provider.propose(request(case="big"))) == MAX_HYPOTHESES


def test_the_bounds_are_the_published_ones():
    assert MAX_PASSES == 2
    assert MAX_HYPOTHESES == 10


def test_the_provider_counts_its_calls_for_reporting():
    provider = FixtureHypothesisProvider({})
    provider.propose(request())
    provider.propose(request())
    assert provider.calls == 2


def test_a_request_carries_the_residual_and_the_artifact_index():
    req = request(residual=9999)
    assert req.residual_paise == 9999
    assert "pay_1" in req.artifacts


def test_a_request_refuses_a_float_residual():
    with pytest.raises(TypeError):
        HypothesisRequest(residual_paise=11.40, instrument="upi",
                          artifacts=ARTIFACTS, case_id="x")


def test_rejected_proposals_can_be_fed_back_for_the_retry_pass():
    """Pass 2 tells the provider what already failed and why."""
    req = request()
    retry = req.with_rejections(["H1 off by 60", "H2 cited a missing refund"])
    assert retry.rejected_summaries == ["H1 off by 60", "H2 cited a missing refund"]
    assert retry.residual_paise == req.residual_paise


def test_the_retry_pass_reads_a_separate_fixture_set():
    """Pass 2 is different thinking, not the same list offered again."""
    first = [Proposal(components=[Component(name="a", amount_paise=1, cites="pay_1")])]
    retry = [Proposal(components=[Component(name="b", amount_paise=2, cites="pay_1")])]
    provider = FixtureHypothesisProvider({"c": first, "c:retry": retry})

    assert provider.propose(request(case="c")) == first
    assert provider.propose(request(case="c").with_rejections(["H1 failed"])) == retry


def test_a_case_with_no_retry_fixture_proposes_nothing_on_pass_two():
    """An honest 'I have no more ideas' beats repeating the same candidates."""
    provider = FixtureHypothesisProvider(
        {"c": [Proposal(components=[Component(name="a", amount_paise=1, cites="pay_1")])]}
    )
    assert provider.propose(request(case="c").with_rejections(["H1 failed"])) == []
