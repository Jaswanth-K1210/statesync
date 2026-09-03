"""The settlement evaluation. Separate run, separate results, separate report.

Never merged into the record-level arm tables: this is measured against payout
data that was manufactured here, and folding it into a headline would make the
headline mean less than it does.

    python -m eval.settlement
"""

from __future__ import annotations

import json
from typing import Any

from statesync.classifier.settlement import settlement_gap_report
from statesync.config import PROJECT_ROOT, SEED
from statesync.generator.payouts import GAP_KINDS, generate_payouts
from statesync.models.enums import ReasonCode
from statesync.reconciler.settlement import attribute, reconcile_payouts

__all__ = ["main", "render", "run_settlement"]

RESULTS_DIR = PROJECT_ROOT / "eval" / "results" / "settlement"


def run_settlement(seed: int = SEED) -> dict[str, Any]:
    """Reconcile every payout, attribute every gap, and score against truth."""
    batch = generate_payouts(seed=seed)
    detected = reconcile_payouts(batch)
    detected_ids = {g.payout_id for g in detected}

    should_flag = batch.gap_payout_ids
    timing_only = {g.payout_id for g in batch.gaps if g.kind == "next_cycle_capture"}

    outcomes: dict[str, str] = {}
    verdicts: dict[str, int] = {}
    for gap in detected:
        packet = attribute(gap, batch)
        outcomes[gap.payout_id] = packet.reason_code.value
        for hypothesis in packet.hypotheses:
            verdicts[hypothesis.verdict.value] = verdicts.get(hypothesis.verdict.value, 0) + 1

    by_kind = {
        g.kind: outcomes.get(g.payout_id, "not_detected")
        for g in batch.gaps if g.kind != "next_cycle_capture"
    }
    reason_codes: dict[str, int] = {}
    for code in outcomes.values():
        reason_codes[code] = reason_codes.get(code, 0) + 1

    total = sum(verdicts.values())
    return {
        "seed": seed,
        "payouts": len(batch.payouts),
        "captures": len(batch.captures),
        "cycles": len({p.cycle for p in batch.payouts}),
        "settlement_lag_cycles": 2,
        "gap_kinds": list(GAP_KINDS),
        "injected": len(should_flag),
        "detected": len(detected_ids & should_flag),
        "missed": len(should_flag - detected_ids),
        # The false-positive count is the number that decides whether this
        # ships. A timing boundary flagged as loss is worse than no detector.
        "false_positives": len(detected_ids - should_flag),
        "timing_boundaries_not_flagged": len(timing_only - detected_ids),
        "outcome_by_kind": dict(sorted(by_kind.items())),
        "reason_codes": dict(sorted(reason_codes.items())),
        "verdicts": dict(sorted(verdicts.items())),
        "hypotheses": total,
        "rejected": total - verdicts.get("VERIFIED", 0),
        "llm_calls": 0,
        "status": settlement_gap_report()["status"],
    }


def render(r: dict[str, Any]) -> str:
    lines: list[str] = []
    w = lines.append
    w("")
    w("  StateSync — settlement reconciliation")
    w("  " + "=" * 74)
    w(f"  seed {r['seed']}  ·  {r['payouts']} payouts over {r['cycles']} cycles  ·  "
      f"{r['captures']} captures  ·  T+{r['settlement_lag_cycles']}")
    w("")
    w("  Detection")
    w("  " + "-" * 74)
    w(f"  {'gaps injected':<48}{r['injected']:>10}")
    w(f"  {'detected':<48}{r['detected']:>10}")
    w(f"  {'missed':<48}{r['missed']:>10}")
    w(f"  {'false positives':<48}{r['false_positives']:>10}")
    w(f"  {'timing boundaries correctly NOT flagged':<48}"
      f"{r['timing_boundaries_not_flagged']:>10}")
    w("")
    w("  A capture made late settles one cycle later. The payout says which")
    w("  captures it covers, so summing by date instead reads timing as loss —")
    w("  that is the false-positive test, and it is the one that decides this.")
    w("")
    w("  Outcome by injected gap")
    w("  " + "-" * 74)
    for kind, outcome in r["outcome_by_kind"].items():
        w(f"  {kind:<40}{outcome:>34}")
    w("")
    w("  Verifier outcomes")
    w("  " + "-" * 74)
    for verdict, count in r["verdicts"].items():
        w(f"  {verdict:<48}{count:>10}")
    w(f"  {'rejection rate':<48}"
      f"{(r['rejected'] / r['hypotheses'] * 100 if r['hypotheses'] else 0):>9.1f}%")
    w("")
    w(f"  status: {r['status']}")
    w("")
    w("  Demonstrated against synthetic payout data, not validated. The sandbox")
    w("  produces no genuine settlement behaviour, so these figures are never")
    w("  merged into the record-level results.")
    w("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    result = run_settlement()
    print(render(result))  # noqa: T201
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "settlement.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _ = argv, ReasonCode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
