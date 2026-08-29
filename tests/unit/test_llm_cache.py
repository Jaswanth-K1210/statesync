"""The LLM response cache.

Constraint 4: every response is cached to disk, keyed by SHA-256 of the
prompt, and committed. This single mechanism is why the demo cannot fail
because of a rate limit, a network blip, or a provider outage.

Built in Phase 1 even though the first real LLM call is Phase 5 — retrofitting
a cache after the fact means the recorded responses are whatever the network
happened to return on the last run.
"""

import hashlib

import pytest

from statesync.llm.cache import LLMCache, LLMCacheMiss


@pytest.fixture
def cache(tmp_path):
    return LLMCache(cache_dir=tmp_path)


def test_key_is_derived_from_a_sha256_of_the_prompt(cache):
    assert cache.key("residual is 1140") == hashlib.sha256(b"residual is 1140").hexdigest()[:16]


def test_different_prompts_get_different_keys(cache):
    assert cache.key("a") != cache.key("b")


def test_miss_calls_the_provider_once_and_stores_the_response(cache):
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return "hypothesis-set"

    assert cache.call("p1", provider) == "hypothesis-set"
    assert calls == ["p1"]


def test_second_call_is_served_from_cache_without_touching_the_provider(cache):
    calls = []

    def provider(prompt):
        calls.append(prompt)
        return "hypothesis-set"

    cache.call("p1", provider)
    assert cache.call("p1", provider) == "hypothesis-set"
    assert calls == ["p1"], "the provider must not be called twice for one prompt"


def test_cache_persists_across_instances(cache, tmp_path):
    cache.call("p1", lambda _: "stored")
    fresh = LLMCache(cache_dir=tmp_path)
    assert fresh.get("p1") == "stored"


def test_miss_without_a_provider_raises_rather_than_returning_nothing(cache):
    """Smoke S5 runs with the network off. A miss must be loud, never a
    silent None that a caller mistakes for an empty hypothesis set."""
    with pytest.raises(LLMCacheMiss):
        cache.call("never-seen", provider=None)


def test_get_returns_none_on_a_miss(cache):
    assert cache.get("never-seen") is None


def test_cached_file_records_the_prompt_alongside_the_response(cache, tmp_path):
    """The cache is a committed artifact a reviewer can read."""
    cache.call("what explains 1140 paise?", lambda _: "mdr+gst")
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    body = written[0].read_text()
    assert "what explains 1140 paise?" in body and "mdr+gst" in body


def test_provider_exception_is_not_cached(cache):
    """A failed call must not poison the cache with a bad entry."""
    def failing(_):
        raise TimeoutError("provider down")

    with pytest.raises(TimeoutError):
        cache.call("p1", failing)
    assert cache.get("p1") is None


def test_hits_and_misses_are_counted_for_reporting(cache):
    cache.call("p1", lambda _: "x")
    cache.call("p1", lambda _: "x")
    cache.call("p2", lambda _: "y")
    assert cache.misses == 2
    assert cache.hits == 1
