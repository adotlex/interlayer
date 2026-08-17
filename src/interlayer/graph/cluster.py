"""Steps 8-9: deterministic igraph conversion, then consensus Leiden.

**Read this before changing anything here.** R3 measured, on one graph with
``seed=42`` held fixed:

===========================================  ====================
condition                                    distinct partitions
===========================================  ====================
12 random vertex permutations, same seed     **12**
12 runs, same node order, same seed          1
12 different seeds, same node order          12
===========================================  ====================

A fixed seed is therefore *not* a determinism mechanism. Leiden's local-moving
phase breaks ties on vertex index order, so node insertion order is part of the
contract. :func:`to_igraph` is what makes the seed mean anything, and the
regression test for it is T2.4, which reproduces the 12-partition failure by
removing the sort.

Consensus (Lancichinetti & Fortunato 2012) sits on top for a second reason: even
with order fixed, 12 *different* seeds still give 12 partitions on an ambiguous
graph, so a single seed is an arbitrary pick among near-ties. Twenty-five runs,
co-association, threshold at tau, re-cluster once. The co-association matrix is
also free confidence: per-node stability is the mean co-association with the rest
of one's final cluster.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

import igraph as ig
import numpy as np
import scipy.sparse as sp

try:  # pragma: no cover - exercised by the fallback test via monkeypatch
    import leidenalg as la

    HAVE_LEIDENALG = True
except ImportError:  # pragma: no cover
    la = None
    HAVE_LEIDENALG = False

DEFAULT_GAMMA = 1.0
DEFAULT_BASE_SEED = 42
DEFAULT_N_RUNS = 25
DEFAULT_TAU = 0.5
GAMMA_SWEEP: tuple[float, ...] = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)

#: Above this many nodes the dense |B|^2 co-association matrix stops being
#: reasonable (5,000 nodes is ~50 MB as uint16, 8,000 is ~128 MB). Beyond it the
#: pipeline runs single-seed Leiden, which is still fully deterministic because of
#: the sort — it just loses the consensus averaging and the stability surface.
DEFAULT_MAX_CONSENSUS_NODES = 5000


@dataclass(frozen=True, slots=True)
class ConsensusPartition:
    """Result of consensus clustering, aligned to ``g.vs["name"]`` order."""

    membership: tuple[int, ...]
    stability: tuple[float, ...]
    names: tuple[str, ...]
    n_runs: int
    tau: float
    gamma: float
    base_seed: int
    modularity: float
    consensus_applied: bool
    co_association: np.ndarray | None = field(default=None, repr=False)

    @property
    def k(self) -> int:
        return len(set(self.membership))

    def by_member(self) -> dict[str, int]:
        return dict(zip(self.names, self.membership, strict=True))

    def stability_by_member(self) -> dict[str, float]:
        return dict(zip(self.names, self.stability, strict=True))

    def blocks(self) -> frozenset[frozenset[str]]:
        """The partition as a set of blocks — label-invariant.

        Consensus at ``base_seed=42`` and at ``base_seed=1000`` produce the same
        *partition* but not the same integer labels, so any test asserting
        "same partition" across seeds has to compare blocks, not label vectors.
        """
        groups: dict[int, set[str]] = {}
        for name, cid in zip(self.names, self.membership, strict=True):
            groups.setdefault(cid, set()).add(name)
        return frozenset(frozenset(v) for v in groups.values())


def to_igraph(matrix: sp.spmatrix, names: Sequence[str]) -> ig.Graph:
    """Convert a symmetric sparse projection to igraph, deterministically.

    The three rules, all mandatory (R3 §2.4):

    1. vertices in sorted name order, and ``g.vs["name"]`` set from that list;
    2. edges emitted as sorted ``(min_idx, max_idx, weight)``;
    3. weights already rounded (see :data:`interlayer.graph.project.ROUND_DP`).
    """
    given = list(names)
    sorted_names = sorted(given)

    csr = sp.csr_matrix(matrix)
    if given != sorted_names:
        # The caller handed us a matrix whose row order is not the sorted order;
        # permute it rather than trusting the caller, because getting this wrong
        # is silent and costs determinism.
        position = {name: i for i, name in enumerate(given)}
        order = [position[name] for name in sorted_names]
        csr = csr[order, :][:, order]

    upper = sp.triu(csr, k=1).tocoo()
    edges = sorted(
        (int(i), int(j), float(w))
        for i, j, w in zip(upper.row, upper.col, upper.data, strict=True)
    )

    g = ig.Graph(n=len(sorted_names))
    g.vs["name"] = sorted_names
    if edges:
        g.add_edges([(a, b) for a, b, _ in edges])
        g.es["weight"] = [w for _, _, w in edges]
    return g


def _weight_attr(g: ig.Graph) -> str | None:
    return "weight" if "weight" in g.edge_attributes() else None


def leiden_once(
    g: ig.Graph, *, gamma: float = DEFAULT_GAMMA, seed: int = DEFAULT_BASE_SEED
) -> list[int]:
    """One Leiden run. Falls back to Louvain when ``leidenalg`` is unavailable.

    ``RBConfigurationVertexPartition`` is used rather than
    ``ModularityVertexPartition`` because it exposes ``resolution_parameter``
    (identical to modularity at gamma=1.0), and rather than ``CPMVertexPartition``
    because CPM's gamma is an absolute density threshold needing per-dataset tuning.
    """
    if g.vcount() == 0:
        return []
    if not HAVE_LEIDENALG:
        return _louvain_fallback(g, seed=seed)
    part = la.find_partition(
        g,
        la.RBConfigurationVertexPartition,
        weights=_weight_attr(g),
        resolution_parameter=gamma,
        n_iterations=-1,
        seed=seed,
    )
    return list(part.membership)


def _louvain_fallback(g: ig.Graph, *, seed: int) -> list[int]:
    """igraph's ``community_multilevel``, seeded through the global RNG.

    Louvain can emit internally disconnected communities (Traag et al. measured up
    to 25% badly connected, 16% disconnected), which for us would mean reporting a
    "pocket of bridges" that is not actually one pocket. It exists only so the
    pipeline still runs on a platform where ``leidenalg`` has no wheel.
    """
    ig.set_random_number_generator(random.Random(seed))
    try:
        clustering = g.community_multilevel(weights=_weight_attr(g))
        return list(clustering.membership)
    finally:
        ig.set_random_number_generator(random)


def modularity(g: ig.Graph, membership: Sequence[int]) -> float:
    """Unweighted Newman modularity of a membership vector.

    Deliberately unweighted: it is what R3 measured and published (0.260000 on the
    golden fixture; the weighted value of the same partition is 0.447066). Report
    only — never assert a floor on it, because modularity is degenerate.
    """
    if g.vcount() == 0 or g.ecount() == 0:
        return 0.0
    return float(g.modularity(list(membership)))


def _co_association(
    g: ig.Graph, *, gamma: float, n_runs: int, base_seed: int
) -> np.ndarray:
    """Count, for each pair, how many of ``n_runs`` runs co-clustered them.

    uint16 keeps an 5,000-node matrix at ~50 MB; the counts never exceed
    ``n_runs``.
    """
    n = g.vcount()
    counts = np.zeros((n, n), dtype=np.uint16)
    for s in range(n_runs):
        membership = np.asarray(leiden_once(g, gamma=gamma, seed=base_seed + s), dtype=np.int64)
        for community in np.unique(membership):
            idx = np.flatnonzero(membership == community)
            counts[np.ix_(idx, idx)] += 1
    return counts


def stability_scores(membership: Sequence[int], co_association: np.ndarray) -> list[float]:
    """Per-node mean co-association with the rest of its own final cluster.

    A singleton scores 1.0: there is nothing it could have been separated from, so
    the assignment is not in doubt even though it is uninformative.
    """
    membership = list(membership)
    n = len(membership)
    if n == 0:
        return []
    groups: dict[int, list[int]] = {}
    for i, c in enumerate(membership):
        groups.setdefault(c, []).append(i)

    out = [1.0] * n
    for members in groups.values():
        if len(members) == 1:
            continue
        idx = np.asarray(sorted(members))
        block = co_association[np.ix_(idx, idx)].astype(np.float64)
        # exclude the diagonal: a node always co-clusters with itself
        sums = block.sum(axis=1) - np.diag(block)
        means = sums / (len(idx) - 1)
        for pos, node in enumerate(idx.tolist()):
            out[node] = float(means[pos])
    return out


def leiden_consensus(
    g: ig.Graph,
    *,
    gamma: float = DEFAULT_GAMMA,
    n_runs: int = DEFAULT_N_RUNS,
    base_seed: int = DEFAULT_BASE_SEED,
    tau: float = DEFAULT_TAU,
    max_dense_nodes: int = DEFAULT_MAX_CONSENSUS_NODES,
    keep_matrix: bool = True,
) -> ConsensusPartition:
    """Consensus Leiden: ``n_runs`` seeds -> co-association -> threshold -> re-cluster.

    One re-clustering pass, not iterate-to-convergence: R3 measured one pass as
    sufficient and stable at this scale, and iterating adds complexity for nothing.
    """
    names = tuple(g.vs["name"]) if g.vcount() else ()
    if g.vcount() == 0:
        return ConsensusPartition(
            membership=(), stability=(), names=(), n_runs=n_runs, tau=tau, gamma=gamma,
            base_seed=base_seed, modularity=0.0, consensus_applied=False, co_association=None,
        )

    if g.vcount() > max_dense_nodes:
        membership = leiden_once(g, gamma=gamma, seed=base_seed)
        return ConsensusPartition(
            membership=tuple(membership),
            stability=tuple([1.0] * g.vcount()),
            names=names,
            n_runs=1,
            tau=tau,
            gamma=gamma,
            base_seed=base_seed,
            modularity=modularity(g, membership),
            consensus_applied=False,
            co_association=None,
        )

    counts = _co_association(g, gamma=gamma, n_runs=n_runs, base_seed=base_seed)

    # Threshold. With n_runs=25 no pair can land exactly on 0.5*25=12.5, so >= and
    # > agree; >= is used because it is what "at least tau of the runs" means.
    cutoff = tau * n_runs
    keep = counts >= cutoff
    np.fill_diagonal(keep, False)
    rows, cols = np.nonzero(np.triu(keep, 1))

    consensus_g = ig.Graph(n=g.vcount())
    consensus_g.vs["name"] = list(names)
    pairs = sorted(zip(rows.tolist(), cols.tolist(), strict=True))
    if pairs:
        consensus_g.add_edges(pairs)
        consensus_g.es["weight"] = [
            round(float(counts[a, b]) / n_runs, 12) for a, b in pairs
        ]

    membership = leiden_once(consensus_g, gamma=gamma, seed=base_seed)
    co = counts.astype(np.float64) / n_runs
    stability = stability_scores(membership, co)

    return ConsensusPartition(
        membership=tuple(membership),
        stability=tuple(stability),
        names=names,
        n_runs=n_runs,
        tau=tau,
        gamma=gamma,
        base_seed=base_seed,
        # modularity is reported against the ORIGINAL graph, not the consensus one
        modularity=modularity(g, membership),
        consensus_applied=True,
        co_association=co if keep_matrix else None,
    )


def select_gamma(
    g: ig.Graph,
    gammas: Sequence[float] = GAMMA_SWEEP,
    *,
    base_seed: int = DEFAULT_BASE_SEED,
    n_runs: int = DEFAULT_N_RUNS,
    tau: float = DEFAULT_TAU,
) -> float:
    """Pick the gamma whose consensus partition is most stable, tie-broken to 1.0.

    Never select on modularity — that just re-runs the degeneracy problem R3
    warned about. Mean co-association is the honest criterion.
    """
    best_gamma = DEFAULT_GAMMA
    best_score = -1.0
    for gamma in sorted(gammas):
        part = leiden_consensus(
            g, gamma=gamma, n_runs=n_runs, base_seed=base_seed, tau=tau, keep_matrix=False
        )
        score = float(np.mean(part.stability)) if part.stability else 0.0
        # strict > keeps the first (lowest) gamma on a tie; then prefer 1.0 exactly
        if score > best_score + 1e-12 or (
            abs(score - best_score) <= 1e-12 and gamma == DEFAULT_GAMMA
        ):
            best_score = score
            best_gamma = gamma
    return best_gamma


__all__ = [
    "DEFAULT_BASE_SEED",
    "DEFAULT_GAMMA",
    "DEFAULT_MAX_CONSENSUS_NODES",
    "DEFAULT_N_RUNS",
    "DEFAULT_TAU",
    "GAMMA_SWEEP",
    "HAVE_LEIDENALG",
    "ConsensusPartition",
    "leiden_consensus",
    "leiden_once",
    "modularity",
    "select_gamma",
    "stability_scores",
    "to_igraph",
]
