"""T1 — the golden fixture. Every value here is published in R3 §T1 and is exact.

If one of these fails, the implementation is wrong, not the expectation.
"""

from __future__ import annotations

import pytest

from interlayer.graph import analyse, brokerage_metrics, build_bipartite, composite_score, project
from interlayer.graph.cluster import leiden_once, modularity, to_igraph
from test_graph_fixtures import (
    GOLDEN_CONSTRAINT,
    GOLDEN_MEMBERSHIP,
    GOLDEN_MODULARITY,
    GOLDEN_RARITY,
    GOLDEN_TARGET_DEGREES,
    golden_edge_records,
    golden_members,
    golden_targets,
)


@pytest.fixture
def golden():
    return golden_members(), golden_targets(), golden_edge_records()


@pytest.fixture
def golden_bg(golden):
    members, targets, edges = golden
    return build_bipartite(members, targets, edges)


# --- T1.1 --------------------------------------------------------------------


def test_t1_1_target_degrees_and_member_reach(golden_bg):
    assert golden_bg.target_degrees() == GOLDEN_TARGET_DEGREES
    reach = golden_bg.member_degrees()
    assert reach["m0"] == 4
    assert all(reach[m] == 2 for m in reach if m != "m0")


def test_t1_1_bridge_set_is_sorted_and_complete(golden_bg):
    assert golden_bg.member_ids == tuple(f"m{i}" for i in range(9))
    assert golden_bg.target_ids == tuple(f"t{i}" for i in range(6))


# --- T1.2 RA projection ------------------------------------------------------


def test_t1_2_ra_projection_edge_count(golden_bg):
    assert project(golden_bg, weighting="ra").n_edges == 15


@pytest.mark.parametrize(
    ("u", "v", "expected"),
    [
        ("m0", "m1", 0.666667), ("m0", "m2", 0.666667), ("m1", "m2", 0.666667),
        ("m0", "m3", 0.25), ("m0", "m4", 0.25), ("m0", "m5", 0.25),
        ("m0", "m6", 0.25), ("m0", "m7", 0.25), ("m0", "m8", 0.25),
        ("m3", "m4", 0.583333), ("m3", "m5", 0.583333), ("m4", "m5", 0.583333),
        ("m6", "m7", 0.583333), ("m6", "m8", 0.583333), ("m7", "m8", 0.583333),
    ],
)
def test_t1_2_ra_projection_weights(golden_bg, u, v, expected):
    assert project(golden_bg, weighting="ra").weight(u, v) == pytest.approx(expected, abs=1e-6)


# --- T1.3 count projection ---------------------------------------------------


def test_t1_3_count_projection_weights(golden_bg):
    p = project(golden_bg, weighting="count")
    assert p.weight("m0", "m1") == pytest.approx(2.0)
    assert p.weight("m0", "m3") == pytest.approx(1.0)
    assert p.weight("m3", "m4") == pytest.approx(2.0)


def test_t1_3_ra_suppresses_the_hub_mediated_tie_relatively_more(golden_bg):
    """Raw count gives m0-m3 half of the maximum weight; RA gives it 37.5%."""
    count = project(golden_bg, weighting="count")
    ra = project(golden_bg, weighting="ra")
    assert count.weight("m0", "m3") / count.weight("m0", "m1") == pytest.approx(0.5)
    assert ra.weight("m0", "m3") / ra.weight("m0", "m1") == pytest.approx(0.375, abs=1e-6)


# --- T1.4 Adamic-Adar --------------------------------------------------------


def test_t1_4_adamic_adar_weights(golden_bg):
    p = project(golden_bg, weighting="adamic_adar")
    assert p.weight("m0", "m1") == pytest.approx(1.820478, abs=1e-6)
    assert p.weight("m0", "m3") == pytest.approx(0.721348, abs=1e-6)
    assert p.weight("m3", "m4") == pytest.approx(1.631587, abs=1e-6)


# --- T1.5 / T1.6 clustering --------------------------------------------------


def test_t1_5_leiden_membership_and_modularity(golden_bg):
    p = project(golden_bg)
    g = to_igraph(p.matrix, p.member_ids)
    membership = leiden_once(g, gamma=1.0, seed=42)
    by_member = dict(zip(g.vs["name"], membership, strict=True))
    assert len(set(membership)) == 3
    assert by_member == GOLDEN_MEMBERSHIP
    assert modularity(g, membership) == pytest.approx(GOLDEN_MODULARITY, abs=1e-6)


def test_t1_5_consensus_reproduces_the_same_membership(golden):
    members, targets, edges = golden
    ctx = analyse(members, targets, edges, return_context=True)
    assert ctx.partition.consensus_applied
    assert ctx.partition.k == 3
    assert ctx.partition.by_member() == GOLDEN_MEMBERSHIP
    assert ctx.partition.modularity == pytest.approx(GOLDEN_MODULARITY, abs=1e-6)


@pytest.mark.parametrize("gamma", [0.5, 0.75, 1.0, 1.25, 1.5, 2.0])
def test_t1_6_resolution_stability(golden_bg, gamma):
    p = project(golden_bg)
    g = to_igraph(p.matrix, p.member_ids)
    membership = leiden_once(g, gamma=gamma, seed=42)
    assert dict(zip(g.vs["name"], membership, strict=True)) == GOLDEN_MEMBERSHIP
    assert modularity(g, membership) == pytest.approx(GOLDEN_MODULARITY, abs=1e-6)


# --- T1.7 brokerage ----------------------------------------------------------


@pytest.fixture
def golden_metrics(golden_bg):
    p = project(golden_bg)
    g = to_igraph(p.matrix, p.member_ids)
    return brokerage_metrics(golden_bg, p, g), g


def test_t1_7_m0_brokerage(golden_metrics):
    metrics, _ = golden_metrics
    m0 = metrics["m0"]
    assert m0.reach == 4
    assert m0.rarity == pytest.approx(1.166667, abs=1e-6)
    assert m0.betweenness == pytest.approx(0.750000, abs=1e-6)
    assert m0.effective_size == pytest.approx(6.250000, abs=1e-6)
    assert m0.constraint == pytest.approx(0.330729, abs=1e-6)


@pytest.mark.parametrize("member_id", ["m1", "m2"])
def test_t1_7_pocket_a_brokerage(golden_metrics, member_id):
    metrics, _ = golden_metrics
    m = metrics[member_id]
    assert m.rarity == pytest.approx(0.666667, abs=1e-6)
    assert m.betweenness == pytest.approx(0.0, abs=1e-9)
    assert m.effective_size == pytest.approx(1.0, abs=1e-6)
    assert m.constraint == pytest.approx(0.878906, abs=1e-6)


@pytest.mark.parametrize("member_id", ["m3", "m4", "m5", "m6", "m7", "m8"])
def test_t1_7_outer_pocket_brokerage(golden_metrics, member_id):
    metrics, _ = golden_metrics
    m = metrics[member_id]
    assert m.rarity == pytest.approx(0.583333, abs=1e-6)
    assert m.betweenness == pytest.approx(0.0, abs=1e-9)
    assert m.effective_size == pytest.approx(1.0, abs=1e-6)
    assert m.constraint == pytest.approx(0.781250, abs=1e-6)


def test_t1_7_all_rarity_and_constraint_values(golden_metrics):
    metrics, _ = golden_metrics
    for member_id, expected in GOLDEN_RARITY.items():
        assert metrics[member_id].rarity == pytest.approx(expected, abs=1e-6)
    for member_id, expected in GOLDEN_CONSTRAINT.items():
        assert metrics[member_id].constraint == pytest.approx(expected, abs=1e-6)


def test_t1_7_igraph_unnormalised_betweenness(golden_metrics):
    """R3 publishes the raw igraph value, not just the normalised one."""
    _, g = golden_metrics
    raw = dict(zip(g.vs["name"], g.betweenness(), strict=True))
    assert raw["m0"] == pytest.approx(21.0)
    assert all(v == pytest.approx(0.0) for k, v in raw.items() if k != "m0")


def test_t1_7_autonomy_is_one_minus_clamped_constraint(golden_metrics):
    metrics, _ = golden_metrics
    for m in metrics.values():
        assert m.autonomy == pytest.approx(1.0 - min(m.constraint, 1.0))


# --- T1.8 composite ----------------------------------------------------------


def test_t1_8_composite_ranks_m0_strictly_first(golden_metrics):
    metrics, _ = golden_metrics
    scores = composite_score(metrics)
    assert scores["m0"] == pytest.approx(1.0)
    assert all(scores["m0"] > v for k, v in scores.items() if k != "m0")


def test_t1_8_result_orders_brokerage_by_composite(golden):
    members, targets, edges = golden
    result = analyse(*(members, targets, edges))
    assert result.brokerage[0].member_id == "m0"
    composites = [b.composite for b in result.brokerage]
    assert composites == sorted(composites, reverse=True)


def test_t1_full_result_is_well_formed(golden):
    members, targets, edges = golden
    result = analyse(members, targets, edges)
    assert len(result.clusters) == 3
    assert sum(len(c.member_ids) for c in result.clusters) == 9
    assert result.coverage.n_targets_total == 6
    assert result.coverage.n_targets_harvested == 6
    assert result.coverage.fraction == pytest.approx(1.0)
    assert result.coverage.n_bridges == 9
    assert result.used_inferred_edges is False
    assert len(result.fingerprint) == 64
    # clusters ranked by summed score, descending
    sums = [c.score_sum for c in result.clusters]
    assert sums == sorted(sums, reverse=True)
    # m0's cluster leads, because m0 carries the highest composite
    assert "m0" in result.clusters[0].member_ids
