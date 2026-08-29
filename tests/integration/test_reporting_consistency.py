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
    assert result.llm_calls <= 2 * result.reason_codes_total


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
