"""Fee resolution — deterministic, and before any inference runs.

    1. `payment.fee_paise` + `payment.tax_paise`   authoritative
    2. a versioned schedule keyed by instrument, with effective dates
    3. neither -> FEE_SCHEDULE_UNKNOWN

**Never guess a rate.** The point of the propose-verify layer is to explain a
residual, and a guessed fee manufactures a residual that never existed —
handing the model a fabricated problem to solve. When the fee is unknown the
system says so, names the config that would resolve it, and escalates.

Coverage is reported as a first-class metric because the alternative is
dishonest: a system claiming 95% verification while silently escalating the
40% it could not price has not verified 95% of anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from statesync.config import PROJECT_ROOT
from statesync.models.domain import Payment

__all__ = [
    "FeeResolution", "FeeSchedule", "FeeSource", "load_fee_schedule", "resolve_fee",
]

FEE_SCHEDULE_PATH = PROJECT_ROOT / "config" / "fee_schedule.yaml"


class FeeSource(StrEnum):
    PAYMENT_OBJECT = "payment_object"
    SCHEDULE = "schedule"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FeeRate:
    mdr_bps: int
    gst_bps: int


@dataclass(frozen=True)
class FeeSchedule:
    version: int
    effective_from: date
    rates: dict[str, FeeRate]

    def rate_for(self, instrument: str, at: datetime) -> FeeRate | None:
        """The rate in force for `instrument` at `at`, or None.

        A schedule that is not yet effective returns None rather than being
        applied retroactively — a future rate charged against a past payment
        invents a discrepancy.
        """
        if at.date() < self.effective_from:
            return None
        return self.rates.get(instrument)


@dataclass(frozen=True)
class FeeResolution:
    source: FeeSource
    total_paise: int | None
    mdr_bps: int | None = None
    gst_bps: int | None = None
    schedule_version: int = 0
    needed_config: str = ""

    @property
    def resolved(self) -> bool:
        return self.source != FeeSource.UNKNOWN

    def as_event(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "total_paise": self.total_paise if self.total_paise is not None else -1,
            "mdr_bps": self.mdr_bps or 0,
            "gst_bps": self.gst_bps or 0,
            "schedule_version": self.schedule_version,
            "needed_config": self.needed_config,
        }


def load_fee_schedule(path: Path = FEE_SCHEDULE_PATH) -> FeeSchedule:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    effective = raw["effective_from"]
    if isinstance(effective, str):
        effective = date.fromisoformat(effective)
    return FeeSchedule(
        version=int(raw["version"]),
        effective_from=effective,
        rates={
            name: FeeRate(mdr_bps=int(v["mdr_bps"]), gst_bps=int(v["gst_bps"]))
            for name, v in raw["rates"].items()
        },
    )


def resolve_fee(payment: Payment, schedule: FeeSchedule,
                at: datetime | None = None) -> FeeResolution:
    """Resolve the gateway's fee for one payment, without ever guessing."""
    at = at or datetime.now(UTC)

    # Source 1 — the gateway told us. Zero is an answer, not a missing value,
    # so this branch tests for presence rather than truthiness.
    if payment.fee_paise is not None:
        return FeeResolution(
            source=FeeSource.PAYMENT_OBJECT,
            total_paise=payment.fee_paise + (payment.tax_paise or 0),
        )

    # Source 2 — the configured schedule, if it was in force at the time.
    rate = schedule.rate_for(payment.instrument, at)
    if rate is not None:
        mdr = payment.amount_paise * rate.mdr_bps // 10_000
        gst = mdr * rate.gst_bps // 10_000
        return FeeResolution(
            source=FeeSource.SCHEDULE, total_paise=mdr + gst,
            mdr_bps=rate.mdr_bps, gst_bps=rate.gst_bps,
            schedule_version=schedule.version,
        )

    # Source 3 — say so, and say what would fix it.
    return FeeResolution(
        source=FeeSource.UNKNOWN, total_paise=None,
        needed_config=(
            f"fee_schedule.rates.{payment.instrument} "
            f"(effective on or before {at.date().isoformat()})"
        ),
    )
