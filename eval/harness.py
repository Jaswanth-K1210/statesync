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


def render(results: list[ArmResult]) -> str:
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
    w(f"  {'arm':<8}{'match':>9}{'detected':>11}{'missed':>9}{'false+':>9}"
      f"{'rec/s':>11}{'p50 µs':>10}{'p99 µs':>10}{'LLM':>6}")
    w("  " + "-" * 74)
    for r in results:
        w(f"  {r.arm:<8}{_pct(r.match_rate):>9}{r.detected:>11}{r.missed:>9}"
          f"{r.false_positives:>9}{r.throughput.records_per_sec:>11,.0f}"
          f"{r.throughput.p50_us:>10,}{r.throughput.p99_us:>10,}{r.llm_calls:>6}")
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
        w("  Four of six classes are set operations. Rules resolve them exactly, and")
        w("  a language model would not beat a set operation at being a set operation.")
        w("  The propose-verify layer is judged on the hard cases, in Phase 4-5.")
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
        run_arm(arm, seed=args.seed, n=args.n, rate=args.rate,
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
