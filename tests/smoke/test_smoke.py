"""The smoke ladder — rungs S0 and S1.

Not a test suite. This proves the system boots and does one real thing per
capability, in under thirty seconds, against real Postgres and real Redis.
It is what you run before every commit.

Rungs are added one per phase and the total budget never moves:

    S0 infra        Phase 1   4s
    S1 chain        Phase 1   2s
    S2 loop         Phase 2   8s
    S3 idempotency  Phase 3   5s
    S4 refusal      Phase 4   3s
    S5 degradation  Phase 5   3s
    S6 fail-closed  Phase 7   2s
                              ---
                              27s of a 30s cap
"""

from datetime import UTC, datetime

import psycopg
import pytest
import redis

from statesync.config import ADMIN_DSN, APP_DSN, REDIS_URL, SEED
from statesync.ledger.chain import GENESIS, Ledger
from statesync.ledger.store import LedgerStore, apply_migrations

pytestmark = pytest.mark.smoke


@pytest.fixture(scope="module")
def clean_ledger():
    """Leave no residue: smoke starts and ends with an empty chain."""
    apply_migrations(ADMIN_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")
    yield LedgerStore(APP_DSN)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ledger_entries")


# ── S0 · infra ──────────────────────────────────────────────────────────────

def test_s0_postgres_is_reachable():
    with psycopg.connect(APP_DSN) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_s0_redis_is_reachable():
    assert redis.Redis.from_url(REDIS_URL).ping() is True


def test_s0_migrations_applied_and_ledger_table_exists(clean_ledger):
    with psycopg.connect(APP_DSN) as conn:
        row = conn.execute("SELECT to_regclass('public.ledger_entries')").fetchone()
    assert row is not None and row[0] == "ledger_entries"


def test_s0_app_role_has_no_update_or_delete_grant():
    """Constraint 7 leans on this: the audit trail is append-only by grant."""
    with psycopg.connect(APP_DSN) as conn:
        rows = conn.execute(
            "SELECT privilege_type FROM information_schema.table_privileges "
            "WHERE table_name = 'ledger_entries' AND grantee = 'statesync_app'"
        ).fetchall()
    granted = {r[0] for r in rows}
    assert granted == {"SELECT", "INSERT"}


# ── S1 · chain ──────────────────────────────────────────────────────────────

def test_s1_writes_three_entries_and_the_chain_verifies(clean_ledger):
    led = Ledger(clock=lambda: datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC))
    for i in range(3):
        clean_ledger.append(led.append("SMOKE", n=i))

    assert clean_ledger.count() == 3
    entries = clean_ledger.load()
    assert [e.seq for e in entries] == [1, 2, 3]
    assert entries[0].prev_hash == GENESIS
    assert clean_ledger.verify() == (True, None)


def test_s1_tampering_is_detected_at_the_right_index(clean_ledger):
    """The fail-closed guarantee is only as good as this assertion."""
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("UPDATE ledger_entries SET payload = '{\"n\":999}' WHERE seq = 2")

    ok, idx = clean_ledger.verify()
    assert not ok and idx == 1


# ── S2 · the loop, end to end ───────────────────────────────────────────────

def test_s2_fifty_record_batch_runs_end_to_end(tmp_path):
    """The whole submission in miniature: generate, inject, reconcile,
    classify, confirm, report — on real data, in about a second."""
    from eval.arms import run_arm

    result = run_arm("rules", seed=SEED, n=50, rate=0.25,
                     exceptions_path=tmp_path / "exceptions.csv")

    assert result.records == 50
    assert result.injected > 0, "the injector should have broken something"
    assert 0.0 < result.match_rate <= 1.0
    assert result.false_positives == 0


def test_s2_every_injected_class_is_detected(tmp_path):
    from eval.arms import run_arm

    result = run_arm("rules", seed=SEED, n=50, rate=0.25)
    assert result.per_class, "per-class detection must be reported, not just a headline"
    for klass, stats in result.per_class.items():
        assert stats["detected"] == stats["injected"], f"{klass.value} under-detected"


def test_s2_throughput_is_measured(tmp_path):
    """Throughput is the first word of the published bar."""
    from eval.arms import run_arm

    throughput = run_arm("rules", seed=SEED, n=50, rate=0.25).throughput
    assert throughput.records_per_sec > 0
    assert throughput.wall_clock_us > 0


def test_s2_exceptions_csv_is_written_with_a_reason_code_column(tmp_path):
    from eval.arms import run_arm

    from statesync.reporting.exceptions_csv import EXCEPTION_COLUMNS

    path = tmp_path / "exceptions.csv"
    run_arm("rules", seed=SEED, n=50, rate=0.25, exceptions_path=path)
    assert path.exists()
    header = next(
        ln for ln in path.read_text().splitlines() if not ln.startswith("#")
    )
    assert header == ",".join(EXCEPTION_COLUMNS)
    assert "reason_code" in EXCEPTION_COLUMNS


def test_s2_the_run_leaves_a_verifiable_chain(tmp_path):
    from eval.arms import run_arm

    result = run_arm("rules", seed=SEED, n=50, rate=0.25)
    assert result.chain_ok is True
    assert result.ledger_entries > 0


# ── S3 · idempotency ────────────────────────────────────────────────────────
# The two lines that are the argument. Everything else is engineering.

def test_s3_the_same_batch_twice_writes_nothing_the_second_time():
    from eval.arms import make_repair_runner, run_arm

    runner = make_repair_runner(flush=True)
    first = run_arm("rules", seed=SEED, n=50, rate=0.25, passes=2,
                    repair=True, runner=runner)
    writes = first.repairs["writes"]
    assert writes > 0, "the first run should have repaired something"

    second = run_arm("rules", seed=SEED, n=50, rate=0.25, passes=2,
                     repair=True, runner=runner)
    assert second.repairs["writes"] == writes


def test_s3_flushing_redis_still_writes_nothing():
    """Redis is a cache. The DB constraint is the guarantee."""
    from eval.arms import make_repair_runner, run_arm

    runner = make_repair_runner(flush=True)
    first = run_arm("rules", seed=SEED, n=50, rate=0.25, passes=2,
                    repair=True, runner=runner)
    writes = first.repairs["writes"]

    runner.redis.flushall()

    second = run_arm("rules", seed=SEED, n=50, rate=0.25, passes=2,
                     repair=True, runner=runner)
    assert second.repairs["writes"] == writes
    assert runner.ledger.has("REPAIR_DEDUPED_BY_DB")


def test_s3_a_stuck_repair_escalates_rather_than_going_silent():
    """A worker that died mid-repair must surface, never return nothing."""
    from datetime import timedelta

    import redis as redis_lib

    from statesync.config import LEASE_SECONDS
    from statesync.executor.store import InMemoryRepairStore
    from statesync.models.domain import Divergence
    from statesync.models.enums import DivergenceClass
    from statesync.policy.idempotency import IdempotentRepairer, RepairStatus

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()
    now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
    divergence = Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER, payment_id="pay_stuck",
                            order_id=None, amount_paise=1000, observed_at=now)
    led = Ledger(clock=lambda: now)

    IdempotentRepairer(redis=client, store=InMemoryRepairStore(), ledger=led,
                       clock=lambda: now).claim(divergence)

    later = now + timedelta(seconds=LEASE_SECONDS + 1)
    result = IdempotentRepairer(redis=client, store=InMemoryRepairStore(), ledger=led,
                                clock=lambda: later).repair(divergence, lambda d: {})

    assert result.status == RepairStatus.ESCALATE
    assert result.reason == "STUCK_REPAIR"
    assert led.has("STUCK_REPAIR_DETECTED")
    client.flushdb()


def test_s3_the_blast_radius_cap_holds():
    from eval.arms import run_arm

    capped = run_arm("rules", seed=SEED, n=50, rate=0.25, passes=2,
                     repair=True, blast_radius=3)
    assert capped.repairs["succeeded"] == 3
    assert capped.repairs["blocked"] > 0


# ── S4 · refusal ────────────────────────────────────────────────────────────
# The three cases where declining to act is the correct outcome. These are
# what a reviewer looks for.

def test_s4_case_07_two_legitimate_orders_are_not_merged():
    """Same customer, same amount, two seconds apart, two real payments."""
    from statesync.generator.synthetic import generate_batch
    from statesync.injector.hard_cases import inject_hard_cases
    from statesync.models.enums import DivergenceClass
    from statesync.reconciler.three_way import reconcile

    now = datetime(2026, 10, 1, tzinfo=UTC)
    hard = inject_hard_cases(generate_batch(seed=SEED, n=40), seed=SEED, now=now)
    case = hard.case("case_07")

    merges = [d for d in reconcile(hard.batch, now=now)
              if d.payment_id in case.payment_ids
              and d.klass == DivergenceClass.DUPLICATE_ORDER]
    assert merges == [], "two legitimate orders were merged"


def test_s4_case_13_two_verified_hypotheses_escalate_as_ambiguous():
    from statesync.generator.synthetic import generate_batch
    from statesync.injector.hard_cases import inject_hard_cases
    from statesync.models.enums import ReasonCode

    now = datetime(2026, 10, 1, tzinfo=UTC)
    hard = inject_hard_cases(generate_batch(seed=SEED, n=40), seed=SEED, now=now)
    packet = hard.packet_for("case_13")

    assert packet.reason_code == ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    assert len(packet.verified) == 2


def test_s4_case_14_escalates_with_every_hypothesis_and_its_arithmetic():
    from statesync.classifier.escalation import FORBIDDEN_PHRASES
    from statesync.generator.synthetic import generate_batch
    from statesync.injector.hard_cases import inject_hard_cases
    from statesync.models.enums import ReasonCode

    now = datetime(2026, 10, 1, tzinfo=UTC)
    hard = inject_hard_cases(generate_batch(seed=SEED, n=40), seed=SEED, now=now)
    packet = hard.packet_for("case_14")

    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert len(packet.hypotheses) >= 5
    assert all(h.arithmetic_shown and h.rejection_reason for h in packet.hypotheses)
    text = packet.suggested_action.lower()
    assert not [p for p in FORBIDDEN_PHRASES if p in text]


def test_s4_the_exception_list_has_reason_codes():
    import tempfile
    from pathlib import Path as _Path

    from eval.arms import run_arm

    from statesync.reporting.exceptions_csv import EXCEPTION_COLUMNS

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "exceptions.csv"
        result = run_arm("rules", seed=SEED, n=50, rate=0.25, hard_cases=True,
                         exceptions_path=path)
        assert result.exceptions_count > 0
        header = next(
        ln for ln in path.read_text().splitlines() if not ln.startswith("#")
    )
    assert header == ",".join(EXCEPTION_COLUMNS)
        assert "no_hypothesis_verified" in path.read_text()


# ── S5 · AI layer and degradation ───────────────────────────────────────────
# The rung that saves the demo: prove the network is not on the critical path
# every single run, rather than hoping on the day.

def test_s5_the_propose_verify_path_resolves_entirely_from_the_committed_cache():
    """No client configured at all. A cache gap raises rather than degrading."""
    from eval.arms import run_arm

    from statesync.classifier.llm_provider import LLMHypothesisProvider
    from statesync.config import CACHE_DIR
    from statesync.llm.cache import LLMCache

    provider = LLMHypothesisProvider(cache=LLMCache(cache_dir=CACHE_DIR), client=None)
    result = run_arm("full", seed=SEED, n=50, rate=0.25, hard_cases=True,
                     provider=provider)

    assert provider.network_calls == 0, "the demo would need a network call"
    assert result.llm_calls > 0


def test_s5_a_malformed_response_degrades_rather_than_crashing():
    import tempfile
    from pathlib import Path as _Path

    from statesync.classifier.llm_provider import LLMHypothesisProvider
    from statesync.llm.cache import LLMCache

    with tempfile.TemporaryDirectory() as tmp:
        provider = LLMHypothesisProvider(
            cache=LLMCache(cache_dir=_Path(tmp)), client=lambda _: "{'broken': ",
        )
        from statesync.classifier.provider import HypothesisRequest
        from statesync.classifier.verifier import ArtifactIndex

        proposals = provider.propose(HypothesisRequest(
            residual_paise=1140, instrument="upi",
            artifacts=ArtifactIndex({"pay_1"}), case_id="pay_1",
        ))
        assert proposals == []


def test_s5_generation_is_bounded_to_two_calls_per_divergence():
    import tempfile
    from pathlib import Path as _Path

    from statesync.classifier.escalation import build_packet
    from statesync.classifier.llm_provider import LLMHypothesisProvider
    from statesync.classifier.provider import HypothesisRequest
    from statesync.classifier.verifier import ArtifactIndex
    from statesync.llm.cache import LLMCache
    from statesync.models.domain import Divergence
    from statesync.models.enums import DivergenceClass

    now = datetime(2026, 9, 5, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as tmp:
        provider = LLMHypothesisProvider(
            cache=LLMCache(cache_dir=_Path(tmp)), client=lambda _: "",
        )
        build_packet(
            divergence=Divergence(klass=DivergenceClass.AMOUNT_MISMATCH,
                                  payment_id="pay_1", order_id=None,
                                  amount_paise=400_000, observed_at=now),
            known_components=[], residual_paise=1140, provider=provider,
            request=HypothesisRequest(residual_paise=1140, instrument="upi",
                                      artifacts=ArtifactIndex({"pay_1"}),
                                      case_id="pay_1"),
        )
        assert provider.calls <= 2


def test_s5_the_cache_manifest_says_which_client_filled_it():
    import json

    from statesync.config import CACHE_DIR

    manifest = json.loads((CACHE_DIR / "MANIFEST.json").read_text())
    assert manifest["client_kind"] in ("live", "offline")


# ── S6 · fail-closed ────────────────────────────────────────────────────────
# The last rung. A reconciler that keeps writing while its own audit trail is
# compromised is worse than no reconciler, so this runs before every commit.

def test_s6_a_tampered_chain_halts_the_run_with_zero_writes():
    import redis as redis_lib

    from statesync.executor.runner import RepairRunner
    from statesync.executor.store import InMemoryRepairStore
    from statesync.ledger.chain import ChainIntegrityError
    from statesync.models.domain import Divergence
    from statesync.models.enums import DivergenceClass
    from statesync.policy.blast_radius import BlastRadiusCap
    from statesync.policy.gates import PolicyGate

    now = datetime(2026, 9, 5, tzinfo=UTC)
    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    client.flushdb()

    led = Ledger(clock=lambda: now)
    for i in range(5):
        led.append("SMOKE", i=i)
    led.entries[2].payload["i"] = 999          # the audit trail is now suspect

    store = InMemoryRepairStore()
    runner = RepairRunner(
        redis=client, store=store, ledger=led,
        gate=PolicyGate(value_threshold_paise=10_000_000),
        cap=BlastRadiusCap(max_repairs=50), clock=lambda: now,
    )
    divergence = Divergence(klass=DivergenceClass.CAPTURED_NO_ORDER,
                            payment_id="pay_smoke", order_id=None,
                            amount_paise=1000, observed_at=now)

    with pytest.raises(ChainIntegrityError):
        runner.run_batch([divergence])

    assert store.write_count == 0, "a repair landed on a compromised audit trail"
    client.flushdb()


def test_s6_the_break_index_is_reported():
    """An ops person needs to know where the trail stopped being trustworthy."""
    from statesync.ledger.chain import ChainIntegrityError, verify_or_halt

    now = datetime(2026, 9, 5, tzinfo=UTC)
    led = Ledger(clock=lambda: now)
    for i in range(6):
        led.append("SMOKE", i=i)
    led.entries[3].payload["i"] = 999

    with pytest.raises(ChainIntegrityError, match="index 3"):
        verify_or_halt(led)


def test_s6_an_intact_chain_does_not_halt_anything():
    """The other half of the claim: no false alarm on a good chain."""
    from statesync.ledger.chain import verify_or_halt

    now = datetime(2026, 9, 5, tzinfo=UTC)
    led = Ledger(clock=lambda: now)
    for i in range(5):
        led.append("SMOKE", i=i)
    verify_or_halt(led)


def test_s6_the_tamper_harness_exits_non_zero():
    """Beat 9 of the demo, asserted rather than rehearsed."""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", "eval.tamper"],
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "HALTED" in proc.stderr
