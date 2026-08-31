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

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import redis as redis_lib

from statesync.classifier.deterministic import classify
from statesync.classifier.escalation import build_packet
from statesync.classifier.llm_provider import LLMHypothesisProvider
from statesync.classifier.provider import (
    HypothesisProvider,
    HypothesisRequest,
    UnionHypothesisProvider,
)
from statesync.classifier.verifier import ArtifactIndex, Component
from statesync.config import BASE_TIME, PROJECT_ROOT, REDIS_URL, SEED, STALENESS_WINDOW
from statesync.executor.runner import RepairRunner
from statesync.executor.store import InMemoryRepairStore
from statesync.generator.synthetic import Batch, generate_batch
from statesync.injector.clean import InjectedBatch, inject_clean
from statesync.injector.hard_cases import inject_hard_cases
from statesync.ledger.chain import Ledger
from statesync.llm.cache import LLMCache
from statesync.llm.client import ClientKind, resolve_client
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

PACKETS_DIR = PROJECT_ROOT / "eval" / "results" / "packets"
"""Escalation packets, one JSON per escalated payment, **namespaced by arm**.

Built in memory and discarded until Phase 6 — only the reason code reached
`exceptions.csv`, so nothing could show a reviewer the arithmetic behind a
refusal.

Namespacing is not tidiness. Writing every arm into one directory makes it
last-write-wins, and the harness runs the three-pass idempotency proof (arm 2)
after arm 3 — so the packets on disk showed arm 2's view while the README
quoted arm 3's. hc09 had zero hypotheses in the file and one resolution in the
table. That is the seam pattern in `docs/seam_bugs.json` arriving in the newest
surface: one artifact re-deriving what another already decided.
"""

SERVED_ARM = "full"
"""The arm the API and the UI show. The same arm the README's headline quotes,
named once so the two cannot drift apart."""

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
    reason_codes_total: int = 0
    fee_coverage_bps: int = 0
    fee_priced: int = 0
    fee_unpriced: int = 0
    verdicts: dict[str, int] = field(default_factory=dict)
    hypotheses_generated: int = 0
    pass2_fired: int = 0
    pass2_resolved: int = 0
    network_calls: int = 0
    provider_calls: int = 0
    client_kind: str = ""
    degraded: bool = False
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
            "fee_coverage_bps": self.fee_coverage_bps,
            "fee_priced": self.fee_priced,
            "fee_unpriced": self.fee_unpriced,
            "verdicts": dict(sorted(self.verdicts.items())),
            "hypotheses_generated": self.hypotheses_generated,
            "pass2_fired": self.pass2_fired,
            "pass2_resolved": self.pass2_resolved,
            "network_calls": self.network_calls,
            "provider_calls": self.provider_calls,
            "client_kind": self.client_kind,
            "degraded": self.degraded,
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
    provider: HypothesisProvider | None = None,
    packets_dir: Path | None = None,
) -> ArmResult:
    """Run one arm over a freshly generated, freshly injected batch."""
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")

    injected = inject_clean(generate_batch(seed=seed, n=n), seed=seed, rate=rate)
    hard_case_count = 0
    hard_payment_ids: set[str] = set()
    deterministic_provider: HypothesisProvider | None = None
    late_arrivals: list[Any] = []
    artifacts: ArtifactIndex | None = None
    client_kind: str = ClientKind.OFFLINE
    if hard_cases:
        # Hard-case timestamps are relative to the reconciliation clock, so
        # case 12's in-flight payment actually lands inside the staleness
        # window and `transient_filtered` becomes a measured number instead of
        # a permanent zero. See docs/PHASE_4_REQUIREMENTS.md.
        hard = inject_hard_cases(injected.batch, seed=seed, now=_RECONCILED_AT)
        injected = InjectedBatch(batch=hard.batch, injections=injected.injections)
        hard_case_count = len(hard.cases)
        hard_payment_ids = hard.payment_ids
        late_arrivals = list(hard.late_arrivals)
        if provider is None and arm != "full":
            provider = hard.provider
        deterministic_provider = hard.provider
        artifacts = hard.artifacts

    if arm == "full" and provider is None:
        # Arm 3 pools the model WITH the deterministic set rather than
        # replacing it. Replacing lost hc13's correct ambiguity finding: the
        # model found neither valid explanation and reported "nothing
        # verified", turning a case the system knew it could not resolve into
        # one it wrongly believed it had settled. Pooled, the model is strictly
        # additive — it can add a resolution, never remove an ambiguity.
        client, client_kind = resolve_client()
        model = LLMHypothesisProvider(cache=LLMCache(), client=client)
        provider = (
            UnionHypothesisProvider([deterministic_provider, model], model_index=1)
            if deterministic_provider is not None else model
        )
    if arm == "full":
        if artifacts is None:
            artifacts = ArtifactIndex({p.payment_id for p in injected.batch.payments})
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
    packets: list[Any] = []

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
                    # The reason code comes from the escalation packet that
                    # actually ran, not from a classifier default. Only the
                    # packet knows whether two hypotheses verified, and
                    # mislabelling case 13 in the exception list would hide
                    # the most interesting row in the file.
                    if divergence.detail.get("fee_source") == "unknown":
                        # Priced nothing, so nothing can be verified. Escalate
                        # with the config that would resolve it.
                        reasons[key] = ReasonCode.FEE_SCHEDULE_UNKNOWN
                        # A packet is still written, with no hypotheses. Without
                        # it the CSV would carry a row the API could not serve,
                        # and the two artifacts would disagree about how many
                        # escalations exist.
                        _write_unpriceable_packet(divergence, packets_dir, arm)
                    else:
                        packet = _escalate(divergence, provider, artifacts, ledger,
                                           result.reason_code)
                        reasons[key] = packet.reason_code if packet else result.reason_code
                        if packet is not None:
                            packets.append(packet)
                            _write_packet(packet, artifacts, packets_dir, arm)

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

    # Fee-schedule coverage: of the payments an amount check applies to, how
    # many could be priced at all. Reporting verification without this is how
    # a system claims 95% while silently escalating the 40% it could not price.
    unpriced = len([d for d in all_found if d.detail.get("fee_source") == "unknown"])
    priced = len([d for d in all_found if d.detail.get("fee_source")
                  and d.detail["fee_source"] != "unknown"])

    # Verifier outcomes, broken out by rejection reason. The rejection rate is
    # the evidence the guard is load-bearing: a model that proposes freely and
    # is refused often is a safer story than one that is never tested.
    verdicts: dict[str, int] = {}
    for packet in packets:
        for hypothesis in packet.hypotheses:
            verdicts[hypothesis.verdict.value] = verdicts.get(hypothesis.verdict.value, 0) + 1

    hard_case_keys = {
        d.deterministic_key() for d in all_found if d.payment_id in hard_payment_ids
    }

    per_class: dict[DivergenceClass, dict[str, int]] = {}
    for key, klass in truth.items():
        stats = per_class.setdefault(klass, {"injected": 0, "detected": 0, "rate_bps": 0})
        stats["injected"] += 1
        if key in detected_keys:
            stats["detected"] += 1

    # The rate is stored, not left for the template to derive. A figure
    # computed in one place and recomputed in another is how case 13's reason
    # code came out wrong — the report must read values, never re-derive them.
    # Basis points keeps it an integer, so it stays ledger-safe.
    for stats in per_class.values():
        stats["rate_bps"] = (
            stats["detected"] * 10_000 // stats["injected"] if stats["injected"] else 0
        )

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
        # Only a real LLM provider counts as an LLM call. Arm 2 consults a
        # fixture provider, and reporting that as an LLM call would undercut
        # the honest framing that arm 2 contains no AI at all.
        # Only the model member's calls count as LLM calls; the deterministic
        # member's consultations are not model usage.
        llm_calls=(
            provider.model_calls if isinstance(provider, UnionHypothesisProvider)
            else getattr(provider, "calls", 0)
            if isinstance(provider, LLMHypothesisProvider) else 0
        ),
        provider_calls=getattr(provider, "calls", 0),
        network_calls=getattr(provider, "network_calls", 0),
        client_kind=client_kind if arm == "full" else "",
        degraded=bool(getattr(provider, "degraded", False)),
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
        reason_codes_total=len(reasons),
        verdicts=verdicts,
        hypotheses_generated=sum(len(p.hypotheses) for p in packets),
        pass2_fired=sum(1 for p in packets if p.passes_used >= 2),
        pass2_resolved=sum(
            1 for p in packets if p.passes_used >= 2 and p.reason_code == ReasonCode.VERIFIED
        ),
        fee_priced=priced,
        fee_unpriced=unpriced,
        fee_coverage_bps=(priced * 10_000 // (priced + unpriced)) if (priced + unpriced) else 0,
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


def _escalate(
    divergence: Divergence,
    provider: HypothesisProvider | None,
    artifacts: ArtifactIndex | None,
    ledger: Ledger,
    fallback: ReasonCode,
) -> Any:
    """Run the bounded propose-verify loop for one under-determined divergence.

    With no provider configured the outcome is `fallback` — which is honest:
    nothing was generated, so nothing verified. This is the seam Phase 5 fills
    with an LLM provider; the loop, the verifier and the packet do not change.
    """
    if provider is None or artifacts is None:
        return None

    residual = int(divergence.detail.get("residual_paise", "0"))
    known = int(divergence.detail.get("known_paise", "0"))
    packet = build_packet(
        divergence=divergence,
        known_components=(
            [Component(name="fee_and_tax", amount_paise=known,
                       cites=divergence.payment_id or "")]
            if known else []
        ),
        residual_paise=residual,
        provider=provider,
        request=HypothesisRequest(
            residual_paise=residual,
            instrument=divergence.detail.get("instrument", "unknown"),
            artifacts=artifacts,
            case_id=divergence.payment_id or "",
        ),
        ledger=ledger,
        base_paise=divergence.amount_paise,
    )
    return packet


def _packet_target(arm: str, packets_dir: Path | None) -> Path:
    """Where packets get written.

    `STATESYNC_PACKETS_DIR` redirects writes away from the committed location.
    The test suite sets it: running pytest was overwriting `eval/results/
    packets/` with test data, so the repo's committed artifacts changed as a
    side effect of running the tests — and the API tests then failed against
    files their own suite had just corrupted.
    """
    if packets_dir is None:
        override = os.getenv("STATESYNC_PACKETS_DIR")
        packets_dir = Path(override) if override else PACKETS_DIR
    target = packets_dir / arm
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_unpriceable_packet(
    divergence: Divergence, packets_dir: Path | None, arm: str
) -> None:
    """A packet for a divergence nothing could price.

    No hypotheses, because none were generated: the fee was unknown, so there
    was no residual to explain. The packet says exactly that, and names the
    config that would resolve it.
    """
    from statesync.classifier.escalation import suggested_action

    target = _packet_target(arm, packets_dir)
    payload: dict[str, Any] = {
        "payment_id": divergence.payment_id or "",
        "order_id": divergence.order_id or "",
        "divergence_key": divergence.deterministic_key(),
        "klass": divergence.klass.value,
        "order_total_paise": divergence.amount_paise,
        "settled_paise": divergence.amount_paise,
        "known_components": [],
        "residual_paise": 0,
        "hypotheses": [],
        "reason_code": ReasonCode.FEE_SCHEDULE_UNKNOWN.value,
        "suggested_action": suggested_action(
            ReasonCode.FEE_SCHEDULE_UNKNOWN, 0, divergence.payment_id or ""
        ),
        "passes_used": 0,
        "needed_config": divergence.detail.get("needed_config", ""),
    }
    (target / f"{payload['payment_id']}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_packet(
    packet: Any, artifacts: Any, packets_dir: Path | None, arm: str
) -> None:
    """Persist one escalation packet exactly as it was decided, under its arm."""
    target = _packet_target(arm, packets_dir)
    payload = packet.as_packet(artifacts)
    (target / f"{payload['payment_id']}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
