"""T2 — determinism.

The single most important property in the build. R3 measured that a fixed Leiden
seed is *not* sufficient: 12 vertex permutations of one graph at ``seed=42``
produced 12 distinct partitions. T2.4 reproduces that failure deliberately, so the
sorting rule has a test that proves why it exists rather than merely asserting it.
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import textwrap
from pathlib import Path

import igraph as ig
import leidenalg as la
import networkx as nx
import numpy as np
import pytest

from interlayer.graph import analyse, membership_fingerprint
from interlayer.graph.build import build_bipartite
from interlayer.graph.cluster import leiden_consensus, to_igraph
from interlayer.graph.project import project
from test_graph_fixtures import (
    GOLDEN_MEMBERSHIP,
    golden_edge_records,
    golden_members,
    golden_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def golden():
    return golden_members(), golden_targets(), golden_edge_records()


# --- T2.1 --------------------------------------------------------------------


def test_t2_1_ten_runs_give_one_fingerprint(golden):
    members, targets, edges = golden
    prints = {analyse(members, targets, edges).fingerprint for _ in range(10)}
    assert len(prints) == 1


# --- T2.2 --------------------------------------------------------------------


def test_t2_2_five_input_orderings_give_one_fingerprint(golden):
    members, targets, edges = golden
    prints = set()
    memberships = set()
    for seed in range(5):
        rng = random.Random(seed)
        shuffled_members = list(members)
        shuffled_edges = list(edges)
        shuffled_targets = list(targets)
        rng.shuffle(shuffled_members)
        rng.shuffle(shuffled_edges)
        rng.shuffle(shuffled_targets)
        result = analyse(shuffled_members, shuffled_targets, shuffled_edges)
        prints.add(result.fingerprint)
        memberships.add(
            tuple(sorted((m, c.cluster_id) for c in result.clusters for m in c.member_ids))
        )
    assert len(prints) == 1
    assert len(memberships) == 1


def test_t2_2_membership_fingerprint_matches_r3_definition(golden):
    """R3 defines the fingerprint as sha256 over sorted {member_id: cluster_id}."""
    members, targets, edges = golden
    result = analyse(members, targets, edges)
    membership = {m: c.cluster_id for c in result.clusters for m in c.member_ids}
    assert membership == GOLDEN_MEMBERSHIP
    digest = membership_fingerprint(membership)
    assert len(digest) == 64
    # order-independent by construction
    assert digest == membership_fingerprint(dict(reversed(list(membership.items()))))


# --- T2.3 --------------------------------------------------------------------


SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {tests!r})
    from interlayer.graph import analyse
    from test_graph_fixtures import golden_members, golden_targets, golden_edge_records
    result = analyse(golden_members(), golden_targets(), golden_edge_records())
    membership = {{m: c.cluster_id for c in result.clusters for m in c.member_ids}}
    print(json.dumps({{"fp": result.fingerprint, "membership": membership}}))
    """
).format(tests=str(REPO_ROOT / "tests"))


def _run_in_subprocess(hashseed: str) -> dict:
    env = {
        "PYTHONHASHSEED": hashseed,
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", SUBPROCESS_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_t2_3_pythonhashseed_does_not_change_the_fingerprint(golden):
    members, targets, edges = golden
    in_process = analyse(members, targets, edges).fingerprint

    fingerprints = {in_process}
    for hashseed in ("0", "1", "999"):
        payload = _run_in_subprocess(hashseed)
        assert payload["membership"] == GOLDEN_MEMBERSHIP, f"PYTHONHASHSEED={hashseed}"
        fingerprints.add(payload["fp"])

    assert len(fingerprints) == 1, f"fingerprint moved across hash seeds: {fingerprints}"


# --- T2.4 negative control ---------------------------------------------------


def _canonical_blocks(order, partition) -> frozenset[frozenset[str]]:
    return frozenset(frozenset(str(order[i]) for i in block) for block in partition)


@pytest.mark.filterwarnings("ignore::UserWarning")
def test_t2_4_negative_control_unsorted_order_is_not_deterministic():
    """Remove the sort and a fixed seed stops meaning anything.

    This is the one place LFR is used. R3 documented that
    ``LFR_benchmark_graph`` raises ``ExceededMaxIterations`` at mu <= 0.2, so it
    is unusable as a CI gate — but this specific call at mu=0.45 is documented to
    work, and an ambiguous graph is exactly what the control needs.
    """
    graph = nx.LFR_benchmark_graph(
        300, 2.8, 1.6, 0.45, average_degree=8, min_community=25, seed=11
    )
    nodes = list(graph.nodes())
    rng = np.random.default_rng(0)

    unsorted_partitions = set()
    for _ in range(12):
        order = list(nodes)
        rng.shuffle(order)
        index = {n: i for i, n in enumerate(order)}
        g = ig.Graph(n=len(order))
        g.add_edges([(index[u], index[v]) for u, v in graph.edges()])  # deliberately unsorted
        part = la.find_partition(
            g, la.RBConfigurationVertexPartition,
            resolution_parameter=1.0, n_iterations=-1, seed=42,
        )
        unsorted_partitions.add(_canonical_blocks(order, part))

    assert len(unsorted_partitions) > 1, (
        "the negative control did not reproduce the instability it documents"
    )

    sorted_partitions = set()
    for _ in range(12):
        order = sorted(nodes)
        index = {n: i for i, n in enumerate(order)}
        edges = sorted(
            {(min(index[u], index[v]), max(index[u], index[v])) for u, v in graph.edges()}
        )
        g = ig.Graph(n=len(order))
        g.add_edges(edges)
        part = la.find_partition(
            g, la.RBConfigurationVertexPartition,
            resolution_parameter=1.0, n_iterations=-1, seed=42,
        )
        sorted_partitions.add(_canonical_blocks(order, part))

    assert len(sorted_partitions) == 1, "sorted node order must collapse this to one partition"


def test_t2_4_to_igraph_normalises_a_permuted_input(golden):
    """``to_igraph`` re-sorts a matrix handed to it out of order."""
    members, targets, edges = golden
    bg = build_bipartite(members, targets, edges)
    p = project(bg)

    order = list(range(len(p.member_ids)))
    random.Random(7).shuffle(order)
    permuted_names = [p.member_ids[i] for i in order]
    permuted_matrix = p.matrix[order, :][:, order]

    g_sorted = to_igraph(p.matrix, p.member_ids)
    g_permuted = to_igraph(permuted_matrix, permuted_names)

    assert g_sorted.vs["name"] == g_permuted.vs["name"]
    assert g_sorted.get_edgelist() == g_permuted.get_edgelist()
    assert g_sorted.es["weight"] == pytest.approx(g_permuted.es["weight"])


# --- T2.5 --------------------------------------------------------------------


def test_t2_5_consensus_is_seed_block_independent(golden):
    """base_seed 42 and 1000 give the same *partition*.

    Not the same integer labels — leidenalg numbers communities by discovery
    order, which the seed block moves. Comparing blocks is the only meaningful
    reading of "same partition".
    """
    members, targets, edges = golden
    bg = build_bipartite(members, targets, edges)
    p = project(bg)
    g = to_igraph(p.matrix, p.member_ids)

    a = leiden_consensus(g, base_seed=42)
    b = leiden_consensus(g, base_seed=1000)
    assert a.blocks() == b.blocks()
    assert a.k == b.k == 3


def test_t2_5_consensus_matrix_is_symmetric_and_bounded(golden):
    members, targets, edges = golden
    bg = build_bipartite(members, targets, edges)
    g = to_igraph(project(bg).matrix, bg.member_ids)
    part = leiden_consensus(g)
    co = part.co_association
    assert co is not None
    assert np.allclose(co, co.T)
    assert co.min() >= 0.0 and co.max() <= 1.0
    assert all(0.0 <= s <= 1.0 for s in part.stability)
    # well-separated pockets: every node is perfectly stable
    assert all(s == pytest.approx(1.0) for s in part.stability)


def test_t2_5_consensus_falls_back_above_the_dense_limit(golden):
    """The dense co-association matrix has a size guard; crossing it must not raise."""
    members, targets, edges = golden
    bg = build_bipartite(members, targets, edges)
    g = to_igraph(project(bg).matrix, bg.member_ids)
    part = leiden_consensus(g, max_dense_nodes=2)
    assert part.consensus_applied is False
    assert part.by_member() == GOLDEN_MEMBERSHIP
    assert part.co_association is None
