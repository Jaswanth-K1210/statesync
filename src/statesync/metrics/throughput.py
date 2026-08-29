"""Throughput measurement: records/sec, wall clock, p50 and p99 per record.

"Throughput" is the first word of the published bar, so it is instrumented
from the first arm rather than bolted on at the end.

**Durations are integer microseconds.** Rates are floats derived at render
time and never stored. That is not fussiness: a float in a stored field would
eventually reach an event body, and a float in an event body makes the hash
chain non-reproducible across environments. Keeping the stored form integral
means a timing number can never break the audit trail.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Stopwatch", "ThroughputReport"]


@dataclass(frozen=True)
class ThroughputReport:
    records: int
    wall_clock_us: int
    p50_us: int
    p99_us: int

    @property
    def wall_clock_s(self) -> float:
        return self.wall_clock_us / 1_000_000

    @property
    def records_per_sec(self) -> float:
        if self.wall_clock_us == 0:
            return 0.0
        return self.records * 1_000_000 / self.wall_clock_us

    def as_event(self) -> dict[str, Any]:
        """Integers only — safe to write straight into the ledger."""
        return {
            "records": self.records,
            "wall_clock_us": self.wall_clock_us,
            "p50_us": self.p50_us,
            "p99_us": self.p99_us,
        }


def _percentile(samples: list[int], pct: int) -> int:
    """Nearest-rank percentile. Integer in, integer out — no interpolation,
    so the reported figure is always a duration that actually occurred."""
    if not samples:
        return 0
    ordered = sorted(samples)
    rank = max(1, (pct * len(ordered) + 99) // 100)
    return ordered[min(rank, len(ordered)) - 1]


@dataclass
class Stopwatch:
    """Measures a run and each record inside it."""

    clock: Callable[[], int] = time.perf_counter_ns
    _samples: list[int] = field(default_factory=list)
    _started_ns: int | None = None
    _last_ns: int | None = None

    def __post_init__(self) -> None:
        self._started_ns = self.clock()

    @contextmanager
    def record(self) -> Iterator[None]:
        """Time one record. A raising body is still recorded — dropping the
        failures would flatter the p99 exactly where it matters most."""
        start = self.clock()
        try:
            yield
        finally:
            end = self.clock()
            self._last_ns = end
            self._samples.append((end - start) // 1000)

    def report(self) -> ThroughputReport:
        started = self._started_ns if self._started_ns is not None else 0
        end = self.clock()
        return ThroughputReport(
            records=len(self._samples),
            wall_clock_us=max((end - started) // 1000, 0),
            p50_us=_percentile(self._samples, 50),
            p99_us=_percentile(self._samples, 99),
        )
