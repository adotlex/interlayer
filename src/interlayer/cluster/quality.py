"""Per-cluster cohesion and the whole-partition sanity gate.

Two different jobs live here. ``cohesion`` is a number attached to every emitted
cluster so the operator can see which groups are tight and which are a shrug.
``quality_warnings`` is the guard rail: clustering always *returns* something, so
without an explicit check a degenerate partition -- one blob containing everyone,
or dust -- looks exactly like a real answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import igraph as ig

__all__ = ["cohesion", "is_connected", "quality_warnings"]

GIANT_CLUSTER_SHARE = 0.25
"""A single cluster holding a quarter of the network is the projection pathology
Wave 1 measured (one employer produced 65% of all edges), not a community."""

SINGLETON_SHARE = 0.5
MIN_MEANINGFUL_MODULARITY = 0.3
"""Below ~0.3 modularity there is no community structure worth reporting."""


def cohesion(graph: ig.Graph, members: Sequence[int]) -> float:
    """Fraction of the cluster's incident edge weight that stays inside it.

    ``w_internal / (w_internal + w_cut)`` -- 1.0 for a cluster with no outside
    ties, 0.0 for one held together by nothing. It is the complement of the
    boundary share used by conductance, chosen over conductance itself only
    because the model field is named ``cohesion`` and must read "higher is
    better"; an isolated member set with no edges at all scores 0.0 rather than
    dividing by zero.
    """
    inside = set(members)
    internal = 0.0
    cut = 0.0
    for member in members:
        for eid in graph.incident(member):
            edge = graph.es[eid]
            weight = float(edge["weight"]) if "weight" in edge.attributes() else 1.0
            other = edge.target if edge.source == member else edge.source
            if other in inside:
                internal += weight  # counted once from each endpoint
            else:
                cut += weight
    internal /= 2.0
    total = internal + cut
    return round(internal / total, 6) if total > 0 else 0.0


def is_connected(graph: ig.Graph, members: Sequence[int]) -> bool:
    """Whether a cluster's members form one connected piece of the graph.

    Leiden guarantees this; Louvain does not, and Wave 1 cites up to 16% of
    Louvain communities being literally disconnected. A disconnected "group of
    people" shown to an operator is a correctness bug, not a quality nit, so the
    guarantee is checked rather than assumed.
    """
    if len(members) <= 1:
        return True
    return bool(graph.subgraph(list(members)).is_connected())


def quality_warnings(
    graph: ig.Graph,
    membership: Sequence[int],
    clusters: Mapping[str, Sequence[str]],
    *,
    n_nodes: int,
) -> list[str]:
    """Human-readable warnings for a partition that should not be trusted as-is.

    Returned rather than raised: a bad partition is still the honest answer to a
    bad graph, and the operator needs to be told, not stopped.
    """
    warnings: list[str] = []
    if n_nodes == 0:
        return ["the graph is empty: no edges were produced upstream, so nothing was clustered"]

    sizes = sorted((len(m) for m in clusters.values()), reverse=True)
    if sizes and sizes[0] > GIANT_CLUSTER_SHARE * n_nodes:
        warnings.append(
            f"largest cluster holds {sizes[0]} of {n_nodes} nodes "
            f"({sizes[0] / n_nodes:.0%}); a single dominant employer can produce this, "
            "so treat it as a projection artefact until checked"
        )
    singletons = sum(1 for size in _membership_sizes(membership) if size == 1)
    if singletons > SINGLETON_SHARE * n_nodes:
        warnings.append(
            f"{singletons} of {n_nodes} nodes ({singletons / n_nodes:.0%}) cluster alone; "
            "the graph is probably too sparse after backboning"
        )
    if graph.ecount() > 0:
        weights = graph.es["weight"] if "weight" in graph.es.attributes() else None
        modularity = float(graph.modularity(list(membership), weights=weights))
        if modularity < MIN_MEANINGFUL_MODULARITY:
            warnings.append(
                f"modularity {modularity:.3f} is below {MIN_MEANINGFUL_MODULARITY}; "
                "there may be no real community structure in this network"
            )
    return warnings


def _membership_sizes(membership: Sequence[int]) -> list[int]:
    counts: dict[int, int] = {}
    for community in membership:
        counts[community] = counts.get(community, 0) + 1
    return sorted(counts.values())
