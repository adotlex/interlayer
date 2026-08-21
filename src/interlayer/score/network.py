"""The walkable network: personalized PageRank and hop distance, in pure Python.

Implemented here rather than called from ``networkx`` for two reasons. The first
is practical: ``nx.pagerank`` in NetworkX 3.6 dispatches to a numpy/scipy backend,
and neither is a dependency of this project. The second is the point of the whole
tool -- summation order decides the last bits of every float, so owning the loop
is what lets the same seed and the same input produce a byte-identical
``scored_people.jsonl``. Every iteration walks nodes in index order over
adjacency lists that were sorted once at construction.

**The alpha convention is a footgun and is worth stating twice.** ``alpha`` here
is the *damping* (continuation) probability, matching ``networkx`` and the
original PageRank paper, so the restart probability is ``1 - alpha``. Much of the
random-walk-with-restart literature defines its constant the other way round.
Wave 1 measured the consequence: at alpha=0.5, 56% of all score mass stays on the
seed nodes and the walk never tells you anything about anyone else; at the
configured 0.85 it leaves ~77% of the mass on non-seeds while staying local
enough to remain personalized.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = ["Network", "personalized_pagerank"]

MAX_ITER = 250
TOLERANCE = 1e-12
"""Tighter than networkx's 1e-6 default: power iteration is cheap at this scale
and converging to near machine precision keeps re-runs bit-stable."""


@dataclass(frozen=True)
class Network:
    """An undirected weighted graph addressed by person id.

    ``node_ids`` is the sorted index; ``adjacency[i]`` is a sorted list of
    ``(neighbour_index, weight)``. Both orderings are fixed at construction so
    that nothing downstream depends on set iteration order, which Wave 1 found
    varied across four distinct ``PYTHONHASHSEED`` values.
    """

    node_ids: tuple[str, ...]
    index: Mapping[str, int]
    adjacency: tuple[tuple[tuple[int, float], ...], ...]
    strength: tuple[float, ...]

    @property
    def size(self) -> int:
        return len(self.node_ids)

    @classmethod
    def from_edges(cls, edges: Iterable[tuple[str, str, float]]) -> Network:
        """Build from ``(source, target, weight)`` triples, summing any duplicates."""
        pairs: dict[tuple[str, str], float] = {}
        for source, target, weight in edges:
            if source == target:
                continue  # GraphEdge forbids self-loops; a walk on one is meaningless
            key = (source, target) if source < target else (target, source)
            pairs[key] = pairs.get(key, 0.0) + float(weight)
        node_ids = tuple(sorted({node for pair in pairs for node in pair}))
        index = {node: i for i, node in enumerate(node_ids)}
        buckets: list[list[tuple[int, float]]] = [[] for _ in node_ids]
        for (source, target), weight in sorted(pairs.items()):
            u, v = index[source], index[target]
            buckets[u].append((v, weight))
            buckets[v].append((u, weight))
        adjacency = tuple(tuple(sorted(bucket)) for bucket in buckets)
        strength = tuple(sum(w for _, w in bucket) for bucket in adjacency)
        return cls(node_ids=node_ids, index=index, adjacency=adjacency, strength=strength)

    def with_nodes(self, extra: Iterable[str]) -> Network:
        """Return a network that also contains ``extra`` as isolated nodes.

        People with no edges must still be rankable -- a connection whose export
        row carried no employer is invisible to the graph but is not invisible to
        the operator, and silently dropping them would misrepresent the network.
        """
        additions = sorted(set(extra) - set(self.node_ids))
        if not additions:
            return self
        node_ids = tuple(sorted([*self.node_ids, *additions]))
        index = {node: i for i, node in enumerate(node_ids)}
        buckets: list[tuple[tuple[int, float], ...]] = []
        for node in node_ids:
            old = self.index.get(node)
            if old is None:
                buckets.append(())
            else:
                buckets.append(
                    tuple(sorted((index[self.node_ids[j]], w) for j, w in self.adjacency[old]))
                )
        return Network(
            node_ids=node_ids,
            index=index,
            adjacency=tuple(buckets),
            strength=tuple(sum(w for _, w in bucket) for bucket in buckets),
        )

    def neighbours(self, node: str) -> tuple[tuple[int, float], ...]:
        position = self.index.get(node)
        return () if position is None else self.adjacency[position]

    def hops_from(self, seeds: Sequence[int]) -> list[int | None]:
        """Breadth-first hop count to the nearest seed; ``None`` when unreachable.

        Unweighted on purpose: "how many introductions away" is a count of people,
        not a sum of edge strengths.
        """
        unreached = -1
        distance = [unreached] * self.size
        queue: deque[int] = deque()
        for seed in sorted(set(seeds)):
            distance[seed] = 0
            queue.append(seed)
        while queue:
            node = queue.popleft()
            for neighbour, _ in self.adjacency[node]:
                if distance[neighbour] == unreached:
                    distance[neighbour] = distance[node] + 1
                    queue.append(neighbour)
        return [None if d == unreached else d for d in distance]


def personalized_pagerank(
    network: Network,
    seeds: Sequence[int],
    *,
    alpha: float,
    max_iter: int = MAX_ITER,
    tol: float = TOLERANCE,
) -> list[float]:
    """Stationary distribution of a random walk restarting on ``seeds``.

    ``alpha`` is the damping probability (restart is ``1 - alpha``). Mass sitting
    on a node with no edges would otherwise vanish each iteration, so it is
    returned to the restart distribution rather than leaked -- without that the
    vector stops summing to one and the scores become incomparable between runs
    with different numbers of isolated people.

    Returns a vector aligned with ``network.node_ids``; all zeros when there are
    no seeds, which is the honest answer to "how close is everyone to a target
    firm" when no target-firm person is known.
    """
    n = network.size
    unique_seeds = sorted(set(seeds))
    if n == 0 or not unique_seeds:
        return [0.0] * n

    restart = [0.0] * n
    share = 1.0 / len(unique_seeds)
    for seed in unique_seeds:
        restart[seed] = share

    current = list(restart)
    for _ in range(max_iter):
        nxt = [0.0] * n
        dangling = 0.0
        for node in range(n):
            mass = current[node]
            if mass == 0.0:
                continue
            strength = network.strength[node]
            if strength <= 0.0:
                dangling += mass
                continue
            spread = alpha * mass / strength
            for neighbour, weight in network.adjacency[node]:
                nxt[neighbour] += spread * weight
        leak = (1.0 - alpha) + alpha * dangling
        for node in range(n):
            nxt[node] += leak * restart[node]
        delta = sum(abs(nxt[node] - current[node]) for node in range(n))
        current = nxt
        if delta < tol * n:
            break

    total = sum(current)
    return [value / total for value in current] if total > 0 else current
