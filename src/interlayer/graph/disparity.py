"""Serrano multiscale backbone, plus the top-k rescue that makes it survivable.

Weighting fixes *relative* importance but removes nothing, so the projection is
still far too dense to cluster. Backboning removes edges, and unlike a global
weight threshold it is **scale-aware**: it tests each edge against the local
weight distribution of its endpoints, so a weakly-connected person is not erased
merely for being weakly connected.

The research verified two properties that decided this choice:

* At alpha=0.05 on the 1,200-person projection the filter kept 97% of nodes while
  cutting 86% of edges, landing density at 0.011 -- inside the plausible range
  for a social graph (~0.01-0.05).
* On a graph with *uniformly random* weights it keeps **zero** edges, where the
  noise-corrected alternative keeps all 3,163. The disparity filter degrades
  toward empty, its competitor degrades toward a dense graph of noise. Silently
  clustering noise is the worse failure.

Its one bad behaviour is also measured: on a sparser 500-person fixture it cut
5,116 edges to 63 and left only 86 of 500 people connected. Hence
:func:`backbone` always unions the statistical backbone with each node's top-k
heaviest edges. The rescue is not a hedge -- without it this stage can hand the
clustering stage a graph in which five out of six people have vanished.
"""

from __future__ import annotations

from dataclasses import dataclass

import networkx as nx

__all__ = ["Backbone", "backbone", "disparity_alpha"]

Pair = tuple[str, str]


def disparity_alpha(weight: float, strength: float, degree: int) -> float:
    """p-value of one edge under the disparity null. Small = significant.

    Serrano, Boguna & Vespignani, *Extracting the multiscale backbone of complex
    weighted networks*, PNAS 106(16):6483-6488 (2009), eq. 2. The null says a
    node's strength is distributed uniformly at random over its k edges, i.e. the
    normalised weights are a uniform draw from the (k-1)-simplex, whose marginal
    density is ``(k-1)(1-x)^(k-2)``. The p-value is that density's upper tail,
    and the integral closes in one line:

        alpha_ij = 1 - (k_i - 1) * INTEGRAL_0^p_ij (1 - x)^(k_i - 2) dx
                 = (1 - p_ij)^(k_i - 1),      p_ij = w_ij / s_i

    Degree-1 nodes are *not testable*: their single edge carries the whole
    strength by construction, so the null has no variance and every
    implementation of this filter skips them. Returning 1.0 leaves them to the
    other endpoint's test, and failing that, to the rescue.
    """
    if degree <= 1 or strength <= 0.0:
        return 1.0
    p_ij = weight / strength
    if p_ij >= 1.0:
        return 0.0
    return (1.0 - p_ij) ** (degree - 1)


@dataclass(frozen=True)
class Backbone:
    """Edges kept, and how many were kept by each mechanism."""

    kept: tuple[Pair, ...]
    n_significant: int
    n_rescued: int
    n_pruned: int


def _canonical(u: str, v: str) -> Pair:
    return (u, v) if u < v else (v, u)


def backbone(graph: nx.Graph, *, alpha: float, rescue_top_k: int) -> Backbone:
    """Statistically significant edges, unioned with each node's top-k heaviest.

    An edge is kept when it is significant **from at least one endpoint**. That
    asymmetry is deliberate and is what protects low-strength nodes: an edge that
    is trivial to a hub can still be the defining relationship of the person on
    the other end.

    Iteration is over sorted nodes and sorted edges throughout. Set iteration
    order varies with ``PYTHONHASHSEED`` -- four values gave four distinct
    orders in the research -- and this function's output feeds a file that must
    be byte-identical between runs.
    """
    strength: dict[str, float] = {
        node: sum(data["weight"] for data in graph[node].values()) for node in graph
    }
    significant: set[Pair] = set()
    for u, v, weight in sorted(graph.edges(data="weight")):
        for endpoint in (u, v):
            if disparity_alpha(weight, strength[endpoint], graph.degree(endpoint)) < alpha:
                significant.add(_canonical(u, v))
                break

    rescued: set[Pair] = set()
    if rescue_top_k > 0:
        for node in sorted(graph.nodes()):
            neighbours = sorted(
                graph[node].items(), key=lambda item: (-float(item[1]["weight"]), item[0])
            )
            for neighbour, _data in neighbours[:rescue_top_k]:
                rescued.add(_canonical(node, neighbour))

    kept = significant | rescued
    return Backbone(
        kept=tuple(sorted(kept)),
        n_significant=len(significant),
        n_rescued=len(rescued - significant),
        n_pruned=graph.number_of_edges() - len(kept),
    )
