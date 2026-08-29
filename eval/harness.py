"""`make eval` — the three-arm measurement run.

Regenerating the README's numbers with one command is the single
highest-signal artefact in the repo: it turns every claim from an assertion
into something a reviewer verifies in one command.

Arm 3 lands in Phase 5. Until then this reports arms 1 and 2, which already
satisfy the published bar: throughput, a measured match rate, and an honest
exception list over a 500-record batch.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.arms import ArmResult, run_arm
from statesync.config import PROJECT_ROOT, SEED

__all__ = ["main", "render"]

RESULTS_DIR = PROJECT_ROOT / "eval" / "results"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render(results: list[ArmResult], include_timing: bool = True) -> str:
    """A fixed template populated from measured values. No prose is generated
    and no figure here comes from anywhere but the run itself."""
    lines: list[str] = []
    w = lines.append

    w("")
    w("  StateSync — evaluation")
    w("  " + "=" * 74)
    w(f"  seed {SEED}  ·  {results[0].records} records  ·  "
      f"{results[0].injected} injected divergences")
    w("")
    # Timing is this report's "timestamp": inherently variable between runs.
    # `include_timing=False` renders the deterministic half, which is what the
    # reproducibility test diffs.
    timing_head = f"{'rec/s':>10}{'insp/s':>10}{'p50 µs':>9}{'p99 µs':>9}" if include_timing else ""
    w(f"  {'arm':<8}{'match':>8}{'detect':>8}{'miss':>6}{'false+':>8}"
      f"{'records':>9}{'insp':>7}{timing_head}{'LLM':>5}")
    w("  " + "-" * (60 + (38 if include_timing else 0)))
    for r in results:
        timing = (
            f"{r.records_per_sec:>10,.0f}{r.inspections_per_sec:>10,.0f}"
            f"{r.throughput.p50_us:>9,}{r.throughput.p99_us:>9,}"
        ) if include_timing else ""
        w(f"  {r.arm:<8}{_pct(r.match_rate):>8}{r.detected:>8}{r.missed:>6}"
          f"{r.false_positives:>8}{r.records:>9}{r.inspections:>7}{timing}{r.llm_calls:>5}")
    w("")
    w("  Two-run confirmation inspects every record twice; this is the measured cost")
    w(f"  of not repairing in-flight payments. Measured {results[0].storage} — rec/s is")
    w("  the reconciler's rate and does not include database I/O.")
    w("")

    rules = next((r for r in results if r.arm == "rules"), None)
    if rules is not None:
        w("  Detection by class")
        w("  " + "-" * 74)
        w(f"  {'class':<26}{'injected':>10}{'detected':>10}{'rate':>10}")
        for klass, stats in sorted(rules.per_class.items()):
            rate = stats["detected"] / stats["injected"] if stats["injected"] else 0.0
            w(f"  {klass.value:<26}{stats['injected']:>10}{stats['detected']:>10}{_pct(rate):>10}")
        w("")
        w("  Safety and integrity")
        w("  " + "-" * 74)
        w(f"  {'audit chain verifies':<44}{str(rules.chain_ok):>14}")
        if rules.invariant_ok:
            w(f"  {'ledger invariant':<44}{'balanced':>14}")
        else:
            # Not a failure. The injector deleted ledger entries, so the books
            # genuinely disagree with the gateway, and the invariant says so
            # without inspecting any single transaction.
            short = -rules.invariant_delta_paise
            w(f"  {'ledger invariant: books short (paise)':<44}{short:>14,}")
        w(f"  {'divergences confirmed (2 passes)':<44}{rules.confirmed:>14,}")
        w(f"  {'transient divergences filtered':<44}{rules.transient_filtered:>14,}")
        w(f"  {'unresolved exceptions':<44}{rules.exceptions_count:>14,}")
        w("")
        if rules.repairs:
            rep = rules.repairs
            w("  Repairs")
            w("  " + "-" * 74)
            w(f"  {'repaired (first run)':<44}{rep.get('succeeded', 0):>14,}")
            w(f"  {'replayed (no write)':<44}{rep.get('replayed', 0):>14,}")
            w(f"  {'deduped by DB constraint':<44}{rep.get('already_applied', 0):>14,}")
            w(f"  {'escalated by policy':<44}{rep.get('escalated', 0):>14,}")
            w(f"  {'blocked by blast radius':<44}{rep.get('blocked', 0):>14,}")
            w(f"  {'rows written':<44}{rep.get('writes', 0):>14,}")
            w("")
        w("  Reading the match rate")
        w("  " + "-" * 74)
        w("  Four of these classes are exact set operations. 100% is the expected floor,")
        w("  not an achievement — if a set difference failed to find a set difference,")
        w("  that would be a bug. Measurement that means anything starts with the")
        w("  ambiguous cases (AMOUNT_MISMATCH, SETTLEMENT_GAP) in Phase 4-5, where the")
        w("  propose-verify layer is judged and where escalation is often the correct")
        w("  answer rather than a miss.")
        w("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval.harness")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--rate", type=float, default=0.25)
    parser.add_argument("--arm", default=None, help="run a single arm")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    arms = [args.arm] if args.arm else ["none", "rules"]
    args.results_dir.mkdir(parents=True, exist_ok=True)

    results = [
        run_arm(arm, seed=args.seed, n=args.n, rate=args.rate, repair=(arm == "rules"),
                exceptions_path=PROJECT_ROOT / "exceptions.csv" if arm == "rules" else None)
        for arm in arms
    ]

    print(render(results))  # noqa: T201

    for result in results:
        payload = result.as_event() | {"throughput": result.throughput.as_event()}
        (args.results_dir / f"arm_{result.arm}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
