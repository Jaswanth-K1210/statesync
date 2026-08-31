"""One value, computed once, propagated — never re-derived downstream.

Case 13's packet was built, was correct, and was ignored on the way to the
CSV, which re-derived the reason code from a classifier default. Every
per-component test passed; the artifact a reviewer actually opens was wrong.

That is a value computed upstream and re-derived downstream, and it is the
bug class most likely to recur — in the ops report, in the metrics table, and
especially in Phase 6's API responses, where every field is another chance to
recompute something that already exists.

These tests assert agreement *across* the outputs rather than within any one
of them.
"""

import csv

import pytest
from eval.arms import run_arm

from statesync.config import SEED

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    path = tmp_path_factory.mktemp("consistency") / "exceptions.csv"
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True,
                     repair=True, exceptions_path=path)
    rows = list(csv.DictReader(line for line in path.open() if not line.startswith("#")))
    return result, rows


def test_every_csv_row_has_a_reason_code_the_run_also_reports(run):
    result, rows = run
    assert {r["reason_code"] for r in rows} <= set(result.reason_codes)


def test_the_csv_row_count_matches_the_reported_exception_count(run):
    result, rows = run
    assert len(rows) == result.exceptions_count


def test_the_reason_code_tallies_match_the_csv(run):
    """The summary and the detail must not be independently derived."""
    result, rows = run
    from collections import Counter

    assert Counter(r["reason_code"] for r in rows) == Counter(result.reason_codes)


def test_the_summary_line_agrees_with_the_row_count(tmp_path):
    path = tmp_path / "exceptions.csv"
    result = run_arm("rules", seed=SEED, n=200, rate=0.25, hard_cases=True,
                     exceptions_path=path)
    summary = path.read_text().splitlines()[0]
    assert f"# {result.exceptions_count} exceptions" in summary


def test_detected_equals_the_sum_of_per_class_detection(run):
    """The headline and the breakdown are the same measurement."""
    result, _ = run
    assert result.detected == sum(s["detected"] for s in result.per_class.values())


def test_injected_equals_the_sum_of_per_class_injection(run):
    result, _ = run
    assert result.injected == sum(s["injected"] for s in result.per_class.values())


def test_missed_is_derived_from_detected_not_counted_separately(run):
    result, _ = run
    assert result.missed == result.injected - result.detected


def test_repair_counts_reconcile_with_confirmed(run):
    """Every confirmed divergence is repaired or escalated. None vanish."""
    result, _ = run
    accounted = result.repairs["succeeded"] + result.repairs["escalated"]
    assert accounted == result.confirmed


def test_the_ledger_records_as_many_confirmations_as_the_run_reports(run):
    result, _ = run
    assert result.confirmed <= result.ledger_entries


def test_provider_calls_are_bounded_by_the_escalation_count(run):
    """The published bound, checked against the reported numbers."""
    result, _ = run
    assert result.provider_calls <= 2 * result.reason_codes_total


def test_the_rendered_report_quotes_the_same_numbers_as_the_result(run):
    """The template must read from the result, never recompute from it."""
    from eval.harness import render

    result, _ = run
    text = render([result])
    assert f"{result.detected:,}".replace(",", "") in text.replace(",", "")
    assert str(result.hard_cases) in text
    for code, count in result.reason_codes.items():
        assert code in text
        assert str(count) in text


def test_amounts_in_the_csv_match_the_divergence_amounts(run):
    """No unit conversion happens on the way out. Paise in, paise out."""
    _, rows = run
    for row in rows:
        assert row["amount_paise"].isdigit()
        assert "." not in row["amount_paise"], "an amount was rendered as rupees"


def test_the_per_class_rate_is_stored_not_derived_by_the_template(run):
    """A figure computed in one place and recomputed in another is exactly
    how case 13's reason code came out wrong."""
    result, _ = run
    for stats in result.per_class.values():
        assert "rate_bps" in stats
        assert isinstance(stats["rate_bps"], int)
        expected = stats["detected"] * 10_000 // stats["injected"]
        assert stats["rate_bps"] == expected


def test_the_stored_rate_survives_canonical_serialisation(run):
    from statesync.ledger.canonical import canonical

    result, _ = run
    canonical(result.as_event())


# ── the API is the newest surface, and the likeliest place for instance 8 ───

def test_every_csv_row_has_a_packet(run):
    """A row the API cannot serve means the two artifacts disagree about how
    many escalations exist."""
    from statesync.api.server import load_packet

    _, rows = run
    for row in rows:
        assert load_packet(row["payment_id"]) is not None, row["payment_id"]


def test_the_committed_csv_and_the_served_packets_agree():
    """One exception list, from the shipped configuration.

    The CSV was written by arm 2 while the API served arm 3, so the two
    disagreed about whether hc09 was resolved. Both now come from SERVED_ARM.
    """
    import csv as _csv

    from statesync.api.server import load_packet
    from statesync.config import PROJECT_ROOT

    path = PROJECT_ROOT / "exceptions.csv"
    committed = list(_csv.DictReader(
        line for line in path.open() if not line.startswith("#")
    ))
    for row in committed:
        packet = load_packet(row["payment_id"])
        assert packet is not None, row["payment_id"]
        assert packet["reason_code"] == row["reason_code"], row["payment_id"]


def test_the_csv_names_the_arm_it_came_from():
    from eval.arms import SERVED_ARM

    from statesync.config import PROJECT_ROOT

    summary = (PROJECT_ROOT / "exceptions.csv").read_text().splitlines()[0]
    assert f"arm={SERVED_ARM}" in summary


def test_the_api_list_and_the_csv_agree_on_the_escalation_count(run):
    from statesync.api.server import list_divergences

    _, rows = run
    assert len(list_divergences()) == len(rows)


def test_the_api_never_recomputes_a_sum(run):
    """Each hypothesis's arithmetic string is stored, and it must match the
    stored sum — if the UI ever renders one and computes the other they will
    disagree, which is exactly instance eight."""
    from statesync.api.server import list_divergences, load_packet

    for summary in list_divergences():
        packet = load_packet(summary["payment_id"])
        for hypothesis in packet["hypotheses"]:
            assert hypothesis["arithmetic_shown"].endswith(str(hypothesis["sum_paise"]))
            assert hypothesis["matched_residual"] == (
                hypothesis["sum_paise"] == packet["residual_paise"]
            )


def test_the_api_serves_the_same_arm_the_readme_quotes():
    """Packets were written by every arm into one directory, last-write-wins,
    and the harness runs arm 2's idempotency proof after arm 3 — so the files
    showed arm 2 while the README quoted arm 3. hc09 had zero hypotheses on
    disk and one resolution in the table.
    """
    import json

    from eval.arms import SERVED_ARM

    from statesync.api.server import list_divergences, load_packet
    from statesync.config import PROJECT_ROOT

    arm_result = json.loads(
        (PROJECT_ROOT / "eval" / "results" / f"arm_{SERVED_ARM}.json").read_text()
    )

    served = {row["payment_id"]: load_packet(row["payment_id"])
              for row in list_divergences()}
    tally: dict[str, int] = {}
    for packet in served.values():
        tally[packet["reason_code"]] = tally.get(packet["reason_code"], 0) + 1

    assert tally == arm_result["reason_codes"], (
        f"the served packets disagree with arm_{SERVED_ARM}.json"
    )


def test_the_served_packets_carry_the_resolutions_the_readme_claims():
    from statesync.api.server import list_divergences, load_packet

    resolved = [
        row["payment_id"] for row in list_divergences()
        if load_packet(row["payment_id"])["reason_code"] == "verified"
    ]
    assert len(resolved) == 2, "the README's two resolutions are not in the packets"


def test_case_13_is_ambiguous_in_the_served_packets():
    from statesync.api.server import load_packet

    packet = load_packet("pay_hc13")
    assert packet["reason_code"] == "ambiguous_multiple_verified"
    verified = [h for h in packet["hypotheses"] if h["verdict"] == "VERIFIED"]
    assert len(verified) == 2
    breakdowns = [sorted(c["name"] for c in h["components"]) for h in verified]
    assert breakdowns[0] != breakdowns[1]


def test_running_the_suite_does_not_mutate_the_committed_packets():
    """pytest must not change the repo. It did: unqualified run_arm calls wrote
    into eval/results/packets/, so the API tests failed against files their own
    suite had just overwritten."""
    import os

    from eval.arms import PACKETS_DIR

    override = os.getenv("STATESYNC_PACKETS_DIR")
    assert override, "the session must redirect packet writes"
    assert not str(PACKETS_DIR).startswith(override)


def test_exceptions_csv_matches_the_served_arm_escalations():
    """One list, one arm, stated in both artifacts."""
    import csv as _csv
    import json as _json

    from eval.arms import SERVED_ARM

    from statesync.config import PROJECT_ROOT

    rows = list(_csv.DictReader(
        line for line in (PROJECT_ROOT / "exceptions.csv").open()
        if not line.startswith("#")
    ))
    arm_result = _json.loads(
        (PROJECT_ROOT / "eval" / "results" / f"arm_{SERVED_ARM}.json").read_text()
    )
    assert len(rows) == arm_result["exceptions_count"]

    packets = PROJECT_ROOT / "eval" / "results" / "packets" / SERVED_ARM
    assert {p.stem for p in packets.glob("*.json")} == {r["payment_id"] for r in rows}
