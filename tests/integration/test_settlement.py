"""Settlement reconciliation — batch-level, against synthetic payout data.

Record-level reconciliation compares one payment against one order. This
compares one payout against a *set* of captures, refunds and fees, which is a
different shape: the T+2 cycle means a payout legitimately covers captures from
a prior day, so a naive `sum(captures today) == payout today` is wrong by
design rather than by accident.

Three tests here gate whether SETTLEMENT_GAP may move off NOT_IMPLEMENTED: the
false-positive test on the cycle boundary, and the two refusals.
"""

import pytest

from statesync.config import SEED
from statesync.generator.payouts import GAP_KINDS, generate_payouts
from statesync.models.enums import ReasonCode
from statesync.reconciler.settlement import attribute, check_payout, reconcile_payouts

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def batch():
    return generate_payouts(seed=SEED)


def gap_for(batch, kind):
    return next(g for g in batch.gaps if g.kind == kind)


# ── the generator models a real cycle ───────────────────────────────────────

def test_payouts_cover_captures_from_a_prior_cycle():
    """T+2: a payout for cycle N settles captures made on cycle N-2."""
    batch = generate_payouts(seed=SEED)
    payout = batch.payouts[-1]
    covered = [c for c in batch.captures if c.capture_id in payout.capture_ids]
    assert covered
    assert all(c.cycle == payout.cycle - 2 for c in covered)


def test_amounts_are_integer_paise():
    for payout in generate_payouts(seed=SEED).payouts:
        for value in (payout.gross_captures_paise, payout.total_refunds_paise,
                      payout.total_fees_paise, payout.reserve_paise, payout.net_paise):
            assert isinstance(value, int) and not isinstance(value, bool)


def test_payout_ids_use_the_gateway_prefix():
    assert all(p.payout_id.startswith("pout_") for p in generate_payouts(seed=SEED).payouts)


def test_the_generator_is_deterministic():
    assert generate_payouts(seed=SEED).digest() == generate_payouts(seed=SEED).digest()


def test_at_least_six_gap_kinds_are_injected(batch):
    assert len(GAP_KINDS) >= 6
    assert {g.kind for g in batch.gaps} == set(GAP_KINDS)


# ── the invariant ───────────────────────────────────────────────────────────

def test_a_clean_payout_satisfies_the_invariant(batch):
    clean = next(p for p in batch.payouts if p.payout_id not in batch.gap_payout_ids)
    assert check_payout(clean, batch).ok


def test_the_invariant_is_net_equals_gross_less_refunds_fees_and_reserve(batch):
    clean = next(p for p in batch.payouts if p.payout_id not in batch.gap_payout_ids)
    assert clean.net_paise == (
        clean.gross_captures_paise - clean.total_refunds_paise
        - clean.total_fees_paise - clean.reserve_paise
    )


# ── the false-positive test: the most important one here ────────────────────

def test_a_capture_landing_in_the_next_cycle_is_not_a_gap(batch):
    """The T+2 boundary. A capture made late settles one cycle later, and the
    payout says which captures it covers — summing by date instead of by that
    list invents a shortfall that does not exist.
    """
    payout = next(p for p in batch.payouts
                  if p.payout_id == gap_for(batch, "next_cycle_capture").payout_id)
    assert check_payout(payout, batch).ok, "a timing boundary was flagged as a gap"
    assert payout.payout_id not in {g.payout_id for g in reconcile_payouts(batch)}


# ── the two refusals ────────────────────────────────────────────────────────

def test_two_exact_attributions_escalate_as_ambiguous(batch):
    gap = gap_for(batch, "ambiguous")
    packet = attribute(gap, batch)
    assert packet.reason_code == ReasonCode.AMBIGUOUS_MULTIPLE_VERIFIED
    verified = [h for h in packet.hypotheses if h.verdict.value == "VERIFIED"]
    assert len(verified) == 2
    assert sorted(c.name for c in verified[0].components) != sorted(
        c.name for c in verified[1].components
    )


def test_an_unexplained_shortfall_escalates_with_every_candidate(batch):
    gap = gap_for(batch, "unexplained")
    packet = attribute(gap, batch)
    assert packet.reason_code == ReasonCode.NO_HYPOTHESIS_VERIFIED
    assert packet.hypotheses, "candidates were generated and all rejected"
    assert all(h.rejection_reason for h in packet.hypotheses)


# ── the three attributable gaps ─────────────────────────────────────────────

@pytest.mark.parametrize("kind,component", [
    ("undisclosed_reserve", "reserve"),
    ("prior_cycle_chargeback", "chargeback"),
    ("instant_settlement", "instant_settlement_fee"),
])
def test_an_attributable_gap_resolves_to_the_right_component(batch, kind, component):
    packet = attribute(gap_for(batch, kind), batch)
    assert packet.reason_code == ReasonCode.VERIFIED
    verified = next(h for h in packet.hypotheses if h.verdict.value == "VERIFIED")
    assert component in [c.name for c in verified.components]


def test_the_chargeback_attribution_records_that_it_crossed_a_cycle(batch):
    gap = gap_for(batch, "prior_cycle_chargeback")
    assert gap.detail.get("cross_cycle") == "true"


# ── detection as a whole ────────────────────────────────────────────────────

def test_every_injected_gap_is_detected(batch):
    detected = {g.payout_id for g in reconcile_payouts(batch)}
    expected = {g.payout_id for g in batch.gaps if g.kind != "next_cycle_capture"}
    assert expected <= detected


def test_no_clean_payout_is_flagged(batch):
    detected = {g.payout_id for g in reconcile_payouts(batch)}
    assert not (detected - batch.gap_payout_ids)


def test_a_gap_carries_its_residual_as_integer_paise(batch):
    for gap in reconcile_payouts(batch):
        assert isinstance(gap.residual_paise, int)
        assert gap.residual_paise != 0
