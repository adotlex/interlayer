"""Structural guarantees a partition must hold before an operator is shown it.

Accuracy is measured in ``test_cluster_consensus``; this file covers the promises
that hold *regardless* of accuracy. The load-bearing one is connectivity: a
consensus component is a union of overlapping Leiden communities and the path
joining two members can run through a node that landed in neither, so "this group
of people" can be a set with no internal path at all. The stage re-splits those,
and every emitted cluster is checked here at several mixing levels and
resolutions.

The rest are the things that make a partition usable: small groups dropped,
ids stable, members sorted, resolution monotone, labels derived from evidence the
operator can audit, and degenerate graphs that return nothing rather than raising.
"""

from __future__ import annotations

import itertools
import random
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import igraph as ig
import networkx as nx
import pytest

from interlayer import cluster as cluster_stage
from interlayer.cluster import _split_disconnected, cluster_graph
from interlayer.cluster.consensus import build_igraph
from interlayer.cluster.labeling import label_for, orgs_by_person, titles_by_person
from interlayer.cluster.quality import (
    GIANT_CLUSTER_SHARE,
    MIN_MEANINGFUL_MODULARITY,
    cohesion,
    is_connected,
    quality_warnings,
)
from interlayer.config import Settings
from interlayer.io import read_jsonl, secure_dir, write_jsonl
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Cluster,
    GraphEdge,
    Org,
    OrgKind,
    Person,
    Position,
)

SMALL: dict[str, Any] = {
    "n": 300,
    "tau1": 3,
    "tau2": 1.5,
    "min_degree": 5,
    "max_degree": 30,
    "min_community": 20,
    "max_community": 60,
}
GENERATION_LIMIT_S = 25.0


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Fail rather than hang if LFR generation stops converging."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _expire(signum: int, frame: object) -> None:
        raise TimeoutError(f"graph generation exceeded {seconds}s")

    try:
        previous = signal.signal(signal.SIGALRM, _expire)
    except ValueError:  # not the main thread
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class Planted:
    node_ids: tuple[str, ...]
    graph: ig.Graph
    edges: tuple[tuple[str, str], ...]

    def positions(self, member_ids: Sequence[str]) -> list[int]:
        lookup = {node: i for i, node in enumerate(self.node_ids)}
        return [lookup[m] for m in member_ids]


def build_planted(mu: float, seed: int = 7) -> Planted:
    with time_limit(GENERATION_LIMIT_S):
        raw = nx.LFR_benchmark_graph(**SMALL, mu=mu, seed=seed, max_iters=500)
    graph = nx.Graph(raw)
    graph.remove_edges_from(nx.selfloop_edges(graph))
    order = sorted(graph.nodes())
    position = {node: i for i, node in enumerate(order)}
    node_ids = tuple(f"p{node:05d}" for node in order)
    triples = sorted(
        (min(position[u], position[v]), max(position[u], position[v]), 1.0)
        for u, v in graph.edges()
    )
    return Planted(
        node_ids=node_ids,
        graph=build_igraph(len(order), triples),
        edges=tuple((node_ids[u], node_ids[v]) for u, v, _ in triples),
    )


PlantedFactory = Callable[[float], Planted]


@pytest.fixture(scope="module")
def planted() -> PlantedFactory:
    cache: dict[float, Planted] = {}

    def build(mu: float) -> Planted:
        if mu not in cache:
            cache[mu] = build_planted(mu)
        return cache[mu]

    return build


@pytest.fixture
def cfg(tmp_path: Path) -> Settings:
    """Small ensemble: every guarantee in this file holds at any ensemble size,
    and 50 runs x 9 (mu, gamma) combinations would dominate the suite."""
    return Settings(artifact_dir=tmp_path / "artifacts", consensus_runs=8)


# ===========================================================================
# a hand-built affiliation network, for labelling and end-to-end checks
# ===========================================================================

GROUPS: tuple[tuple[str, str], ...] = (
    ("Vector Capital", "Quantitative Researcher"),
    ("Delta Labs", "Software Engineer"),
    ("Nimbus Trading", "Portfolio Manager"),
)
BACKGROUND_ORG = "Global Mega Corp"
BACKGROUND_TITLE = "Analyst"
GROUP_SIZE = 6


@dataclass(frozen=True)
class Affiliated:
    people: tuple[Person, ...]
    orgs: tuple[Org, ...]
    affiliations: tuple[Affiliation, ...]
    edges: tuple[GraphEdge, ...]
    node_ids: tuple[str, ...]
    graph: ig.Graph
    members_by_group: tuple[tuple[str, ...], ...]


@pytest.fixture(scope="module")
def affiliated() -> Affiliated:
    """Three tight cohorts, each with its own employer, all sharing one huge one.

    The background employer exists to prove the labelling lift correction works:
    every member of every cluster passed through it, so raw dominance would put it
    in all three names and make the names useless.
    """
    orgs = [Org.make(name, OrgKind.COMPANY) for name, _ in GROUPS]
    background = Org.make(BACKGROUND_ORG, OrgKind.COMPANY)
    people: list[Person] = []
    affiliations: list[Affiliation] = []
    members_by_group: list[tuple[str, ...]] = []

    for group, ((firm, title), org) in enumerate(zip(GROUPS, orgs, strict=True)):
        ids: list[str] = []
        for member in range(GROUP_SIZE):
            person = Person.make(
                f"Given{group}{member}",
                f"Family{group}{member}",
                linkedin_slug=f"g{group}m{member}",
                positions=(
                    Position(
                        company_raw=firm,
                        title_raw=title,
                        start=ApproxDate(year=2018),
                        is_current=True,
                    ),
                ),
            )
            people.append(person)
            ids.append(person.person_id)
            affiliations.append(
                Affiliation(
                    person_id=person.person_id,
                    org_id=org.org_id,
                    kind=AffiliationKind.EMPLOYMENT,
                    title=title,
                    start=ApproxDate(year=2018),
                    is_current=True,
                )
            )
            affiliations.append(
                Affiliation(
                    person_id=person.person_id,
                    org_id=background.org_id,
                    kind=AffiliationKind.EMPLOYMENT,
                    title=BACKGROUND_TITLE,
                    start=ApproxDate(year=2010),
                    end=ApproxDate(year=2014),
                )
            )
        members_by_group.append(tuple(sorted(ids)))

    node_ids = tuple(sorted(p.person_id for p in people))
    position = {node: i for i, node in enumerate(node_ids)}
    triples: dict[tuple[int, int], float] = {}
    for group_ids in members_by_group:
        for left, right in itertools.combinations(sorted(group_ids), 2):
            triples[(position[left], position[right])] = 1.0
    # A thread between consecutive cohorts, weak enough not to merge them.
    for first, second in itertools.pairwise(members_by_group):
        u, v = position[first[0]], position[second[0]]
        triples[(min(u, v), max(u, v))] = 0.01

    ordered = sorted((u, v, w) for (u, v), w in triples.items())
    edges = tuple(
        GraphEdge(source=node_ids[u], target=node_ids[v], weight=w) for u, v, w in ordered
    )
    return Affiliated(
        people=tuple(people),
        orgs=(*orgs, background),
        affiliations=tuple(affiliations),
        edges=edges,
        node_ids=node_ids,
        graph=build_igraph(len(node_ids), ordered),
        members_by_group=tuple(members_by_group),
    )


# ===========================================================================
# the connectivity guarantee
# ===========================================================================


@pytest.mark.parametrize("mu", [0.1, 0.3, 0.5])
@pytest.mark.parametrize("resolution", [1.0, 2.0, 4.0])
def test_every_returned_cluster_is_internally_connected(
    planted: PlantedFactory, cfg: Settings, mu: float, resolution: float
) -> None:
    """A cluster whose halves have no path between them is not a group of people
    who know each other; it is two groups displayed as one. Consensus components
    do not inherit Leiden's connectivity guarantee, so it is re-imposed and
    checked here rather than assumed."""
    case = planted(mu)
    clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=resolution)
    assert clusters, f"no clusters at mu={mu}, gamma={resolution}"
    disconnected = [
        c.cluster_id for c in clusters if not is_connected(case.graph, case.positions(c.member_ids))
    ]
    assert not disconnected, f"{len(disconnected)} of {len(clusters)} clusters disconnected"


def test_split_disconnected_breaks_a_chained_member_set() -> None:
    """The re-split itself: members 0 and 2 are joined only via 1, which is not a
    member, so they are two clusters and not one."""
    graph = build_igraph(3, [(0, 1, 1.0), (1, 2, 1.0)])
    assert not is_connected(graph, [0, 2])
    assert _split_disconnected(graph, [0, 2]) == [[0], [2]]
    assert _split_disconnected(graph, [0, 1, 2]) == [[0, 1, 2]]


def test_is_connected_is_true_for_trivial_member_sets() -> None:
    graph = build_igraph(3, [(0, 1, 1.0)])
    assert is_connected(graph, [])
    assert is_connected(graph, [2])
    assert is_connected(graph, [0, 1])
    assert not is_connected(graph, [0, 2])


# ===========================================================================
# membership shape
# ===========================================================================


def test_clusters_below_min_cluster_size_are_dropped(
    planted: PlantedFactory, tmp_path: Path
) -> None:
    """Raising the floor must remove exactly the groups under it and nothing else --
    a two-person "community" is not a lead, but the survivors must not be re-cut."""
    case = planted(0.3)

    def at(minimum: int) -> list[Cluster]:
        settings = Settings(
            artifact_dir=tmp_path / f"a{minimum}", consensus_runs=8, min_cluster_size=minimum
        )
        return cluster_graph(case.node_ids, case.graph, settings, resolution=4.0)

    permissive = at(2)
    strict = at(12)
    assert any(c.size < 12 for c in permissive), "nothing was small enough to drop"
    assert all(c.size >= 12 for c in strict)
    assert {c.member_ids for c in strict} == {c.member_ids for c in permissive if c.size >= 12}


def test_min_cluster_size_can_drop_everything_without_raising(
    affiliated: Affiliated, tmp_path: Path
) -> None:
    settings = Settings(artifact_dir=tmp_path / "huge", consensus_runs=6, min_cluster_size=10_000)
    assert cluster_graph(affiliated.node_ids, affiliated.graph, settings, resolution=1.0) == []


def test_member_ids_are_sorted_and_unique(planted: PlantedFactory, cfg: Settings) -> None:
    case = planted(0.3)
    clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=2.0)
    for cluster in clusters:
        assert list(cluster.member_ids) == sorted(cluster.member_ids)
        assert len(set(cluster.member_ids)) == cluster.size


def test_clusters_partition_the_node_set_without_overlap(
    planted: PlantedFactory, cfg: Settings
) -> None:
    """Nobody may appear in two groups at one resolution -- the report shows a
    person one cluster, and two would be an unresolvable contradiction."""
    case = planted(0.3)
    clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=2.0)
    seen: list[str] = [m for c in clusters for m in c.member_ids]
    assert len(seen) == len(set(seen))
    assert set(seen) <= set(case.node_ids)


def test_cluster_ids_are_stable_for_the_same_membership(
    planted: PlantedFactory, cfg: Settings
) -> None:
    """The id is content-addressed over the membership, so a second run that finds
    the same group must reuse the same id -- otherwise nothing downstream can be
    compared between runs."""
    case = planted(0.3)
    first = cluster_graph(case.node_ids, case.graph, cfg, resolution=2.0)
    second = cluster_graph(case.node_ids, case.graph, cfg, resolution=2.0)
    assert [c.cluster_id for c in first] == [c.cluster_id for c in second]
    assert [c.member_ids for c in first] == [c.member_ids for c in second]
    assert len({c.cluster_id for c in first}) == len(first)


def test_cluster_ids_differ_between_resolutions_for_the_same_members(
    affiliated: Affiliated, cfg: Settings
) -> None:
    """gamma is part of the id: the same six people at two granularities are two
    different findings and must not collide in the sweep file."""
    coarse = cluster_graph(affiliated.node_ids, affiliated.graph, cfg, resolution=1.0)
    fine = cluster_graph(affiliated.node_ids, affiliated.graph, cfg, resolution=2.0)
    shared = {c.member_ids: c.cluster_id for c in coarse}
    overlapping = [c for c in fine if c.member_ids in shared]
    assert overlapping, "expected at least one identical membership across resolutions"
    for cluster in overlapping:
        assert cluster.cluster_id != shared[cluster.member_ids]


def test_clusters_are_ordered_largest_first(planted: PlantedFactory, cfg: Settings) -> None:
    case = planted(0.3)
    clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=2.0)
    assert [c.size for c in clusters] == sorted((c.size for c in clusters), reverse=True)


def test_every_cluster_records_the_algorithm_and_resolution(
    affiliated: Affiliated, cfg: Settings
) -> None:
    clusters = cluster_graph(affiliated.node_ids, affiliated.graph, cfg, resolution=2.0)
    assert clusters
    assert {c.algorithm for c in clusters} == {cluster_stage.ALGORITHM}
    assert {c.resolution for c in clusters} == {2.0}


# ===========================================================================
# resolution
# ===========================================================================


def test_higher_resolution_gives_more_and_smaller_clusters(
    planted: PlantedFactory, cfg: Settings
) -> None:
    """gamma must behave the way the config docstring claims. Monotone in the
    aggregate, not per cluster -- an individual group can survive a bump."""
    case = planted(0.3)
    counts: list[int] = []
    medians: list[float] = []
    for resolution in (0.5, 1.0, 2.0, 4.0):
        clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=resolution)
        sizes = sorted(c.size for c in clusters)
        counts.append(len(clusters))
        medians.append(sizes[len(sizes) // 2])
    assert counts == sorted(counts), counts
    assert counts[-1] > counts[0], counts
    assert medians[-1] < medians[0], medians


def test_run_writes_one_clustering_per_sweep_entry(planted: PlantedFactory, tmp_path: Path) -> None:
    """``clusters.jsonl`` is one partition so downstream never has to guess the
    granularity; the sweep lives in its own file and covers every configured gamma."""
    case = planted(0.3)
    artifacts = tmp_path / "artifacts"
    secure_dir(artifacts)
    write_jsonl(
        artifacts / "edges.jsonl",
        [GraphEdge(source=u, target=v, weight=1.0) for u, v in case.edges],
    )
    settings = Settings(
        artifact_dir=artifacts,
        consensus_runs=6,
        resolution=2.0,
        resolution_sweep=(1.0, 2.0, 4.0),
    )
    cluster_stage.run(settings)

    primary = read_jsonl(settings.clusters_path, Cluster)
    sweep = read_jsonl(settings.clusters_sweep_path, Cluster)
    assert {c.resolution for c in primary} == {2.0}
    assert {c.resolution for c in sweep} == {1.0, 2.0, 4.0}
    assert [c.model_dump() for c in primary] == [
        c.model_dump() for c in sweep if c.resolution == 2.0
    ]
    assert len({c.cluster_id for c in sweep}) == len(sweep)


def test_run_includes_the_primary_resolution_even_when_absent_from_the_sweep(
    planted: PlantedFactory, tmp_path: Path
) -> None:
    case = planted(0.3)
    artifacts = tmp_path / "artifacts"
    secure_dir(artifacts)
    write_jsonl(
        artifacts / "edges.jsonl",
        [GraphEdge(source=u, target=v, weight=1.0) for u, v in case.edges],
    )
    settings = Settings(
        artifact_dir=artifacts, consensus_runs=6, resolution=3.0, resolution_sweep=(1.0,)
    )
    cluster_stage.run(settings)
    assert {c.resolution for c in read_jsonl(settings.clusters_path, Cluster)} == {3.0}
    assert {c.resolution for c in read_jsonl(settings.clusters_sweep_path, Cluster)} == {1.0, 3.0}


# ===========================================================================
# labels
# ===========================================================================


def test_labels_name_the_distinctive_employer_not_the_background_one(
    affiliated: Affiliated, cfg: Settings
) -> None:
    """Every person here also worked at one huge shared employer. Raw dominance
    would put it in all three names; the lift correction must prefer the employer
    that actually explains why these six people are grouped."""
    clusters = cluster_graph(
        affiliated.node_ids,
        affiliated.graph,
        cfg,
        resolution=1.0,
        people=affiliated.people,
        orgs=affiliated.orgs,
        affiliations=affiliated.affiliations,
    )
    assert len(clusters) == len(GROUPS)
    by_members = {c.member_ids: c for c in clusters}
    member_titles = {title for _, title in GROUPS} | {BACKGROUND_TITLE}
    for ids, (firm, title) in zip(affiliated.members_by_group, GROUPS, strict=True):
        cluster = by_members[tuple(ids)]
        assert cluster.label.startswith(firm), cluster.label
        assert not cluster.label.startswith(BACKGROUND_ORG)
        # The name is "<orgs> - <title>"; the title half is drawn from what members
        # actually hold, never invented.
        assert cluster.label.rsplit(" - ", 1)[-1] in member_titles, cluster.label
        assert (title, GROUP_SIZE) in cluster.top_titles


def test_top_orgs_and_titles_are_real_member_evidence(
    affiliated: Affiliated, cfg: Settings
) -> None:
    """``top_orgs`` reports honest raw counts an operator can check against the
    membership, even where the label deliberately disagrees with them."""
    clusters = cluster_graph(
        affiliated.node_ids,
        affiliated.graph,
        cfg,
        resolution=1.0,
        people=affiliated.people,
        orgs=affiliated.orgs,
        affiliations=affiliated.affiliations,
    )
    known_orgs = {o.name for o in affiliated.orgs}
    known_titles = {title for _, title in GROUPS} | {BACKGROUND_TITLE}
    for cluster in clusters:
        assert cluster.top_orgs
        for name, count in cluster.top_orgs:
            assert name in known_orgs
            assert 0 < count <= cluster.size
        for name, count in cluster.top_titles:
            assert name in known_titles
            assert 0 < count <= cluster.size
        assert BACKGROUND_ORG in {name for name, _ in cluster.top_orgs}


def test_labels_are_never_empty_even_with_no_affiliation_inputs(
    affiliated: Affiliated, cfg: Settings
) -> None:
    """Missing labelling artifacts degrade the name, never the membership."""
    clusters = cluster_graph(affiliated.node_ids, affiliated.graph, cfg, resolution=1.0)
    assert clusters
    for cluster in clusters:
        assert cluster.label.strip()
        assert cluster.label == f"Unlabelled group of {cluster.size}"


def test_label_for_prefers_the_over_represented_org() -> None:
    """The lift rule in isolation, without any clustering in the way."""
    org_names = {"org_small": "Tiny Shop", "org_big": "Mega Corp"}
    members = tuple(f"p{i}" for i in range(5))
    label = label_for(
        members,
        person_orgs={m: frozenset({"org_small", "org_big"}) for m in members},
        person_titles={m: frozenset({"Trader"}) for m in members},
        org_names=org_names,
        global_org_counts={"org_small": 5, "org_big": 400},
        n_people=400,
    )
    assert label.label.startswith("Tiny Shop")
    assert dict(label.top_orgs) == {"Tiny Shop": 5, "Mega Corp": 5}
    assert label.top_titles == (("Trader", 5),)


def test_the_title_half_of_a_label_has_no_lift_correction() -> None:
    """Pins a known asymmetry so a future change is a decision, not an accident.

    The org half of a label is lift-corrected so a background employer everybody
    passed through cannot out-shout the small firm that explains the cluster. The
    title half is *not*: it is the modal member title with an alphabetical
    tie-break. Where the background employer's job title is as common as the
    cohort's own -- which is exactly the situation the org correction exists for --
    the background title wins the tie and lands in every cluster's name.
    """
    members = tuple(f"p{i}" for i in range(5))
    label = label_for(
        members,
        person_orgs={m: frozenset({"org_small", "org_big"}) for m in members},
        person_titles={m: frozenset({"Quantitative Researcher", "Analyst"}) for m in members},
        org_names={"org_small": "Tiny Shop", "org_big": "Mega Corp"},
        global_org_counts={"org_small": 5, "org_big": 400},
        n_people=400,
    )
    assert label.label.startswith("Tiny Shop")  # lift correction applied
    assert label.label.endswith("Analyst")  # ... but not to the title
    assert dict(label.top_titles) == {"Analyst": 5, "Quantitative Researcher": 5}


def test_orgs_and_titles_by_person_deduplicate_repeat_stints() -> None:
    """Three consecutive stints at one employer are one employer, not three."""
    person = Person.make(
        "Repeat",
        "Employee",
        linkedin_slug="repeat",
        positions=(
            Position(company_raw="Acme", title_raw="Trader", start=ApproxDate(year=2010)),
            Position(company_raw="Acme", title_raw="Trader ", start=ApproxDate(year=2015)),
        ),
    )
    affiliations = [
        Affiliation(
            person_id=person.person_id,
            org_id="org_acme",
            kind=AffiliationKind.EMPLOYMENT,
            title="Trader",
            start=ApproxDate(year=year),
        )
        for year in (2010, 2015, 2020)
    ]
    assert orgs_by_person(affiliations) == {person.person_id: frozenset({"org_acme"})}
    assert titles_by_person([person], affiliations) == {person.person_id: frozenset({"Trader"})}


# ===========================================================================
# cohesion
# ===========================================================================


def test_cohesion_is_higher_for_a_planted_clique_than_a_random_subgraph() -> None:
    """``cohesion`` is shown to the operator as "how tight is this group"; if it
    cannot separate a clique from an arbitrary set of the same size it says nothing."""
    rng = random.Random(3)
    n = 60
    clique = {(u, v) for u, v in itertools.combinations(range(8), 2)}
    noise: set[tuple[int, int]] = set()
    while len(noise) < 200:
        a, b = rng.sample(range(8, n), 2)
        noise.add((min(a, b), max(a, b)))
    graph = build_igraph(n, sorted((u, v, 1.0) for u, v in clique | noise | {(0, 20)}))

    tight = cohesion(graph, list(range(8)))
    loose = cohesion(graph, sorted(rng.sample(range(8, n), 8)))
    assert 0.0 <= loose < tight <= 1.0
    assert tight > 0.9, tight
    assert loose < 0.5, loose


def test_cohesion_is_bounded_and_defined_for_edgeless_members() -> None:
    complete = build_igraph(4, [(u, v, 1.0) for u, v in itertools.combinations(range(4), 2)])
    assert cohesion(complete, [0, 1, 2, 3]) == 1.0
    assert cohesion(build_igraph(3, []), [0, 1, 2]) == 0.0  # no division by zero
    assert cohesion(build_igraph(3, [(0, 1, 1.0)]), [0]) == 0.0


def test_cohesion_respects_edge_weights() -> None:
    """A pair held together by a heavy edge is more cohesive than one held by a
    thread, even with identical topology."""
    heavy = build_igraph(3, [(0, 1, 10.0), (1, 2, 1.0)])
    light = build_igraph(3, [(0, 1, 1.0), (1, 2, 10.0)])
    assert cohesion(heavy, [0, 1]) > cohesion(light, [0, 1])


def test_every_emitted_cluster_reports_a_cohesion_in_range(
    planted: PlantedFactory, cfg: Settings
) -> None:
    case = planted(0.1)
    clusters = cluster_graph(case.node_ids, case.graph, cfg, resolution=1.0)
    assert clusters
    for cluster in clusters:
        assert 0.0 <= cluster.cohesion <= 1.0
    assert max(c.cohesion for c in clusters) > 0.5, "well-separated planted groups look loose"


# ===========================================================================
# degenerate inputs -- none of these may raise
# ===========================================================================


def test_empty_graph_yields_no_clusters(cfg: Settings) -> None:
    assert cluster_graph([], build_igraph(0, []), cfg, resolution=2.0) == []


def test_single_node_yields_no_clusters(cfg: Settings) -> None:
    assert cluster_graph(["only"], build_igraph(1, []), cfg, resolution=2.0) == []


def test_two_disconnected_nodes_yield_no_clusters(cfg: Settings) -> None:
    assert cluster_graph(["a", "b"], build_igraph(2, []), cfg, resolution=2.0) == []


def test_a_complete_graph_is_one_cluster_at_gamma_one(cfg: Settings) -> None:
    """No community structure exists, so the honest answer is a single group."""
    ids = [f"k{i}" for i in range(6)]
    graph = build_igraph(6, [(u, v, 1.0) for u, v in itertools.combinations(range(6), 2)])
    clusters = cluster_graph(ids, graph, cfg, resolution=1.0)
    assert len(clusters) == 1
    assert clusters[0].member_ids == tuple(ids)
    assert clusters[0].cohesion == 1.0


def test_a_complete_graph_at_high_gamma_shatters_without_raising(cfg: Settings) -> None:
    """gamma=2.0 on a clique drives every node into its own community; the stage
    must return nothing rather than a partition of singletons or an exception."""
    ids = [f"k{i}" for i in range(6)]
    graph = build_igraph(6, [(u, v, 1.0) for u, v in itertools.combinations(range(6), 2)])
    assert cluster_graph(ids, graph, cfg, resolution=2.0) == []


def test_one_giant_component_plus_isolates(cfg: Settings) -> None:
    ids = [f"n{i}" for i in range(14)]
    graph = build_igraph(14, [(u, v, 1.0) for u, v in itertools.combinations(range(8), 2)])
    clusters = cluster_graph(ids, graph, cfg, resolution=1.0)
    assert len(clusters) == 1
    assert clusters[0].size == 8
    assert set(clusters[0].member_ids) == set(ids[:8])


def test_run_on_an_empty_edge_file_writes_empty_artifacts(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    secure_dir(artifacts)
    write_jsonl(artifacts / "edges.jsonl", [])
    settings = Settings(artifact_dir=artifacts, consensus_runs=5, resolution_sweep=(2.0,))
    cluster_stage.run(settings)
    assert settings.clusters_path.read_text(encoding="utf-8") == ""
    assert read_jsonl(settings.clusters_path, Cluster) == []


def test_a_single_edge_graph_survives_every_swept_resolution(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    secure_dir(artifacts)
    write_jsonl(artifacts / "edges.jsonl", [GraphEdge(source="a", target="b", weight=1.0)])
    settings = Settings(
        artifact_dir=artifacts,
        consensus_runs=5,
        min_cluster_size=2,
        resolution=1.0,
        resolution_sweep=(1.0, 2.0, 4.0),
    )
    cluster_stage.run(settings)
    assert [c.member_ids for c in read_jsonl(settings.clusters_path, Cluster)] == [("a", "b")]
    # gamma >= 2 splits a lone pair apart; the sweep records that rather than failing.
    sweep = read_jsonl(settings.clusters_sweep_path, Cluster)
    assert {c.resolution for c in sweep} == {1.0}


# ===========================================================================
# the whole-partition sanity gate
# ===========================================================================


def test_quality_warnings_flags_an_empty_graph() -> None:
    warnings = quality_warnings(build_igraph(0, []), [], {}, n_nodes=0)
    assert len(warnings) == 1
    assert "empty" in warnings[0]


def test_quality_warnings_flags_a_giant_cluster() -> None:
    n = 20
    giant = tuple(f"n{i}" for i in range(12))
    graph = build_igraph(n, [(i, i + 1, 1.0) for i in range(n - 1)])
    warnings = quality_warnings(graph, [0] * n, {"c1": giant}, n_nodes=n)
    assert any("largest cluster holds 12 of 20" in w for w in warnings)
    assert len(giant) > GIANT_CLUSTER_SHARE * n


def test_quality_warnings_flags_a_dust_partition() -> None:
    n = 10
    graph = build_igraph(n, [(0, 1, 1.0)])
    warnings = quality_warnings(graph, list(range(n)), {}, n_nodes=n)
    assert any("cluster alone" in w for w in warnings)


def test_quality_warnings_flags_structureless_modularity() -> None:
    graph = build_igraph(6, [(u, v, 1.0) for u, v in itertools.combinations(range(6), 2)])
    warnings = quality_warnings(graph, [0] * 6, {"c1": tuple(f"k{i}" for i in range(6))}, n_nodes=6)
    assert any(str(MIN_MEANINGFUL_MODULARITY) in w for w in warnings)


def test_quality_warnings_is_silent_on_a_healthy_partition() -> None:
    """Four disjoint cliques with one thread between them: real structure, no giant,
    no dust. A warning here would train the operator to ignore all of them."""
    blocks = [range(b * 8, b * 8 + 8) for b in range(4)]
    triples = [(u, v, 1.0) for block in blocks for u, v in itertools.combinations(block, 2)]
    triples += [(b * 8, b * 8 + 8, 0.01) for b in range(3)]
    graph = build_igraph(32, sorted(triples))
    membership = [i // 8 for i in range(32)]
    clusters = {f"c{b}": tuple(f"n{i}" for i in blocks[b]) for b in range(4)}
    assert quality_warnings(graph, membership, clusters, n_nodes=32) == []
