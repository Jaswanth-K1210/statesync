"""Escalation packets and the templated ops report.

An escalation that says only "ambiguous" hands the human back the same
under-determined problem the system just declined. Every packet carries the
residual, the components already subtracted, and **every** hypothesis —
verified and rejected alike — with its arithmetic shown and the reason it
failed.

The wording rule is absolute: the system may report *"no generated hypothesis
verified."* It may never claim *"no explanation exists."* Anything stronger is
a claim the architecture cannot support.
"""


from statesync.classifier.escalation import (
    FORBIDDEN_PHRASES,
    build_packet,
    suggested_action,
)
from statesync.classifier.provider import FixtureHypothesisProvider, HypothesisRequest
from statesync.classifier.verifier import ArtifactIndex, Component, Proposal
from statesync.models.enums import DivergenceClass, ReasonCode
from tests.unit.helpers import divergence_for

ARTIFACTS = ArtifactIndex({"pay_1", "stl_4471", "rfnd_real"})


def comp(name, amount, cites="pay_1", rate_bps=None):
    return Component(name=name, amount_paise=amount, cites=cites, rate_bps=rate_bps)


def packet_for(proposals, residual=1140, known=None):
    provider = FixtureHypothesisProvider({"c": proposals})
    request = HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                                artifacts=ARTIFACTS, case_id="c")
    return build_packet(
        divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
        known_components=known or [comp("fee", 8000), comp("tax", 1440)],
        residual_paise=residual, provider=provider, request=request,
    )


# ── exactly one verifies ────────────────────────────────────────────────────

def test_a_single_verified_hypothesis_resolves():
    packet = packet_for([Proposal(components=[comp("mdr", 800), comp("gst", 340)])])
    assert packet.reason_code == ReasonCode.VERIFIED
    assert len(packet.verified) == 1


# ── case 13: two verify ─────────────────────────────────────────────────────

def test_two_verified_hypotheses_escalate_as_ambiguous():
    """Case 13. The system must not pick one."""
    packet = packet_for([
        Proposal(components=[comp("mdr", 800), comp("gst", 340)]),
        Proposal(components=[comp("refund", 1000, cites="rfnd_real"), comp("mdr", 140)]),
    ])
    assert packet.reason_code == ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    assert len(packet.verified) == 2


def test_both_verified_hypotheses_are_attached_with_their_breakdowns():
    """The ops person distinguishes them without re-deriving anything."""
    packet = packet_for([
        Proposal(components=[comp("mdr", 800), comp("gst", 340)]),
        Proposal(components=[comp("refund", 1000, cites="rfnd_real"), comp("mdr", 140)]),
    ])
    names = [sorted(c.name for c in h.components) for h in packet.verified]
    assert names == [["gst", "mdr"], ["mdr", "refund"]]


# ── case 14: none verify ────────────────────────────────────────────────────

def test_no_verified_hypothesis_escalates_with_all_of_them_attached():
    """Case 14. Every rejected hypothesis travels with the escalation."""
    packet = packet_for([
        Proposal(components=[comp("a", 500)]),
        Proposal(components=[comp("b", 1200)]),
        Proposal(components=[comp("c", 1140, cites="rfnd_ghost")]),
        Proposal(components=[comp("mdr", 1140, rate_bps=1250)]),
        Proposal(components=[comp("d", 1139)]),
    ])
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert len(packet.hypotheses) == 5
    assert not packet.verified


def test_every_hypothesis_shows_its_arithmetic():
    packet = packet_for([Proposal(components=[comp("a", 500), comp("b", 400)])])
    assert all(h.arithmetic_shown for h in packet.hypotheses)


def test_every_rejected_hypothesis_carries_a_reason():
    packet = packet_for([
        Proposal(components=[comp("a", 500)]),
        Proposal(components=[comp("c", 1140, cites="rfnd_ghost")]),
    ])
    assert all(h.rejection_reason for h in packet.hypotheses)


def test_the_rejection_verdicts_are_distinguishable():
    packet = packet_for([
        Proposal(components=[comp("a", 500)]),
        Proposal(components=[comp("c", 1140, cites="rfnd_ghost")]),
        Proposal(components=[comp("mdr", 1140, rate_bps=1250)]),
    ])
    assert {h.verdict.value for h in packet.hypotheses} == {
        "ARITHMETIC_FAILED", "ARTIFACT_MISSING", "RANGE_VIOLATION",
    }


def test_hypotheses_are_labelled_h1_upward():
    packet = packet_for([Proposal(components=[comp("a", i)]) for i in range(3)])
    assert [h.label for h in packet.hypotheses] == ["H1", "H2", "H3"]


# ── no fee data ─────────────────────────────────────────────────────────────

def test_missing_fee_data_escalates_without_guessing_a_rate():
    packet = packet_for([], residual=1140, known=[])
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


# ── the wording rule ────────────────────────────────────────────────────────

def test_the_suggested_action_never_claims_no_explanation_exists():
    """The system can only conclude that no generated hypothesis verified."""
    for reason in ReasonCode:
        text = suggested_action(reason, residual_paise=1140, payment_id="pay_1")
        lowered = text.lower()
        for phrase in FORBIDDEN_PHRASES:
            assert phrase not in lowered, f"{reason.value} said: {text}"


def test_the_no_hypothesis_wording_is_exact():
    text = suggested_action(ReasonCode.NO_HYPOTHESIS_VERIFIED, residual_paise=1140,
                            payment_id="pay_1")
    assert "no generated hypothesis verified" in text.lower()


def test_every_reason_code_has_a_template():
    for reason in ReasonCode:
        assert suggested_action(reason, residual_paise=1, payment_id="p")


def test_the_action_is_templated_not_generated():
    """Every number comes from the ledger via a template. No model output
    produces any figure that reaches a human."""
    text = suggested_action(ReasonCode.NO_HYPOTHESIS_VERIFIED, residual_paise=98765,
                            payment_id="pay_xyz")
    assert "98765" in text and "pay_xyz" in text


def test_two_packets_for_one_input_are_identical():
    a = packet_for([Proposal(components=[comp("a", 500)])])
    b = packet_for([Proposal(components=[comp("a", 500)])])
    assert a.as_event() == b.as_event()


def test_a_packet_is_ledger_safe():
    from statesync.ledger.canonical import canonical
    canonical(packet_for([Proposal(components=[comp("a", 500)])]).as_event())


def test_the_packet_reports_the_residual_and_known_components():
    packet = packet_for([], residual=1140)
    assert packet.residual_paise == 1140
    assert [c.name for c in packet.known_components] == ["fee", "tax"]


def test_the_provider_is_called_at_most_twice():
    """Two passes, hard. Bounded before any model is involved."""
    provider = FixtureHypothesisProvider({"c": [Proposal(components=[comp("a", 500)])]})
    request = HypothesisRequest(residual_paise=1140, instrument="upi",
                                artifacts=ARTIFACTS, case_id="c")
    build_packet(divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
                 known_components=[], residual_paise=1140,
                 provider=provider, request=request)
    assert provider.calls <= 2


def test_the_second_pass_receives_the_rejections():
    seen: list[list[str]] = []

    class Recording(FixtureHypothesisProvider):
        def propose(self, request):
            seen.append(list(request.rejected_summaries))
            return super().propose(request)

    provider = Recording({"c": [Proposal(components=[comp("a", 500)])]})
    request = HypothesisRequest(residual_paise=1140, instrument="upi",
                                artifacts=ARTIFACTS, case_id="c")
    build_packet(divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
                 known_components=[], residual_paise=1140,
                 provider=provider, request=request)
    assert seen[0] == []
    assert seen[1], "pass 2 must be told what already failed"


def test_a_hallucinating_provider_is_rejected_wholesale():
    """The chaos case, available in Phase 4 because of the fixture seam.

    Every proposal cites an artifact that does not exist — exactly what a
    model that invents ids emits. All of them fail, none are accepted.
    """
    packet = packet_for([
        Proposal(components=[comp("refund", 1140, cites=f"rfnd_hallucinated_{i}")])
        for i in range(5)
    ])
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert all(h.verdict.value == "ARTIFACT_MISSING" for h in packet.hypotheses)
