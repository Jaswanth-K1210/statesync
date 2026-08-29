"""Disk-backed LLM response cache, keyed by SHA-256 of the prompt.

Constraint 4. The cache directory is committed to the repo, so a fresh clone
replays every response the eval needs without a network call. The demo cannot
fail because of a rate limit, a blip, or a provider outage.

A miss with no provider raises `LLMCacheMiss` rather than returning None. That
is deliberate: smoke S5 runs with the network disabled and asserts the cached
path is complete, and a silent None would let an empty hypothesis set look
like a legitimate "nothing verified" result.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from statesync.config import CACHE_DIR

__all__ = ["LLMCache", "LLMCacheMiss"]


class LLMCacheMiss(RuntimeError):
    """Raised when a prompt is absent from the cache and no provider is set."""


class LLMCache:
    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

    def _path(self, prompt: str) -> Path:
        return self.cache_dir / f"{self.key(prompt)}.json"

    def get(self, prompt: str) -> str | None:
        path = self._path(prompt)
        if not path.exists():
            return None
        response: str = json.loads(path.read_text(encoding="utf-8"))["response"]
        return response

    def put(self, prompt: str, response: str) -> Path:
        path = self._path(prompt)
        # The prompt is stored beside the response so the committed cache is
        # readable evidence rather than an opaque blob of hashes.
        path.write_text(
            json.dumps({"prompt": prompt, "response": response}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def call(self, prompt: str, provider: Callable[[str], str] | None = None) -> str:
        cached = self.get(prompt)
        if cached is not None:
            self.hits += 1
            return cached
        if provider is None:
            raise LLMCacheMiss(
                f"prompt {self.key(prompt)} is not in {self.cache_dir} and no provider "
                f"is configured — the cached path is incomplete"
            )
        self.misses += 1
        # A raised provider error propagates uncached: a failed call must not
        # poison the committed cache with a bad entry.
        response = provider(prompt)
        self.put(prompt, response)
        return response
