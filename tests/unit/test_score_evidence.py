"""The audit trail, and the invariant that the score is nothing but its sum.

``ScoredPerson`` refuses to validate with a non-zero score and no evidence. That
model rule is only half the guarantee: it stops an evidence-less score being
*stored*, but it cannot stop the numbers drifting away from the sentences. The
stage builds evidence first and derives the components by summing it, so the
tests here assert the arithmetic identity directly -- score == sum of every
evidence contribution -- on every person of every fixture, plus the rules that
keep the trail readable and honest when it gets long.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from interlayer import score as score_stage
from interlayer.config import ScoreWeights, Settings
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Cluster,
    EvidenceItem,
    GraphEdge,
    MutualObservation,
    Org,
    OrgKind,
    Person,
    Position,
    Provenance,
    ScoreComponents,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
)
from interlayer.score.evidence import (
    EVIDENCE_KINDS,
    MAX_ITEMS_PER_KIND,
    Signal,
    condense,
    damped,
)

BUCKET_FOR_KIND = {
    "observed_mutual": "direct",
    "direct_employment": "direct",
    "past_employment": "direct",
    "shared_employer": "direct",
    "shared_school": "alumni",
    "path_proximity": "proximity",
    "cluster_membership": "cluster",
    "title_signal": "title",
}


def person(name: str, **kw: object) -> Person:
    return Person.make(name, "Trail", linkedin_slug=name.lower().replace(" ", "-"), **kw)


def edge(a: str, b: str, weight: float = 1.0) -> GraphEdge:
    return GraphEdge(source=a, target=b, weight=weight)


JANE = Org.make("Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET)
CITADEL_LLC = Org.make(
    "Citadel LLC", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.CITADEL_LLC
)
CITADEL_SECURITIES = Org.make(
    "Citadel Securities",
    OrgKind.COMPANY,
    is_target=True,
    target_firm=TargetFirm.CITADEL_SECURITIES,
)
ACME = Org.make("Acme Widgets", OrgKind.COMPANY)
UNIVERSITY = Org.make("State University", OrgKind.SCHOOL)
ORGS = (JANE, CITADEL_LLC, CITADEL_SECURITIES, ACME, UNIVERSITY)


# ===========================================================================
# a world exercising every evidence kind at once
# ===========================================================================


@dataclass(frozen=True)
class World:
    cfg: Settings
    people: tuple[Person, ...]
    scored: tuple[ScoredPerson, ...]
    by_id: dict[str, ScoredPerson]
    bridge: Person
    insider: Person
    former: Person
    classmate: Person
    quant: Person


def build_world(tmp_path: Path, **overrides: object) -> World:
    """Eight people arranged so that every evidence kind fires somewhere."""
    insider = person(
        "Current Insider",
        positions=(Position(company_raw="Jane Street", title_raw="Trader", is_current=True),),
    )
    former = person("Former Insider")
    llc = person("Fund Insider")
    securities = person("Market Maker Insider")
    colleague = person("Old Colleague")
    classmate = person("Old Classmate")
    quant = person(
        "Quant Bridge",
        positions=(
            Position(
                company_raw="Acme Widgets", title_raw="Quantitative Researcher", is_current=True
            ),
        ),
    )
    bridge = person("Recorded Bridge")
    people = (insider, former, llc, securities, colleague, classmate, quant, bridge)

    targets = [
        TargetPerson.make("Read Target One", TargetFirm.JANE_STREET, linkedin_slug="rt1"),
        TargetPerson.make("Read Target Two", TargetFirm.CITADEL_LLC, linkedin_slug="rt2"),
        TargetPerson.make("Read Target Three", TargetFirm.CITADEL_SECURITIES, linkedin_slug="rt3"),
    ]
    observations = [
        MutualObservation(
            target_person_id=targets[0].target_person_id,
            bridge_person_ids=(bridge.person_id, quant.person_id),
            observed_on=date(2026, 3, 1),
            stated_count=9,
        ),
        MutualObservation(
            target_person_id=targets[1].target_person_id,
            bridge_person_ids=(bridge.person_id,),
        ),
        MutualObservation(
            target_person_id=targets[2].target_person_id,
            bridge_person_ids=(bridge.person_id,),
        ),
    ]

    def stint(
        who: Person,
        org: Org,
        *,
        start: int,
        end: int | None = None,
        current: bool = False,
        kind: AffiliationKind = AffiliationKind.EMPLOYMENT,
        title: str | None = None,
    ) -> Affiliation:
        return Affiliation(
            person_id=who.person_id,
            org_id=org.org_id,
            kind=kind,
            title=title,
            start=ApproxDate(year=start),
            end=ApproxDate(year=end) if end else None,
            is_current=current,
        )

    affiliations = [
        stint(insider, JANE, start=2019, current=True, title="Trader"),
        stint(former, JANE, start=2012, end=2016),
        stint(llc, CITADEL_LLC, start=2018, current=True),
        stint(securities, CITADEL_SECURITIES, start=2018, current=True),
        stint(insider, ACME, start=2014, end=2018),
        stint(colleague, ACME, start=2015, end=2019),
        stint(quant, ACME, start=2016, end=2020, title="Quantitative Researcher"),
        stint(insider, UNIVERSITY, start=2008, end=2012, kind=AffiliationKind.EDUCATION),
        stint(classmate, UNIVERSITY, start=2009, end=2013, kind=AffiliationKind.EDUCATION),
        stint(llc, UNIVERSITY, start=2008, end=2012, kind=AffiliationKind.EDUCATION),
    ]

    ids = {p.person_id for p in people}
    edges = [
        edge(insider.person_id, colleague.person_id, 0.8),
        edge(insider.person_id, quant.person_id, 0.6),
        edge(insider.person_id, classmate.person_id, 0.4),
        edge(former.person_id, colleague.person_id, 0.5),
        edge(llc.person_id, securities.person_id, 0.2),
        edge(llc.person_id, classmate.person_id, 0.3),
        edge(bridge.person_id, quant.person_id, 0.7),
        edge(bridge.person_id, colleague.person_id, 0.1),
    ]
    assert {e.source for e in edges} | {e.target for e in edges} <= ids

    clusters = [
        Cluster(
            cluster_id="cluster_alpha",
            label="Acme Widgets - Quantitative Researcher",
            member_ids=tuple(sorted(p.person_id for p in (insider, colleague, quant, classmate))),
            top_orgs=(("Acme Widgets", 3),),
            top_titles=(("Quantitative Researcher", 1),),
            resolution=2.0,
        ),
        Cluster(
            cluster_id="cluster_beta",
            label="Citadel LLC - Analyst",
            member_ids=tuple(sorted(p.person_id for p in (llc, securities, former, bridge))),
            top_orgs=(("Citadel LLC", 1),),
            resolution=2.0,
        ),
    ]

    cfg = Settings(artifact_dir=tmp_path / "artifacts", consensus_runs=5, **overrides)  # type: ignore[arg-type]
    scored, _ = score_stage.score_all(
        cfg,
        people=list(people),
        edges=edges,
        clusters=clusters,
        orgs=list(ORGS),
        affiliations=affiliations,
        observations=observations,
        targets=targets,
    )
    return World(
        cfg=cfg,
        people=people,
        scored=tuple(scored),
        by_id={s.person_id: s for s in scored},
        bridge=bridge,
        insider=insider,
        former=former,
        classmate=classmate,
        quant=quant,
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return build_world(tmp_path)


def kinds_present(scored: Sequence[ScoredPerson]) -> set[str]:
    return {item.kind for row in scored for item in row.evidence}


def test_the_fixture_exercises_every_evidence_kind(world: World) -> None:
    """Guards the tests below: an invariant that holds because a signal never fired
    is not an invariant that was tested."""
    assert kinds_present(world.scored) == set(EVIDENCE_KINDS)


# ===========================================================================
# the arithmetic identity
# ===========================================================================


def test_score_equals_the_sum_of_its_evidence_contributions(world: World) -> None:
    """The whole design: there is no arrangement of the code in which the number
    moves without a sentence explaining the movement."""
    for scored in world.scored:
        total = sum(item.contribution for item in scored.evidence)
        assert scored.score == pytest.approx(total, abs=1e-6), scored.full_name


def test_components_partition_the_evidence_by_bucket(world: World) -> None:
    """Each ``ScoreComponents`` field is the sum of exactly the evidence kinds that
    map to it -- the five fields against eight weights mapping stated once."""
    for scored in world.scored:
        expected = {"direct": 0.0, "alumni": 0.0, "proximity": 0.0, "cluster": 0.0, "title": 0.0}
        for item in scored.evidence:
            expected[BUCKET_FOR_KIND[item.kind]] += item.contribution
        actual = scored.components.model_dump()
        for bucket, value in expected.items():
            assert actual[bucket] == pytest.approx(value, abs=1e-6), (scored.full_name, bucket)


def test_components_total_is_the_reported_score(world: World) -> None:
    for scored in world.scored:
        assert scored.score == pytest.approx(scored.components.total, abs=1e-9)


def test_score_components_total_is_the_plain_sum() -> None:
    components = ScoreComponents(direct=6.0, alumni=1.5, proximity=2.0, cluster=1.0, title=0.5)
    assert components.total == pytest.approx(11.0)
    assert ScoreComponents().total == 0.0


def test_no_evidence_item_is_a_silent_zero(world: World) -> None:
    """An item worth nothing is noise in a trail a human has to read."""
    for scored in world.scored:
        for item in scored.evidence:
            assert item.contribution > 0.0, (scored.full_name, item.kind)


def test_the_identity_survives_a_reweighted_configuration(tmp_path: Path) -> None:
    """It is an identity, not a coincidence of the default weights."""
    world = build_world(
        tmp_path,
        weights=ScoreWeights(
            observed_mutual=25.0,
            direct_employment=1.0,
            past_employment=0.25,
            alumni_overlap=7.0,
            shared_employer=3.0,
            proximity=0.5,
            cluster=4.0,
            title_signal=2.0,
        ),
    )
    for scored in world.scored:
        assert scored.score == pytest.approx(
            sum(item.contribution for item in scored.evidence), abs=1e-6
        )
    top = min(world.scored, key=lambda s: s.rank)
    assert top.person_id == world.bridge.person_id


# ===========================================================================
# evidence is mandatory
# ===========================================================================


def test_every_non_zero_scored_person_carries_evidence(world: World) -> None:
    scored_at_all = [s for s in world.scored if s.score != 0.0]
    assert scored_at_all, "the fixture scored nobody; the assertion would be vacuous"
    for scored in scored_at_all:
        assert scored.evidence
        assert all(item.detail.strip() for item in scored.evidence)


def test_the_stage_never_constructs_an_evidence_less_non_zero_score(world: World) -> None:
    """The model raises; this asserts the stage never gets close enough to find out."""
    for scored in world.scored:
        assert bool(scored.evidence) or scored.score == 0.0


def test_the_model_refuses_a_score_without_evidence() -> None:
    with pytest.raises(ValidationError, match="must carry the evidence"):
        ScoredPerson(person_id="p_x", full_name="No Trail", score=4.0)
    # Zero is fine: a person with no link to anything has nothing to explain.
    assert ScoredPerson(person_id="p_x", full_name="No Trail", score=0.0).evidence == ()


def test_zero_scored_people_carry_no_evidence_either(world: World) -> None:
    for scored in world.scored:
        if scored.score == 0.0:
            assert not scored.evidence


# ===========================================================================
# provenance
# ===========================================================================


def test_observed_provenance_belongs_to_mutual_readings_only() -> None:
    observed = EvidenceItem(
        kind="observed_mutual", detail="Seen on the profile.", provenance=Provenance.OBSERVED
    )
    assert observed.provenance is Provenance.OBSERVED
    with pytest.raises(ValidationError, match="only valid for kind"):
        EvidenceItem(
            kind="shared_employer",
            detail="Inference dressed as fact.",
            provenance=Provenance.OBSERVED,
        )


def test_evidence_defaults_to_inferred() -> None:
    assert EvidenceItem(kind="path_proximity", detail="A walk.").provenance is Provenance.INFERRED


def test_the_bridge_is_the_only_person_with_observed_evidence(world: World) -> None:
    observed = {s.person_id for s in world.scored if s.has_observed_evidence}
    assert observed == {world.bridge.person_id, world.quant.person_id}
    for scored in world.scored:
        for item in scored.evidence:
            assert (item.provenance is Provenance.OBSERVED) == (item.kind == "observed_mutual")


def test_an_observed_item_says_it_is_not_an_inference(world: World) -> None:
    """``detail`` is rendered verbatim to a human, so the distinction has to be in
    the sentence -- the report cannot add it later."""
    bridge = world.by_id[world.bridge.person_id]
    observed = [item for item in bridge.evidence if item.kind == "observed_mutual"]
    assert observed
    for item in observed:
        assert "not an inference" in item.detail
        assert item.hops == 1
        assert item.via_person_id


def test_a_truncated_reading_still_counts_and_says_so(tmp_path: Path) -> None:
    """Absence from a truncated list proves nothing, but the names that *were* seen
    are real, so a truncated observation must not be discarded."""
    bridge = person("Truncated Bridge")
    other = person("Someone Else")
    target = TargetPerson.make("Partly Read", TargetFirm.JANE_STREET, linkedin_slug="pr1")
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=(bridge.person_id,),
        stated_count=17,
    )
    assert observation.truncated
    cfg = Settings(artifact_dir=tmp_path / "artifacts", consensus_runs=5)
    scored, _ = score_stage.score_all(
        cfg,
        people=[bridge, other],
        edges=[edge(bridge.person_id, other.person_id)],
        clusters=[],
        orgs=list(ORGS),
        affiliations=[],
        observations=[observation],
        targets=[target],
    )
    row = next(s for s in scored if s.person_id == bridge.person_id)
    item = next(e for e in row.evidence if e.kind == "observed_mutual")
    assert item.contribution == pytest.approx(ScoreWeights().observed_mutual)
    assert "truncated at 1 of 17" in item.detail
    assert "floor, not a full reading" in item.detail


def test_details_are_sentences_a_human_can_read(world: World) -> None:
    for scored in world.scored:
        for item in scored.evidence:
            assert item.detail == item.detail.strip()
            assert item.detail.endswith(".")
            assert item.detail[0].isupper() or item.detail.startswith("And ")
            assert len(item.detail.split()) >= 5


def test_redaction_keeps_names_out_of_the_pre_rendered_details(tmp_path: Path) -> None:
    """The sentence is written once and rendered verbatim, so redaction has to
    happen where it is written or the report cannot strip it out later."""
    plain = build_world(tmp_path / "plain")
    redacted = build_world(tmp_path / "redacted", redact=True)
    names = {p.full_name for p in plain.people}

    plain_details = " ".join(i.detail for s in plain.scored for i in s.evidence)
    redacted_details = " ".join(i.detail for s in redacted.scored for i in s.evidence)
    assert any(name in plain_details for name in names)
    assert not any(name in redacted_details for name in names)
    assert [s.score for s in plain.scored] == [s.score for s in redacted.scored]


# ===========================================================================
# damping and condensation
# ===========================================================================


def test_damped_is_harmonic() -> None:
    assert damped(10.0, 1) == 10.0
    assert damped(10.0, 2) == 5.0
    assert damped(10.0, 4) == 2.5


def test_damped_rejects_a_zero_based_rank() -> None:
    with pytest.raises(ValueError, match="1-based"):
        damped(10.0, 0)


def test_damping_stops_weak_links_out_voting_observed_evidence() -> None:
    """Twenty shared employers at 1.0 each would beat one observed mutual at 10.0
    without damping. With it they reach ~3.6."""
    weights = ScoreWeights()
    weak = sum(damped(weights.shared_employer, rank) for rank in range(1, 21))
    assert weak < weights.observed_mutual
    assert 3.0 < weak < 4.0


def _signal(contribution: float, firm: TargetFirm = TargetFirm.JANE_STREET) -> Signal:
    return Signal(
        item=EvidenceItem(
            kind="shared_employer", detail="Worked somewhere together.", contribution=contribution
        ),
        bucket="direct",
        per_firm=((firm, contribution),),
    )


def test_condense_leaves_a_short_trail_alone() -> None:
    signals = [_signal(1.0 / n) for n in range(1, MAX_ITEMS_PER_KIND + 1)]
    assert condense(signals, summary=lambda n, total: "unused") == signals


def test_condense_preserves_the_total_it_rolls_up() -> None:
    """A trail that quietly omits half the total is worse than no trail, because it
    looks complete."""
    signals = [_signal(1.0 / n) for n in range(1, 16)]
    original = sum(s.item.contribution for s in signals)
    rolled = condense(signals, summary=lambda n, total: f"And {n} more, adding {total:.2f}.")
    assert len(rolled) == MAX_ITEMS_PER_KIND + 1
    assert sum(s.item.contribution for s in rolled) == pytest.approx(original, abs=1e-6)
    assert rolled[-1].item.detail.startswith("And 9 more, adding ")
    assert rolled[-1].item.kind == signals[0].item.kind
    assert rolled[-1].bucket == signals[0].bucket


def test_condense_rolls_per_firm_attribution_up_too() -> None:
    signals = [
        _signal(1.0, TargetFirm.CITADEL_LLC if n % 2 else TargetFirm.CITADEL_SECURITIES)
        for n in range(15)
    ]
    rolled = condense(signals, summary=lambda n, total: f"And {n} more, adding {total:.2f}.")
    tail = dict(rolled[-1].per_firm)
    assert set(tail) == {TargetFirm.CITADEL_LLC, TargetFirm.CITADEL_SECURITIES}
    assert sum(tail.values()) == pytest.approx(len(signals) - MAX_ITEMS_PER_KIND)


def test_condense_keeps_observed_provenance_on_the_rolled_item() -> None:
    signals = [
        Signal(
            item=EvidenceItem(
                kind="observed_mutual",
                detail="Seen on a profile.",
                contribution=1.0,
                provenance=Provenance.OBSERVED,
            ),
            bucket="direct",
        )
        for _ in range(MAX_ITEMS_PER_KIND + 3)
    ]
    rolled = condense(signals, summary=lambda n, total: f"And {n} more, adding {total:.2f}.")
    assert rolled[-1].item.provenance is Provenance.OBSERVED
    assert rolled[-1].item.kind == "observed_mutual"


def test_a_long_colleague_trail_is_capped_but_still_adds_up(tmp_path: Path) -> None:
    """The cap is a readability limit, not an accounting one: the identity must
    survive the roll-up on a real scoring run."""
    insiders = [person(f"Insider {i:02d}") for i in range(MAX_ITEMS_PER_KIND + 6)]
    bridge = person("Very Connected")
    affiliations = [
        Affiliation(
            person_id=who.person_id,
            org_id=JANE.org_id,
            kind=AffiliationKind.EMPLOYMENT,
            start=ApproxDate(year=2019),
            is_current=True,
        )
        for who in insiders
    ]
    affiliations += [
        Affiliation(
            person_id=who.person_id,
            org_id=ACME.org_id,
            kind=AffiliationKind.EMPLOYMENT,
            start=ApproxDate(year=2015),
            end=ApproxDate(year=2020),
        )
        for who in (*insiders, bridge)
    ]
    cfg = Settings(artifact_dir=tmp_path / "artifacts", consensus_runs=5)
    scored, _ = score_stage.score_all(
        cfg,
        people=[*insiders, bridge],
        edges=[edge(bridge.person_id, who.person_id) for who in insiders],
        clusters=[],
        orgs=list(ORGS),
        affiliations=affiliations,
    )
    row = next(s for s in scored if s.person_id == bridge.person_id)
    shared = [item for item in row.evidence if item.kind == "shared_employer"]
    assert len(shared) == MAX_ITEMS_PER_KIND + 1
    assert row.score == pytest.approx(sum(i.contribution for i in row.evidence), abs=1e-6)
    assert "further colleagues" in shared[-1].detail


# ===========================================================================
# ordering and determinism of the trail
# ===========================================================================


def test_observed_evidence_is_listed_first(world: World) -> None:
    """The operator should read the fact before the guesses."""
    bridge = world.by_id[world.bridge.person_id]
    kinds = [item.kind for item in bridge.evidence]
    assert kinds[0] == "observed_mutual"
    assert "observed_mutual" not in kinds[kinds.index("observed_mutual") + 3 :]


def test_the_same_inputs_produce_the_same_trail_twice(tmp_path: Path) -> None:
    first = build_world(tmp_path / "one")
    second = build_world(tmp_path / "two")
    assert [s.model_dump() for s in first.scored] == [s.model_dump() for s in second.scored]


def test_evidence_is_a_frozen_tuple(world: World) -> None:
    """Every contract model is frozen; a mutable trail could drift from its score."""
    bridge = world.by_id[world.bridge.person_id]
    assert isinstance(bridge.evidence, tuple)
    with pytest.raises(ValidationError):
        bridge.score = 99.0
