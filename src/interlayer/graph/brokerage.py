"""Step 10: the five structural metrics, computed on the right graph each time.

Which graph a metric comes from is not interchangeable:

======================  ===================  =======================================
metric                  graph                why
======================  ===================  =======================================
``reach`` ``d_m``       bipartite ``B``      "knows N people at the firms"
``rarity``              bipartite ``B``      ``sum 1/d_t`` — knows people few others know
``betweenness``         projection, igraph   sits between otherwise separate pockets
``effective_size``      projection, networkx Burt non-redundant contacts
``constraint``          projection, networkx how boxed-in; inverted to ``autonomy``
======================  ===================  =======================================

``rarity`` is the node-level analogue of RA weighting and the most
decision-relevant metric after reach: knowing one obscure Citadel Securities
developer is a better introduction path than knowing the same well-connected
recruiter as 200 other people.

Two hard rules. **Betweenness comes from igraph** — networkx's exact version is
~40x slower (70.7s vs 1.8s at n=2,000) and its k-sample approximation is both
slower *and* less accurate at n=8,000. **NaN must be coalesced** — networkx
returns ``nan`` from ``effective_size`` and ``constraint`` for degree-0 nodes in
the projection, which is a real state (a member whose targets are all private to
them), and one un-coalesced NaN turns the whole score column into NaN.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import igraph as ig
import networkx as nx

from interlayer.graph.build import BipartiteGraph
from interlayer.graph.project import Projection

#: NaN coalescing targets (R3 §3.2). ``constraint`` goes to 1.0 — maximum
#: redundancy — because an isolate has no structural hole to exploit, not because
#: it has an infinite one.
NAN_EFFECTIVE_SIZE = 0.0
NAN_CONSTRAINT = 1.0


@dataclass(frozen=True, slots=True)
class RawMetrics:
    """The five components before percentile ranking."""

    member_id: str
    reach: int
    rarity: float
    betweenness: float
    effective_size: float
    constraint: float
    autonomy: float

    def as_components(self) -> dict[str, float]:
        return {
            "reach": float(self.reach),
            "rarity": self.rarity,
            "betweenness": self.betweenness,
            "effective_size": self.effective_size,
            "autonomy": self.autonomy,
        }


def reach_and_rarity(bg: BipartiteGraph) -> tuple[dict[str, int], dict[str, float]]:
    """``d_m`` and ``sum_{t in N(m)} 1/d_t``, both from the unweighted bipartite graph.

    ``d_t`` is computed over harvested targets only. That is what makes T5.1 hold:
    adding an unharvested target must not move anybody's rarity, because it was
    never observed and its absence from an edge list is not evidence.
    """
    target_degrees = bg.target_degrees()
    reach: dict[str, int] = {}
    rarity: dict[str, float] = {}
    for m in bg.member_ids:
        neighbours = bg.neighbours(m)
        reach[m] = len(neighbours)
        rarity[m] = sum(1.0 / max(target_degrees.get(t, 0), 1) for t in neighbours)
    return reach, rarity


def normalised_betweenness(g: ig.Graph) -> dict[str, float]:
    """igraph betweenness, scaled to [0, 1] by ``2 / ((n-1)(n-2))``.

    Unweighted on purpose. igraph reads edge weights as *distances*, so passing
    the projection weights would invert their meaning — a strong tie would become
    a long path. The raw counts are what R3 published (m0 == 21.0 on the golden
    fixture, which is 0.75 normalised).
    """
    n = g.vcount()
    if n == 0:
        return {}
    names = list(g.vs["name"])
    if n < 3 or g.ecount() == 0:
        return dict.fromkeys(names, 0.0)
    raw = g.betweenness()
    scale = 2.0 / ((n - 1) * (n - 2))
    return {name: float(value) * scale for name, value in zip(names, raw, strict=True)}


def projection_to_networkx(projection: Projection) -> nx.Graph:
    """Sorted-order networkx view of the projection, for Burt's measures.

    networkx has no igraph equivalent for ``effective_size``/``constraint`` and
    their cost is acceptable (1.2s / 0.5s at 2,000 nodes).
    """
    graph = nx.Graph()
    for name in sorted(projection.member_ids):
        graph.add_node(name)

    coo = projection.matrix.tocoo()
    names = projection.member_ids
    edges = sorted(
        (min(int(i), int(j)), max(int(i), int(j)), float(w))
        for i, j, w in zip(coo.row, coo.col, coo.data, strict=True)
        if i != j
    )
    for i, j, w in edges:
        graph.add_edge(names[i], names[j], weight=w)
    return graph


def burt_measures(graph: nx.Graph) -> tuple[dict[str, float], dict[str, float]]:
    """Burt effective size and constraint, NaN-coalesced.

    Both are computed unweighted (``weight=None``), which is what produced R3's
    published values: on the golden fixture m0 has 8 neighbours with 7 ties among
    them, so ``e = 8 - 2*7/8 = 6.25``.
    """
    if graph.number_of_nodes() == 0:
        return {}, {}

    raw_size = nx.effective_size(graph)
    raw_constraint = nx.constraint(graph)

    effective_size = {
        node: (NAN_EFFECTIVE_SIZE if value is None or math.isnan(value) else float(value))
        for node, value in raw_size.items()
    }
    constraint = {
        node: (NAN_CONSTRAINT if value is None or math.isnan(value) else float(value))
        for node, value in raw_constraint.items()
    }
    return effective_size, constraint


def autonomy_from_constraint(constraint: float) -> float:
    """``1 - min(constraint, 1)``. High is good; a NaN-coalesced isolate gets 0.0."""
    return 1.0 - min(constraint, 1.0)


def brokerage_metrics(
    bg: BipartiteGraph,
    projection: Projection,
    g: ig.Graph,
) -> dict[str, RawMetrics]:
    """All five components for every bridge, keyed by ``member_id``."""
    reach, rarity = reach_and_rarity(bg)
    betweenness = normalised_betweenness(g)
    nx_projection = projection_to_networkx(projection)
    effective_size, constraint = burt_measures(nx_projection)

    out: dict[str, RawMetrics] = {}
    for m in bg.member_ids:
        c = constraint.get(m, NAN_CONSTRAINT)
        out[m] = RawMetrics(
            member_id=m,
            reach=reach.get(m, 0),
            rarity=rarity.get(m, 0.0),
            betweenness=betweenness.get(m, 0.0),
            effective_size=effective_size.get(m, NAN_EFFECTIVE_SIZE),
            constraint=c,
            autonomy=autonomy_from_constraint(c),
        )
    return out


__all__ = [
    "NAN_CONSTRAINT",
    "NAN_EFFECTIVE_SIZE",
    "RawMetrics",
    "autonomy_from_constraint",
    "brokerage_metrics",
    "burt_measures",
    "normalised_betweenness",
    "projection_to_networkx",
    "reach_and_rarity",
]
