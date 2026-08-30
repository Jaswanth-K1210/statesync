"""Provider clients, and the fallback chain.

    primary -> secondary -> deterministic (DEGRADED_MODE)

`resolve_client()` returns whatever this environment can actually reach. With
no API key configured that is the offline client, and the caller is told so
rather than left to infer it from a suspiciously fast run.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from statesync.llm.providers import (
    ProviderError,
    apply_dotenv,
    groq_client,
    openrouter_client,
)

__all__ = [
    "ClientKind", "FallbackClient", "OfflineHeuristicClient", "resolve_client",
]


class ClientKind:
    LIVE = "live"
    OFFLINE = "offline"


@dataclass
class OfflineHeuristicClient:
    """A deterministic stand-in for a model. **This is not a model.**

    It proposes decompositions of a residual using fixed heuristics — an
    MDR/GST split, a rounding term, a refund-plus-fee split — so the pipeline,
    the cache and the verifier can all be exercised end to end without a
    provider key.

    Everything downstream treats its output exactly as it would a model's: the
    verifier accepts nothing that does not reconcile to the paisa and cite a
    real artifact, and several of these heuristics are deliberately wrong so
    that rejection paths are exercised too.

    Any run whose cache was populated by this client must say so. The cold
    generation cost of a real provider cannot be inferred from it.
    """

    kind: str = ClientKind.OFFLINE

    def __call__(self, prompt: str) -> str:
        residual = _residual_from(prompt)
        payment_id = _field_from(prompt, "payment_id") or "unknown"
        gst_share = residual * 1800 // 11800
        mdr_share = residual - gst_share

        hypotheses = [
            # An MDR/GST split that reconciles exactly.
            {"components": [
                {"name": "mdr", "amount_paise": mdr_share, "cites": payment_id, "rate_bps": 200},
                {"name": "gst", "amount_paise": gst_share, "cites": payment_id, "rate_bps": 1800},
            ]},
            # Deliberately short by a paisa: exercises ARITHMETIC_FAILED.
            {"components": [
                {"name": "rounding", "amount_paise": residual - 1, "cites": payment_id},
            ]},
            # Cites an artifact that does not exist: exercises ARTIFACT_MISSING.
            {"components": [
                {"name": "refund", "amount_paise": residual, "cites": "rfnd_unverified"},
            ]},
        ]
        return json.dumps({"hypotheses": hypotheses})


def _residual_from(prompt: str) -> int:
    value = _field_from(prompt, "residual_paise")
    return int(value) if value else 0


def _field_from(prompt: str, key: str) -> str:
    for line in prompt.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip()
    return ""


@dataclass
class FallbackClient:
    """primary -> secondary -> ... Each link is tried in order.

    A link that raises, or returns nothing, has not answered. Every failure is
    recorded rather than swallowed: a run that quietly fell through to the
    secondary produces different numbers, and the results table has to be able
    to say which provider actually answered.
    """

    links: list[tuple[str, Callable[[str], str]]]
    answered_by: str = ""
    failures: list[tuple[str, str]] = field(default_factory=list)
    disabled: set[str] = field(default_factory=set)
    """Links that failed terminally — a bad key, no credit, an unknown model.
    Re-probing those on every prompt costs a round trip each time and buries
    the real error under a hundred identical ones."""

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.links]

    def __call__(self, prompt: str) -> str:
        for name, call in self.links:
            if name in self.disabled:
                continue
            try:
                response = call(prompt)
            except ProviderError as exc:
                self.failures.append((name, f"HTTP {exc.status}"))
                if exc.terminal:
                    self.disabled.add(name)
                continue
            except Exception as exc:
                self.failures.append((name, type(exc).__name__))
                continue
            if not response:
                self.failures.append((name, "EmptyResponse"))
                continue
            self.answered_by = name
            return response

        raise RuntimeError(
            f"every provider failed: {self.failures}"
        )


def resolve_client() -> tuple[Callable[[str], str], str]:
    """The best client this environment can reach, and which kind it is.

    Returns the offline client when no provider key is configured, or when
    `STATESYNC_LLM_OFFLINE=1`. The kind is returned rather than logged so it
    reaches the results table: a cost figure measured against the offline
    client is not a provider cost.
    """
    # A hard offline switch, checked before anything else. The test suite sets
    # it so that adding a key to .env cannot silently turn a hermetic suite
    # into one that makes hundreds of real API calls with backoff — which is
    # slow, costs money, and makes the tests fail on someone else's rate limit.
    if os.getenv("STATESYNC_LLM_OFFLINE") == "1":
        return OfflineHeuristicClient(), ClientKind.OFFLINE

    apply_dotenv()

    links: list[tuple[str, Callable[[str], str]]] = []

    # Groq is primary. Ordering is by observed capacity, not by preference:
    # OpenRouter authenticates but has no credit on this account, so putting it
    # first spends a round trip on a 402 before every single call.
    #
    # The fallback path is NOT proven by that accident. It is proven by
    # tests/chaos/test_provider_fallback.py, which injects a 402 deterministically
    # — so the property survives someone topping up the account, or reordering
    # this list again.
    groq_key = os.getenv("GROQ_API_KEY")
    if groq_key:
        links.append((
            "groq",
            groq_client(groq_key, os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")),
        ))

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        links.append((
            "openrouter",
            openrouter_client(
                openrouter_key,
                os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
            ),
        ))

    if links:
        return FallbackClient(links), ClientKind.LIVE

    return OfflineHeuristicClient(), ClientKind.OFFLINE
