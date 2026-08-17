"""T5 — missing data. Never let "not collected" masquerade as "no edge".

The failure mode this guards: a target whose mutual-connections page was never
opened has ``d_t = 0``, which in a matrix is indistinguishable from a target
genuinely connected to nobody. Every statistic over targets — rarity denominators,
coverage, "who is most connected" — is then silently wrong.
"""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import normalized_mutual_info_score

from interlayer.core.models import Target
from interlayer.graph import analyse, wilson_lb
from interlayer.graph.brokerage import reach_and_rarity
from interlayer.graph.build import build_bipartite
from interlayer.graph.cluster import leiden_once, to_igraph
from interlayer.graph.nextsteps import next_targets
from interlayer.graph.project import project
from test_graph_fixtures import (
    GOLDEN_RARITY,
    golden_edge_records,
    golden_members,
    golden_targets,
    hub_fixture,
    make_target,
)

# --- T5.1 --------------------------------------------------------------------


def test_t5_1_unharvested_targets_contribute_no_edges():
    members = golden_members()
    edges = golden_edge_records()
    targets = golden_targets()

    baseline = build_bipartite(members, targets, edges)
    _, rarity_before = reach_and_rarity(baseline)

    # add 20 unharvested targets, and edges pointing at them
    extra = [make_target(f"u{i}", harvested=False) for i in range(20)]
    from interlayer.core.models import Edge

    extra_edges = [Edge(member_id=f"m{i % 9}", target_id=f"u{i}") for i in range(20)]

    widened = build_bipartite(members, [*targets, *extra], [*edges, *extra_edges])
    _, rarity_after = reach_and_rarity(widened)

    assert rarity_before == rarity_after
    assert widened.n_edges_dropped_unharvested == 20
    assert widened.target_ids == baseline.target_ids
    for member_id, expected in GOLDEN_RARITY.items():
        assert rarity_after[member_id] == pytest.approx(expected, abs=1e-6)


def test_t5_1_unharvested_target_is_absent_from_the_graph():
    members = golden_members()
    targets = [*golden_targets(), make_target("t_never", harvested=False)]
    bg = build_bipartite(members, targets, golden_edge_records())
    assert "t_never" not in bg.graph
    assert "t_never" not in bg.target_degrees()


def test_t5_1_harvested_target_with_zero_mutuals_is_kept_as_a_column():
    """It is a real observation — "we looked and found nobody" — not a gap."""
    members = golden_members()
    targets = [*golden_targets(), make_target("t_empty", harvested=True)]
    bg = build_bipartite(members, targets, golden_edge_records())
    assert "t_empty" in bg.graph
    assert bg.target_degrees()["t_empty"] == 0
    assert bg.n_targets_harvested == 7


# --- T5.2 --------------------------------------------------------------------


@pytest.mark.parametrize(("n_harvested", "n_total"), [(0, 6), (1, 6), (3, 6), (6, 6), (2, 9)])
def test_t5_2_coverage_is_exactly_harvested_over_total(n_harvested, n_total):
    members = golden_members()
    targets = [make_target(f"t{i}", harvested=i < n_harvested) for i in range(n_total)]
    result = analyse(members, targets, golden_edge_records())
    assert result.coverage.n_targets_total == n_total
    assert result.coverage.n_targets_harvested == n_harvested
    assert result.coverage.fraction == pytest.approx(n_harvested / n_total)


# --- T5.3 --------------------------------------------------------------------


def _drop_target_columns(targets, fraction, seed=11):
    """Mark a fraction of targets unharvested — the tri-state way to "drop"."""
    rng = np.random.default_rng(seed)
    ordered = sorted(targets, key=lambda t: t.target_id)
    n_drop = int(round(fraction * len(ordered)))
    dropped = set(rng.choice(len(ordered), size=n_drop, replace=False).tolist())
    return [
        Target(target_id=t.target_id, firm=t.firm, harvested=(i not in dropped))
        for i, t in enumerate(ordered)
    ]


def _nmi_after_loss(members, targets, edges, truth, fraction):
    bg = build_bipartite(members, _drop_target_columns(targets, fraction), edges)
    if len(bg.member_ids) < 2:
        return 0.0, 0, 0
    p = project(bg)
    g = to_igraph(p.matrix, p.member_ids)
    membership = leiden_once(g, gamma=1.0, seed=42)
    names = list(g.vs["name"])
    # members retaining >= 1 edge only; build_bipartite already guarantees that
    truth_ordered = [truth[int(name[1:])] for name in names]
    return (
        normalized_mutual_info_score(truth_ordered, membership),
        len(names),
        len(set(membership)),
    )


@pytest.mark.parametrize(("fraction", "floor"), [(0.30, 0.80), (0.60, 0.70), (0.80, 0.70)])
def test_t5_3_degradation_is_graceful(fraction, floor):
    """Target-column loss must degrade gracefully, not fall off a cliff.

    Fixture note: this uses a 320-member / 8-pocket / 64-target instance rather
    than the 12-target T4 base fixture named in R3 §T5.3. With only 12 target
    columns an 80% drop leaves two or three, which cannot encode six pockets at
    all — Leiden correctly returns k=1 and NMI 0.0. See ``docs/wave2-notes/B3.md``.
    """
    members, targets, edges, truth, _ = hub_fixture(
        n_pockets=8, per_pocket=40, targets_per_pocket=4, noise=0.12, with_hub=False
    )
    nmi, n_bridges, k = _nmi_after_loss(members, targets, edges, truth, fraction)
    assert n_bridges > 0
    assert nmi >= floor, f"NMI {nmi:.4f} below floor {floor} at {fraction:.0%} loss (k={k})"


@pytest.mark.parametrize(("fraction", "floor"), [(0.30, 0.80), (0.60, 0.70)])
def test_t5_3_t4_base_fixture_degradation(fraction, floor):
    """The same assertion on R3's named fixture, for the two levels it supports."""
    members, targets, edges, truth, _ = hub_fixture(with_hub=False)
    nmi, n_bridges, _ = _nmi_after_loss(members, targets, edges, truth, fraction)
    assert n_bridges > 0
    assert nmi >= floor


def test_t5_3_twelve_column_fixture_cannot_survive_eighty_percent_loss():
    """Documents *why* the 80% case is measured on a richer fixture.

    Two surviving target columns cannot distinguish six pockets. This is an
    information-theoretic limit of the fixture, not a defect in the pipeline.
    """
    members, targets, edges, truth, _ = hub_fixture(with_hub=False)
    surviving = [t for t in _drop_target_columns(targets, 0.80) if t.harvested]
    assert len(surviving) <= 3
    nmi, _, k = _nmi_after_loss(members, targets, edges, truth, 0.80)
    assert k <= 3
    assert nmi < 0.70


# --- T5.4 --------------------------------------------------------------------


def test_t5_4_wilson_lower_bound():
    assert wilson_lb(3, 5) == pytest.approx(0.231, abs=0.001)
    assert wilson_lb(0, 0) == 0.0


@pytest.mark.parametrize(("k", "n"), [(0, 5), (5, 5), (1, 1), (0, 1), (50, 100), (3, 5)])
def test_t5_4_wilson_is_bounded_and_never_exceeds_the_point_estimate(k, n):
    lb = wilson_lb(k, n)
    assert 0.0 <= lb <= 1.0
    assert lb <= k / n + 1e-12


def test_t5_4_wilson_tightens_as_evidence_accumulates():
    """Same 60% hit rate, more samples: the bound must rise toward it."""
    bounds = [wilson_lb(int(0.6 * n), n) for n in (5, 25, 100, 1000)]
    assert bounds == sorted(bounds)
    assert bounds[0] < 0.35
    assert bounds[-1] > 0.55


# --- T5.5 --------------------------------------------------------------------


def test_t5_5_next_targets_is_deterministic():
    members, targets, edges, _, _ = hub_fixture(with_hub=False)
    partial = [
        Target(target_id=t.target_id, firm=t.firm, harvested=(i % 3 != 0))
        for i, t in enumerate(sorted(targets, key=lambda x: x.target_id))
    ]
    runs = {
        tuple((n.target_id, n.priority) for n in analyse(members, partial, edges).next_targets)
        for _ in range(5)
    }
    assert len(runs) == 1


def test_t5_5_ties_break_by_target_id_ascending():
    """With no signal at all, every target ties at priority 0 and sorts by id."""
    members = golden_members()
    targets = [
        *golden_targets(),
        make_target("t_zz", harvested=False),
        make_target("t_aa", harvested=False),
        make_target("t_mm", harvested=False),
    ]
    result = analyse(members, targets, golden_edge_records())
    ids = [n.target_id for n in result.next_targets]
    assert ids == ["t_aa", "t_mm", "t_zz"]
    assert all(n.priority == 0.0 for n in result.next_targets)


def test_t5_5_priority_formula_and_ordering():
    """A target reaching more bridges and more clusters must outrank one that does not."""
    from interlayer.core.models import Edge

    members = golden_members()
    targets = [
        *golden_targets(),
        make_target("t_span", harvested=False),
        make_target("t_one", harvested=False),
    ]
    signal = [
        # t_span touches all three pockets
        Edge(member_id="m1", target_id="t_span"),
        Edge(member_id="m3", target_id="t_span"),
        Edge(member_id="m6", target_id="t_span"),
        # t_one touches a single member in a single pocket
        Edge(member_id="m4", target_id="t_one"),
    ]
    result = analyse(members, targets, [*golden_edge_records(), *signal])
    ranked = list(result.next_targets)
    assert ranked[0].target_id == "t_span"
    assert ranked[0].n_known_bridges == 3
    assert ranked[0].n_clusters_touched == 3
    assert ranked[1].target_id == "t_one"
    assert ranked[1].n_known_bridges == 1
    assert ranked[1].n_clusters_touched == 1
    # 1.0*bridges + 1.0*clusters + 0.5*novelty, novelty = sum 1/(1+seen_c)
    assert ranked[0].priority == pytest.approx(
        3.0 + 3.0 + 0.5 * ranked[0].novelty
    )
    assert ranked[0].priority > ranked[1].priority


def test_t5_5_top_is_capped_at_twenty():
    members = golden_members()
    targets = [*golden_targets(), *(make_target(f"u{i:03d}", harvested=False) for i in range(50))]
    result = analyse(members, targets, golden_edge_records())
    assert len(result.next_targets) == 20


def test_t5_5_harvested_targets_are_never_suggested():
    members = golden_members()
    targets = golden_targets()
    result = analyse(members, targets, golden_edge_records())
    assert result.next_targets == ()


def test_t5_5_next_targets_direct_call_is_order_independent():
    from interlayer.core.models import Edge

    targets = [make_target("t_b", harvested=False), make_target("t_a", harvested=False)]
    signal = [Edge(member_id="m0", target_id="t_a"), Edge(member_id="m0", target_id="t_b")]
    membership = {"m0": 0}
    forward = next_targets(targets, signal, [], membership)
    backward = next_targets(list(reversed(targets)), list(reversed(signal)), [], membership)
    assert forward == backward
    assert [n.target_id for n in forward] == ["t_a", "t_b"]
