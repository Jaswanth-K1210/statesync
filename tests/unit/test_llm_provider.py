"""The LLM hypothesis provider, behind the existing seam.

The verifier, the escalation packet and cases 13/14 were built and proven in
Phase 4 against fixtures. This adds a provider that asks a model instead, and
changes nothing else — that was the point of inverting the dependency.

**Cost accounting is the subtle part.** Every response is cached and committed
(constraint 4), so on any run after the first the cache serves everything:
calls read zero, arm 3 throughput approaches arm 2, and cost per 1,000 records
reads zero. All three of those measure cache hits, not generation. The provider
therefore counts `calls` (total) and `network_calls` (cache misses) separately,
and both are reported.
"""

import json

import pytest

from statesync.classifier.llm_provider import (
    LLMHypothesisProvider,
    parse_proposals,
)
from statesync.classifier.provider import HypothesisRequest
from statesync.classifier.verifier import ArtifactIndex
from statesync.llm.cache import LLMCache

ARTIFACTS = ArtifactIndex({"pay_1", "stl_1"})


def request(residual=1140, case="pay_1"):
    return HypothesisRequest(residual_paise=residual, instrument="card_domestic",
                             artifacts=ARTIFACTS, case_id=case)


def response(components):
    return json.dumps({"hypotheses": [{"components": components}]})


@pytest.fixture
def cache(tmp_path):
    return LLMCache(cache_dir=tmp_path)


# ── parsing ─────────────────────────────────────────────────────────────────

def test_a_well_formed_response_becomes_proposals():
    raw = response([{"name": "mdr", "amount_paise": 800, "cites": "pay_1"}])
    proposals = parse_proposals(raw)
    assert len(proposals) == 1
    assert proposals[0].components[0].amount_paise == 800


def test_malformed_json_yields_no_proposals_rather_than_raising():
    """A broken response degrades to 'nothing proposed', never to a crash."""
    assert parse_proposals("{'broken': ") == []


def test_a_response_that_is_not_an_object_yields_nothing():
    assert parse_proposals("[1, 2, 3]") == []


def test_a_float_amount_from_the_model_is_discarded():
    """Constraint 1 is enforced against model output, not assumed of it."""
    raw = response([{"name": "mdr", "amount_paise": 11.40, "cites": "pay_1"}])
    assert parse_proposals(raw) == []


def test_a_component_missing_its_amount_is_discarded():
    assert parse_proposals(response([{"name": "mdr", "cites": "pay_1"}])) == []


def test_extra_fields_from_the_model_are_ignored_not_fatal():
    raw = response([{"name": "mdr", "amount_paise": 800, "cites": "pay_1",
                     "confidence": "high", "explanation": "prose"}])
    assert len(parse_proposals(raw)) == 1


def test_the_model_cannot_supply_a_verdict():
    """A provider does not get to mark its own work correct."""
    raw = json.dumps({"hypotheses": [{
        "verdict": "VERIFIED",
        "components": [{"name": "mdr", "amount_paise": 800, "cites": "pay_1"}],
    }]})
    proposals = parse_proposals(raw)
    assert not hasattr(proposals[0], "verdict")


# ── cache-first behaviour ───────────────────────────────────────────────────

def test_a_cached_prompt_makes_no_network_call(cache):
    calls = []

    def client(prompt):
        calls.append(prompt)
        return response([{"name": "mdr", "amount_paise": 1140, "cites": "pay_1"}])

    provider = LLMHypothesisProvider(cache=cache, client=client)
    provider.propose(request())
    provider.propose(request())

    assert len(calls) == 1
    assert provider.calls == 2
    assert provider.network_calls == 1


def test_the_cold_and_warm_counts_are_distinguishable(cache):
    def client(_):
        return response([{"name": "mdr", "amount_paise": 1140, "cites": "pay_1"}])

    cold = LLMHypothesisProvider(cache=cache, client=client)
    cold.propose(request())
    assert cold.network_calls == 1

    warm = LLMHypothesisProvider(cache=LLMCache(cache_dir=cache.cache_dir), client=client)
    warm.propose(request())
    assert warm.network_calls == 0, "the warm run must be served entirely from cache"
    assert warm.calls == 1


def test_a_miss_with_no_client_raises_rather_than_returning_nothing(cache):
    """The demo runs with no key. A gap in the cache must be loud."""
    from statesync.llm.cache import LLMCacheMiss

    provider = LLMHypothesisProvider(cache=cache, client=None)
    with pytest.raises(LLMCacheMiss):
        provider.propose(request(case="never_cached"))


def test_the_prompt_contains_the_residual_and_the_instrument(cache):
    captured = []

    def client(prompt):
        captured.append(prompt)
        return response([])

    LLMHypothesisProvider(cache=cache, client=client).propose(request(residual=98765))
    assert "98765" in captured[0]
    assert "card_domestic" in captured[0]


def test_the_prompt_never_contains_the_known_components(cache):
    """Fee and tax are subtracted deterministically first. The model only ever
    sees the residual — it is not given the chance to re-explain a known fee."""
    captured = []

    def client(prompt):
        captured.append(prompt)
        return response([])

    LLMHypothesisProvider(cache=cache, client=client).propose(request(residual=1140))
    assert "fee_paise" not in captured[0]


def test_the_retry_prompt_carries_the_rejections(cache):
    captured = []

    def client(prompt):
        captured.append(prompt)
        return response([])

    provider = LLMHypothesisProvider(cache=cache, client=client)
    provider.propose(request().with_rejections(["H1 ARITHMETIC_FAILED: off by 60"]))
    assert "off by 60" in captured[0]


def test_two_identical_requests_hash_to_one_cache_entry(cache):
    def client(_):
        return response([])

    provider = LLMHypothesisProvider(cache=cache, client=client)
    provider.propose(request())
    provider.propose(request())
    assert len(list(cache.cache_dir.glob("*.json"))) == 1


def test_a_client_failure_falls_back_to_no_proposals(cache):
    """Degraded, not crashed. The deterministic path still escalates honestly."""
    def client(_):
        raise TimeoutError("provider down")

    provider = LLMHypothesisProvider(cache=cache, client=client, strict=False)
    assert provider.propose(request()) == []
    assert provider.degraded is True


def test_the_prompt_names_the_artifact_the_model_may_cite(cache):
    """Without this the model can only guess an id, and every proposal fails
    ARTIFACT_MISSING for a reason that says nothing about its reasoning."""
    captured = []

    def client(prompt):
        captured.append(prompt)
        return response([])

    LLMHypothesisProvider(cache=cache, client=client).propose(request(case="pay_xyz"))
    assert "payment_id: pay_xyz" in captured[0]
