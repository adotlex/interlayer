"""T3 — planted-partition / SBM correctness, and T4 — the hub pathology.

T3 uses ``stochastic_block_model`` and ``planted_partition_graph``, never LFR.
R3 measured LFR raising ``ExceededMaxIterations`` at mu <= 0.2 and scoring NMI
0.24 at mu=0.4: it fails on the easy cases and cannot discriminate on the hard
ones. SBM gives exact, assertable values, so the first three cases assert
``NMI == 1.0`` rather than a floor.

T4 is the regression test for the projection weighting. It is the reason RA is the
default and not raw co-count.
"""

from __future__ import annotations

import pytest
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

from interlayer.graph.build import build_bipartite
from interlayer.graph.cluster import leiden_once, modularity, to_igraph
from interlayer.graph.project import project
from test_graph_fixtures import hub_fixture, sbm_igraph

# --- T3 ----------------------------------------------------------------------

SBM_CASES = [
    # id,     sizes,     p_in, p_out, planted, k, nmi_floor, exact, modularity
    ("T3.1", [25] * 4, 0.85, 0.02, False, 4, 1.0, True, 0.6752),
    ("T3.2", [30] * 3, 0.60, 0.03, False, 3, 1.0, True, 0.5564),
    ("T3.3", [40] * 5, 0.30, 0.01, False, 5, 1.0, True, 0.6618),
    ("T3.4", [20] * 6, 0.40, 0.05, False, 6, 0.95, False, 0.4316),
    ("T3.5", [15] * 8, 0.50, 0.04, False, 8, 0.95, False, 0.4838),
    ("T3.6", [25] * 4, 0.85, 0.02, True, 4, 1.0, True, 0.6752),
]


@pytest.mark.parametrize(
    ("case_id", "sizes", "p_in", "p_out", "planted", "expected_k", "nmi_floor", "exact", "q"),
    SBM_CASES,
    ids=[c[0] for c in SBM_CASES],
)
def test_t3_planted_partition_recovery(
    case_id, sizes, p_in, p_out, planted, expected_k, nmi_floor, exact, q
):
    g, truth = sbm_igraph(sizes, p_in, p_out, seed=42, planted=planted)
    membership = leiden_once(g, gamma=1.0, seed=42)

    nmi = normalized_mutual_info_score(truth, membership)
    ari = adjusted_rand_score(truth, membership)

    assert len(set(membership)) == expected_k, f"{case_id}: wrong k"
    if exact:
        assert nmi == pytest.approx(1.0), f"{case_id}: NMI"
        assert ari == pytest.approx(1.0), f"{case_id}: ARI"
    else:
        assert nmi >= nmi_floor, f"{case_id}: NMI {nmi:.4f} below {nmi_floor}"
    # modularity is reported, never gated on a floor — but R3 published it, so a
    # drift here is worth surfacing
    assert modularity(g, membership) == pytest.approx(q, abs=5e-4), f"{case_id}: modularity"


# --- T4 ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def hub():
    members, targets, edges, truth, hub_members = hub_fixture()
    bg = build_bipartite(members, targets, edges)
    return bg, truth, hub_members


@pytest.fixture(scope="module")
def hub_only_pair(hub):
    """Two members whose *only* shared target is the hub.

    This is the pair the pathology acts on: they have nothing in common, so any
    weight between them is manufactured by the hub alone.
    """
    bg, _, hub_members = hub
    neighbourhoods = {m: set(bg.neighbours(m)) for m in hub_members}
    for i, a in enumerate(hub_members):
        for b in hub_members[i + 1 :]:
            if neighbourhoods[a] & neighbourhoods[b] == {"t_hub"}:
                return a, b
    pytest.fail("fixture produced no hub-only pair")


def test_t4_fixture_shape(hub):
    """The hub really is a hub: degree 100 against a median in the twenties."""
    bg, truth, hub_members = hub
    degrees = bg.target_degrees()
    assert degrees["t_hub"] == 100
    assert len(hub_members) == 100
    assert len(truth) == 150
    others = sorted(v for k, v in degrees.items() if k != "t_hub")
    assert max(others) < 100


def test_t4_1_raw_count_gives_the_hub_only_pair_full_weight(hub, hub_only_pair):
    bg, _, _ = hub
    a, b = hub_only_pair
    assert project(bg, weighting="count").weight(a, b) == pytest.approx(1.0)


def test_t4_2_ra_suppresses_the_hub_only_pair_to_one_percent(hub, hub_only_pair):
    bg, _, _ = hub
    a, b = hub_only_pair
    weight = project(bg, weighting="ra").weight(a, b)
    assert weight == pytest.approx(0.01, abs=1e-6)
    assert weight <= 0.05


def test_t4_3_adamic_adar_under_corrects(hub, hub_only_pair):
    """AA is on the right axis but ``1/log d`` decays too slowly to fix this."""
    bg, _, _ = hub
    a, b = hub_only_pair
    weight = project(bg, weighting="adamic_adar").weight(a, b)
    assert weight == pytest.approx(0.2171, abs=1e-3)
    assert weight > 0.15


def test_t4_ra_is_a_hundred_fold_stronger_correction_than_raw_count(hub, hub_only_pair):
    bg, _, _ = hub
    a, b = hub_only_pair
    count = project(bg, weighting="count").weight(a, b)
    ra = project(bg, weighting="ra").weight(a, b)
    assert count / ra == pytest.approx(100.0, rel=1e-6)


def test_t4_4_ra_still_recovers_the_planted_structure(hub):
    bg, truth, _ = hub
    p = project(bg, weighting="ra")
    g = to_igraph(p.matrix, p.member_ids)
    membership = leiden_once(g, gamma=1.0, seed=42)
    order = {name: i for i, name in enumerate(g.vs["name"])}
    truth_ordered = [truth[int(name[1:])] for name in sorted(order, key=lambda n: order[n])]

    assert len(set(membership)) == 6
    assert normalized_mutual_info_score(truth_ordered, membership) >= 0.85
