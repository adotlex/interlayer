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


# --- fallback, gamma sweep, and the network boundary -------------------------


def test_louvain_fallback_runs_when_leidenalg_is_unavailable(monkeypatch):
    """The fallback exists for a platform with no ``leidenalg`` wheel.

    Louvain is only a fallback because it can emit internally disconnected
    communities — a "pocket of bridges" that is not one pocket. On a fixture this
    clean it still finds the right three.
    """
    from interlayer.graph import cluster as cluster_module
    from test_graph_fixtures import golden_edge_records, golden_members, golden_targets

    bg = build_bipartite(golden_members(), golden_targets(), golden_edge_records())
    p = project(bg)
    g = to_igraph(p.matrix, p.member_ids)

    monkeypatch.setattr(cluster_module, "HAVE_LEIDENALG", False)
    membership = cluster_module.leiden_once(g, gamma=1.0, seed=42)

    assert len(set(membership)) == 3
    blocks = {
        frozenset(name for name, c in zip(g.vs["name"], membership, strict=True) if c == cid)
        for cid in set(membership)
    }
    assert blocks == {
        frozenset({"m0", "m1", "m2"}),
        frozenset({"m3", "m4", "m5"}),
        frozenset({"m6", "m7", "m8"}),
    }


def test_louvain_fallback_restores_the_global_igraph_rng(monkeypatch):
    """Seeding igraph's global RNG must not leak into the rest of the process."""
    import random as random_module

    import igraph as ig

    from interlayer.graph import cluster as cluster_module
    from test_graph_fixtures import golden_edge_records, golden_members, golden_targets

    bg = build_bipartite(golden_members(), golden_targets(), golden_edge_records())
    g = to_igraph(project(bg).matrix, bg.member_ids)

    monkeypatch.setattr(cluster_module, "HAVE_LEIDENALG", False)
    cluster_module.leiden_once(g, seed=42)

    # a fresh Graph.Erdos_Renyi must still vary, i.e. the RNG is not pinned
    ig.set_random_number_generator(random_module)
    samples = {ig.Graph.Erdos_Renyi(n=30, p=0.3).ecount() for _ in range(8)}
    assert len(samples) > 1


def test_select_gamma_prefers_the_most_stable_resolution():
    from interlayer.graph.cluster import GAMMA_SWEEP, select_gamma
    from test_graph_fixtures import golden_edge_records, golden_members, golden_targets

    bg = build_bipartite(golden_members(), golden_targets(), golden_edge_records())
    g = to_igraph(project(bg).matrix, bg.member_ids)
    chosen = select_gamma(g, n_runs=5)
    assert chosen in GAMMA_SWEEP
    # every gamma is perfectly stable on this fixture, so the tie-break wins
    assert chosen == 1.0


def test_select_gamma_is_deterministic():
    from interlayer.graph.cluster import select_gamma
    from test_graph_fixtures import hub_fixture

    members, targets, edges, _, _ = hub_fixture(with_hub=False)
    bg = build_bipartite(members, targets, edges)
    g = to_igraph(project(bg).matrix, bg.member_ids)
    assert len({select_gamma(g, n_runs=5) for _ in range(3)}) == 1


def test_boundary_1_graph_package_imports_no_network_module():
    """Setup guide §3, boundary 1: the analysis layer never touches the network."""
    import ast
    from pathlib import Path

    banned = {"httpx", "requests", "urllib", "socket", "http", "ftplib", "telnetlib"}
    package = Path(__file__).resolve().parents[1] / "src" / "interlayer" / "graph"

    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in banned:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert offenders == []
