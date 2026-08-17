"""Step 14: which target to harvest next.

Quota, not time, is the binding constraint — the monthly Commercial Use Limit caps
every acquisition mode at roughly the same 150-250 targets/month — so the order in
which targets are harvested is one of the few things that actually moves coverage.

A pure uncertainty criterion is unavailable: ``P(edge)`` cannot be estimated
before looking. So this is a representativeness + novelty proxy that needs no
model::

    priority(t) = 1.0 * n_known_bridges_touching(t)     # yield
                + 1.0 * n_distinct_clusters_touched(t)  # spanning
                + 0.5 * sum_{c in clusters(t)} 1/(1+seen_c)   # novelty

``n_known_bridges_touching`` uses whatever partial signal exists — an inferred
edge, or an observed edge naming a target that has not yet been harvested. Ties
break by ``target_id`` ascending, which keeps the ordering total and the output
reproducible (T5.5).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from interlayer.core.models import Edge, NextTarget, Target

ALPHA_KNOWN_BRIDGES = 1.0
BETA_CLUSTERS_TOUCHED = 1.0
GAMMA_NOVELTY = 0.5
DEFAULT_TOP = 20


def _cluster_exposure(
    harvested_edges: Iterable[Edge],
    membership: Mapping[str, int],
) -> dict[int, int]:
    """How many harvested targets already touch each cluster.

    The novelty denominator. A cluster nobody has sampled scores 1/(1+0) = 1.0; a
    cluster already covered by ten harvested targets scores 1/11.
    """
    seen: dict[int, set[str]] = {}
    for edge in harvested_edges:
        cluster = membership.get(edge.member_id)
        if cluster is None:
            continue
        seen.setdefault(cluster, set()).add(edge.target_id)
    return {cluster: len(targets) for cluster, targets in seen.items()}


def next_targets(
    targets: Sequence[Target],
    signal_edges: Iterable[Edge],
    harvested_edges: Iterable[Edge],
    membership: Mapping[str, int],
    *,
    alpha: float = ALPHA_KNOWN_BRIDGES,
    beta: float = BETA_CLUSTERS_TOUCHED,
    gamma: float = GAMMA_NOVELTY,
    top: int = DEFAULT_TOP,
) -> tuple[NextTarget, ...]:
    """Rank unharvested targets. Returns at most ``top``, best first.

    ``signal_edges`` are edges naming *unharvested* targets — the partial evidence
    that a harvest would pay off. ``harvested_edges`` are the edges already in the
    graph, used only for the novelty denominator.
    """
    unharvested = sorted({t.target_id for t in targets if not t.harvested})
    if not unharvested:
        return ()

    exposure = _cluster_exposure(harvested_edges, membership)

    bridges_by_target: dict[str, set[str]] = {t: set() for t in unharvested}
    clusters_by_target: dict[str, set[int]] = {t: set() for t in unharvested}
    for edge in signal_edges:
        if edge.target_id not in bridges_by_target:
            continue
        if edge.member_id not in membership:
            # only *known bridges* count as yield; an unknown member is no signal
            continue
        bridges_by_target[edge.target_id].add(edge.member_id)
        clusters_by_target[edge.target_id].add(membership[edge.member_id])

    scored: list[NextTarget] = []
    for target_id in unharvested:
        n_bridges = len(bridges_by_target[target_id])
        clusters = clusters_by_target[target_id]
        novelty = sum(1.0 / (1.0 + exposure.get(c, 0)) for c in sorted(clusters))
        priority = alpha * n_bridges + beta * len(clusters) + gamma * novelty
        scored.append(
            NextTarget(
                target_id=target_id,
                priority=round(priority, 12),
                n_known_bridges=n_bridges,
                n_clusters_touched=len(clusters),
                novelty=round(novelty, 12),
            )
        )

    scored.sort(key=lambda nt: (-nt.priority, nt.target_id))
    return tuple(scored[:top])


__all__ = [
    "ALPHA_KNOWN_BRIDGES",
    "BETA_CLUSTERS_TOUCHED",
    "DEFAULT_TOP",
    "GAMMA_NOVELTY",
    "next_targets",
]
