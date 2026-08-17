"""T9 — performance. Marked ``slow`` and excluded from the default CI run.

R3's measurements on the reference machine, for comparison:

* RA sparse projection at 8,000 x 1,600: **0.02s**, 339,091 projection edges
* Leiden (``n_iterations=2``) on that projection: **2.4s**, k=40, NMI 0.9434
* ``igraph.betweenness()`` at 2,000 nodes / 71,758 edges: **1.8s**
  (``networkx.betweenness_centrality()`` exact: 70.7s — the ~40x that decided it)

The thresholds asserted below are the generous CI budgets from R3's test spec, not
the observed timings; a machine slower than the reference should still pass.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
import scipy.sparse as sp
from sklearn.metrics import normalized_mutual_info_score

from interlayer.core.models import Edge
from interlayer.graph import analyse
from interlayer.graph.build import build_bipartite
from interlayer.graph.cluster import leiden_once, to_igraph
from interlayer.graph.project import project
from test_graph_fixtures import make_member, make_target

pytestmark = pytest.mark.slow


def _pocket_fixture(n_members: int, n_targets: int, n_pockets: int, seed: int = 5):
    """Structured bipartite instance: each member draws targets from its own pocket."""
    rng = np.random.default_rng(seed)
    targets_per_pocket = n_targets // n_pockets
    truth = [i % n_pockets for i in range(n_members)]

    member_ids = [f"m{i:05d}" for i in range(n_members)]
    target_ids = [f"t{i:05d}" for i in range(n_targets)]

    pairs: set[tuple[str, str]] = set()
    for i, member_id in enumerate(member_ids):
        pocket = truth[i]
        base = pocket * targets_per_pocket
        picks = rng.choice(targets_per_pocket, size=min(4, targets_per_pocket), replace=False)
        for pick in picks:
            pairs.add((member_id, target_ids[base + int(pick)]))
        if rng.random() < 0.05:  # cross-pocket noise
            pairs.add((member_id, target_ids[int(rng.integers(0, n_targets))]))

    members = [make_member(m) for m in member_ids]
    targets = [make_target(t) for t in target_ids]
    edges = [Edge(member_id=m, target_id=t) for m, t in sorted(pairs)]
    return members, targets, edges, truth, member_ids


def test_t9_1_projection_and_leiden_at_scale():
    """8,000 members x 1,600 targets x 40 pockets."""
    members, targets, edges, truth, member_ids = _pocket_fixture(8_000, 1_600, 40)
    truth_by_id = dict(zip(member_ids, truth, strict=True))

    bg = build_bipartite(members, targets, edges)

    start = time.perf_counter()
    p = project(bg, weighting="ra")
    projection_seconds = time.perf_counter() - start
    assert projection_seconds < 1.0, f"RA projection took {projection_seconds:.3f}s"

    g = to_igraph(p.matrix, p.member_ids)

    start = time.perf_counter()
    membership = leiden_once(g, gamma=1.0, seed=42)
    leiden_seconds = time.perf_counter() - start
    assert leiden_seconds < 15.0, f"Leiden took {leiden_seconds:.3f}s"

    truth_ordered = [truth_by_id[name] for name in g.vs["name"]]
    nmi = normalized_mutual_info_score(truth_ordered, membership)
    assert nmi >= 0.90, f"NMI {nmi:.4f}"
    print(
        f"\nT9.1 projection {projection_seconds:.3f}s ({p.n_edges} edges), "
        f"leiden {leiden_seconds:.3f}s, k={len(set(membership))}, NMI={nmi:.4f}"
    )


def test_t9_2_full_pipeline_at_ten_thousand_members():
    """Whole pipeline, betweenness excluded, under 60s.

    Betweenness is excluded by keeping the graph above the consensus dense limit
    is *not* how it is done — instead the projection is large enough that
    ``igraph.betweenness`` would dominate, so this test measures the rest.
    """
    members, targets, edges, _, _ = _pocket_fixture(10_000, 2_000, 50)
    bg = build_bipartite(members, targets, edges)

    start = time.perf_counter()
    p = project(bg, weighting="ra")
    g = to_igraph(p.matrix, p.member_ids)
    membership = leiden_once(g, gamma=1.0, seed=42)
    elapsed = time.perf_counter() - start

    assert elapsed < 60.0, f"pipeline (excluding betweenness) took {elapsed:.1f}s"
    assert len(set(membership)) > 1
    print(f"\nT9.2 {elapsed:.2f}s at 10,000 members, {p.n_edges} projection edges")


def test_t9_3_igraph_betweenness_at_two_thousand_nodes():
    members, targets, edges, _, _ = _pocket_fixture(2_000, 400, 20)
    bg = build_bipartite(members, targets, edges)
    p = project(bg, weighting="ra")
    g = to_igraph(p.matrix, p.member_ids)

    start = time.perf_counter()
    values = g.betweenness()
    elapsed = time.perf_counter() - start

    assert elapsed < 10.0, f"igraph betweenness took {elapsed:.2f}s"
    assert len(values) == g.vcount()
    print(f"\nT9.3 betweenness {elapsed:.2f}s at {g.vcount()} nodes / {g.ecount()} edges")


def test_t9_end_to_end_analyse_stays_deterministic_at_scale():
    """A smaller instance, but through the real entry point, run twice."""
    members, targets, edges, _, _ = _pocket_fixture(1_200, 240, 12)
    first = analyse(members, targets, edges)
    second = analyse(list(reversed(members)), list(reversed(targets)), list(reversed(edges)))
    assert first.fingerprint == second.fingerprint


def test_t9_sparse_projection_matches_a_dense_reference():
    """Guard the matmul itself against a naive dense computation."""
    members, targets, edges, _, _ = _pocket_fixture(200, 40, 4)
    bg = build_bipartite(members, targets, edges)
    p = project(bg, weighting="ra")

    A = np.asarray(p.biadjacency.todense())
    degrees = A.sum(axis=0)
    expected = A @ np.diag(1.0 / np.maximum(degrees, 1.0)) @ A.T
    np.fill_diagonal(expected, 0.0)

    actual = np.asarray(sp.csr_matrix(p.matrix).todense())
    assert np.allclose(actual, np.round(expected, 12), atol=1e-11)
