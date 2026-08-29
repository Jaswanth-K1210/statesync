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
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


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
    assert path.read_text().strip() == ",".join(EXCEPTION_COLUMNS)


def test_timestamps_are_iso8601_utc(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions_csv(path, [divergence()])
    assert read(path)[0]["observed_at"] == "2026-09-05T12:00:00Z"


def test_writing_twice_produces_an_identical_file(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    write_exceptions_csv(a, [divergence(pid="pay_1"), divergence(pid="pay_2")])
    write_exceptions_csv(b, [divergence(pid="pay_1"), divergence(pid="pay_2")])
    assert a.read_bytes() == b.read_bytes()
