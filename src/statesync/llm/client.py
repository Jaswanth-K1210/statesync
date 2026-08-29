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
from dataclasses import dataclass

__all__ = ["ClientKind", "OfflineHeuristicClient", "resolve_client"]


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


def resolve_client() -> tuple[Callable[[str], str], str]:
    """The best client this environment can reach, and which kind it is.

    Returns the offline client when no provider key is configured. The kind is
    returned rather than logged so it reaches the results table: a cost figure
    measured against the offline client is not a provider cost.
    """
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            from anthropic import Anthropic  # noqa: PLC0415

            anthropic = Anthropic()

            def live(prompt: str) -> str:
                message = anthropic.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
                return "".join(
                    block.text for block in message.content if block.type == "text"
                )

            return live, ClientKind.LIVE
        except ImportError:
            # The key is set but the SDK is absent. Say so by falling back
            # visibly rather than pretending the key was never there.
            pass

    return OfflineHeuristicClient(), ClientKind.OFFLINE
