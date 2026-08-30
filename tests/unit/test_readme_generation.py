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


def _blank_throughput(text: str) -> str:
    """Blank only the throughput column.

    Wall-clock figures cannot be byte-identical between runs, but the LLM call
    count beside them is a real measurement and must still be compared.
    """
    return re.sub(r"\| [\d,]+ (\| \d+ \|)$", r"| ~ \1", text, flags=re.M)


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
        current = _blank_throughput(readme.read_text())
        expected = _blank_throughput(render_readme(include_timing=False))
        assert current == expected, "README.md is stale; run make readme"


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
