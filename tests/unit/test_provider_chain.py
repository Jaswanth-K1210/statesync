"""The provider fallback chain: primary -> secondary -> deterministic.

The brief's §3 calls for one primary provider, one fallback, then a
deterministic classifier marked DEGRADED. Only the last link existed until
now. These tests drive the chain itself; no network is touched.
"""

import pytest

from statesync.llm.client import ClientKind, FallbackClient, resolve_client
from statesync.llm.providers import load_dotenv

# ── .env loading ────────────────────────────────────────────────────────────

def test_dotenv_loads_key_value_pairs(tmp_path):
    path = tmp_path / ".env"
    path.write_text("FOO=bar\nBAZ=qux\n")
    assert load_dotenv(path) == {"FOO": "bar", "BAZ": "qux"}


def test_dotenv_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# a comment\n\nFOO=bar\n\n#BAZ=qux\n")
    assert load_dotenv(path) == {"FOO": "bar"}


def test_dotenv_keeps_values_containing_equals(tmp_path):
    """Base64 and JWT-shaped secrets contain '='. Splitting on every one
    truncates the key and produces an authentication failure that looks like
    a wrong key rather than a parsing bug."""
    path = tmp_path / ".env"
    path.write_text("TOKEN=abc=def==\n")
    assert load_dotenv(path) == {"TOKEN": "abc=def=="}


def test_dotenv_strips_surrounding_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text('FOO="bar"\nBAZ=\'qux\'\n')
    assert load_dotenv(path) == {"FOO": "bar", "BAZ": "qux"}


def test_dotenv_skips_empty_values(tmp_path):
    """An unfilled placeholder must not shadow a real environment variable."""
    path = tmp_path / ".env"
    path.write_text("FOO=\nBAR=set\n")
    assert load_dotenv(path) == {"BAR": "set"}


def test_a_missing_dotenv_is_not_an_error(tmp_path):
    assert load_dotenv(tmp_path / "nope") == {}


def test_dotenv_never_overrides_a_real_environment_variable(tmp_path, monkeypatch):
    """CI sets real secrets in the environment; a stale .env must not win."""
    monkeypatch.setenv("FOO", "from-environment")
    path = tmp_path / ".env"
    path.write_text("FOO=from-file\n")
    from statesync.llm.providers import apply_dotenv

    apply_dotenv(path)
    import os

    assert os.environ["FOO"] == "from-environment"


# ── the chain ───────────────────────────────────────────────────────────────

def test_the_primary_answers_when_it_can():
    chain = FallbackClient([("primary", lambda _: "ok"), ("secondary", lambda _: "no")])
    assert chain("prompt") == "ok"
    assert chain.answered_by == "primary"


def test_the_secondary_answers_when_the_primary_fails():
    def down(_):
        raise ConnectionError("primary unreachable")

    chain = FallbackClient([("primary", down), ("secondary", lambda _: "ok")])
    assert chain("prompt") == "ok"
    assert chain.answered_by == "secondary"


def test_a_failed_primary_is_recorded_not_swallowed():
    def down(_):
        raise ConnectionError("primary unreachable")

    chain = FallbackClient([("primary", down), ("secondary", lambda _: "ok")])
    chain("prompt")
    assert chain.failures == [("primary", "ConnectionError")]


def test_every_link_failing_raises_rather_than_returning_nothing():
    def down(_):
        raise TimeoutError("gone")

    chain = FallbackClient([("primary", down), ("secondary", down)])
    with pytest.raises(RuntimeError, match="every provider failed"):
        chain("prompt")


def test_an_empty_response_is_treated_as_a_failure():
    """A provider that returns nothing has not answered."""
    chain = FallbackClient([("primary", lambda _: ""), ("secondary", lambda _: "ok")])
    assert chain("prompt") == "ok"
    assert chain.answered_by == "secondary"


def test_the_chain_reports_which_providers_are_configured():
    chain = FallbackClient([("openrouter", lambda _: "ok")])
    assert chain.names == ["openrouter"]


# ── resolution ──────────────────────────────────────────────────────────────

def test_no_keys_resolves_to_the_offline_client(monkeypatch):
    monkeypatch.delenv("STATESYNC_LLM_OFFLINE", raising=False)
    for var in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("statesync.llm.client.apply_dotenv", lambda *a, **k: None)
    _, kind = resolve_client()
    assert kind == ClientKind.OFFLINE


def test_a_configured_key_resolves_to_a_live_chain(monkeypatch):
    # The session runs offline by default; lift it for this one assertion.
    monkeypatch.delenv("STATESYNC_LLM_OFFLINE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setattr("statesync.llm.client.apply_dotenv", lambda *a, **k: None)
    client, kind = resolve_client()
    assert kind == ClientKind.LIVE
    assert "openrouter" in client.names


def test_both_keys_resolve_to_a_two_link_chain(monkeypatch):
    monkeypatch.delenv("STATESYNC_LLM_OFFLINE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr("statesync.llm.client.apply_dotenv", lambda *a, **k: None)
    client, _ = resolve_client()
    assert client.names == ["openrouter", "groq"], "primary then secondary"


# ── terminal failures are not retried ───────────────────────────────────────

def test_a_terminal_failure_disables_that_link_for_the_run():
    """A bad key, no credit, or an unknown model will not fix itself mid-run.

    Re-probing costs a round trip on every one of a hundred prompts and buries
    the real error under a hundred identical ones.
    """
    from statesync.llm.providers import ProviderError

    calls = []

    def broke(_):
        calls.append(1)
        raise ProviderError(402, "no credit")

    chain = FallbackClient([("primary", broke), ("secondary", lambda _: "ok")])
    chain("first")
    chain("second")
    chain("third")

    assert len(calls) == 1, "a terminal failure was retried"
    assert "primary" in chain.disabled


def test_a_transient_failure_is_retried_on_the_next_prompt():
    """A 429 or a 5xx may well succeed a moment later."""
    from statesync.llm.providers import ProviderError

    calls = []

    def flaky(_):
        calls.append(1)
        raise ProviderError(429, "rate limited")

    chain = FallbackClient([("primary", flaky), ("secondary", lambda _: "ok")])
    chain("first")
    chain("second")

    assert len(calls) == 2, "a retryable failure was wrongly disabled"
    assert "primary" not in chain.disabled


def test_the_status_code_is_recorded_not_just_the_exception_type():
    """'HTTP 402' tells you to add credit. 'ProviderError' tells you nothing."""
    from statesync.llm.providers import ProviderError

    def broke(_):
        raise ProviderError(402, "no credit")

    chain = FallbackClient([("primary", broke), ("secondary", lambda _: "ok")])
    chain("prompt")
    assert chain.failures == [("primary", "HTTP 402")]


def test_terminal_statuses_are_the_ones_that_cannot_self_heal():
    from statesync.llm.providers import TERMINAL_STATUSES

    assert TERMINAL_STATUSES == {401, 402, 403, 404}


def test_a_provider_error_reports_whether_it_is_terminal():
    from statesync.llm.providers import ProviderError

    assert ProviderError(402, "x").terminal is True
    assert ProviderError(429, "x").terminal is False


def test_every_link_disabled_raises_rather_than_going_quiet():
    from statesync.llm.providers import ProviderError

    def broke(_):
        raise ProviderError(401, "bad key")

    chain = FallbackClient([("primary", broke), ("secondary", broke)])
    with pytest.raises(RuntimeError, match="every provider failed"):
        chain("prompt")


def test_requests_identify_themselves():
    """Several providers sit behind a CDN that rejects Python-urllib outright,
    which surfaces as a 403 and looks exactly like a bad credential."""
    from statesync.llm.providers import USER_AGENT

    assert "StateSync" in USER_AGENT
    assert "urllib" not in USER_AGENT.lower()


# ── rate limits need backoff, not immediate failure ─────────────────────────

def test_a_rate_limit_is_retried_with_backoff():
    """Warming the cache is a burst of prompts, which is exactly what a free
    tier rate-limits. Failing over on the first 429 wastes the primary and
    then fails the run for a condition that clears in a second."""
    from statesync.llm.providers import ProviderError, with_retries

    attempts = []

    def flaky(prompt):
        attempts.append(prompt)
        if len(attempts) < 3:
            raise ProviderError(429, "rate limited")
        return "ok"

    assert with_retries(flaky, sleep=lambda _: None)("p") == "ok"
    assert len(attempts) == 3


def test_a_terminal_failure_is_not_retried():
    from statesync.llm.providers import ProviderError, with_retries

    attempts = []

    def broke(prompt):
        attempts.append(prompt)
        raise ProviderError(402, "no credit")

    with pytest.raises(ProviderError):
        with_retries(broke, sleep=lambda _: None)("p")
    assert len(attempts) == 1, "a terminal failure must not be retried"


def test_retries_are_bounded():
    from statesync.llm.providers import MAX_ATTEMPTS, ProviderError, with_retries

    attempts = []

    def always_limited(prompt):
        attempts.append(prompt)
        raise ProviderError(429, "rate limited")

    with pytest.raises(ProviderError):
        with_retries(always_limited, sleep=lambda _: None)("p")
    assert len(attempts) == MAX_ATTEMPTS


def test_backoff_grows_between_attempts():
    from statesync.llm.providers import ProviderError, with_retries

    delays = []

    def always_limited(_):
        raise ProviderError(429, "rate limited")

    with pytest.raises(ProviderError):
        with_retries(always_limited, sleep=delays.append)("p")
    assert delays == sorted(delays) and delays[0] < delays[-1]


def test_the_offline_switch_beats_a_configured_key(monkeypatch):
    """Adding a key to .env must not turn a hermetic suite into one that makes
    hundreds of real API calls with backoff."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("STATESYNC_LLM_OFFLINE", "1")
    _, kind = resolve_client()
    assert kind == ClientKind.OFFLINE


def test_the_test_suite_runs_offline_by_default():
    """conftest sets this for the whole session."""
    import os

    assert os.getenv("STATESYNC_LLM_OFFLINE") == "1"
