"""Escalation packets are written to disk, exactly as they were decided.

Until now packets were built in memory and thrown away — only the reason code
survived, into `exceptions.csv`. Nothing could show a reviewer the arithmetic
that produced a refusal, which is the one thing in this project that is
genuinely hard to read as text.

**Stored, not recomputed.** Every field is written as the verifier decided it:
the residual, each hypothesis's sum, its matched flag, its verdict, its
rejection reason. Anything the API or the UI recomputes is instance eight of
the pattern in `docs/seam_bugs.json`.
"""

import json

import pytest
from eval.arms import PACKETS_DIR, run_arm

from statesync.config import SEED

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def written(tmp_path_factory):
    out = tmp_path_factory.mktemp("packets")
    run_arm("rules", seed=SEED, n=500, rate=0.25, hard_cases=True, packets_dir=out)
    return {p.stem: json.loads(p.read_text()) for p in (out / "rules").glob("*.json")}


def test_a_packet_is_written_for_every_escalation(written):
    assert len(written) > 0
    assert "pay_hc13" in written and "pay_hc14" in written


def test_the_packet_carries_the_residual_as_decided(written):
    assert written["pay_hc13"]["residual_paise"] == 1140


def test_the_packet_carries_every_hypothesis_including_rejected(written):
    packet = written["pay_hc14"]
    assert len(packet["hypotheses"]) >= 5
    assert not [h for h in packet["hypotheses"] if h["verdict"] == "VERIFIED"]


def test_each_hypothesis_stores_its_arithmetic_rather_than_the_inputs(written):
    """The UI must render the sum, never compute it."""
    for hypothesis in written["pay_hc14"]["hypotheses"]:
        assert "sum_paise" in hypothesis
        assert "matched_residual" in hypothesis
        assert "arithmetic_shown" in hypothesis


def test_each_hypothesis_stores_its_verdict_and_reason(written):
    for hypothesis in written["pay_hc14"]["hypotheses"]:
        assert hypothesis["verdict"]
        if hypothesis["verdict"] != "VERIFIED":
            assert hypothesis["rejection_reason"]


def test_each_hypothesis_records_which_pass_produced_it(written):
    for hypothesis in written["pay_hc14"]["hypotheses"]:
        assert hypothesis["pass_no"] in (1, 2)


def test_citations_record_whether_each_artifact_resolved(written):
    for hypothesis in written["pay_hc14"]["hypotheses"]:
        for citation in hypothesis["citations"]:
            assert "artifact" in citation and "resolves" in citation


def test_the_ambiguous_packet_has_two_verified_with_different_components(written):
    packet = written["pay_hc13"]
    verified = [h for h in packet["hypotheses"] if h["verdict"] == "VERIFIED"]
    assert len(verified) == 2
    breakdowns = [sorted(c["name"] for c in h["components"]) for h in verified]
    assert breakdowns[0] != breakdowns[1]


def test_known_components_record_where_they_came_from(written):
    """What the system worked out before any model was involved."""
    packet = written["pay_hc13"]
    assert packet["known_components"]
    for component in packet["known_components"]:
        assert component["source"]


def test_the_packet_carries_the_templated_action(written):
    assert "no generated hypothesis verified" in (
        written["pay_hc14"]["suggested_action"].lower()
    )


def test_amounts_are_stored_as_integer_paise(written):
    for packet in written.values():
        assert isinstance(packet["residual_paise"], int)
        for hypothesis in packet["hypotheses"]:
            assert isinstance(hypothesis["sum_paise"], int)


def test_packets_are_written_to_the_committed_location_by_default():
    assert PACKETS_DIR.name == "packets"
    assert PACKETS_DIR.parent.name == "results"


def test_packets_are_namespaced_by_arm(tmp_path):
    """One directory for every arm is last-write-wins, and the harness runs
    arm 2 after arm 3 — the files then disagree with the headline."""
    run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True, packets_dir=tmp_path)
    run_arm("full", seed=SEED, n=200, rate=0.25, hard_cases=True, packets_dir=tmp_path)
    assert (tmp_path / "rules").is_dir() and (tmp_path / "full").is_dir()
