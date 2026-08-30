"""Candidate sets union; they never replace one another.

**The verifier is sound but not complete.** It can only detect ambiguity among
hypotheses that were *generated*. A proposer that misses the second valid
explanation converts a correctly-ambiguous case into either false confidence
or a false "nothing verified" — and a weaker proposer makes that more likely,
not less. The guard catches fabrication; it cannot catch omission.

Observed: hc13 is built so two decompositions reconcile exactly. Under arm 2
it correctly reported AMBIGUOUS_MULTIPLE_VERIFIED. Under arm 3 the model
provider *displaced* the deterministic set, found neither explanation, and
reported NO_HYPOTHESIS_VERIFIED — losing a correct ambiguity finding, and
leaving the project's most important hard case with no coverage in the arm
being shipped.

Unioning fixes both: the model can only ever add resolutions, never remove
ambiguity the deterministic set already established.
"""


from statesync.classifier.provider import (
    MAX_HYPOTHESES,
    FixtureHypothesisProvider,
    HypothesisRequest,
    UnionHypothesisProvider,
)
from statesync.classifier.verifier import ArtifactIndex, Component, Proposal

ARTIFACTS = ArtifactIndex({"pay_1", "stl_1"})


def comp(name, amount, cites="pay_1"):
    return Component(name=name, amount_paise=amount, cites=cites)


def request(case="pay_1"):
    return HypothesisRequest(residual_paise=1140, instrument="upi",
                             artifacts=ARTIFACTS, case_id=case)


def test_the_union_returns_both_sets():
    a = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("a", 1)])]})
    b = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("b", 2)])]})
    proposals = UnionHypothesisProvider([a, b]).propose(request())
    assert [c.name for p in proposals for c in p.components] == ["a", "b"]


def test_the_deterministic_set_comes_first():
    """Order matters for labelling: H1 should be the explanation the system
    could derive on its own, not the one it was handed."""
    a = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("deterministic", 1)])]})
    b = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("model", 2)])]})
    proposals = UnionHypothesisProvider([a, b]).propose(request())
    assert proposals[0].components[0].name == "deterministic"


def test_a_provider_that_proposes_nothing_does_not_erase_the_others():
    """The regression this exists to prevent."""
    a = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("a", 1)])]})
    empty = FixtureHypothesisProvider({})
    assert len(UnionHypothesisProvider([a, empty]).propose(request())) == 1


def test_identical_proposals_are_not_counted_twice():
    """Two providers arriving at the same explanation is agreement, not
    ambiguity. Counting it twice would manufacture a second 'verified'."""
    same = [Proposal(components=[comp("mdr", 1140)])]
    a = FixtureHypothesisProvider({"pay_1": list(same)})
    b = FixtureHypothesisProvider({"pay_1": list(same)})
    assert len(UnionHypothesisProvider([a, b]).propose(request())) == 1


def test_the_cap_applies_across_the_union():
    many = [Proposal(components=[comp("x", i)]) for i in range(8)]
    a = FixtureHypothesisProvider({"pay_1": list(many)})
    b = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("y", i)])
                                             for i in range(8)]})
    assert len(UnionHypothesisProvider([a, b]).propose(request())) == MAX_HYPOTHESES


def test_calls_are_counted_across_members():
    a = FixtureHypothesisProvider({})
    b = FixtureHypothesisProvider({})
    union = UnionHypothesisProvider([a, b])
    union.propose(request())
    assert union.calls == 1
    assert a.calls == 1 and b.calls == 1


def test_model_calls_are_attributable_to_the_model_member():
    """`llm_calls` must not include the deterministic member's consultations."""
    fixtures = FixtureHypothesisProvider({})
    model = FixtureHypothesisProvider({})
    union = UnionHypothesisProvider([fixtures, model], model_index=1)
    union.propose(request())
    union.propose(request())
    assert union.model_calls == 2


def test_a_failing_member_does_not_take_down_the_union():
    """A degraded model must not cost the deterministic explanations too."""
    class Broken:
        calls = 0

        def propose(self, request):
            raise TimeoutError("provider down")

    a = FixtureHypothesisProvider({"pay_1": [Proposal(components=[comp("a", 1)])]})
    union = UnionHypothesisProvider([a, Broken()])
    assert len(union.propose(request())) == 1
    assert union.degraded is True


def test_the_retry_pass_is_passed_through_to_members():
    seen = []

    class Recording(FixtureHypothesisProvider):
        def propose(self, request):
            seen.append(list(request.rejected_summaries))
            return super().propose(request)

    union = UnionHypothesisProvider([Recording({}), Recording({})])
    union.propose(request().with_rejections(["H1 failed"]))
    assert seen == [["H1 failed"], ["H1 failed"]]
