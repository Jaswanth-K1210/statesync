"""The deterministic verifier.

The architectural thesis in one module: **the model proposes, the calculator
decides.** A hypothesis is accepted only if it reconciles exactly, in paise,
with every citation resolving and every component inside its permitted range.

A hallucinated decomposition fails by construction. That is structural, not a
prompt instruction — which is why this is built and proven in Phase 4, against
fixtures, before any model output touches it in Phase 5.
"""

import pytest

from statesync.classifier.verifier import (
    ArtifactIndex,
    Component,
    Proposal,
    Verdict,
    verify,
)

ARTIFACTS = ArtifactIndex({"pay_1", "rfnd_real", "stl_4471"})


def proposal(*components: Component) -> Proposal:
    return Proposal(components=list(components))


def comp(name, amount, cites="pay_1", rate_bps=None):
    return Component(name=name, amount_paise=amount, cites=cites, rate_bps=rate_bps)


# ── arithmetic ──────────────────────────────────────────────────────────────

def test_an_exact_decomposition_verifies():
    h = verify(proposal(comp("mdr", 800), comp("gst", 340)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED
    assert h.matched_residual is True


def test_off_by_one_paisa_is_rejected():
    """Exactly, to the paisa. There is no tolerance."""
    h = verify(proposal(comp("mdr", 1141)), residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARITHMETIC_FAILED


def test_off_by_one_paisa_the_other_way_is_rejected():
    h = verify(proposal(comp("mdr", 1139)), residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARITHMETIC_FAILED


def test_the_sum_is_reported_even_when_it_fails():
    """The ops person needs the arithmetic, not just the word 'rejected'."""
    h = verify(proposal(comp("a", 500), comp("b", 400)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.sum_paise == 900
    assert "900" in h.rejection_reason and "1140" in h.rejection_reason


def test_an_empty_proposal_explains_nothing_and_is_rejected():
    h = verify(proposal(), residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARITHMETIC_FAILED


def test_a_zero_residual_is_explained_by_an_empty_proposal():
    h = verify(proposal(), residual_paise=0, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED


def test_negative_components_are_allowed_for_reversals():
    h = verify(proposal(comp("refund", 2000, cites="rfnd_real"), comp("reversal", -860)),
               residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED


# ── citations ───────────────────────────────────────────────────────────────

def test_a_missing_artifact_is_rejected_even_when_the_arithmetic_matches():
    """The single most important case in this file.

    Arithmetic alone is not sufficient. A model that invents a refund id can
    always make the numbers work; the citation check is what stops it.
    """
    h = verify(proposal(comp("refund", 966, cites="rfnd_ghost"), comp("mdr", 174)),
               residual_paise=1140, artifacts=ARTIFACTS)
    assert h.matched_residual is True, "the arithmetic did match"
    assert h.verdict == Verdict.ARTIFACT_MISSING


def test_the_missing_artifact_is_named_in_the_rejection():
    h = verify(proposal(comp("refund", 1140, cites="rfnd_ghost")),
               residual_paise=1140, artifacts=ARTIFACTS)
    assert "rfnd_ghost" in h.rejection_reason


def test_a_component_with_no_citation_is_rejected():
    h = verify(proposal(comp("mystery", 1140, cites=None)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARTIFACT_MISSING


def test_citations_are_collected_for_the_packet():
    h = verify(proposal(comp("mdr", 800), comp("fee", 340, cites="stl_4471")),
               residual_paise=1140, artifacts=ARTIFACTS)
    assert sorted(h.citations) == ["pay_1", "stl_4471"]


# ── ranges ──────────────────────────────────────────────────────────────────

def test_an_out_of_range_mdr_is_rejected():
    """12.5% MDR is not a thing, however well the arithmetic works."""
    h = verify(proposal(comp("mdr", 1140, rate_bps=1250)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.RANGE_VIOLATION


def test_an_mdr_at_the_permitted_ceiling_is_accepted():
    h = verify(proposal(comp("mdr", 1140, rate_bps=400)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED


def test_the_range_violation_names_the_component_and_the_rate():
    h = verify(proposal(comp("mdr", 1140, rate_bps=1250)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert "mdr" in h.rejection_reason and "12.5" in h.rejection_reason


def test_a_component_with_no_declared_rate_skips_the_range_check():
    h = verify(proposal(comp("adjustment", 1140)), residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED


# ── check ordering ──────────────────────────────────────────────────────────

def test_arithmetic_is_checked_before_citations():
    """A proposal that is wrong twice reports the arithmetic failure first —
    the ops person fixes the sum before chasing a citation."""
    h = verify(proposal(comp("x", 999, cites="rfnd_ghost")), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARITHMETIC_FAILED


def test_citations_are_checked_before_ranges():
    h = verify(proposal(comp("mdr", 1140, cites="rfnd_ghost", rate_bps=1250)),
               residual_paise=1140, artifacts=ARTIFACTS)
    assert h.verdict == Verdict.ARTIFACT_MISSING


# ── constraints hold in the verifier too ────────────────────────────────────

def test_float_amounts_are_refused():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Component(name="mdr", amount_paise=11.40, cites="pay_1")


def test_a_verified_hypothesis_is_ledger_safe():
    from statesync.ledger.canonical import canonical
    h = verify(proposal(comp("mdr", 1140)), residual_paise=1140, artifacts=ARTIFACTS)
    canonical(h.as_event())


def test_every_hypothesis_shows_its_arithmetic():
    """Case 14 attaches all ten rejected hypotheses with the reason each
    failed. A human seeing seven off by consistent paise learns something a
    bare 'escalated' never conveys."""
    h = verify(proposal(comp("a", 500), comp("b", 400)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.arithmetic_shown == "500 + 400 = 900"


# ── a declared rate must be consistent with its own amount ──────────────────
# Checking only that a rate is in range lets a decomposition claim "MDR at 2%"
# for an amount that is nothing like 2% of the payment. The arithmetic sums,
# the artifact exists, the rate is plausible — and the explanation is still
# fiction. This is the coincidental fit that bounded search is meant to avoid,
# and range checking alone does not catch it.

def test_a_component_whose_amount_contradicts_its_declared_rate_is_rejected():
    h = verify(proposal(comp("mdr", 1140, rate_bps=200)), residual_paise=1140,
               artifacts=ARTIFACTS, base_paise=400_000)
    assert h.verdict == Verdict.RATE_INCONSISTENT


def test_a_component_whose_amount_matches_its_declared_rate_is_accepted():
    # 2% of 400000 = 8000
    h = verify(proposal(comp("mdr", 8000, rate_bps=200)), residual_paise=8000,
               artifacts=ARTIFACTS, base_paise=400_000)
    assert h.verdict == Verdict.VERIFIED


def test_the_rate_check_names_what_it_expected():
    h = verify(proposal(comp("mdr", 1140, rate_bps=200)), residual_paise=1140,
               artifacts=ARTIFACTS, base_paise=400_000)
    assert "8000" in h.rejection_reason and "1140" in h.rejection_reason


def test_the_rate_check_is_skipped_without_a_base_amount():
    """Settlement-level attribution has no single base to check against."""
    h = verify(proposal(comp("mdr", 1140, rate_bps=200)), residual_paise=1140,
               artifacts=ARTIFACTS)
    assert h.verdict == Verdict.VERIFIED


def test_a_component_with_no_declared_rate_is_unaffected():
    h = verify(proposal(comp("adjustment", 1140)), residual_paise=1140,
               artifacts=ARTIFACTS, base_paise=400_000)
    assert h.verdict == Verdict.VERIFIED


def test_gst_is_checked_against_the_fee_not_the_payment():
    """GST is a percentage of the MDR, not of the transaction."""
    # MDR 8000 at 2% of 400000; GST 1440 at 18% of 8000.
    h = verify(
        proposal(comp("mdr", 8000, rate_bps=200), comp("gst", 1440, rate_bps=1800)),
        residual_paise=9440, artifacts=ARTIFACTS, base_paise=400_000,
    )
    assert h.verdict == Verdict.VERIFIED


def test_a_wrong_gst_on_a_right_mdr_is_rejected():
    h = verify(
        proposal(comp("mdr", 8000, rate_bps=200), comp("gst", 9000, rate_bps=1800)),
        residual_paise=17000, artifacts=ARTIFACTS, base_paise=400_000,
    )
    assert h.verdict == Verdict.RATE_INCONSISTENT
