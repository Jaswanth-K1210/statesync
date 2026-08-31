"""Test-session defaults.

**The suite never talks to a provider.** Adding a key to `.env` must not turn
a hermetic test run into hundreds of real API calls: that is slow, costs
money, and makes the build fail on someone else's rate limit. Live provider
behaviour is exercised by `make warm-cache` and by the manifest it writes,
not by the test suite.
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _never_call_a_real_provider() -> None:
    os.environ["STATESYNC_LLM_OFFLINE"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _never_overwrite_committed_artifacts(tmp_path_factory) -> None:
    """Packet writes go to a temp directory for the whole session.

    Running the suite was overwriting `eval/results/packets/` with test data,
    so `pytest` mutated the repo's committed artifacts as a side effect — and
    the API tests then failed against files their own suite had corrupted.
    Tests that assert on the committed packets read them; nothing writes them.
    """
    os.environ["STATESYNC_PACKETS_DIR"] = str(tmp_path_factory.mktemp("packets"))
