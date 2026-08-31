"""`exceptions.csv` — the honest exception list the bar asks for.

"Throughput plus measured accuracy plus an honest exception list." This is the
third of those. It is committed to the repo, so it is a claim a reviewer can
open rather than a number they have to believe.
"""

import csv
from datetime import UTC, datetime

from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass, ReasonCode
from statesync.reporting.exceptions_csv import EXCEPTION_COLUMNS, write_exceptions_csv

NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, pid="pay_1", amount=400000):
    return Divergence(klass=klass, payment_id=pid, order_id=None,
                      amount_paise=amount, observed_at=NOW)


def read(path):
    """Skip the leading `#` comment lines the way any CSV consumer would."""
    with path.open(newline="", encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.startswith("#")]
    return list(csv.DictReader(lines))


def test_writes_one_row_per_exception(tmp_path):
    path = tmp_path / "exceptions.csv"
    assert write_exceptions_csv(path, [divergence(pid="pay_1"), divergence(pid="pay_2")]) == 2
    assert len(read(path)) == 2


def test_header_includes_the_reason_code(tmp_path):
    """A list without reasons is a list of shrugs."""
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence()])
    assert "reason_code" in EXCEPTION_COLUMNS
    assert read(path)[0]["reason_code"]


def test_amount_is_written_as_integer_paise_not_rupees(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence(amount=389420)])
    assert read(path)[0]["amount_paise"] == "389420"


def test_carries_the_divergence_key_so_rows_join_to_the_ledger(tmp_path):
    path = tmp_path / "exceptions.csv"
    d = divergence()
    write_exceptions_csv(path, [d])
    assert read(path)[0]["divergence_key"] == d.deterministic_key()


def test_explicit_reason_codes_are_preserved(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence()],
                         reasons={divergence().deterministic_key():
                                  ReasonCode.NO_HYPOTHESIS_VERIFIED})
    assert read(path)[0]["reason_code"] == "no_hypothesis_verified"


def test_rows_are_sorted_for_a_stable_diff(tmp_path):
    """The file is committed, so two runs must not produce a spurious diff."""
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence(pid="pay_z"), divergence(pid="pay_a")])
    keys = [r["divergence_key"] for r in read(path)]
    assert keys == sorted(keys)


def test_an_empty_run_still_writes_a_header(tmp_path):
    """An empty file and a missing file mean different things."""
    path = tmp_path / "exceptions.csv"
    assert write_exceptions_csv(path, []) == 0
    header = next(ln for ln in path.read_text().splitlines() if not ln.startswith("#"))
    assert header == ",".join(EXCEPTION_COLUMNS)


def test_timestamps_are_iso8601_utc(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence()])
    assert read(path)[0]["observed_at"] == "2026-09-05T12:00:00Z"


def test_writing_twice_produces_an_identical_file(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_exceptions_csv(a, [divergence(pid="pay_1"), divergence(pid="pay_2")])
    write_exceptions_csv(b, [divergence(pid="pay_1"), divergence(pid="pay_2")])
    assert a.read_bytes() == b.read_bytes()


def test_file_describes_itself_so_empty_does_not_read_as_broken(tmp_path):
    """A header-only file looks like a bug to anyone without the test open."""
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [], detected=124, context="arm=rules, clean-only batch")
    first = path.read_text().splitlines()[0]
    assert first.startswith("#")
    assert "0 exceptions" in first and "124 divergences" in first
    assert "arm=rules" in first


def test_the_comment_line_is_not_mistaken_for_data(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence()], detected=124)
    assert len(read(path)) == 1


def test_summary_counts_reflect_actual_rows(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence(pid="a"), divergence(pid="b")], detected=10)
    assert "2 exceptions of 10 divergences" in path.read_text().splitlines()[0]


def test_the_file_explains_that_a_correct_refusal_produces_no_row(tmp_path):
    """Someone opening this file alone must not need the README to learn that
    an absent row can be the right outcome — case 7 is the case the project is
    proudest of and it appears here as nothing at all."""
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [], detected=124)
    body = path.read_text()
    assert "Correct refusals produce NO row" in body
    assert "Case 7" in body
