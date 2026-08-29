"""The committed exception list.

The published bar asks for "throughput plus measured accuracy plus an honest
exception list". This writes the third one. It is committed to the repo, so
it is a claim a reviewer opens rather than a number they take on trust.

Rows are sorted by divergence key: the file lives in git, and two runs of the
same seed must not produce a spurious diff.
"""

from __future__ import annotations

import csv
from pathlib import Path

from statesync.models.domain import Divergence
from statesync.models.enums import ReasonCode

__all__ = ["EXCEPTION_COLUMNS", "write_exceptions_csv"]

EXCEPTION_COLUMNS = [
    "divergence_key",
    "class",
    "reason_code",
    "payment_id",
    "order_id",
    "amount_paise",
    "observed_at",
    "detail",
]


def write_exceptions_csv(
    path: Path,
    divergences: list[Divergence],
    reasons: dict[str, ReasonCode] | None = None,
    detected: int | None = None,
    context: str = "",
) -> int:
    """Write every unresolved divergence. Returns the row count.

    An empty run still writes the header — an empty file and a missing file
    mean different things, and only one of them means "nothing was left over".

    The file opens with a `#` summary line so it is self-describing. A
    zero-row file reads as a bug to anyone who does not have the test suite
    open; one that says "0 exceptions of 124 divergences detected" reads as
    the result it is. The line restates measured counts and makes no new claim.
    """
    reasons = reasons or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(divergences, key=lambda d: d.deterministic_key())

    total = detected if detected is not None else len(rows)
    summary = f"# {len(rows)} exceptions of {total} divergences detected"
    if context:
        summary += f" ({context})"

    with path.open("w", newline="", encoding="utf-8") as fh:
        fh.write(summary + "\n")
        writer = csv.DictWriter(fh, fieldnames=EXCEPTION_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for divergence in rows:
            key = divergence.deterministic_key()
            writer.writerow({
                "divergence_key": key,
                "class": divergence.klass.value,
                # Absent a specific outcome the row is unexplained, not verified.
                "reason_code": reasons.get(key, ReasonCode.NO_HYPOTHESIS_VERIFIED).value,
                "payment_id": divergence.payment_id or "",
                "order_id": divergence.order_id or "",
                "amount_paise": divergence.amount_paise,
                "observed_at": divergence.observed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "detail": ";".join(f"{k}={v}" for k, v in sorted(divergence.detail.items())),
            })
    return len(rows)
