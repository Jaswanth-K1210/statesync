"""Populate the committed LLM cache.

    python -m eval.warm_cache

Every response the eval needs is written to `llm_cache/` and committed, so a
fresh clone reproduces the run with no network and no API key. Smoke rung S5
asserts that completeness rather than trusting it.

**Which client filled the cache matters and is recorded.** With no provider key
configured this uses the offline heuristic client, which is not a model. A cost
or throughput figure measured against it is not a provider cost, and the
manifest says so in the file rather than leaving it to be inferred.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from eval.arms import run_arm
from statesync.classifier.llm_provider import LLMHypothesisProvider
from statesync.config import CACHE_DIR, SEED
from statesync.llm.cache import LLMCache
from statesync.llm.client import ClientKind, resolve_client
from statesync.llm.providers import STATS

MANIFEST = CACHE_DIR / "MANIFEST.json"


def _model_for(answered_by: str) -> str:
    """Which model produced these responses. '8 calls, 22s' means nothing
    without it, and the model was switched during diagnosis."""
    import os

    return {
        "groq": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "openrouter": os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
    }.get(answered_by, answered_by)


def _when(measured: bool, cold: dict[str, Any]) -> str:
    return "measured now" if measured else f"preserved from {cold.get('measured_at')}"


def _previous() -> dict[str, Any]:
    """The last manifest, so a warm run can carry its cold figures forward."""
    if MANIFEST.exists():
        loaded: dict[str, Any] = json.loads(MANIFEST.read_text())
        return loaded
    return {}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(prog="eval.warm_cache")
    parser.add_argument(
        "--rebuild", action="store_true",
        help="delete cached responses first and take a true cold measurement",
    )
    args = parser.parse_args(argv)

    previous = _previous()
    STATS.reset()
    if args.rebuild:
        for entry in CACHE_DIR.glob("*.json"):
            if entry.name not in {"MANIFEST.json", "OBSERVED_EVENTS.json"}:
                entry.unlink()

    # Build the client here and hand it to the run. Probing one client and
    # measuring another reports the chain that never ran — the same bug class
    # as case 13, where a value was computed upstream and re-derived below.
    client, kind = resolve_client()
    provider = LLMHypothesisProvider(cache=LLMCache(), client=client)

    # Time the successful provider calls separately from everything else.
    provider_ns = 0
    inner = client

    def timed(prompt: str) -> str:
        nonlocal provider_ns
        at = time.perf_counter_ns()
        try:
            return inner(prompt)
        finally:
            provider_ns += time.perf_counter_ns() - at

    provider.client = timed

    started = time.perf_counter()
    result = run_arm("full", seed=SEED, n=500, rate=0.25, hard_cases=True,
                     provider=provider)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    provider_ms = provider_ns // 1_000_000
    answered = getattr(client, "answered_by", kind)

    entries = sorted(
        p.name for p in CACHE_DIR.glob("*.json")
        if p.name not in {"MANIFEST.json", "OBSERVED_EVENTS.json"}
    )
    # A run against an already-warm cache makes no provider calls, so its
    # timings are all zero. Writing those over a real cold measurement destroys
    # the only figure a README may quote — so a warm run preserves the previous
    # cold numbers and says when they were taken. `--rebuild` forces a fresh one.
    cold_measured = result.network_calls > 0
    cold = {
        "provider_calls_cold": result.network_calls,
        "cold_wall_clock_ms": elapsed_ms,
        # Request time only. Backoff and chain overhead are reported beside
        # it rather than folded into it.
        "provider_latency_ms": STATS.request_ns // 1_000_000,
        "mean_request_ms": STATS.mean_request_ms,
        "http_requests": STATS.requests,
        "retries": STATS.retries,
        "backoff_ms": STATS.backoff_ns // 1_000_000,
        "chain_overhead_ms": max(elapsed_ms - provider_ms, 0),
        "model": _model_for(answered),
        "answered_by": answered,
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    } if cold_measured else dict(previous.get("cold", {}))

    MANIFEST.write_text(
        json.dumps({
            "populated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "client_kind": kind,
            "seed": SEED,
            "records": 500,
            "entries": len(entries),
            "chain": getattr(client, "names", [kind]),
            "chain_failures": getattr(client, "failures", []),
            "cold_measured_this_run": cold_measured,
            # Wall clock alone is contaminated by failed links and their
            # backoff. A README quotes generation cost, so the two are split.
            "cold": cold,
            "note": (
                "Populated by the offline heuristic client, which is NOT a model. "
                "Cold provider cost is unmeasured until an API key is configured."
                if kind == ClientKind.OFFLINE else
                "Populated from a live provider. Cold cost figures are real."
            ),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    print(  # noqa: T201
        f"cache: {len(entries)} entries, client={kind}, "
        f"cold={_when(cold_measured, cold)}, "
        f"provider calls={result.network_calls}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
