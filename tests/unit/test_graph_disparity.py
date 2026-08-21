"""Unit tests for :mod:`interlayer.graph.disparity` and the Tier-2 exemption.

Two things are being defended here.

The first is that this really is the Serrano/Boguna/Vespignani multiscale
backbone and not a percentile threshold wearing its name. The tell is the
degenerate case: on *uniform* weights a genuine disparity filter keeps
essentially nothing, because no edge is statistically surprising. A percentile
rule would happily keep the top 5% of a pile of noise.

The second is that ground truth is never pruned. Tier-2 observed bridges are
unioned in after the filter runs and are excluded from the strength sums the
filter tests against, so no amount of alpha can delete them and one heavy
observed edge cannot make a person's inferred edges look insignificant.
"""

from __future__ import annotations

import math
import random
from itertools import pairwise
from pathlib import Path

import networkx as nx
import pytest

from interlayer.config import Settings
from interlayer.graph.build import build_graph
from interlayer.graph.disparity import backbone, disparity_alpha
from interlayer.graph.weights import MAX_INFERRED_WEIGHT, OBSERVED_FLOOR, observed_bonus
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    GraphEdge,
    MutualObservation,
    Org,
    OrgKind,
    Provenance,
    TargetFirm,
    TargetPerson,
)

ALPHA = 0.05

SHARED_START = ApproxDate(year=2010, month=1, day=1)
SHARED_END = ApproxDate(year=2019, month=12, day=31)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _weighted(edges: dict[tuple[str, str], float]) -> nx.Graph:
    graph = nx.Graph()
    for (left, right), weight in sorted(edges.items()):
        graph.add_edge(left, right, weight=weight)
    return graph


def _uniform_random_graph(n: int = 120, m: int = 900, seed: int = 3) -> nx.Graph:
    """A graph of pure noise: structure-free topology, uniform random weights."""
    graph = nx.gnm_random_graph(n, m, seed=seed)
    rng = random.Random(seed)
    for left, right in sorted(graph.edges()):
        graph[left][right]["weight"] = rng.random()
    return nx.relabel_nodes(graph, {node: f"n{node:04d}" for node in graph})


def _sparse_projection_like(seed: int = 4) -> nx.Graph:
    """Many small, weakly-differentiated cliques -- the shape that over-pruned.

    Research 02 recorded the disparity filter cutting a sparse 500-person
    fixture from 5,116 edges to 63 and leaving only 86 people connected.
    """
    rng = random.Random(seed)
    graph = nx.Graph()
    person = 0
    for _ in range(60):
        members = [f"q{person + i:04d}" for i in range(rng.randrange(2, 12))]
        person += len(members)
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                graph.add_edge(left, right, weight=round(rng.uniform(0.02, 0.06), 6))
    return graph


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    """Settings whose gazetteer is deliberately absent: headcounts are irrelevant
    to filtering, and ``load_firm_sizes`` documents a missing file as an empty index."""
    return Settings(
        artifact_dir=tmp_path / "artifacts",
        gazetteer=tmp_path / "no-such-gazetteer.yaml",
        **overrides,  # type: ignore[arg-type]
    )


def _org(name: str) -> Org:
    return Org.make(name, OrgKind.COMPANY)


def _aff(person: str, org: Org) -> Affiliation:
    return Affiliation(
        person_id=person,
        org_id=org.org_id,
        kind=AffiliationKind.EMPLOYMENT,
        start=SHARED_START,
        end=SHARED_END,
    )


def _inferred_world(
    firms: dict[str, int],
) -> tuple[list[Org], list[Affiliation]]:
    orgs: list[Org] = []
    affiliations: list[Affiliation] = []
    for name, members in sorted(firms.items()):
        org = _org(name)
        orgs.append(org)
        affiliations += [_aff(f"{name}_p{i:02d}", org) for i in range(members)]
    return orgs, affiliations


def _by_provenance(edges: tuple[GraphEdge, ...], provenance: Provenance) -> list[GraphEdge]:
    return [edge for edge in edges if edge.provenance is provenance]


# ---------------------------------------------------------------------------
# the closed-form p-value
# ---------------------------------------------------------------------------


def test_disparity_alpha_matches_the_serrano_closed_form() -> None:
    """``alpha_ij = (1 - w_ij/s_i)^(k_i - 1)`` -- PNAS 106(16):6483, eq. 2."""
    for weight, strength, degree in ((1.0, 4.0, 5), (0.2, 2.0, 3), (3.0, 10.0, 8)):
        assert disparity_alpha(weight, strength, degree) == pytest.approx(
            (1.0 - weight / strength) ** (degree - 1)
        )


def test_a_degree_one_node_is_not_testable() -> None:
    """Its single edge carries the whole strength by construction: no variance,
    no test. Every implementation of this filter skips them."""
    assert disparity_alpha(1.0, 1.0, 1) == 1.0
    assert disparity_alpha(99.0, 99.0, 1) == 1.0
    assert disparity_alpha(1.0, 0.0, 5) == 1.0


def test_an_edge_carrying_all_of_a_nodes_strength_is_maximally_significant() -> None:
    assert disparity_alpha(2.0, 2.0, 4) == 0.0


def test_disparity_alpha_is_a_probability() -> None:
    for degree in (1, 2, 5, 50):
        for share in (0.0, 0.01, 0.5, 0.99, 1.0):
            value = disparity_alpha(share, 1.0, degree)
            assert math.isfinite(value)
            assert 0.0 <= value <= 1.0


# ---------------------------------------------------------------------------
# degenerate behaviour: noise in, nothing out
# ---------------------------------------------------------------------------


def test_perfectly_uniform_weights_keep_nothing() -> None:
    """No edge in a graph of identical weights is statistically surprising."""
    graph = nx.gnm_random_graph(120, 900, seed=3)
    nx.set_edge_attributes(graph, 1.0, "weight")
    graph = nx.relabel_nodes(graph, {node: f"n{node:04d}" for node in graph})

    result = backbone(graph, alpha=ALPHA, rescue_top_k=0)
    assert result.kept == ()
    assert result.n_significant == 0
    assert result.n_rescued == 0
    assert result.n_pruned == graph.number_of_edges()


def test_uniformly_random_weights_keep_essentially_nothing() -> None:
    """The correct degenerate behaviour: degrade toward empty, not toward a
    dense graph of noise. Research 02 measured 0 edges kept out of 3,163."""
    graph = _uniform_random_graph()
    result = backbone(graph, alpha=ALPHA, rescue_top_k=0)
    assert len(result.kept) <= graph.number_of_edges() * 0.01


def test_a_percentile_threshold_would_not_behave_this_way() -> None:
    """Guard against the filter being replaced by "keep the top x%".

    A percentile rule keeps a fixed fraction whatever the weights look like.
    The disparity filter keeps ~nothing on noise and a real backbone on
    structure, from the same edge count.
    """
    noise = _uniform_random_graph()

    # Thirty people, each with one dominant relationship against a background of
    # ten incidental ones -- exactly the signal a backbone is meant to recover.
    structured = nx.Graph()
    for cluster in range(30):
        centre = f"c{cluster:02d}"
        structured.add_edge(centre, f"{centre}_partner", weight=5.0)
        for leaf in range(10):
            structured.add_edge(centre, f"{centre}_leaf{leaf:02d}", weight=0.01)

    kept_noise = len(backbone(noise, alpha=ALPHA, rescue_top_k=0).kept)
    kept_structured = len(backbone(structured, alpha=ALPHA, rescue_top_k=0).kept)

    assert kept_noise / noise.number_of_edges() <= 0.01
    assert kept_structured == 30
    assert kept_structured / structured.number_of_edges() > 20 * (
        kept_noise / noise.number_of_edges() + 1e-9
    )


# ---------------------------------------------------------------------------
# structure survives
# ---------------------------------------------------------------------------


def test_the_heavy_edge_of_a_hub_and_spoke_graph_survives() -> None:
    graph = _weighted(
        {("hub", f"spoke{i:02d}"): 0.01 for i in range(30)} | {("heavy", "hub"): 10.0}
    )
    result = backbone(graph, alpha=ALPHA, rescue_top_k=0)
    assert ("heavy", "hub") in result.kept
    assert result.n_significant == 1


def test_an_edge_significant_only_from_the_weaker_endpoint_is_kept() -> None:
    """Deliberate asymmetry: an edge that is trivial to a hub can still be the
    defining relationship of the person on the other end."""
    graph = _weighted(
        {("hub", f"spoke{i:02d}"): 1.0 for i in range(40)}
        | {("side", "hub"): 1.0, ("side", "minor"): 0.001}
    )
    result = backbone(graph, alpha=0.02, rescue_top_k=0)
    # From the hub, ("hub", "side") is one of 41 equal edges and unremarkable;
    # from ``side`` it is ~99.9% of the strength.
    assert ("hub", "side") in result.kept


# ---------------------------------------------------------------------------
# alpha
# ---------------------------------------------------------------------------

ALPHAS = (1e-9, 1e-4, 0.01, 0.05, 0.2, 0.5, 1.0)


def test_a_higher_alpha_keeps_more_edges_and_keeps_a_superset() -> None:
    graph = _uniform_random_graph()
    kept = [set(backbone(graph, alpha=a, rescue_top_k=0).kept) for a in ALPHAS]
    counts = [len(k) for k in kept]
    assert counts == sorted(counts), dict(zip(ALPHAS, counts, strict=True))
    assert counts[-1] > counts[0]
    for smaller, larger in pairwise(kept):
        assert smaller <= larger


def test_alpha_of_one_keeps_every_testable_edge_and_no_untestable_one() -> None:
    graph = _weighted({("a", "b"): 1.0, ("b", "c"): 2.0, ("c", "a"): 3.0, ("c", "d"): 4.0})
    result = backbone(graph, alpha=1.0, rescue_top_k=0)
    # "d" has degree 1, so the (c, d) edge is untestable from that side; it is
    # kept only because "c" can test it.
    assert set(result.kept) == set(map(tuple, map(sorted, graph.edges())))

    lone = _weighted({("a", "b"): 1.0})
    assert backbone(lone, alpha=1.0, rescue_top_k=0).kept == ()


def test_a_two_node_graph_is_entirely_untestable_and_needs_the_rescue() -> None:
    lone = _weighted({("a", "b"): 1.0})
    assert backbone(lone, alpha=ALPHA, rescue_top_k=0).kept == ()
    rescued = backbone(lone, alpha=ALPHA, rescue_top_k=1)
    assert rescued.kept == (("a", "b"),)
    assert rescued.n_significant == 0
    assert rescued.n_rescued == 1


# ---------------------------------------------------------------------------
# the rescue
# ---------------------------------------------------------------------------


def test_without_the_rescue_a_sparse_graph_loses_almost_every_node() -> None:
    """The regression the rescue exists for: 500 people pruned down to 86."""
    graph = _sparse_projection_like()
    bare = backbone(graph, alpha=ALPHA, rescue_top_k=0)
    connected = {node for pair in bare.kept for node in pair}
    assert len(connected) < graph.number_of_nodes() / 2, (
        f"expected the bare filter to strand most of {graph.number_of_nodes()} nodes"
    )


def test_the_rescue_leaves_no_node_with_a_candidate_edge_orphaned() -> None:
    graph = _sparse_projection_like()
    rescued = backbone(graph, alpha=ALPHA, rescue_top_k=3)
    connected = {node for pair in rescued.kept for node in pair}
    candidates = {node for node in graph if graph.degree(node) > 0}
    assert candidates - connected == set()


def test_the_rescue_keeps_a_nodes_heaviest_edges() -> None:
    graph = _weighted(
        {
            ("centre", "heavy"): 9.0,
            ("centre", "middle"): 5.0,
            ("centre", "light"): 1.0,
            ("centre", "lightest"): 0.5,
        }
    )
    kept = set(backbone(graph, alpha=1e-12, rescue_top_k=2).kept)
    assert ("centre", "heavy") in kept
    assert ("centre", "middle") in kept
    # ...but "light" and "lightest" are each other's -- rather, ``centre``'s --
    # only neighbour, so their own top-1 rescue pulls them back in too.
    assert kept == {
        ("centre", "heavy"),
        ("centre", "light"),
        ("centre", "lightest"),
        ("centre", "middle"),
    }


def test_rescue_top_k_zero_is_the_bare_statistical_filter() -> None:
    graph = _sparse_projection_like()
    bare = backbone(graph, alpha=ALPHA, rescue_top_k=0)
    assert bare.n_rescued == 0
    assert len(bare.kept) == bare.n_significant


def test_backbone_accounting_adds_up() -> None:
    graph = _sparse_projection_like()
    result = backbone(graph, alpha=0.3, rescue_top_k=3)
    assert len(result.kept) == result.n_significant + result.n_rescued
    assert result.n_pruned == graph.number_of_edges() - len(result.kept)
    assert result.n_pruned >= 0


def test_backbone_output_is_sorted_and_canonically_oriented() -> None:
    graph = _sparse_projection_like()
    result = backbone(graph, alpha=0.2, rescue_top_k=3)
    assert list(result.kept) == sorted(result.kept)
    assert all(left < right for left, right in result.kept)
    assert len(set(result.kept)) == len(result.kept)


def test_backbone_does_not_invent_edges() -> None:
    graph = _sparse_projection_like()
    existing = {tuple(sorted(edge)) for edge in graph.edges()}
    for k in (0, 1, 5):
        assert set(backbone(graph, alpha=0.4, rescue_top_k=k).kept) <= existing


def test_isolated_nodes_neither_crash_nor_gain_an_edge() -> None:
    graph = _weighted({("a", "b"): 1.0})
    graph.add_node("orphan")
    result = backbone(graph, alpha=ALPHA, rescue_top_k=3)
    assert all("orphan" not in pair for pair in result.kept)


# ---------------------------------------------------------------------------
# Tier 2: observed bridges are never pruned
# ---------------------------------------------------------------------------


def _observed_world(
    tmp_path: Path, **overrides: object
) -> tuple[Settings, list[Org], list[Affiliation], MutualObservation, TargetPerson]:
    orgs, affiliations = _inferred_world({"Alpha": 6, "Beta": 5, "Gamma": 4})
    target_org = Org.make(
        "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
    )
    target = TargetPerson.make("Jane Target", TargetFirm.JANE_STREET)
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("Alpha_p00", "Beta_p00", "outsider"),
    )
    return _settings(tmp_path, **overrides), [*orgs, target_org], affiliations, observation, target


def test_an_observed_bridge_survives_a_punishing_alpha(tmp_path: Path) -> None:
    """Ground truth is structurally incapable of being pruned.

    alpha=1e-9 with the rescue disabled deletes every inferred edge in this
    fixture; the three observed bridges must all still be there.
    """
    cfg, orgs, affiliations, observation, target = _observed_world(
        tmp_path, disparity_alpha=1e-9, rescue_top_k=0
    )
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )
    observed = _by_provenance(result.edges, Provenance.OBSERVED)
    assert _by_provenance(result.edges, Provenance.INFERRED) == []
    assert {(e.source, e.target) for e in observed} == {
        ("Alpha_p00", "Beta_p00"),
        ("Alpha_p00", "outsider"),
        ("Beta_p00", "outsider"),
    }


def test_observed_and_inferred_edges_are_labelled_as_such(tmp_path: Path) -> None:
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )
    observed = {(e.source, e.target) for e in _by_provenance(result.edges, Provenance.OBSERVED)}
    inferred = _by_provenance(result.edges, Provenance.INFERRED)

    assert observed == {
        ("Alpha_p00", "Beta_p00"),
        ("Alpha_p00", "outsider"),
        ("Beta_p00", "outsider"),
    }
    assert inferred, "the fixture is supposed to produce inferred edges too"
    assert all(e.provenance is Provenance.INFERRED for e in inferred)


def test_every_observed_edge_outweighs_every_inferred_edge(tmp_path: Path) -> None:
    """Mirrors ``ScoreWeights.observed_mutual`` (10.0) sitting above
    ``direct_employment`` (6.0) downstream: a human reading beats any inference."""
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )
    observed = _by_provenance(result.edges, Provenance.OBSERVED)
    inferred = _by_provenance(result.edges, Provenance.INFERRED)
    assert observed and inferred
    assert min(e.weight for e in observed) > max(e.weight for e in inferred)
    assert min(e.weight for e in observed) >= OBSERVED_FLOOR
    assert max(e.weight for e in inferred) <= MAX_INFERRED_WEIGHT


def test_an_observed_edge_carries_the_floor_plus_its_reading_bonus(tmp_path: Path) -> None:
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )
    observed = _by_provenance(result.edges, Provenance.OBSERVED)
    expected = OBSERVED_FLOOR + observed_bonus(3)
    assert all(e.weight == pytest.approx(expected) for e in observed)


def test_a_heavy_observed_edge_cannot_prune_a_persons_inferred_edges(tmp_path: Path) -> None:
    """Observed edges are excluded from the strength sums the filter tests against.

    An edge weighted 2.07 among inferred edges weighted 0.02 would make every
    one of that person's inferred edges look insignificant and delete them all,
    so the two edge sets are kept apart until after filtering.
    """
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)

    without = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    with_observation = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )

    def inferred_pairs(edges: tuple[GraphEdge, ...]) -> dict[tuple[str, str], float]:
        return {(e.source, e.target): e.weight for e in _by_provenance(edges, Provenance.INFERRED)}

    baseline = inferred_pairs(without.edges)
    assert baseline, "the fixture must produce inferred edges for this to mean anything"
    # ``Alpha_p00`` is one of the bridges; its inferred edges must be untouched.
    assert any("Alpha_p00" in pair for pair in baseline)
    assert inferred_pairs(with_observation.edges) == baseline


def test_a_single_bridge_reading_contributes_a_node_but_no_edge(tmp_path: Path) -> None:
    """That person is a real, observed neighbour of a target and must not vanish
    merely because nobody else was on the list with them."""
    cfg, orgs, affiliations, _observation, target = _observed_world(tmp_path)
    lonely = MutualObservation(
        target_person_id=target.target_person_id, bridge_person_ids=("hermit",)
    )
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[lonely],
        targets=[target],
    )
    assert _by_provenance(result.edges, Provenance.OBSERVED) == []
    assert result.diagnostics["observations_without_a_pair"] == 1
    assert "hermit" not in {node for e in result.edges for node in (e.source, e.target)}
    # ...but it is still counted as a node in the graph.
    assert result.stats.n_nodes == len({a.person_id for a in affiliations}) + 1


def test_an_observed_edge_names_the_target_firm_it_bridges(tmp_path: Path) -> None:
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)
    target_org = next(org for org in orgs if org.target_firm is TargetFirm.JANE_STREET)
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )
    for edge in _by_provenance(result.edges, Provenance.OBSERVED):
        assert target_org.org_id in edge.shared_org_ids


def test_observed_edges_are_built_even_without_the_targets_artifact(tmp_path: Path) -> None:
    """``targets.jsonl`` is Agent 3's artifact; the graph must build without it."""
    cfg, orgs, affiliations, observation, _target = _observed_world(tmp_path)
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs, observations=[observation])
    observed = _by_provenance(result.edges, Provenance.OBSERVED)
    assert len(observed) == 3
    assert all(edge.shared_org_ids == () for edge in observed)


def test_a_truncated_reading_is_not_damped(tmp_path: Path) -> None:
    """Presence in a partial list is still something a human saw; only *absence*
    from it may not be treated as evidence."""
    cfg, orgs, affiliations, observation, target = _observed_world(tmp_path)
    truncated = observation.model_copy(update={"stated_count": 40, "complete": False})
    assert truncated.truncated is True

    full = build_graph(
        cfg, affiliations=affiliations, orgs=orgs, observations=[observation], targets=[target]
    )
    partial = build_graph(
        cfg, affiliations=affiliations, orgs=orgs, observations=[truncated], targets=[target]
    )
    assert [e.weight for e in _by_provenance(partial.edges, Provenance.OBSERVED)] == [
        e.weight for e in _by_provenance(full.edges, Provenance.OBSERVED)
    ]
    assert partial.diagnostics["observations_truncated"] == 1


def test_a_pair_that_is_both_co_worked_and_observed_keeps_the_sum(tmp_path: Path) -> None:
    cfg, orgs, affiliations, _observation, target = _observed_world(tmp_path)
    colleagues = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("Alpha_p00", "Alpha_p01"),
    )
    inferred_only = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    inferred_weight = next(
        e.weight for e in inferred_only.edges if (e.source, e.target) == ("Alpha_p00", "Alpha_p01")
    )
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[colleagues],
        targets=[target],
    )
    merged = next(e for e in result.edges if (e.source, e.target) == ("Alpha_p00", "Alpha_p01"))
    assert merged.provenance is Provenance.OBSERVED
    assert merged.weight == pytest.approx(inferred_weight + OBSERVED_FLOOR + observed_bonus(2))
    assert merged.weight > MAX_INFERRED_WEIGHT
