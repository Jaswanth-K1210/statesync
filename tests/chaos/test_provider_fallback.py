"""The fallback chain, proven deterministically.

A live run once showed OpenRouter returning 402 and Groq answering. That was
real, and it is recorded in `llm_cache/MANIFEST.json` as a dated observation —
but it is **not** how the property is proven. Relying on it would mean the
chain's most interesting behaviour becomes unobservable the moment someone
tops up the account or reorders the links.

So the failure is injected here instead: primary refuses, secondary answers,
both refuse and the system degrades. The property holds regardless of chain
order, account balance, or which provider is configured.
"""

import pytest
from tests.unit.helpers import divergence_for

from statesync.classifier.escalation import build_packet
from statesync.classifier.llm_provider import LLMHypothesisProvider
from statesync.classifier.provider import HypothesisRequest
from statesync.classifier.verifier import ArtifactIndex
from statesync.llm.cache import LLMCache
from statesync.llm.client import FallbackClient, OfflineHeuristicClient
from statesync.llm.providers import ProviderError
from statesync.models.enums import DivergenceClass, ReasonCode

pytestmark = pytest.mark.chaos

ARTIFACTS = ArtifactIndex({"pay_1"})
GOOD = '{"hypotheses": [{"components": [{"name": "adj", "amount_paise": 1140, "cites": "pay_1"}]}]}'


def out_of_credit(_):
    raise ProviderError(402, "This request requires more credits")


def rate_limited(_):
    raise ProviderError(429, "rate limited")


# ── primary out of credit ───────────────────────────────────────────────────

def test_a_402_on_the_primary_falls_through_to_the_secondary():
    chain = FallbackClient([("primary", out_of_credit), ("secondary", lambda _: GOOD)])
    assert chain("prompt") == GOOD
    assert chain.answered_by == "secondary"
    assert chain.failures == [("primary", "HTTP 402")]


def test_the_secondary_result_still_reaches_the_verifier(tmp_path):
    """Falling through must not change what happens downstream."""
    chain = FallbackClient([("primary", out_of_credit), ("secondary", lambda _: GOOD)])
    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=tmp_path), client=chain)

    packet = build_packet(
        divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
        known_components=[], residual_paise=1140, provider=provider,
        request=HypothesisRequest(residual_paise=1140, instrument="upi",
                                  artifacts=ARTIFACTS, case_id="pay_1"),
    )
    assert packet.reason_code == ReasonCode.VERIFIED


def test_the_exhausted_primary_is_not_probed_again(tmp_path):
    probes = []

    def counting_402(prompt):
        probes.append(prompt)
        raise ProviderError(402, "no credit")

    chain = FallbackClient([("primary", counting_402), ("secondary", lambda _: GOOD)])
    for _ in range(5):
        chain("prompt")
    assert len(probes) == 1


# ── both links gone ─────────────────────────────────────────────────────────

def test_both_providers_failing_degrades_rather_than_crashing(tmp_path):
    chain = FallbackClient([("primary", out_of_credit), ("secondary", out_of_credit)])
    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=tmp_path),
                                     client=chain, strict=False)
    assert provider.propose(
        HypothesisRequest(residual_paise=1140, instrument="upi",
                          artifacts=ARTIFACTS, case_id="pay_1")
    ) == []
    assert provider.degraded is True


def test_a_fully_degraded_run_still_escalates_honestly(tmp_path):
    chain = FallbackClient([("primary", out_of_credit), ("secondary", out_of_credit)])
    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=tmp_path),
                                     client=chain, strict=False)

    packet = build_packet(
        divergence=divergence_for(DivergenceClass.AMOUNT_MISMATCH),
        known_components=[], residual_paise=1140, provider=provider,
        request=HypothesisRequest(residual_paise=1140, instrument="upi",
                                  artifacts=ARTIFACTS, case_id="pay_1"),
    )
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert "no generated hypothesis verified" in packet.suggested_action.lower()


def test_the_deterministic_client_is_the_last_resort():
    """primary -> secondary -> deterministic. The third link never fails."""
    chain = FallbackClient([
        ("primary", out_of_credit),
        ("secondary", out_of_credit),
        ("deterministic", OfflineHeuristicClient()),
    ])
    assert chain("residual_paise: 1140\npayment_id: pay_1")
    assert chain.answered_by == "deterministic"


# ── transient vs terminal ───────────────────────────────────────────────────

def test_a_rate_limited_primary_is_retried_on_a_later_prompt():
    attempts = []

    def flaky(prompt):
        attempts.append(prompt)
        raise ProviderError(429, "slow down")

    chain = FallbackClient([("primary", flaky), ("secondary", lambda _: GOOD)])
    chain("one")
    chain("two")
    assert len(attempts) == 2
    assert "primary" not in chain.disabled


def test_the_chain_records_every_failure_for_the_results_table():
    chain = FallbackClient([("primary", out_of_credit), ("secondary", rate_limited),
                            ("tertiary", lambda _: GOOD)])
    chain("prompt")
    assert chain.failures == [("primary", "HTTP 402"), ("secondary", "HTTP 429")]
    assert chain.answered_by == "tertiary"
