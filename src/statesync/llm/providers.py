"""Provider transports, and loading credentials from `.env`.

OpenRouter and Groq are both OpenAI-compatible, so one POST serves both. The
transport is `urllib` from the standard library rather than a HTTP dependency:
the demo never makes a network call at all, and adding a dependency that only
the cache-warming path uses would be paid for on every clean install.

**Credentials are read from the environment, never from source.** `.env` is
gitignored and is only a convenience for local runs — a real environment
variable always wins, so CI secrets are never shadowed by a stale file.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from statesync.config import PROJECT_ROOT

__all__ = [
    "MAX_ATTEMPTS", "STATS", "ProviderError", "RequestStats", "apply_dotenv",
    "groq_client", "load_dotenv", "openrouter_client", "with_retries",
]


class ProviderError(RuntimeError):
    """A provider returned an HTTP error. Carries the status so the fallback
    chain can tell a transient failure from one that will not fix itself."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

DOTENV_PATH = PROJECT_ROOT / ".env"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT_SECONDS = 60
MAX_TOKENS = int(os.getenv("STATESYNC_LLM_MAX_TOKENS", "1024"))

@dataclass
class RequestStats:
    """Time spent in HTTP requests, excluding retry backoff.

    Wrapping the retry helper and timing that instead counts `sleep()` as
    provider latency, which inflates the mean by however long a rate limit
    happened to last. A README quotes generation cost, so the request
    itself is what gets measured.
    """

    request_ns: int = 0
    requests: int = 0
    retries: int = 0
    backoff_ns: int = 0

    def reset(self) -> None:
        self.request_ns = self.requests = self.retries = self.backoff_ns = 0

    @property
    def mean_request_ms(self) -> int:
        return self.request_ns // 1_000_000 // max(self.requests, 1)


STATS = RequestStats()
"""Process-wide. Only the cache-warming path reads it."""


USER_AGENT = "StateSync/0.1 (+https://github.com/statesync)"
"""Sent on every request. Several providers sit behind a CDN that rejects
`Python-urllib/x.y` outright, which surfaces as a 403 with a CDN error code
and looks exactly like a bad credential. Identifying properly avoids a
diagnosis that would otherwise start by rotating a perfectly good key."""

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (1.0, 2.0, 4.0)
"""Warming the cache is a burst of prompts, which is exactly what a free tier
rate-limits. Failing over on the first 429 wastes the primary and then fails
the whole run for a condition that clears in about a second."""

TERMINAL_STATUSES = frozenset({401, 402, 403, 404})
"""Failures that will not fix themselves within a run: bad credential, no
credit, forbidden, unknown model. Retrying these on every one of a hundred
prompts wastes a second each time and buries the real error."""


def load_dotenv(path: Path = DOTENV_PATH) -> dict[str, str]:
    """Parse a `.env` file. A missing file is not an error."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # Split once only: base64 and JWT-shaped secrets contain '=', and
        # splitting on every one truncates the key into an auth failure that
        # looks like a wrong credential rather than a parsing bug.
        key, _, value = stripped.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:  # an unfilled placeholder must not shadow a real variable
            values[key.strip()] = value
    return values


def apply_dotenv(path: Path = DOTENV_PATH) -> None:
    """Load `.env` into the environment without overriding what is set.

    A real environment variable always wins. CI injects secrets that way, and
    a stale local file silently taking precedence is a debugging afternoon.
    """
    for key, value in load_dotenv(path).items():
        os.environ.setdefault(key, value)


def with_retries(
    call: Callable[[str], str],
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[str], str]:
    """Retry a transient provider failure with growing backoff.

    Terminal failures — a bad key, no credit, an unknown model — are raised
    immediately: waiting four seconds to be told the same thing again helps
    nobody and hides the real error.
    """
    def attempt(prompt: str) -> str:
        last: ProviderError | None = None
        for i in range(MAX_ATTEMPTS):
            try:
                return call(prompt)
            except ProviderError as exc:
                if exc.terminal:
                    raise
                last = exc
                if i < len(BACKOFF_SECONDS):
                    STATS.retries += 1
                    slept = time.perf_counter_ns()
                    sleep(BACKOFF_SECONDS[i])
                    STATS.backoff_ns += time.perf_counter_ns() - slept
        assert last is not None
        raise last

    return attempt


def _post_chat(url: str, api_key: str, model: str, prompt: str,
               extra_headers: dict[str, str] | None = None) -> str:
    """One OpenAI-compatible chat completion. Returns the message text."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,   # diversity across the five pass-1 hypotheses
        "max_tokens": MAX_TOKENS,
    }).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **(extra_headers or {}),
    }
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")

    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        STATS.request_ns += time.perf_counter_ns() - started
        STATS.requests += 1
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ProviderError(exc.code, f"HTTP {exc.code}: {detail}") from exc
    STATS.request_ns += time.perf_counter_ns() - started
    STATS.requests += 1

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"provider returned no choices: {payload.get('error', payload)}")
    content: str = choices[0].get("message", {}).get("content", "")
    return content


def openrouter_client(api_key: str, model: str) -> Callable[[str], str]:
    @with_retries
    def call(prompt: str) -> str:
        return _post_chat(
            OPENROUTER_URL, api_key, model, prompt,
            # OpenRouter asks callers to identify themselves; omitting these
            # works but lands in a stricter rate-limit bucket.
            extra_headers={
                "HTTP-Referer": "https://github.com/statesync",
                "X-Title": "StateSync",
            },
        )

    return call


def groq_client(api_key: str, model: str) -> Callable[[str], str]:
    @with_retries
    def call(prompt: str) -> str:
        return _post_chat(GROQ_URL, api_key, model, prompt)

    return call
