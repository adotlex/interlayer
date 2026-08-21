"""Sparse consensus Leiden -- the reason this tool's groups do not reshuffle.

Wave 1 measured the failure this module exists to prevent. At n=3,000, twenty
differently-seeded Leiden runs agreed with one another at a mean pairwise ARI of
only 0.877 (min 0.794) and returned anywhere between 66 and 70 clusters. Seeding
alone buys *reproducibility*, not *stability*: it freezes one arbitrary local
optimum, and one extra connection in the input can jump the partition somewhere
else entirely. An operator re-running the tool would be shown different groups
with no way to tell which run to believe, which disqualifies output a person is
meant to act on.

Lancichinetti & Fortunato's consensus clustering (Sci Rep 2:336, 2012) fixes it:
run the base algorithm many times, record how often each pair of nodes landed
together, discard the pairs that failed to survive a majority, and re-cluster
what is left, repeating until the co-association matrix is block-structured.
Wave 1 measured cross-ensemble ARI rising to 1.000 / 0.997 / 0.921 at LFR
mu = 0.2 / 0.3 / 0.4, while accuracy *also* improved (NMI 0.719 -> 0.756 at
mu=0.4). Consensus both stabilises and denoises, for well under fifteen seconds
at our scale, so it is the default rather than an option.

The co-association matrix is kept sparse -- a ``Counter`` over index pairs --
rather than an n x n array. Dense costs 36 MB at n=3,000 and would need roughly
10 GB at n=50,000, and this project deliberately carries no numpy/scipy
dependency to lean on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

import igraph as ig
import leidenalg as la

__all__ = [
    "ConsensusResult",
    "build_igraph",
    "consensus_membership",
    "leiden_membership",
]

DEFAULT_MAX_ITER = 10
"""Lancichinetti-Fortunato iterate to convergence; Wave 1 saw 2-3 in practice."""


def build_igraph(n_nodes: int, edges: Sequence[tuple[int, int, float]]) -> ig.Graph:
    """Assemble a weighted undirected igraph from index triples.

    Edges are consumed in the order given and never re-sorted here: callers pass
    a deterministically ordered sequence, and igraph preserves insertion order,
    so two runs over the same input build a byte-identical graph. That matters
    because leidenalg's tie-breaking depends on edge ordering.
    """
    graph = ig.Graph(n=n_nodes, edges=[(u, v) for u, v, _ in edges], directed=False)
    graph.es["weight"] = [w for _, _, w in edges]
    return graph


def leiden_membership(graph: ig.Graph, *, resolution: float, seed: int) -> list[int]:
    """One seeded Leiden partition of ``graph``.

    Always ``leidenalg.find_partition``, never ``igraph.Graph.community_leiden``:
    the latter takes no ``seed`` argument, draws from igraph's global RNG, and was
    observed in Wave 1 to *diverge across repeated calls* at mu=0.4 even with that
    global RNG seeded once. ``find_partition(seed=...)`` was byte-identical over
    ten repeats at every mixing level tested.

    ``RBConfigurationVertexPartition`` is modularity with a resolution knob, so
    ``resolution`` behaves intuitively (larger gamma, smaller clusters).
    """
    if graph.vcount() == 0:
        return []
    if graph.ecount() == 0:
        # find_partition on an edgeless graph is a no-op; skip the C call so the
        # fully-disconnected input stays a cheap, obviously-correct path.
        return list(range(graph.vcount()))
    weights = graph.es["weight"] if "weight" in graph.es.attributes() else None
    partition = la.find_partition(
        graph,
        la.RBConfigurationVertexPartition,
        weights=weights,
        resolution_parameter=resolution,
        seed=seed,
    )
    return list(partition.membership)


def _coassociation(
    graph: ig.Graph, *, runs: int, resolution: float, seed: int
) -> Counter[tuple[int, int]]:
    """Count, over ``runs`` seeded partitions, how often each pair co-occurs.

    Every run's seed is derived as ``seed + r`` so the whole ensemble is a pure
    function of the caller's single seed -- no clock, no global RNG, no os.urandom.
    """
    co: Counter[tuple[int, int]] = Counter()
    for run in range(runs):
        membership = leiden_membership(graph, resolution=resolution, seed=seed + run)
        groups: dict[int, list[int]] = {}
        for node, community in enumerate(membership):
            groups.setdefault(community, []).append(node)
        for members in groups.values():
            if len(members) > 1:
                # members is ascending by construction, so combinations yields
                # canonical (lo, hi) pairs without a sort.
                co.update(combinations(members, 2))
    return co


@dataclass(frozen=True)
class ConsensusResult:
    """A consensus partition plus the diagnostics needed to trust it."""

    membership: list[int]
    iterations: int
    converged: bool
    """True when the co-association matrix reached exact block structure."""

    @property
    def n_clusters(self) -> int:
        return len(set(self.membership))


def consensus_membership(
    graph: ig.Graph,
    *,
    runs: int,
    threshold: float,
    resolution: float,
    seed: int,
    max_iter: int = DEFAULT_MAX_ITER,
) -> ConsensusResult:
    """Lancichinetti-Fortunato consensus clustering over seeded Leiden runs.

    ``runs`` partitions vote on every pair; pairs supported by fewer than
    ``threshold`` of the runs are dropped; the surviving pairs form a weighted
    consensus graph which is clustered again, until the matrix is block-structured
    (every surviving pair agreed unanimously) and the answer stops moving.

    Returns the membership over the *original* node indices; the node set is
    preserved across iterations so index i always means the same person.
    """
    n = graph.vcount()
    if n == 0:
        return ConsensusResult(membership=[], iterations=0, converged=True)
    if runs < 1:
        raise ValueError("runs must be >= 1")

    min_votes = threshold * runs
    current = graph
    surviving: list[tuple[int, int, float]] = []
    converged = False
    iterations = 0

    for attempt in range(1, max_iter + 1):
        iterations = attempt
        co = _coassociation(current, runs=runs, resolution=resolution, seed=seed)
        # Sorted, so the next iteration's graph -- and therefore leidenalg's
        # tie-breaking -- does not inherit Counter insertion order.
        surviving = sorted(
            (u, v, count / runs) for (u, v), count in co.items() if count >= min_votes
        )
        if not surviving:
            converged = True  # nothing survived a majority: every node stands alone
            break
        if all(count == runs for count in co.values() if count >= min_votes):
            converged = True  # unanimous on every surviving pair == block structure
            break
        current = build_igraph(n, surviving)

    if converged:
        # Block-structured: connected components ARE the consensus clusters, and
        # taking them avoids adding one more stochastic step at the very end.
        components = ig.Graph(
            n=n, edges=[(u, v) for u, v, _ in surviving], directed=False
        ).connected_components()
        return ConsensusResult(
            membership=list(components.membership), iterations=iterations, converged=True
        )

    # Pathological input that never settled. Connected components on a matrix that
    # is still chained would fuse everything into one blob, so cluster the final
    # consensus graph instead and say so, rather than returning a silent giant.
    final = build_igraph(n, surviving)
    return ConsensusResult(
        membership=leiden_membership(final, resolution=resolution, seed=seed),
        iterations=iterations,
        converged=False,
    )
