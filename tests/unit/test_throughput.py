"""Throughput instrumentation.

"Throughput" is the first word of the published bar. It is measured from the
first arm rather than retrofitted at the end, because a number added on the
last day is a number nobody trusts.

Durations are stored as integer microseconds. Rates are floats, computed only
at render time — that keeps constraint 1 true by construction, so a timing
figure can never break the hash chain by reaching an event body as a float.
"""

from statesync.ledger.canonical import canonical
from statesync.metrics.throughput import Stopwatch


def scripted_clock(*ticks_us: int):
    """A fake nanosecond clock that walks a scripted sequence."""
    values = iter([t * 1000 for t in ticks_us])
    return lambda: next(values)


def test_counts_the_records_it_measured():
    watch = Stopwatch(clock=scripted_clock(0, 0, 10, 10, 20, 20, 30, 30))
    for _ in range(3):
        with watch.record():
            pass
    assert watch.report().records == 3


def test_wall_clock_is_measured_end_to_end():
    watch = Stopwatch(clock=scripted_clock(100, 150, 200, 400))
    with watch.record():
        pass
    assert watch.report().wall_clock_us == 300


def test_percentiles_come_from_the_per_record_samples():
    # ten records of 1..10 microseconds
    ticks = [0]
    t = 0
    for d in range(1, 11):
        ticks += [t, t + d]
        t += d
    watch = Stopwatch(clock=scripted_clock(*ticks, t))
    for _ in range(10):
        with watch.record():
            pass
    report = watch.report()
    assert report.p50_us == 5
    assert report.p99_us == 10


def test_p99_is_never_below_p50():
    watch = Stopwatch(clock=scripted_clock(0, 0, 7, 7, 2, 2, 99, 99))
    for _ in range(3):
        with watch.record():
            pass
    report = watch.report()
    assert report.p99_us >= report.p50_us


def test_every_stored_duration_is_an_integer():
    """A float in a duration field would break the chain the moment it was
    written to the ledger."""
    watch = Stopwatch(clock=scripted_clock(0, 0, 10, 20))
    with watch.record():
        pass
    report = watch.report()
    for value in (report.records, report.wall_clock_us, report.p50_us, report.p99_us):
        assert isinstance(value, int) and not isinstance(value, bool)


def test_report_as_event_survives_canonical_serialisation():
    """The direct assertion: a throughput report is ledger-safe."""
    watch = Stopwatch(clock=scripted_clock(0, 0, 10, 20))
    with watch.record():
        pass
    canonical(watch.report().as_event())  # raises TypeError on any float


def test_records_per_sec_is_derived_at_render_time():
    watch = Stopwatch(clock=scripted_clock(0, 0, 10, 1_000_000))
    with watch.record():
        pass
    assert watch.report().records_per_sec == 1.0


def test_an_empty_run_reports_zeros_rather_than_dividing_by_zero():
    report = Stopwatch(clock=scripted_clock(0, 0)).report()
    assert report.records == 0
    assert report.records_per_sec == 0.0
    assert report.p50_us == 0 and report.p99_us == 0


def test_a_raising_body_still_records_the_sample():
    """A failed record is still a record. Dropping it would flatter the p99."""
    watch = Stopwatch(clock=scripted_clock(0, 0, 40, 40))
    try:
        with watch.record():
            raise ValueError("boom")
    except ValueError:
        pass
    assert watch.report().records == 1
