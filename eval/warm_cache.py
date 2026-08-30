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

from eval.arms import run_arm
from statesync.classifier.llm_provider import LLMHypothesisProvider
from statesync.config import CACHE_DIR, SEED
from statesync.llm.cache import LLMCache
from statesync.llm.client import ClientKind, resolve_client

MANIFEST = CACHE_DIR / "MANIFEST.json"


def main() -> int:
    import time

    # Build the client here and hand it to the run. Probing one client and
    # measuring another reports the chain that never ran — the same bug class
    # as case 13, where a value was computed upstream and re-derived below.
    client, kind = resolve_client()
    provider = LLMHypothesisProvider(cache=LLMCache(), client=client)

    started = time.perf_counter()
    result = run_arm("full", seed=SEED, n=500, rate=0.25, hard_cases=True,
                     provider=provider)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    entries = sorted(p.name for p in CACHE_DIR.glob("*.json") if p.name != "MANIFEST.json")
    MANIFEST.write_text(
        json.dumps({
            "populated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "client_kind": kind,
            "seed": SEED,
            "records": 500,
            "entries": len(entries),
            "provider_calls_cold": result.network_calls,
            "cold_wall_clock_ms": elapsed_ms,
            "answered_by": getattr(client, "answered_by", kind),
            "chain": getattr(client, "names", [kind]),
            "chain_failures": getattr(client, "failures", []),
            "note": (
                "Populated by the offline heuristic client, which is NOT a model. "
                "Cold provider cost is unmeasured until an API key is configured."
                if kind == ClientKind.OFFLINE else
                "Populated from a live provider. Cold cost figures are real."
            ),
        }, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"cache: {len(entries)} entries, client={kind}, "  # noqa: T201
          f"cold provider calls={result.network_calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
