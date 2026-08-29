"""The three measurement arms.

    none    no detection at all — the divergences a merchant lives with today
    rules   deterministic set operations, no LLM
    full    rules + propose-verify for ambiguous attribution   (Phase 5)

Expect arms 2 and 3 to tie on clean cases. Four of six classes are set
operations and a language model will not beat a set operation at being one.
That is stated here, in the README, and in the video — before a reviewer says
it first. The propose-verify layer is judged on Phase 4's hard cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import redis as redis_lib

from statesync.classifier.deterministic import classify
from statesync.config import BASE_TIME, REDIS_URL, SEED, STALENESS_WINDOW
from statesync.executor.runner import RepairRunner
from statesync.executor.store import InMemoryRepairStore
from statesync.generator.synthetic import Batch, generate_batch
from statesync.injector.clean import InjectedBatch, inject_clean
from statesync.injector.hard_cases import inject_hard_cases
from statesync.ledger.chain import Ledger
from statesync.metrics.throughput import Stopwatch, ThroughputReport
from statesync.models.domain import Divergence
from statesync.models.enums import DivergenceClass, ReasonCode
from statesync.policy.blast_radius import BlastRadiusCap
from statesync.policy.gates import PolicyGate
from statesync.reconciler.invariants import check_ledger_invariant
from statesync.reconciler.staleness import ConfirmationTracker
from statesync.reconciler.three_way import build_index, reconcile_payment
from statesync.reporting.exceptions_csv import write_exceptions_csv

__all__ = ["ARMS", "ArmResult", "make_repair_runner", "run_arm"]

ARMS: tuple[str, ...] = ("none", "rules", "full")

# Reconciliation happens well after the generated window, so nothing is inside
# the staleness window for reasons of the clock rather than reasons of state.
_RECONCILED_AT = BASE_TIME + timedelta(days=30)


@dataclass(frozen=True)
class ArmResult:
    arm: str
    records: int
    injected: int
    detected: int
    misclassified: int
    false_positives: int
    confirmed: int
    transient_filtered: int
    exceptions_count: int
    llm_calls: int
    ledger_entries: int
    chain_ok: bool
    invariant_ok: bool
    invariant_delta_paise: int
    throughput: ThroughputReport
    repairs: dict[str, int] = field(default_factory=dict)
    hard_cases: int = 0
    reason_codes: dict[str, int] = field(default_factory=dict)
    storage: str = "in-memory"
    """What the throughput figure was measured against.

    The reconciler runs over in-memory views, so rec/s measures detection and
    classification rather than database I/O. That is a real number for a real
    component, but a reviewer will assume it includes I/O unless told, so the
    label travels with the result instead of living in a comment.
    """
    per_class: dict[DivergenceClass, dict[str, int]] = field(default_factory=dict)

    @property
    def inspections(self) -> int:
        """Per-record inspections. Two-run confirmation looks at every record
        once per pass, so this is `records * passes`."""
        return self.throughput.records

    @property
    def records_per_sec(self) -> float:
        """Business rate: distinct records reconciled per second."""
        if self.throughput.wall_clock_us == 0:
            return 0.0
        return self.records * 1_000_000 / self.throughput.wall_clock_us

    @property
    def inspections_per_sec(self) -> float:
        """Work rate: per-record inspections per second."""
        return self.throughput.records_per_sec

    @property
    def missed(self) -> int:
        return self.injected - self.detected

    @property
    def match_rate(self) -> float:
        """Correctly detected *and* correctly classified, over injected."""
        if self.injected == 0:
            return 0.0
        return (self.detected - self.misclassified) / self.injected

    def as_event(self) -> dict[str, Any]:
        """The deterministic record of this arm — integers and strings only,
        safe to write to the ledger.

        Timing is deliberately excluded. Two runs of one seed must produce
        identical metrics, and wall-clock duration never will; it is reported
        separately via `throughput.as_event()` for exactly that reason. This
        is the same carve-out the plan makes for timestamps.
        """
        return {
            "arm": self.arm,
            "records": self.records,
            "injected": self.injected,
            "detected": self.detected,
            "misclassified": self.misclassified,
            "false_positives": self.false_positives,
            "confirmed": self.confirmed,
            "transient_filtered": self.transient_filtered,
            "exceptions_count": self.exceptions_count,
            "llm_calls": self.llm_calls,
            "ledger_entries": self.ledger_entries,
            "chain_ok": self.chain_ok,
            "invariant_ok": self.invariant_ok,
            "storage": self.storage,
            "repairs": dict(sorted(self.repairs.items())),
            "hard_cases": self.hard_cases,
            "reason_codes": dict(sorted(self.reason_codes.items())),
            "inspections": self.inspections,
            "invariant_delta_paise": self.invariant_delta_paise,
            "per_class": {k.value: v for k, v in sorted(self.per_class.items())},
        }


def run_arm(
    arm: Literal["none", "rules", "full"] | str,
    seed: int = SEED,
    n: int = 500,
    rate: float = 0.25,
    passes: int = 2,
    exceptions_path: Path | None = None,
    repair: bool = False,
    blast_radius: int = 200,
    runner: RepairRunner | None = None,
    hard_cases: bool = False,
) -> ArmResult:
    """Run one arm over a freshly generated, freshly injected batch."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
    if arm == "full":
        raise NotImplementedError(
            "arm 'full' needs the propose-verify layer, which lands in Phase 5"
        )

    injected = inject_clean(generate_batch(seed=seed, n=n), seed=seed, rate=rate)
    hard_case_count = 0
    hard_payment_ids: set[str] = set()
    late_arrivals: list[Any] = []
    if hard_cases:
        # Hard-case timestamps are relative to the reconciliation clock, so
        # case 12's in-flight payment actually lands inside the staleness
        # window and `transient_filtered` becomes a measured number instead of
        # a permanent zero. See docs/PHASE_4_REQUIREMENTS.md.
        hard = inject_hard_cases(injected.batch, seed=seed, now=_RECONCILED_AT)
        injected = InjectedBatch(batch=hard.batch, injections=injected.injections)
        hard_case_count = len(hard.cases)
        hard_payment_ids = hard.payment_ids | {"pay_hc16"}
        late_arrivals = list(hard.late_arrivals)
    # A caller may pass an existing runner to re-run the same batch against
    # state that already has the repairs in it — that is how the "running it
    # twice writes nothing" claim is exercised end to end.
    if repair and runner is None:
        runner = make_repair_runner(blast_radius=blast_radius, flush=True)
    if runner is not None:
        # The cap bounds this run, not every run the pipeline has ever seen.
        runner.cap.reset()
    invariant = check_ledger_invariant(injected.batch)
    truth = injected.truth
    ledger = Ledger(clock=lambda: _RECONCILED_AT)
    tracker = ConfirmationTracker(ledger=ledger)

    watch = Stopwatch()
    detected_keys: set[str] = set()
    misclassified = 0
    confirmed_total = 0
    unresolved: list[Divergence] = []
    reasons: dict[str, ReasonCode] = {}
    all_found: list[Divergence] = []

    for pass_no in range(passes):
        now = _RECONCILED_AT + STALENESS_WINDOW * 2 * pass_no
        index = build_index(injected.batch)
        found: list[Divergence] = []

        # Timed per record, not per batch. A batch-level figure hides a long
        # tail, and p50/p99 per record is the number that shows one. Arm 1
        # walks the same records and does nothing, so the comparison between
        # "no detection" and "rules" is like for like.
        for payment in injected.batch.payments:
            with watch.record():
                if arm == "none":
                    continue
                for divergence in reconcile_payment(payment, index, now):
                    found.append(divergence)
                    classify(divergence)

        for divergence in found:
            key = divergence.deterministic_key()
            if pass_no == 0:
                all_found.append(divergence)
                detected_keys.add(key)
                result = classify(divergence)
                if key in truth and truth[key] != result.klass:
                    misclassified += 1
                if result.resolved_by != "rules":
                    unresolved.append(divergence)
                    reasons[key] = result.reason_code

        # The late webhook lands between passes, so pass 2 sees a batch that
        # has moved on — as a real one would.
        if late_arrivals and pass_no == 0:
            injected = InjectedBatch(
                batch=Batch(seed=injected.batch.seed, payments=injected.batch.payments,
                            orders=[*injected.batch.orders, *late_arrivals],
                            ledger_entries=injected.batch.ledger_entries),
                injections=injected.injections,
            )

        confirmed = tracker.observe(found, now=now)
        confirmed_total += len(confirmed)

        # Repairs run only on *confirmed* divergences — the second observation
        # is what authorises action, never the first.
        if runner is not None:
            for divergence in confirmed:
                runner.run(divergence)

    # The honest exception list: everything the pipeline could not settle.
    # On a clean-only batch this is legitimately empty — the four clean classes
    # are exact set operations. Phase 4's ambiguous cases are what populate it,
    # and manufacturing rows before then would be inventing exceptions.
    exceptions = unresolved
    detected_count = len(detected_keys & set(truth))
    count = (
        write_exceptions_csv(
            exceptions_path, exceptions, reasons=reasons,
            detected=detected_count,
            context=f"arm={arm}, seed={seed}, "
                    f"{'with hard cases' if hard_cases else 'clean-only batch'}",
        )
        if exceptions_path is not None
        else len(exceptions)
    )

    hard_case_keys = {
        d.deterministic_key() for d in all_found if d.payment_id in hard_payment_ids
    }

    per_class: dict[DivergenceClass, dict[str, int]] = {}
    for key, klass in truth.items():
        stats = per_class.setdefault(klass, {"injected": 0, "detected": 0})
        stats["injected"] += 1
        if key in detected_keys:
            stats["detected"] += 1

    return ArmResult(
        arm=arm,
        records=n,
        injected=len(truth),
        detected=len(detected_keys & set(truth)),
        misclassified=misclassified,
        # Hard-case detections are expected, not false positives: they are
        # scored separately (several have refusal as the correct outcome), so
        # merging them into this number would misreport both.
        false_positives=len({
            k for k in detected_keys - set(truth)
            if k not in hard_case_keys
        }),
        confirmed=confirmed_total,
        transient_filtered=tracker.transient_filtered,
        exceptions_count=count,
        llm_calls=0,
        ledger_entries=len(ledger.entries),
        chain_ok=ledger.verify()[0],
        invariant_ok=invariant.ok,
        invariant_delta_paise=invariant.delta_paise,
        throughput=watch.report(),
        per_class=per_class,
        repairs=runner.summary() if runner is not None else {},
        hard_cases=hard_case_count,
        reason_codes={r.value: sum(1 for v in reasons.values() if v == r)
                      for r in {*reasons.values()}},
    )


def make_repair_runner(blast_radius: int = 200, flush: bool = False) -> RepairRunner:
    """Build a repair pipeline over real Redis and an in-memory store.

    `flush=True` starts from a clean cache. Passing the same runner to two
    `run_arm` calls is what proves a second run writes nothing.
    """
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    if flush:
        client.flushdb()
    return RepairRunner(
        redis=client,
        store=InMemoryRepairStore(),
        ledger=Ledger(clock=lambda: _RECONCILED_AT),
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=blast_radius),
        clock=lambda: _RECONCILED_AT,
    )
