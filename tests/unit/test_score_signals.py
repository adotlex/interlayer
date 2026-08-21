"""The signals that move a score, and the walk they are measured against.

Three properties here are the ones an operator's trust rests on:

* **Observed beats inferred.** A human who read a mutual-connections list knows
  something this tool can only guess at, and ``weights.observed_mutual`` (10.0)
  against ``direct_employment`` (6.0) has to survive contact with the rest of the
  pipeline, not just the config file.
* **Citadel LLC and Citadel Securities never merge.** They are separate companies
  with separate staff; a summed "Citadel" number would be a firm that does not
  exist.
* **The walk is a walk.** Personalized PageRank must conserve mass, must leave the
  seed set at alpha=0.85, and must not simply hand the top of the ranking back to
  the seeds that defined it.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from interlayer import score as score_stage
from interlayer.config import ScoreWeights, Settings
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Cluster,
    GraphEdge,
    MutualObservation,
    Org,
    OrgKind,
    Person,
    Position,
    Provenance,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
)
from interlayer.score.network import Network, personalized_pagerank
from interlayer.score.signals import (
    FIRM_LABELS,
    MAX_EVIDENTIAL_ORG_MEMBERS,
    TITLE_KEYWORDS,
    build_context,
    colleague_signals,
    split_by_firm,
    title_signals,
)


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(artifact_dir=tmp_path / "artifacts", consensus_runs=5, **overrides)  # type: ignore[arg-type]


def person(name: str, **kw: object) -> Person:
    return Person.make(name, "Case", linkedin_slug=name.lower().replace(" ", "-"), **kw)


def employment(
    who: Person, org: Org, *, start: int = 2018, end: int | None = None, current: bool = False
) -> Affiliation:
    return Affiliation(
        person_id=who.person_id,
        org_id=org.org_id,
        kind=AffiliationKind.EMPLOYMENT,
        start=ApproxDate(year=start),
        end=ApproxDate(year=end) if end else None,
        is_current=current,
    )


def edge(a: Person, b: Person, weight: float = 1.0) -> GraphEdge:
    return GraphEdge(source=a.person_id, target=b.person_id, weight=weight)


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
MEGACORP = Org.make("Global Mega Corp", OrgKind.COMPANY)
UNIVERSITY = Org.make("State University", OrgKind.SCHOOL)
ALL_ORGS = (JANE, CITADEL_LLC, CITADEL_SECURITIES, ACME, MEGACORP, UNIVERSITY)


# ===========================================================================
# personalized PageRank
# ===========================================================================


@dataclass(frozen=True)
class Walkable:
    network: Network
    seeds: list[int]
    seed_ids: tuple[str, ...]


@pytest.fixture(scope="module")
def walkable() -> Walkable:
    """A sparse network with scattered seeds -- the shape a backbone actually has."""
    rng = random.Random(11)
    n = 300
    ids = [f"p{i:04d}" for i in range(n)]
    pairs: set[tuple[str, str]] = set()
    for i in range(1, n):  # a spanning tree, so the graph is connected
        j = rng.randrange(i)
        pairs.add((min(ids[i], ids[j]), max(ids[i], ids[j])))
    while len(pairs) < 460:
        a, b = rng.sample(ids, 2)
        pairs.add((min(a, b), max(a, b)))
    network = Network.from_edges((u, v, 1.0) for u, v in sorted(pairs))
    seed_ids = tuple(rng.sample(sorted(network.node_ids), 10))
    return Walkable(
        network=network,
        seeds=sorted(network.index[s] for s in seed_ids),
        seed_ids=seed_ids,
    )


def test_pagerank_mass_sums_to_one(walkable: Walkable) -> None:
    vector = personalized_pagerank(walkable.network, walkable.seeds, alpha=0.85)
    assert len(vector) == walkable.network.size
    assert sum(vector) == pytest.approx(1.0, abs=1e-9)
    assert all(value >= 0.0 for value in vector)


def test_most_pagerank_mass_lands_off_the_seed_set_at_alpha_085(walkable: Walkable) -> None:
    """alpha is the *damping* probability, so restart is 1-alpha. At alpha=0.5 the
    walk barely leaves home and tells you nothing about anyone new; 0.85 is chosen
    because most of the mass ends up on people the operator has not already found."""
    vector = personalized_pagerank(walkable.network, walkable.seeds, alpha=0.85)
    off_seed = 1.0 - sum(vector[s] for s in walkable.seeds)
    assert off_seed > 0.70, f"only {off_seed:.1%} of the mass left the seed set"


def test_lower_alpha_traps_more_mass_on_the_seeds(walkable: Walkable) -> None:
    """The convention footgun, asserted: if alpha were the *restart* probability
    this ordering would invert."""
    retained = []
    for alpha in (0.5, 0.7, 0.85, 0.95):
        vector = personalized_pagerank(walkable.network, walkable.seeds, alpha=alpha)
        retained.append(sum(vector[s] for s in walkable.seeds))
    assert retained == sorted(retained, reverse=True), retained
    assert retained[0] > 2 * retained[-1]


def test_seeds_do_not_trivially_rank_themselves_top() -> None:
    """A hub every seed is attached to must outrank the seeds themselves; otherwise
    the walk is just reading the restart vector back and the ranking is circular."""
    network = Network.from_edges(
        [("hub", f"seed{i}", 1.0) for i in (1, 2, 3)]
        + [("hub", f"other{i}", 1.0) for i in range(1, 6)]
    )
    seeds = [network.index[f"seed{i}"] for i in (1, 2, 3)]
    vector = personalized_pagerank(network, seeds, alpha=0.85)
    mass = dict(zip(network.node_ids, vector, strict=True))
    assert mass["hub"] > max(mass[f"seed{i}"] for i in (1, 2, 3))
    assert max(mass, key=lambda k: mass[k]) == "hub"


def test_pagerank_without_seeds_is_all_zero(walkable: Walkable) -> None:
    """The honest answer to "how close is everyone to a target firm" when no
    target-firm person is known -- not a uniform distribution that looks like data."""
    assert set(personalized_pagerank(walkable.network, [], alpha=0.85)) == {0.0}


def test_isolated_nodes_do_not_leak_mass() -> None:
    """Dangling mass is returned to the restart vector rather than vanishing; without
    that the vector stops summing to one and two runs become incomparable."""
    base = Network.from_edges([("a", "b", 1.0), ("b", "c", 1.0)])
    with_isolates = base.with_nodes(["x", "y", "z"])
    vector = personalized_pagerank(with_isolates, [with_isolates.index["a"]], alpha=0.85)
    assert sum(vector) == pytest.approx(1.0, abs=1e-9)
    assert vector[with_isolates.index["x"]] == 0.0


def test_pagerank_is_bit_identical_across_repeated_calls(walkable: Walkable) -> None:
    first = personalized_pagerank(walkable.network, walkable.seeds, alpha=0.85)
    second = personalized_pagerank(walkable.network, walkable.seeds, alpha=0.85)
    assert first == second


def test_pagerank_respects_edge_weight() -> None:
    network = Network.from_edges([("seed", "heavy", 9.0), ("seed", "light", 1.0)])
    vector = personalized_pagerank(network, [network.index["seed"]], alpha=0.85)
    mass = dict(zip(network.node_ids, vector, strict=True))
    assert mass["heavy"] > mass["light"]


def test_network_drops_self_loops_and_sums_duplicate_edges() -> None:
    network = Network.from_edges([("a", "a", 5.0), ("a", "b", 1.0), ("b", "a", 2.0)])
    assert network.node_ids == ("a", "b")
    assert network.adjacency[network.index["a"]] == ((network.index["b"], 3.0),)


# ===========================================================================
# hop distance
# ===========================================================================


def test_hops_from_is_correct_on_a_path_graph() -> None:
    network = Network.from_edges([(f"n{i}", f"n{i + 1}", 1.0) for i in range(6)])
    hops = dict(zip(network.node_ids, network.hops_from([network.index["n0"]]), strict=True))
    assert hops == {f"n{i}": i for i in range(7)}


def test_hops_from_takes_the_nearest_seed_and_ignores_weight() -> None:
    """ "How many introductions away" counts people, not edge strength."""
    network = Network.from_edges(
        [(f"n{i}", f"n{i + 1}", 0.001 if i == 3 else 100.0) for i in range(6)]
    )
    hops = dict(
        zip(
            network.node_ids,
            network.hops_from([network.index["n0"], network.index["n6"]]),
            strict=True,
        )
    )
    assert hops == {"n0": 0, "n1": 1, "n2": 2, "n3": 3, "n4": 2, "n5": 1, "n6": 0}


def test_hops_is_none_for_an_unreachable_person() -> None:
    network = Network.from_edges([("a", "b", 1.0), ("y", "z", 1.0)])
    hops = dict(zip(network.node_ids, network.hops_from([network.index["a"]]), strict=True))
    assert hops == {"a": 0, "b": 1, "y": None, "z": None}


def test_hops_to_target_is_reported_on_a_hand_built_path(tmp_path: Path) -> None:
    """The number the report renders as "N introductions away", end to end."""
    chain = [person(f"Link {i}") for i in range(5)]
    people = chain
    affiliations = [employment(chain[0], JANE, current=True)]
    edges = [edge(chain[i], chain[i + 1]) for i in range(4)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=edges,
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=affiliations,
    )
    hops = {s.person_id: s.hops_to_target for s in scored}
    assert [hops[p.person_id] for p in chain] == [0, 1, 2, 3, 4]


def test_hops_to_target_is_none_when_nobody_is_seeded(tmp_path: Path) -> None:
    people = [person(f"Alone {i}") for i in range(3)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=[edge(people[0], people[1])],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[],
    )
    assert {s.hops_to_target for s in scored} == {None}


# ===========================================================================
# the weight hierarchy
# ===========================================================================


@dataclass(frozen=True)
class Hierarchy:
    scored: dict[str, ScoredPerson]
    observed: Person
    insider: Person
    neighbour: Person
    outsider: Person


@pytest.fixture
def hierarchy(tmp_path: Path) -> Hierarchy:
    """One person whose only link is an observed mutual, one whose only link is
    employment inside a target firm, and two with neither."""
    observed = person("Observed Only")
    insider = person("Insider Only")
    neighbour = person("Neighbour")
    outsider = person("Outsider")
    people = [observed, insider, neighbour, outsider]

    target = TargetPerson.make("Recorded Target", TargetFirm.JANE_STREET, linkedin_slug="rt1")
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=(observed.person_id,),
    )
    edges = [edge(insider, neighbour), edge(observed, neighbour), edge(outsider, neighbour)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=edges,
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[employment(insider, JANE, current=True)],
        observations=[observation],
        targets=[target],
    )
    return Hierarchy(
        scored={s.person_id: s for s in scored},
        observed=observed,
        insider=insider,
        neighbour=neighbour,
        outsider=outsider,
    )


def test_observed_evidence_outranks_direct_employment(hierarchy: Hierarchy) -> None:
    """A human read this person's name off a target's profile. That is ground truth,
    and it must beat the strongest inference the tool can make about anybody else."""
    observed = hierarchy.scored[hierarchy.observed.person_id]
    insider = hierarchy.scored[hierarchy.insider.person_id]
    assert observed.score > insider.score, (observed.score, insider.score)
    assert observed.rank < insider.rank
    assert observed.has_observed_evidence
    assert not insider.has_observed_evidence


def test_only_observed_evidence_carries_observed_provenance(hierarchy: Hierarchy) -> None:
    """``Provenance.OBSERVED`` belongs to mutual-connection readings and nothing else;
    the report renders the two differently and may never present a guess as fact."""
    for scored in hierarchy.scored.values():
        for item in scored.evidence:
            if item.provenance is Provenance.OBSERVED:
                assert item.kind == "observed_mutual"
            else:
                assert item.kind != "observed_mutual"


def test_the_configured_weights_state_the_hierarchy() -> None:
    weights = ScoreWeights()
    assert weights.observed_mutual > weights.direct_employment > weights.past_employment
    assert weights.past_employment > weights.proximity > weights.alumni_overlap
    assert weights.alumni_overlap > weights.shared_employer >= weights.cluster
    assert weights.cluster > weights.title_signal


def test_ranks_are_dense_one_based_and_tie_broken_by_person_id(hierarchy: Hierarchy) -> None:
    ordered = sorted(hierarchy.scored.values(), key=lambda s: s.rank)
    assert [s.rank for s in ordered] == list(range(1, len(ordered) + 1))
    for left, right in itertools.pairwise(ordered):
        assert (-left.score, left.person_id) < (-right.score, right.person_id)


def test_tied_scores_rank_in_person_id_order(tmp_path: Path) -> None:
    """Two indistinguishable people must not swap places between runs."""
    people = [person(f"Twin {i}") for i in range(6)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=[edge(people[0], people[1])],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[],
    )
    assert {s.score for s in scored} == {0.0}
    ordered = sorted(scored, key=lambda s: s.rank)
    assert [s.person_id for s in ordered] == sorted(s.person_id for s in scored)


def test_the_ego_is_never_ranked(tmp_path: Path) -> None:
    """ "You are one hop from Jane Street" is not a lead."""
    ego = person("The Operator", is_ego=True)
    other = person("Somebody")
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=[ego, other],
        edges=[edge(ego, other)],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[employment(ego, JANE, current=True)],
    )
    assert [s.person_id for s in scored] == [other.person_id]


# ===========================================================================
# per-firm attribution
# ===========================================================================


@dataclass(frozen=True)
class Citadels:
    scored: dict[str, ScoredPerson]
    llc_only: Person
    securities_only: Person
    both: Person
    bridge: Person


@pytest.fixture
def citadels(tmp_path: Path) -> Citadels:
    llc_only = person("Fund Person")
    securities_only = person("Market Maker Person")
    both = person("Moved Across")
    bridge = person("Shared Colleague")
    people = [llc_only, securities_only, both, bridge]
    affiliations = [
        employment(llc_only, CITADEL_LLC, start=2018, current=True),
        employment(securities_only, CITADEL_SECURITIES, start=2018, current=True),
        employment(both, CITADEL_LLC, start=2014, end=2017),
        employment(both, CITADEL_SECURITIES, start=2018, current=True),
        employment(bridge, ACME, start=2015, end=2020),
        employment(llc_only, ACME, start=2015, end=2020),
        employment(securities_only, ACME, start=2015, end=2020),
    ]
    edges = [edge(llc_only, bridge), edge(securities_only, bridge), edge(both, bridge, 0.5)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=edges,
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=affiliations,
    )
    return Citadels(
        scored={s.person_id: s for s in scored},
        llc_only=llc_only,
        securities_only=securities_only,
        both=both,
        bridge=bridge,
    )


def test_citadel_llc_and_citadel_securities_are_separate_per_firm_entries(
    citadels: Citadels,
) -> None:
    """The load-bearing one. They are different companies with different staff, and
    a merged "Citadel" number would name a firm that does not exist."""
    moved = citadels.scored[citadels.both.person_id]
    assert set(moved.per_firm) == {TargetFirm.CITADEL_LLC, TargetFirm.CITADEL_SECURITIES}
    llc = moved.per_firm[TargetFirm.CITADEL_LLC]
    securities = moved.per_firm[TargetFirm.CITADEL_SECURITIES]
    assert llc > 0 and securities > 0
    assert llc != securities, "the two firms produced an identical number; check for a merge"
    # The current role (6.0) must outweigh the former one (3.0), firm by firm.
    assert securities > llc


def test_per_firm_never_sums_the_two_citadels(citadels: Citadels) -> None:
    """Each firm's number is built only from evidence naming that firm.

    "Moved Across" left Citadel LLC in 2017 (past employment, 3.0) and is now at
    Citadel Securities (direct employment, 6.0). Neither entry may carry the
    other's weight, and no entry may hold their total.
    """
    weights = ScoreWeights()
    moved = citadels.scored[citadels.both.person_id]
    llc = moved.per_firm[TargetFirm.CITADEL_LLC]
    securities = moved.per_firm[TargetFirm.CITADEL_SECURITIES]

    assert weights.past_employment <= llc < weights.past_employment + weights.proximity
    assert weights.direct_employment <= securities < weights.direct_employment + weights.proximity
    assert not any(value == pytest.approx(llc + securities) for value in moved.per_firm.values())

    for scored in citadels.scored.values():
        assert set(scored.per_firm) <= set(TargetFirm)
    assert not any(firm.value == "citadel" for firm in TargetFirm)
    assert FIRM_LABELS[TargetFirm.CITADEL_LLC] != FIRM_LABELS[TargetFirm.CITADEL_SECURITIES]


def test_employment_evidence_is_attributed_to_exactly_one_firm(citadels: Citadels) -> None:
    fund = citadels.scored[citadels.llc_only.person_id]
    employment_items = [
        e for e in fund.evidence if e.kind in {"direct_employment", "past_employment"}
    ]
    assert len(employment_items) == 1
    assert "Citadel LLC" in employment_items[0].detail
    assert "Citadel Securities" not in employment_items[0].detail


def test_a_colleague_signal_names_the_colleagues_firm_not_the_shared_employer(
    citadels: Citadels,
) -> None:
    bridge = citadels.scored[citadels.bridge.person_id]
    shared = [e for e in bridge.evidence if e.kind == "shared_employer"]
    assert len(shared) == 2
    named = {
        firm
        for firm in ("Citadel LLC", "Citadel Securities")
        for item in shared
        if firm in item.detail
    }
    assert named == {"Citadel LLC", "Citadel Securities"}
    assert all("Acme Widgets" in item.detail for item in shared)


def test_split_by_firm_divides_a_contribution_without_duplicating_it() -> None:
    """One random walk cannot be claimed in full by two firms."""
    split = split_by_firm(3.0, {TargetFirm.CITADEL_LLC: 1.0, TargetFirm.CITADEL_SECURITIES: 3.0})
    assert dict(split) == {
        TargetFirm.CITADEL_LLC: 0.75,
        TargetFirm.CITADEL_SECURITIES: 2.25,
    }
    assert sum(amount for _, amount in split) == pytest.approx(3.0)


def test_split_by_firm_is_empty_when_no_firm_has_mass() -> None:
    assert split_by_firm(3.0, {}) == ()
    assert split_by_firm(3.0, {TargetFirm.JANE_STREET: 0.0}) == ()


# ===========================================================================
# colleague and title signals
# ===========================================================================


def test_a_shared_employer_needs_overlapping_dates(tmp_path: Path) -> None:
    """No co-tenure, no link: two people five years apart at one firm have
    essentially zero chance of knowing each other."""
    insider = person("Target Insider")
    overlapping = person("Same Years")
    disjoint = person("Different Decade")
    people = [insider, overlapping, disjoint]
    affiliations = [
        employment(insider, JANE, start=2019, current=True),
        employment(insider, ACME, start=2012, end=2016),
        employment(overlapping, ACME, start=2014, end=2018),
        employment(disjoint, ACME, start=2000, end=2004),
    ]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=[edge(insider, overlapping), edge(insider, disjoint)],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=affiliations,
    )
    kinds = {s.person_id: {e.kind for e in s.evidence} for s in scored}
    assert "shared_employer" in kinds[overlapping.person_id]
    assert "shared_employer" not in kinds[disjoint.person_id]


def test_an_affiliation_marked_not_current_is_not_scored_as_a_present_role(
    tmp_path: Path,
) -> None:
    """FAILING -- ``Affiliation.is_current`` is never read by the score stage.

    ``signals.is_current`` claims to "prefer the export's explicit ``is_current``
    flag", but it looks for that flag on ``Person.positions`` guarded by
    ``position.company_org_id == affiliation.org_id`` -- and ``ingest`` documents
    that ``company_org_id`` stays ``None``, so the branch can never match real
    ingest output. Currency therefore reduces to ``affiliation.end is None``,
    and ``Affiliation.is_current`` -- the field that carries the flag through
    normalisation -- is consulted nowhere in the package.

    The model's own docstring calls the two distinct: "Still there. Distinct from
    an unrecorded end date, which is merely unknown." A stint whose end date was
    simply never recorded is scored as ``direct_employment`` at 6.0 and described
    to the operator in the present tense -- "Works at Jane Street ... inside the
    target firm" -- instead of ``past_employment`` at 3.0. That is the tool
    asserting a fact its input did not contain, in the one place the whole design
    is built to prevent it.
    """
    unknown_end = person("Dates Unknown")
    neighbour = person("Their Neighbour")
    affiliation = Affiliation(
        person_id=unknown_end.person_id,
        org_id=JANE.org_id,
        kind=AffiliationKind.EMPLOYMENT,
        start=ApproxDate(year=2012),
        end=None,
        is_current=False,
    )
    assert not affiliation.is_current

    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=[unknown_end, neighbour],
        edges=[edge(unknown_end, neighbour)],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[affiliation],
    )
    row = next(s for s in scored if s.person_id == unknown_end.person_id)
    item = next(e for e in row.evidence if e.kind in {"direct_employment", "past_employment"})
    assert item.kind == "past_employment", item.detail
    assert item.contribution == pytest.approx(ScoreWeights().past_employment)
    assert "Works at" not in item.detail


def test_a_school_lands_in_the_alumni_bucket_not_direct(tmp_path: Path) -> None:
    insider = person("Alum Insider")
    classmate = person("Classmate")
    school = [
        Affiliation(
            person_id=who.person_id,
            org_id=UNIVERSITY.org_id,
            kind=AffiliationKind.EDUCATION,
            start=ApproxDate(year=2008),
            end=ApproxDate(year=2012),
        )
        for who in (insider, classmate)
    ]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=[insider, classmate],
        edges=[edge(insider, classmate)],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[employment(insider, JANE, current=True), *school],
    )
    alum = next(s for s in scored if s.person_id == classmate.person_id)
    assert alum.components.alumni == pytest.approx(ScoreWeights().alumni_overlap)
    assert alum.components.direct == 0.0
    assert {e.kind for e in alum.evidence} >= {"shared_school"}


def test_a_very_large_employer_stops_being_evidence(tmp_path: Path) -> None:
    """Above ``MAX_EVIDENTIAL_ORG_MEMBERS`` a shared employer says almost nothing
    about acquaintance, and enumerating its pairs is expensive noise."""
    crowd = [person(f"Mega {i}") for i in range(MAX_EVIDENTIAL_ORG_MEMBERS + 2)]
    insider = crowd[0]
    affiliations = [employment(insider, JANE, current=True)]
    affiliations += [employment(who, MEGACORP, start=2015, end=2020) for who in crowd]
    ctx = build_context(
        settings_for(tmp_path),
        people=crowd,
        edges=[edge(crowd[0], crowd[1])],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=affiliations,
        observations=[],
        targets=[],
    )
    assert ctx.org_member_counts[MEGACORP.org_id] > MAX_EVIDENTIAL_ORG_MEMBERS
    assert colleague_signals(ctx, crowd[1]) == []


def test_colleague_signals_damp_harmonically(tmp_path: Path) -> None:
    """Twenty weak links must never out-shout one observed mutual, so the n-th link
    of a kind is worth weight/n."""
    insiders = [person(f"Insider {i}") for i in range(4)]
    bridge = person("Well Connected")
    affiliations = [employment(who, JANE, start=2019, current=True) for who in insiders]
    affiliations += [employment(who, ACME, start=2015, end=2020) for who in (*insiders, bridge)]
    ctx = build_context(
        settings_for(tmp_path),
        people=[*insiders, bridge],
        edges=[edge(bridge, who) for who in insiders],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=affiliations,
        observations=[],
        targets=[],
    )
    contributions = [s.item.contribution for s in colleague_signals(ctx, bridge)]
    weight = ScoreWeights().shared_employer
    assert contributions == [pytest.approx(weight / n) for n in range(1, 5)]
    assert sum(contributions) < 3 * ScoreWeights().shared_employer


def test_a_title_alone_never_puts_anybody_on_the_list(tmp_path: Path) -> None:
    """A job title says nothing about whose network someone is in."""
    quant = person(
        "Unlinked Quant",
        positions=(Position(company_raw="Nowhere", title_raw="Quant Trader", is_current=True),),
    )
    ctx = build_context(
        settings_for(tmp_path),
        people=[quant],
        edges=[],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[],
        observations=[],
        targets=[],
    )
    assert title_signals(ctx, quant, has_link=False) == []
    boosted = title_signals(ctx, quant, has_link=True)
    assert len(boosted) == 1
    assert boosted[0].item.contribution == pytest.approx(ScoreWeights().title_signal)
    assert boosted[0].bucket == "title"


def test_an_unremarkable_title_adds_nothing_even_with_a_link(tmp_path: Path) -> None:
    florist = person(
        "Florist",
        positions=(Position(company_raw="Nowhere", title_raw="Florist", is_current=True),),
    )
    ctx = build_context(
        settings_for(tmp_path),
        people=[florist],
        edges=[],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[],
        observations=[],
        targets=[],
    )
    assert title_signals(ctx, florist, has_link=True) == []
    assert not any(keyword in "florist" for keyword in TITLE_KEYWORDS)


# ===========================================================================
# clusters
# ===========================================================================


def test_clusters_rank_by_mean_non_seed_proximity_not_by_size(tmp_path: Path) -> None:
    """Summing would rank the biggest cluster first regardless of proximity, and
    counting seeds would rank a cluster by what the operator already knows."""
    insider = person("Seed Insider")
    close = [person(f"Close {i}") for i in range(3)]
    far = [person(f"Far {i}") for i in range(9)]
    people = [insider, *close, *far]
    edges = [edge(insider, who) for who in close]
    edges += [edge(close[0], far[0])]
    edges += [edge(far[0], who) for who in far[1:]]

    near_cluster = Cluster(
        cluster_id="cluster_near",
        label="Near",
        member_ids=tuple(sorted(p.person_id for p in close)),
        resolution=2.0,
    )
    far_cluster = Cluster(
        cluster_id="cluster_far",
        label="Far",
        member_ids=tuple(sorted(p.person_id for p in far)),
        resolution=2.0,
    )
    _, clusters = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=edges,
        clusters=[near_cluster, far_cluster],
        orgs=list(ALL_ORGS),
        affiliations=[employment(insider, JANE, current=True)],
    )
    ranked = sorted(clusters, key=lambda c: c.rank)
    assert [c.cluster_id for c in ranked] == ["cluster_near", "cluster_far"]
    assert ranked[0].size < ranked[1].size, "the smaller cluster must be allowed to win"
    assert [c.rank for c in ranked] == [1, 2]
    assert ranked[0].score > ranked[1].score


def test_cluster_rationale_reports_auditable_counts(tmp_path: Path) -> None:
    insider = person("Cluster Insider")
    others = [person(f"Member {i}") for i in range(3)]
    members = tuple(sorted(p.person_id for p in (insider, *others)))
    cluster = Cluster(
        cluster_id="cluster_one",
        label="A Group",
        member_ids=members,
        top_orgs=(("Acme Widgets", 3),),
        top_titles=(("Trader", 2),),
        resolution=2.0,
    )
    _, scored_clusters = score_stage.score_all(
        settings_for(tmp_path),
        people=[insider, *others],
        edges=[edge(insider, who) for who in others],
        clusters=[cluster],
        orgs=list(ALL_ORGS),
        affiliations=[employment(insider, JANE, current=True)],
    )
    row = scored_clusters[0]
    assert row.size == 4
    assert row.target_density == pytest.approx(0.25)
    assert "4 people" in row.rationale
    assert "Acme Widgets (3)" in row.rationale
    assert row.top_person_ids
    assert set(row.top_person_ids) <= set(members)


def test_a_mixed_resolution_cluster_file_is_filtered_to_one_partition(tmp_path: Path) -> None:
    """Nobody may end up in three overlapping clusters because two artifacts were
    concatenated by hand."""
    people = [person(f"Member {i}") for i in range(4)]
    members = tuple(sorted(p.person_id for p in people))
    clusters = [
        Cluster(
            cluster_id=f"cluster_{gamma:g}", label=f"g{gamma}", member_ids=members, resolution=gamma
        )
        for gamma in (1.0, 2.0, 4.0)
    ]
    scored_people, scored_clusters = score_stage.score_all(
        settings_for(tmp_path, resolution=2.0),
        people=people,
        edges=[edge(people[0], people[1])],
        clusters=clusters,
        orgs=list(ALL_ORGS),
        affiliations=[],
    )
    assert [c.cluster_id for c in scored_clusters] == ["cluster_2"]
    assert {s.cluster_id for s in scored_people} == {"cluster_2"}


# ===========================================================================
# no targets at all
# ===========================================================================


def test_no_targets_means_every_score_is_zero_and_nothing_crashes(tmp_path: Path) -> None:
    """A clean export with no target-firm people is a legitimate input. It must
    produce zeros, not an exception and not a fabricated ranking."""
    people = [person(f"Nobody {i}") for i in range(5)]
    affiliations = [employment(who, ACME, start=2018) for who in people]
    edges = [edge(people[0], who) for who in people[1:]]
    scored, clusters = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=edges,
        clusters=[],
        orgs=[ACME, MEGACORP, UNIVERSITY],
        affiliations=affiliations,
    )
    assert len(scored) == len(people)
    assert {s.score for s in scored} == {0.0}
    assert all(not s.evidence for s in scored)
    assert all(s.per_firm == {} for s in scored)
    assert all(s.hops_to_target is None for s in scored)
    assert [s.rank for s in sorted(scored, key=lambda s: s.rank)] == [1, 2, 3, 4, 5]
    assert clusters == []


def test_target_orgs_with_nobody_in_them_still_score_zero(tmp_path: Path) -> None:
    """The orgs are marked as targets but no person is affiliated with them."""
    people = [person(f"Unaffiliated {i}") for i in range(3)]
    scored, _ = score_stage.score_all(
        settings_for(tmp_path),
        people=people,
        edges=[edge(people[0], people[1])],
        clusters=[],
        orgs=list(ALL_ORGS),
        affiliations=[],
    )
    assert {s.score for s in scored} == {0.0}


def test_run_writes_both_artifacts(tmp_path: Path) -> None:
    """The stage end to end, off disk, exactly as the CLI invokes it."""
    from interlayer.io import read_jsonl, secure_dir, write_jsonl
    from interlayer.models import ScoredCluster

    cfg = settings_for(tmp_path)
    secure_dir(cfg.artifact_dir)
    insider = person("Disk Insider")
    others = [person(f"Disk Other {i}") for i in range(3)]
    members = tuple(sorted(p.person_id for p in (insider, *others)))
    write_jsonl(cfg.people_path, [insider, *others])
    write_jsonl(cfg.edges_path, [edge(insider, who) for who in others])
    write_jsonl(
        cfg.clusters_path,
        [Cluster(cluster_id="cluster_disk", label="Disk", member_ids=members, resolution=2.0)],
    )
    write_jsonl(cfg.orgs_path, list(ALL_ORGS))
    write_jsonl(cfg.affiliations_path, [employment(insider, JANE, current=True)])

    score_stage.run(cfg)
    people_rows = read_jsonl(cfg.scored_people_path, ScoredPerson)
    cluster_rows = read_jsonl(cfg.scored_clusters_path, ScoredCluster)
    assert len(people_rows) == 4
    assert [s.rank for s in sorted(people_rows, key=lambda s: s.rank)] == [1, 2, 3, 4]
    assert len(cluster_rows) == 1
    assert cfg.scored_people_path.stat().st_mode & 0o777 == 0o600
