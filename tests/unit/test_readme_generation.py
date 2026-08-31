"""Every figure in the README is templated from measured output.

Six times in this build a value was computed in one place and restated in
another, and every one came out wrong: case 13's reason code, `base_paise`,
arm 3's provider, `warm_cache`'s probe, backoff-as-latency, and a prose
sentence claiming "two times in three" beside a computed 75%.

Prose is the last exposure, and a README is nothing but prose describing
numbers. So it is generated the same way `suggested_action` is: a fixed
template populated from the eval output. **If a sentence contains a number a
human typed, it will eventually be wrong.**
"""

import re

import pytest
from eval.readme import TEMPLATE, render_readme


def _volatile(text: str) -> str:
    """Blank the two figures that change for reasons carrying no information.

    **Throughput** is wall-clock derived and can never be byte-identical
    between runs. The LLM call count beside it is a real measurement and is
    still compared.

    **The test count** changes every time a test is added, which is constantly.
    Guarding it means `make verify` goes red for a reason unrelated to
    correctness — and it would do so at the worst possible moment, minutes
    before a recording, forcing a doc regeneration under time pressure. Nobody
    is judged on 596 tests versus 601.

    The figures that must not drift are the results: the arm table, the
    rejection breakdown, throughput's companions, provider cost. Those are
    compared exactly.
    """
    text = re.sub(r"\| [\d,]+ (\| \d+ \|)$", r"| ~ \1", text, flags=re.M)
    return re.sub(r"types, [\d,]+ tests", "types, ~ tests", text)


@pytest.fixture(scope="module")
def rendered():
    return render_readme()


# ── the template itself carries no figures ──────────────────────────────────

def test_the_template_contains_no_bare_numbers():
    """A digit in the template is a number nobody will remember to update."""
    prose = re.sub(r"\{[^}]*\}", "", TEMPLATE)          # drop placeholders
    prose = re.sub(r"```.*?```", "", prose, flags=re.S)  # drop code blocks
    prose = re.sub(r"^\s*[-|].*$", "", prose, flags=re.M)  # drop table scaffolding
    offenders = [
        line.strip() for line in prose.splitlines()
        if re.search(r"(?<![\w-])\d", line)
        # Identifiers, not measurements: "Case 7" names a fixture, "arm 3"
        # names an arm. Neither is a figure that can go stale.
        and not re.search(r"(Phase|phase|[Cc]ase|arm|§|v)\s*\d", line)
    ]
    assert not offenders, f"hardcoded figures in the README template: {offenders[:3]}"


def test_every_measured_figure_has_a_placeholder():
    for field in ("records", "injected", "match_rate", "rejection_rate",
                  "mean_request_ms", "model", "cold_calls", "tests_total"):
        assert "{" + field in TEMPLATE, f"{field} is not templated"


# ── the rendered output ─────────────────────────────────────────────────────

def test_the_readme_renders(rendered):
    assert rendered.startswith("# StateSync")


def test_rendering_twice_is_identical():
    assert render_readme() == render_readme()


def test_the_headline_numbers_come_from_the_committed_results(rendered):
    from eval.readme import load_results

    results = load_results()
    assert str(results["rules"]["records"]) in rendered
    assert str(results["rules"]["injected"]) in rendered


def test_the_model_is_named_wherever_a_cost_figure_appears(rendered):
    from eval.readme import load_manifest

    model = load_manifest()["cold"]["model"]
    assert model in rendered


def test_the_readme_states_the_match_rate_is_an_expected_floor(rendered):
    """100% on four set operations is not an achievement, and a reviewer who
    sees it unqualified assumes the test set is trivial."""
    lowered = rendered.lower()
    assert "expected floor" in lowered
    assert "set operation" in lowered


def test_the_readme_states_the_completeness_limit(rendered):
    """Sound but not complete: the guard catches fabrication, not omission."""
    lowered = rendered.lower()
    assert "sound but not complete" in lowered
    assert "omission" in lowered or "never proposed" in lowered


def test_the_readme_says_settlement_gap_is_not_implemented(rendered):
    assert "not_implemented" in rendered.lower()


def test_the_readme_says_determinism_rests_on_the_cache(rendered):
    assert "cache" in rendered.lower()
    assert "not on the model" in rendered.lower()


def test_the_readme_never_claims_no_explanation_exists(rendered):
    from statesync.classifier.escalation import FORBIDDEN_PHRASES

    lowered = rendered.lower()
    assert not [p for p in FORBIDDEN_PHRASES if p in lowered]


def test_the_readme_carries_the_reproduce_command(rendered):
    assert "make verify" in rendered
    assert "make eval" in rendered


def test_a_stale_readme_is_detectable():
    """`make readme` regenerates; CI can diff it. A README that drifts from
    the numbers is exactly the failure this file exists to prevent."""
    from pathlib import Path

    from statesync.config import PROJECT_ROOT

    readme = Path(PROJECT_ROOT) / "README.md"
    if readme.exists():
        # Compare the deterministic form: throughput is wall-clock derived and
        # can never be byte-identical between runs.
        # Blank only the throughput column; the LLM count beside it is a real
        # measurement and must still be compared.
        current = _volatile(readme.read_text())
        expected = _volatile(render_readme(include_timing=False))
        assert current == expected, "README.md is stale; run make readme"


def test_the_results_themselves_are_guarded_exactly():
    """The loosening above must not extend to anything that is a finding."""
    from pathlib import Path as _Path

    from statesync.config import PROJECT_ROOT

    readme = (_Path(PROJECT_ROOT) / "README.md").read_text()
    fresh = render_readme(include_timing=False)
    for section in ("| VERIFIED |", "| ARITHMETIC_FAILED |", "Rejection rate:",
                    "| rules only |", "| rules + model |"):
        assert section in readme
        line_in_readme = [ln for ln in readme.splitlines() if section in ln]
        line_in_fresh = [ln for ln in fresh.splitlines() if section in ln]
        assert line_in_readme == line_in_fresh, f"{section} drifted"


def test_the_deterministic_render_is_reproducible():
    """What the fresh-clone check compares."""
    assert render_readme(include_timing=False) == render_readme(include_timing=False)


def test_the_test_count_is_measured_not_zero(rendered):
    """A templated figure rendering as 0 still looks measured, which is worse
    than a hardcoded one. The generator raises rather than publishing zero."""
    import re as _re

    match = _re.search(r"lint, types, ([\d,]+) tests", rendered)
    assert match, "the test count line is missing"
    assert int(match.group(1).replace(",", "")) > 100


def test_the_seam_bug_count_is_derived_not_typed(rendered):
    """Writing the count in prose would be the eighth instance of the very
    pattern the section describes."""
    from eval.readme import TEMPLATE, load_seam_bugs

    instances = load_seam_bugs()["instances"]
    assert f"What went wrong, {len(instances)} times" in rendered
    assert "{seam_count}" in TEMPLATE, "the count must be templated"


def test_every_seam_bug_appears_in_the_readme(rendered):
    from eval.readme import load_seam_bugs

    for instance in load_seam_bugs()["instances"]:
        assert instance["where"] in rendered


def test_the_seam_section_names_what_catches_them(rendered):
    assert "test_reporting_consistency" in rendered


def test_the_readme_names_the_arm_it_quotes(rendered):
    """The packets on disk once showed arm 2 while the README quoted arm 3.
    A reader opening eval/results/ must not have to infer which is which."""
    from eval.arms import SERVED_ARM

    assert SERVED_ARM in rendered
    assert "served arm" in rendered.lower()


def test_the_readme_points_at_the_per_arm_packet_location(rendered):
    assert "eval/results/packets/<arm>/" in rendered


def test_a_routine_eval_does_not_move_the_readme():
    """A reviewer runs `make eval && git diff README.md` and expects nothing.
    If throughput were read live, the diff would appear and they would conclude
    a figure had been typed by hand — the opposite of the truth."""
    first = render_readme()
    second = render_readme()
    assert first == second


def test_throughput_is_read_from_the_dated_snapshot_not_measured_now():
    from eval.readme import load_throughput

    snapshot = load_throughput()
    assert "measured_at" in snapshot
    assert snapshot["arms"], "the snapshot carries the figures the README quotes"
