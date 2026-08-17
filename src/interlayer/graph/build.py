"""Step 4-6: edge selection, bridge restriction, bipartite graph construction.

Pure and offline. Imports only ``core.models`` / ``core.ids`` plus networkx.

Three rules from R3 are load-bearing here and every one of them is a bug fix,
not a preference:

* **Unharvested targets contribute no edges** (R3 §4.1). An edge row pointing at
  a target whose mutual-connections list was never opened is not evidence; it is
  an artefact. Absence of an edge means "no edge" only when ``harvested`` is True.
* **Nodes are inserted in sorted ID order** (R3 §2.4). Leiden's tie-breaking reads
  vertex index order, and 12 vertex permutations of one graph at ``seed=42``
  produced 12 distinct partitions. Sorting is the determinism contract, not tidiness.
* **Never call ``networkx.algorithms.bipartite.sets()``** — it raises
  ``AmbiguousSolution`` on disconnected graphs, and a bridge graph is always
  disconnected. The explicit member-node set is carried on the result instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import networkx as nx

from interlayer.core.ids import edge_key
from interlayer.core.models import Edge, EdgeOrigin, Member, Target

#: Node attribute values, matching the networkx bipartite convention.
MEMBER_SIDE = 0
TARGET_SIDE = 1


@dataclass(frozen=True, slots=True)
class BipartiteGraph:
    """A built bipartite graph plus the bookkeeping the rest of the pipeline needs.

    ``member_ids`` holds bridges only (``d_m > 0``), sorted. ``target_ids`` holds
    every *harvested* target, sorted — including harvested targets with zero
    mutuals, which are legal and must reach the projection as a zero column so the
    ``max(d_t, 1)`` guard is genuinely exercised (T8.6).
    """

    graph: nx.Graph
    member_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    edges: tuple[Edge, ...]
    used_inferred_edges: bool
    n_members_total: int
    n_targets_total: int
    n_targets_harvested: int
    n_edges_dropped_unharvested: int
    n_edges_dropped_duplicate: int
    n_edges_dropped_confidence: int
    n_edges_dropped_inferred: int
    n_edges_dropped_unknown: int

    @property
    def member_set(self) -> set[str]:
        """The explicit member-node set to pass wherever networkx wants one.

        This exists so that no caller is ever tempted to reach for
        ``bipartite.sets()``.
        """
        return set(self.member_ids)

    def target_degrees(self) -> dict[str, int]:
        """``d_t`` over harvested targets only (R3 §4.1)."""
        return {t: int(self.graph.degree(t)) for t in self.target_ids}

    def member_degrees(self) -> dict[str, int]:
        """``d_m`` — the member's *reach*. A lower bound, always (R3 §4.1)."""
        return {m: int(self.graph.degree(m)) for m in self.member_ids}

    def neighbours(self, member_id: str) -> tuple[str, ...]:
        return tuple(sorted(self.graph.neighbors(member_id)))


def select_edges(
    edges: Iterable[Edge],
    targets: Iterable[Target],
    members: Iterable[Member] | None = None,
    *,
    include_inferred: bool = False,
    min_confidence: float = 0.5,
) -> tuple[tuple[Edge, ...], dict[str, int]]:
    """Filter and deduplicate the raw edge list. Returns ``(edges, drop_counts)``.

    Order of operations matters for the counters to be interpretable, so it is
    fixed: unknown endpoints, then inferred gating, then confidence, then the
    unharvested-target rule, then dedupe.

    ``min_confidence`` gates *inferred* edges only. An observed edge is a thing the
    user actually saw; the contract already fixes its confidence at 1.0, and
    silently dropping an observation because a field was mis-populated would be a
    worse failure than keeping it.
    """
    harvested: set[str] = set()
    known_targets: set[str] = set()
    for t in targets:
        known_targets.add(t.target_id)
        if t.harvested:
            harvested.add(t.target_id)

    known_members: set[str] | None = None
    if members is not None:
        known_members = {m.member_id for m in members}

    counts = {
        "unknown": 0,
        "inferred": 0,
        "confidence": 0,
        "unharvested": 0,
        "duplicate": 0,
    }

    kept: list[Edge] = []
    for e in edges:
        if e.target_id not in known_targets:
            counts["unknown"] += 1
            continue
        if known_members is not None and e.member_id not in known_members:
            counts["unknown"] += 1
            continue
        if e.origin is EdgeOrigin.INFERRED:
            if not include_inferred:
                counts["inferred"] += 1
                continue
            if e.confidence < min_confidence:
                counts["confidence"] += 1
                continue
        if e.target_id not in harvested:
            counts["unharvested"] += 1
            continue
        kept.append(e)

    # T8.5 — deduplicate. Sort so the survivor of a duplicate group is chosen
    # deterministically: observed beats inferred, then higher confidence wins.
    kept.sort(
        key=lambda e: (e.member_id, e.target_id, e.origin is EdgeOrigin.INFERRED, -e.confidence)
    )
    deduped: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for e in kept:
        key = edge_key(e.member_id, e.target_id)
        if key in seen:
            counts["duplicate"] += 1
            continue
        seen.add(key)
        deduped.append(e)

    return tuple(deduped), counts


def build_bipartite(
    members: Sequence[Member],
    targets: Sequence[Target],
    edges: Sequence[Edge],
    *,
    include_inferred: bool = False,
    min_confidence: float = 0.5,
) -> BipartiteGraph:
    """Build the bipartite graph over bridges x harvested targets.

    Members with ``d_m == 0`` are dropped before any graph work (R3 step 5):
    typically ``|B| << |M|``, since most of a 500-10,000 connection export touches
    no target at all.
    """
    selected, counts = select_edges(
        edges,
        targets,
        members,
        include_inferred=include_inferred,
        min_confidence=min_confidence,
    )

    harvested_ids = sorted({t.target_id for t in targets if t.harvested})
    bridge_ids = sorted({e.member_id for e in selected})

    graph = nx.Graph()
    # Sorted insertion, members first. Both halves individually sorted, which is
    # what the igraph conversion and Leiden's tie-breaking depend on.
    for m in bridge_ids:
        graph.add_node(m, bipartite=MEMBER_SIDE)
    for t in harvested_ids:
        graph.add_node(t, bipartite=TARGET_SIDE)
    for e in sorted(selected, key=lambda x: (x.member_id, x.target_id)):
        graph.add_edge(
            e.member_id,
            e.target_id,
            confidence=float(e.confidence),
            observed=e.observed,
        )

    used_inferred = any(e.origin is EdgeOrigin.INFERRED for e in selected)

    return BipartiteGraph(
        graph=graph,
        member_ids=tuple(bridge_ids),
        target_ids=tuple(harvested_ids),
        edges=selected,
        used_inferred_edges=used_inferred,
        n_members_total=len(members),
        n_targets_total=len(targets),
        n_targets_harvested=len(harvested_ids),
        n_edges_dropped_unharvested=counts["unharvested"],
        n_edges_dropped_duplicate=counts["duplicate"],
        n_edges_dropped_confidence=counts["confidence"],
        n_edges_dropped_inferred=counts["inferred"],
        n_edges_dropped_unknown=counts["unknown"],
    )


def bridge_members(graph: nx.Graph, member_ids: Sequence[str]) -> list[str]:
    """Sorted members with ``d_m > 0``.

    Takes the member-node set explicitly. This is the function that exists so
    nobody calls ``bipartite.sets()`` (T8.4).
    """
    return sorted(m for m in member_ids if m in graph and graph.degree(m) > 0)


__all__ = [
    "MEMBER_SIDE",
    "TARGET_SIDE",
    "BipartiteGraph",
    "bridge_members",
    "build_bipartite",
    "select_edges",
]
