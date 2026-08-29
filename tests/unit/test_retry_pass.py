"""The second pass — enforced, not asserted.

The published bound is "2 passes, 10 hypotheses per divergence". A retry that
is never exercised is a bound the code claims rather than one it keeps, so
these tests drive the path where pass 1 fails entirely and pass 2 has to do
the work.

Every pass is written to the ledger. The count of passes used per divergence
is a reported number, and an unlogged retry is an LLM call nobody can audit.
"""

import pytest

from statesync.classifier.escalation import build_packet
from statesync.classifier.provider import (
    MAX_HYPOTHESES,
    FixtureHypothesisProvider,
    HypothesisRequest,
)
from statesync.classifier.verifier import ArtifactIndex, Component, Proposal
from statesync.ledger.chain import Ledger
from statesync.models.enums import DivergenceClass, ReasonCode
from tests.unit.helpers import NOW, divergence_for

ARTIFACTS = ArtifactIndex({"pay_1", "stl_1"})


def comp(name, amount, cites="pay_1", rate_bps=None):
    return Component(name=name, amount_paise=amount, cites=cites, rate_bps=rate_bps)


def build(fixtures, residual=1140, ledger=None):
    provider = FixtureHypothesisProvider(fixtures)
    request = HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                                artifacts=ARTIFACTS, case_id="c")
    packet = build_packet(
        divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
        known_components=[], residual_paise=residual,
        provider=provider, request=request, ledger=ledger,
    )
    return packet, provider


# ── the retry fires when pass 1 fails entirely ──────────────────────────────

def test_pass_two_fires_when_no_pass_one_hypothesis_verifies():
    packet, provider = build({"c": [Proposal(components=[comp("a", 500)])]})
    assert packet.passes_used == 2
    assert provider.calls == 2


def test_pass_two_does_not_fire_when_pass_one_succeeds():
    """A verified explanation ends the search. No wasted call."""
    packet, provider = build({"c": [Proposal(components=[comp("mdr", 1140)])]})
    assert packet.passes_used == 1
    assert provider.calls == 1


# ── the retry can actually resolve — the path that earns its place ──────────

def test_a_hypothesis_found_on_the_retry_verifies_and_resolves():
    """Pass 1 fails completely; pass 2 finds the answer.

    Without this, "2 passes" is a bound nobody has ever seen exercised to a
    successful conclusion.
    """
    packet, _ = build({
        "c": [Proposal(components=[comp("wrong", 500)])],
        "c:retry": [Proposal(components=[comp("mdr", 800), comp("gst", 340)])],
    })
    assert packet.reason_code == ReasonCode.VERIFIED
    assert packet.passes_used == 2
    assert len(packet.verified) == 1


def test_the_retry_hypotheses_are_labelled_after_the_first_pass():
    packet, _ = build({
        "c": [Proposal(components=[comp("wrong", 500)])],
        "c:retry": [Proposal(components=[comp("mdr", 1140)])],
    })
    assert [h.label for h in packet.hypotheses] == ["H1", "H2"]


def test_rejected_pass_one_hypotheses_are_kept_in_the_packet():
    """The ops person sees what was tried first, not just what worked."""
    packet, _ = build({
        "c": [Proposal(components=[comp("wrong", 500)])],
        "c:retry": [Proposal(components=[comp("mdr", 1140)])],
    })
    assert len(packet.hypotheses) == 2
    assert packet.hypotheses[0].verdict.value == "ARITHMETIC_FAILED"


# ── the bound is enforced ───────────────────────────────────────────────────

def test_there_is_never_a_third_pass():
    packet, provider = build({
        "c": [Proposal(components=[comp("a", 1)])],
        "c:retry": [Proposal(components=[comp("b", 2)])],
    })
    assert provider.calls == 2
    assert packet.passes_used == 2
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_the_ten_hypothesis_cap_holds_across_both_passes():
    """Six plus six is twelve, and twelve is more than the cap."""
    six = [Proposal(components=[comp("x", i)]) for i in range(6)]
    packet, _ = build({"c": six, "c:retry": six})
    assert len(packet.hypotheses) == MAX_HYPOTHESES


# ── every pass is auditable ─────────────────────────────────────────────────

def test_each_pass_is_written_to_the_ledger():
    ledger = Ledger(clock=lambda: NOW)
    build({"c": [Proposal(components=[comp("a", 500)])]}, ledger=ledger)
    passes = [e for e in ledger.entries if e.event_type == "HYPOTHESIS_PASS"]
    assert len(passes) == 2
    assert [e.payload["pass_no"] for e in passes] == [1, 2]


def test_the_ledger_records_how_many_hypotheses_each_pass_produced():
    ledger = Ledger(clock=lambda: NOW)
    build({
        "c": [Proposal(components=[comp("a", 500)]), Proposal(components=[comp("b", 600)])],
        "c:retry": [Proposal(components=[comp("mdr", 1140)])],
    }, ledger=ledger)
    passes = [e for e in ledger.entries if e.event_type == "HYPOTHESIS_PASS"]
    assert [e.payload["proposed"] for e in passes] == [2, 1]


def test_the_outcome_is_written_to_the_ledger():
    ledger = Ledger(clock=lambda: NOW)
    build({"c": [Proposal(components=[comp("a", 500)])]}, ledger=ledger)
    assert ledger.has("HYPOTHESIS_EXHAUSTED")


def test_a_resolved_divergence_records_the_verification_not_exhaustion():
    ledger = Ledger(clock=lambda: NOW)
    build({"c": [Proposal(components=[comp("mdr", 1140)])]}, ledger=ledger)
    assert ledger.has("HYPOTHESIS_VERIFIED")
    assert not ledger.has("HYPOTHESIS_EXHAUSTED")


def test_the_ledger_still_verifies_after_a_full_propose_verify_run():
    ledger = Ledger(clock=lambda: NOW)
    build({"c": [Proposal(components=[comp("a", 500)])]}, ledger=ledger)
    assert ledger.verify() == (True, None)


def test_passes_used_is_reported_on_the_packet():
    packet, _ = build({"c": [Proposal(components=[comp("a", 500)])]})
    assert packet.as_event()["passes_used"] == 2


@pytest.mark.parametrize("residual", [0, 1, 999_999])
def test_the_bound_holds_whatever_the_residual(residual):
    _, provider = build({"c": [Proposal(components=[comp("a", -1)])]}, residual=residual)
    assert provider.calls <= 2
