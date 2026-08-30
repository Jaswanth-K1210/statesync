"""The suite must never talk to a provider.

The guarantee currently lives in one `conftest.py` line. A future edit could
delete it and every other test would still pass — while `make verify` quietly
became network-bound and billable, and "fresh clone -> make verify green"
became a lie on someone else's machine.

These tests make that removal fail loudly.
"""

import os

from statesync.llm.client import ClientKind, resolve_client


def test_the_offline_switch_is_set_for_this_session():
    """If conftest's autouse fixture is removed, this is what says so."""
    assert os.getenv("STATESYNC_LLM_OFFLINE") == "1", (
        "the test session is not pinned offline — check tests/conftest.py"
    )


def test_resolving_a_client_inside_a_test_never_reaches_a_provider():
    """Even with real keys in .env, resolution must stay offline."""
    _, kind = resolve_client()
    assert kind == ClientKind.OFFLINE


def test_the_resolved_client_is_the_deterministic_one():
    from statesync.llm.client import OfflineHeuristicClient

    client, _ = resolve_client()
    assert isinstance(client, OfflineHeuristicClient)


def test_conftest_still_declares_the_autouse_fixture():
    """Belt and braces: the mechanism itself is asserted, not just its effect,
    so deleting the fixture fails here even if something else sets the var."""
    from pathlib import Path

    conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    body = conftest.read_text()
    assert "autouse=True" in body
    assert "STATESYNC_LLM_OFFLINE" in body
