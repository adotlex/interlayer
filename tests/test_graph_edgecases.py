"""T8 — edge cases, plus the scoring and fingerprint invariants.

Every case here is a real state the pipeline will meet: a first run with nothing
collected, one lucky connection, a member whose contacts are all private to them,
a disconnected graph (which ours always is), a double-pasted capture, and a target
that was checked and had nobody.
"""

from __future__ import annotations

import math

import pytest

from interlayer.core.models import Edge, EdgeOrigin
from interlayer.graph import analyse
from interlayer.graph.brokerage import brokerage_metrics
from interlayer.graph.build import build_bipartite
from interlayer.graph.cluster import to_igraph
from interlayer.graph.fingerprint import membership_fingerprint
from interlayer.graph.project import project
from interlayer.graph.score import composite_score, percentile_rank, wilson_lb
from test_graph_fixtures import (
    golden_edge_records,
    golden_members,
    golden_targets,
    make_member,
    make_target,
)


def _is_finite(value: float) -> bool:
    return not (math.isnan(value) or math.isinf(value))


def _assert_no_nan(result):
    for b in result.brokerage:
        for field in (b.rarity, b.betweenness, b.effective_size, b.constraint,
                      b.autonomy, b.composite):
            assert _is_finite(field), f"{b.member_id} produced {field}"
    for c in result.clusters:
        assert _is_finite(c.stability)
        assert _is_finite(c.score_sum)


# --- T8.1 --------------------------------------------------------------------


def test_t8_1_empty_edge_set():
    members = golden_members()
    targets = golden_targets(harvested=False)
    result = analyse(members, targets, [])
    assert result.clusters == ()
    assert result.brokerage == ()
    assert result.coverage.fraction == 0.0
    assert result.coverage.n_bridges == 0
    assert result.coverage.n_members_total == 9
    assert result.fingerprint
    _assert_no_nan(result)


def test_t8_1_empty_everything():
    result = analyse([], [], [])
    assert result.clusters == ()
    assert result.brokerage == ()
    assert result.next_targets == ()
    assert result.coverage.fraction == 0.0
    assert result.fingerprint


def test_t8_1_harvested_targets_but_no_edges_does_not_raise():
    """Coverage is 1.0 — we looked at everything and found nothing. Legal."""
    result = analyse(golden_members(), golden_targets(harvested=True), [])
    assert result.coverage.fraction == pytest.approx(1.0)
    assert result.coverage.n_bridges == 0
    assert result.clusters == ()


# --- T8.2 --------------------------------------------------------------------


def test_t8_2_single_member_single_target():
    result = analyse(
        [make_member("m0")], [make_target("t0")], [Edge(member_id="m0", target_id="t0")]
    )
    assert len(result.clusters) == 1
    assert result.clusters[0].member_ids == ("m0",)
    assert len(result.brokerage) == 1
    _assert_no_nan(result)

    only = result.brokerage[0]
    assert only.reach == 1
    assert only.rarity == pytest.approx(1.0)
    assert only.betweenness == 0.0
    # a lone node has no projection neighbours: NaN coalesced to the safe defaults
    assert only.effective_size == 0.0
    assert only.constraint == 1.0
    assert only.autonomy == 0.0
    # single element: every percentile rank is 1.0, so the composite is 1.0
    assert only.composite == pytest.approx(1.0)


def test_t8_2_two_members_no_shared_target():
    result = analyse(
        [make_member("m0"), make_member("m1")],
        [make_target("t0"), make_target("t1")],
        [Edge(member_id="m0", target_id="t0"), Edge(member_id="m1", target_id="t1")],
    )
    assert len(result.clusters) == 2
    _assert_no_nan(result)


# --- T8.3 --------------------------------------------------------------------


def test_t8_3_projection_isolate_gets_coalesced_defaults():
    """A member whose targets are all degree-1 has no projection neighbours."""
    members = [make_member("m0"), make_member("m1"), make_member("m2")]
    targets = [make_target(t) for t in ("t0", "t1", "t2", "t3")]
    edges = [
        Edge(member_id="m0", target_id="t0"),  # private to m0
        Edge(member_id="m1", target_id="t1"),
        Edge(member_id="m2", target_id="t1"),  # m1 and m2 share t1
        Edge(member_id="m1", target_id="t2"),
        Edge(member_id="m2", target_id="t3"),
    ]
    bg = build_bipartite(members, targets, edges)
    p = project(bg)
    g = to_igraph(p.matrix, p.member_ids)
    metrics = brokerage_metrics(bg, p, g)

    isolate = metrics["m0"]
    assert isolate.effective_size == 0.0
    assert isolate.constraint == 1.0
    assert isolate.autonomy == 0.0

    scores = composite_score(metrics)
    assert all(_is_finite(v) for v in scores.values())
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_t8_3_nan_never_reaches_the_composite():
    result = analyse(
        [make_member(f"m{i}") for i in range(4)],
        [make_target(f"t{i}") for i in range(4)],
        [Edge(member_id=f"m{i}", target_id=f"t{i}") for i in range(4)],
    )
    _assert_no_nan(result)
    assert all(b.effective_size == 0.0 and b.constraint == 1.0 for b in result.brokerage)


# --- T8.4 --------------------------------------------------------------------


def test_t8_4_disconnected_bipartite_input_does_not_raise():
    """The regression test for ``bipartite.sets()``.

    Three components with nothing joining them. ``bipartite.sets()`` raises
    ``AmbiguousSolution`` on exactly this shape, and a bridge graph is always this
    shape.
    """
    members = [make_member(f"m{i}") for i in range(6)]
    targets = [make_target(f"t{i}") for i in range(3)]
    edges = [
        Edge(member_id="m0", target_id="t0"), Edge(member_id="m1", target_id="t0"),
        Edge(member_id="m2", target_id="t1"), Edge(member_id="m3", target_id="t1"),
        Edge(member_id="m4", target_id="t2"), Edge(member_id="m5", target_id="t2"),
    ]
    bg = build_bipartite(members, targets, edges)

    import networkx as nx

    assert not nx.is_connected(bg.graph)
    with pytest.raises(nx.AmbiguousSolution):
        nx.algorithms.bipartite.sets(bg.graph)

    # the pipeline, which passes the explicit member set, is unbothered
    result = analyse(members, targets, edges)
    assert len(result.clusters) == 3
    assert bg.member_set == {f"m{i}" for i in range(6)}
    _assert_no_nan(result)


def test_t8_4_no_source_file_calls_bipartite_sets():
    """Static guard: the call must not reappear anywhere under ``graph/``.

    Parsed rather than grepped, so the prose in ``build.py`` explaining why the
    call is banned does not itself trip the guard.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "src" / "interlayer" / "graph"
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "sets"
                and isinstance(node.func.value, ast.Attribute | ast.Name)
                and getattr(node.func.value, "attr", getattr(node.func.value, "id", ""))
                == "bipartite"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


# --- T8.5 --------------------------------------------------------------------


def test_t8_5_duplicate_edges_are_deduplicated():
    members = golden_members()
    targets = golden_targets()
    edges = golden_edge_records()
    doubled = [*edges, *edges, *edges]

    bg = build_bipartite(members, targets, doubled)
    assert len(bg.edges) == len(edges)
    assert bg.n_edges_dropped_duplicate == 2 * len(edges)
    assert bg.graph.number_of_edges() == len(edges)

    # and the result is identical to the un-doubled input
    assert (
        analyse(members, targets, doubled).fingerprint
        == analyse(members, targets, edges).fingerprint
    )


def test_t8_5_duplicate_resolution_prefers_the_observation():
    members = [make_member("m0")]
    targets = [make_target("t0")]
    inferred = Edge(
        member_id="m0", target_id="t0", origin=EdgeOrigin.INFERRED,
        confidence=0.9, evidence="guess",
    )
    observed = Edge(member_id="m0", target_id="t0", origin=EdgeOrigin.OBSERVED, confidence=1.0)
    for ordering in ([inferred, observed], [observed, inferred]):
        bg = build_bipartite(members, targets, ordering, include_inferred=True)
        assert len(bg.edges) == 1
        assert bg.edges[0].origin is EdgeOrigin.OBSERVED
        assert bg.used_inferred_edges is False


# --- T8.6 --------------------------------------------------------------------


def test_t8_6_harvested_target_with_zero_mutuals_does_not_divide_by_zero():
    members = golden_members()
    targets = [*golden_targets(), make_target("t_empty"), make_target("t_also_empty")]
    bg = build_bipartite(members, targets, golden_edge_records())
    p = project(bg)

    degrees = dict(zip(bg.target_ids, p.target_degrees.tolist(), strict=True))
    assert degrees["t_empty"] == 0.0
    # the max(d_t, 1) guard: no inf, no nan anywhere in the projection
    assert all(_is_finite(v) for v in p.matrix.data.tolist())
    assert p.n_edges == 15

    result = analyse(members, targets, golden_edge_records())
    _assert_no_nan(result)
    assert result.coverage.n_targets_harvested == 8


@pytest.mark.parametrize("weighting", ["ra", "count", "adamic_adar"])
def test_t8_6_every_weighting_survives_a_zero_degree_column(weighting):
    members = golden_members()
    targets = [*golden_targets(), make_target("t_empty")]
    bg = build_bipartite(members, targets, golden_edge_records())
    p = project(bg, weighting=weighting)
    assert all(_is_finite(v) for v in p.matrix.data.tolist())


def test_t8_6_degree_one_target_survives_adamic_adar():
    """``log(1) == 0``; the ``max(d_t, e)`` guard is what stops the division."""
    bg = build_bipartite(
        [make_member("m0")], [make_target("t0")], [Edge(member_id="m0", target_id="t0")]
    )
    p = project(bg, weighting="adamic_adar")
    assert all(_is_finite(v) for v in p.matrix.data.tolist())


# --- percentile rank invariants ----------------------------------------------


def test_percentile_rank_ties_share_the_mean_rank():
    values = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 5.0}
    ranks = percentile_rank(values)
    assert ranks["a"] == ranks["b"] == ranks["c"] == pytest.approx(2.0 / 4.0)
    assert ranks["d"] == pytest.approx(1.0)


def test_percentile_rank_is_insertion_order_independent():
    forward = percentile_rank({"a": 3.0, "b": 1.0, "c": 2.0})
    backward = percentile_rank({"c": 2.0, "b": 1.0, "a": 3.0})
    assert forward == backward


def test_percentile_rank_handles_empty_and_singleton():
    assert percentile_rank({}) == {}
    assert percentile_rank({"only": 7.0}) == {"only": 1.0}


def test_composite_weights_sum_to_one():
    from interlayer.graph.score import DEFAULT_WEIGHTS

    assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    assert set(DEFAULT_WEIGHTS) == {
        "reach", "rarity", "betweenness", "effective_size", "autonomy"
    }


def test_composite_respects_custom_weights():
    members = golden_members()
    targets = golden_targets()
    bg = build_bipartite(members, targets, golden_edge_records())
    p = project(bg)
    metrics = brokerage_metrics(bg, p, to_igraph(p.matrix, p.member_ids))
    rarity_only = composite_score(metrics, {"rarity": 1.0})
    assert rarity_only["m0"] == pytest.approx(1.0)
    assert rarity_only["m1"] == pytest.approx(percentile_rank(
        {k: v.rarity for k, v in metrics.items()}
    )["m1"])


def test_wilson_is_used_for_reach_share():
    """Reach is a lower bound; the Wilson bound is how that gets quantified."""
    result = analyse(golden_members(), golden_targets(), golden_edge_records())
    harvested = result.coverage.n_targets_harvested
    m0 = next(b for b in result.brokerage if b.member_id == "m0")
    bound = wilson_lb(m0.reach, harvested)
    assert 0.0 < bound < m0.reach / harvested


# --- fingerprint invariants --------------------------------------------------


def test_fingerprint_changes_when_the_partition_changes():
    a = membership_fingerprint({"m0": 0, "m1": 0})
    b = membership_fingerprint({"m0": 0, "m1": 1})
    assert a != b


def test_fingerprint_is_stable_under_key_reordering():
    a = membership_fingerprint({"m0": 0, "m1": 1, "m2": 2})
    b = membership_fingerprint({"m2": 2, "m0": 0, "m1": 1})
    assert a == b


def test_result_fingerprint_reacts_to_a_score_change():
    """The canonical fingerprint is strictly stronger than the membership one."""
    members = golden_members()
    targets = golden_targets()
    edges = golden_edge_records()
    default = analyse(members, targets, edges)
    reweighted = analyse(members, targets, edges, weights={"rarity": 1.0})

    membership_default = {m: c.cluster_id for c in default.clusters for m in c.member_ids}
    membership_reweighted = {m: c.cluster_id for c in reweighted.clusters for m in c.member_ids}
    assert membership_default == membership_reweighted
    assert membership_fingerprint(membership_default) == membership_fingerprint(
        membership_reweighted
    )
    assert default.fingerprint != reweighted.fingerprint


def test_params_are_recorded_on_the_result():
    result = analyse(golden_members(), golden_targets(), golden_edge_records())
    params = result.params
    assert params["gamma"] == 1.0
    assert params["base_seed"] == 42
    assert params["n_runs"] == 25
    assert params["tau"] == 0.5
    assert params["weighting"] == "ra"
    assert params["consensus_applied"] is True
    assert params["k"] == 3
    assert params["modularity"] == pytest.approx(0.26, abs=1e-6)
