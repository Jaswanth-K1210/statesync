"""Test-session defaults.

**The suite never talks to a provider.** Adding a key to `.env` must not turn
a hermetic test run into hundreds of real API calls: that is slow, costs
money, and makes the build fail on someone else's rate limit. Live provider
behaviour is exercised by `make warm-cache` and by the manifest it writes,
not by the test suite.
"""

import hashlib
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

COMMITTED_ARTIFACTS = (
    "README.md",
    "exceptions.csv",
    "eval/results",
    "llm_cache",
)
"""The evidence. A suite that edits these is editing what it checks."""


def _fingerprint() -> dict[str, str]:
    digests: dict[str, str] = {}
    for entry in COMMITTED_ARTIFACTS:
        path = REPO / entry
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for file in files:
            if file.is_file():
                digests[str(file.relative_to(REPO))] = hashlib.sha256(
                    file.read_bytes()
                ).hexdigest()
    return digests


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


@pytest.fixture(scope="session", autouse=True)
def _the_suite_must_not_edit_the_evidence():
    """The most serious instance of the seam pattern, made impossible.

    Running the suite used to overwrite `eval/results/packets/`, so pytest
    mutated the artifacts it then asserted against — the evidence was being
    edited by the thing that checks it. Given the submission rests on "clone
    this and reproduce every number", that is the one bug that could have
    invalidated the reproduction claim itself.

    Fingerprints are compared rather than `git status`, so this holds whatever
    uncommitted work is in the tree.
    """
    before = _fingerprint()
    yield
    after = _fingerprint()

    changed = sorted(
        name for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    )
    assert not changed, (
        "the test suite modified committed artifacts:\n  "
        + "\n  ".join(changed)
    )
