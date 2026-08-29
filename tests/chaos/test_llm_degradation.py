"""Degradation paths for the AI layer.

Most submissions pretend nothing broke. These are the paths where something
does, and every one of them ends in an honest escalation rather than a crash
or, worse, a fabricated explanation.

The fallback chain is: primary -> secondary -> deterministic, marked DEGRADED.
"""

import json

import pytest
from tests.unit.helpers import NOW, divergence_for

from statesync.classifier.escalation import build_packet
from statesync.classifier.llm_provider import LLMHypothesisProvider, parse_proposals
from statesync.classifier.provider import HypothesisRequest
from statesync.classifier.verifier import ArtifactIndex
from statesync.ledger.chain import Ledger
from statesync.llm.cache import LLMCache
from statesync.models.enums import DivergenceClass, ReasonCode

pytestmark = pytest.mark.chaos

ARTIFACTS = ArtifactIndex({"pay_1"})


def request():
    return HypothesisRequest(residual_paise=1140, instrument="card_domestic",
                             artifacts=ARTIFACTS, case_id="pay_1")


def packet_with(provider, ledger=None):
    return build_packet(
        divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
        known_components=[], residual_paise=1140,
        provider=provider, request=request(), ledger=ledger, base_paise=400_000,
    )


@pytest.fixture
def cache(tmp_path):
    return LLMCache(cache_dir=tmp_path)


# ── malformed output ────────────────────────────────────────────────────────

def test_malformed_json_escalates_rather_than_crashing(cache):
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: "{'broken': ")
    assert packet_with(provider).reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_an_invalid_class_in_the_response_is_ignored(cache):
    """The model does not get to name a divergence class."""
    raw = json.dumps({"hypotheses": [], "klass": "BANANA"})
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: raw)
    assert packet_with(provider).reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_prose_instead_of_json_escalates(cache):
    provider = LLMHypothesisProvider(
        cache=cache, client=lambda _: "I think the fee is about eleven rupees."
    )
    assert packet_with(provider).reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_an_empty_response_escalates(cache):
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: "")
    assert packet_with(provider).reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


def test_a_response_with_a_float_amount_is_discarded(cache):
    raw = json.dumps({"hypotheses": [{"components": [
        {"name": "mdr", "amount_paise": 11.40, "cites": "pay_1"}]}]})
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: raw)
    assert packet_with(provider).reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED


# ── transport failures ──────────────────────────────────────────────────────

def test_a_timeout_degrades_rather_than_raising(cache):
    def client(_):
        raise TimeoutError("provider down")

    provider = LLMHypothesisProvider(cache=cache, client=client, strict=False)
    packet = packet_with(provider)
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert provider.degraded is True


def test_degradation_is_recorded_not_swallowed(cache):
    def client(_):
        raise ConnectionError("network unreachable")

    provider = LLMHypothesisProvider(cache=cache, client=client, strict=False)
    packet_with(provider)
    assert provider.as_event()["degraded"] is True


def test_a_degraded_run_never_claims_no_explanation_exists(cache):
    from statesync.classifier.escalation import FORBIDDEN_PHRASES

    provider = LLMHypothesisProvider(cache=cache, client=lambda _: "", strict=False)
    text = packet_with(provider).suggested_action.lower()
    assert not [p for p in FORBIDDEN_PHRASES if p in text]


# ── a hallucinating provider ────────────────────────────────────────────────

def test_invented_artifact_ids_are_rejected_wholesale(cache):
    """The structural guarantee: arithmetic alone never suffices."""
    raw = json.dumps({"hypotheses": [
        {"components": [{"name": "refund", "amount_paise": 1140,
                         "cites": f"rfnd_invented_{i}"}]}
        for i in range(5)
    ]})
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: raw)
    packet = packet_with(provider)
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert all(h.verdict.value == "ARTIFACT_MISSING" for h in packet.hypotheses)


def test_a_plausible_but_inconsistent_rate_is_rejected(cache):
    """The subtler hallucination: a rate in range but wrong for its amount."""
    raw = json.dumps({"hypotheses": [{"components": [
        {"name": "mdr", "amount_paise": 1140, "cites": "pay_1", "rate_bps": 200}]}]})
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: raw)
    packet = packet_with(provider)
    assert packet.hypotheses[0].verdict.value == "RATE_INCONSISTENT"


# ── bounds hold under failure ───────────────────────────────────────────────

def test_generation_stays_bounded_when_everything_fails(cache):
    calls = []

    def client(prompt):
        calls.append(prompt)
        return ""

    provider = LLMHypothesisProvider(cache=cache, client=client)
    packet_with(provider)
    assert provider.calls <= 2


def test_a_provider_returning_fifty_hypotheses_is_capped(cache):
    raw = json.dumps({"hypotheses": [
        {"components": [{"name": "x", "amount_paise": i, "cites": "pay_1"}]}
        for i in range(50)
    ]})
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: raw)
    assert len(packet_with(provider).hypotheses) <= 10


def test_the_ledger_still_verifies_after_a_degraded_run(cache):
    ledger = Ledger(clock=lambda: NOW)
    provider = LLMHypothesisProvider(cache=cache, client=lambda _: "", strict=False)
    packet_with(provider, ledger=ledger)
    assert ledger.verify() == (True, None)


def test_parse_never_raises_on_arbitrary_bytes():
    for junk in ("", "null", "[]", "{}", "\\x00\\xff", '{"hypotheses": "not a list"}'):
        assert parse_proposals(junk) == []
