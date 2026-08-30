"""The cold cost measurement must survive a warm run.

A run against an already-warm cache makes no provider calls, so every timing
it produces is zero. Writing those over a real cold measurement destroys the
only figure a README may honestly quote — and it did, silently, once.

It also must not fold retry backoff into provider latency. Wrapping the retry
helper and timing that counts `sleep()` as generation cost, inflating the mean
by however long a rate limit happened to last.
"""

import json

from statesync.config import CACHE_DIR
from statesync.llm.providers import RequestStats


def manifest() -> dict:
    return json.loads((CACHE_DIR / "MANIFEST.json").read_text())


def test_the_manifest_carries_a_cold_measurement():
    assert manifest()["cold"]["provider_calls_cold"] > 0


def test_the_cold_measurement_names_its_model():
    """'8 calls, 16s' means nothing without the model that produced them."""
    assert manifest()["cold"]["model"]


def test_the_cold_measurement_is_dated():
    assert manifest()["cold"]["measured_at"].endswith("Z")


def test_backoff_is_reported_separately_from_request_time():
    cold = manifest()["cold"]
    assert "backoff_ms" in cold and "provider_latency_ms" in cold
    assert cold["mean_request_ms"] > 0


def test_the_timings_reconcile_with_the_wall_clock():
    """Request time plus backoff plus chain overhead accounts for the run."""
    cold = manifest()["cold"]
    parts = cold["provider_latency_ms"] + cold["backoff_ms"] + cold["chain_overhead_ms"]
    assert abs(parts - cold["cold_wall_clock_ms"]) < 500


def test_retries_are_visible_rather_than_hidden_in_the_mean():
    cold = manifest()["cold"]
    assert cold["http_requests"] >= cold["provider_calls_cold"]
    assert cold["retries"] == cold["http_requests"] - cold["provider_calls_cold"]


def test_request_stats_reset_cleanly():
    stats = RequestStats(request_ns=5, requests=2, retries=1, backoff_ns=9)
    stats.reset()
    assert (stats.request_ns, stats.requests, stats.retries, stats.backoff_ns) == (0, 0, 0, 0)


def test_the_mean_never_divides_by_zero():
    assert RequestStats().mean_request_ms == 0
