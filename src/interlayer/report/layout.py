"""Graph layout, computed in Python and shipped as coordinates.

Two decisions from ``docs/research/03-project-stack.md`` §9 drive this module:

* **igraph's ``fr``, not networkx's ``spring_layout``** — 0.28 s against 15.5 s
  at 2 800 nodes, and deterministic under a seeded RNG. Measured, not assumed.
* **No force simulation in the browser.** The page ships final coordinates, so
  opening the report costs one paint rather than several seconds of CPU, and two
  runs of the same input produce the same picture rather than two settlings of
  the same simulation.

The rendered target is inline SVG rather than the prototype's ``<canvas>``. The
prototype's model is otherwise kept intact — server-side ``fr`` layout,
coordinates normalised to ``[0,1]``, cluster-indexed palette, radius from degree
— but a canvas needs JavaScript, and the report's mandated
``default-src 'none'`` Content-Security-Policy forbids script entirely. Inline
SVG is markup, not a fetched sub-resource, so it renders under that policy with
no directive relaxed at all. See ``templates/report.html.j2`` for the CSS-only
interaction that replaces the prototype's mouse handlers.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import igraph as ig

from interlayer.models import GraphEdge, Provenance

__all__ = ["VIEW", "GraphLayout", "LaidOutNode", "build_layout", "is_observed_edge"]

#: SVG user-space extent. Coordinates are emitted as integers in this space
#: rather than as decimals in a smaller one: 2 000 steps is finer than a pixel
#: at any zoom the panel offers, and dropping the decimal point saves roughly a
#: character per coordinate, which at four coordinates per edge is tens of
#: kilobytes on a large graph.
VIEW = 2000.0
_PAD = 36.0

#: Beyond this the picture is mush and the file is large for no benefit; the
#: lowest-ranked nodes are dropped and the omission is stated on the page.
MAX_GRAPH_NODES = 4000

_MIN_R = 3.0
_MAX_R = 18.0


@dataclass(frozen=True)
class LaidOutNode:
    """One placed node. ``cluster`` is a palette index, not a cluster id."""

    person_id: str
    x: int
    y: int
    r: int
    cluster: int
    rank: int | None
    observed: bool


@dataclass(frozen=True)
class GraphLayout:
    nodes: tuple[LaidOutNode, ...]
    inferred_d: str
    observed_d: str
    n_nodes: int
    n_edges: int
    n_observed_edges: int
    omitted_nodes: int


def is_observed_edge(edge: GraphEdge) -> bool:
    """Whether an edge is a human-read bridge rather than an inference.

    Reads the provenance the graph stage recorded. The fallback on the absence of
    an inferential basis covers edges written before that field existed, and is
    deliberately one-sided: anything ambiguous reads as inferred, because
    mislabelling an inference as observed is the failure that matters.
    """
    if edge.provenance is Provenance.OBSERVED:
        return True
    return not edge.shared_org_ids and not edge.kinds


def _normalise(values: Sequence[float]) -> list[float]:
    """Map to ``[_PAD, VIEW - _PAD]``, tolerating a zero-width range."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 1e-9:
        return [VIEW / 2.0 for _ in values]
    scale = (VIEW - 2 * _PAD) / span
    return [_PAD + (v - lo) * scale for v in values]


def build_layout(
    edges: Sequence[GraphEdge],
    *,
    seed: int,
    node_ids: Sequence[str],
    degrees: Mapping[str, int],
    cluster_index: Mapping[str, int],
    ranks: Mapping[str, int],
    observed_people: frozenset[str],
    priority: Mapping[str, float],
) -> GraphLayout:
    """Place ``node_ids`` with a seeded Fruchterman-Reingold layout.

    ``node_ids`` must already be sorted; ``priority`` decides who survives the
    :data:`MAX_GRAPH_NODES` cap (higher stays). Every collection is sorted before
    it reaches igraph so the layout does not depend on ``PYTHONHASHSEED``.
    """
    kept = list(node_ids)
    omitted = 0
    if len(kept) > MAX_GRAPH_NODES:
        kept = sorted(
            sorted(kept), key=lambda pid: (-priority.get(pid, 0.0), -degrees.get(pid, 0), pid)
        )[:MAX_GRAPH_NODES]
        kept.sort()
        omitted = len(node_ids) - len(kept)

    index = {pid: i for i, pid in enumerate(kept)}
    pairs: list[tuple[int, int]] = []
    weights: list[float] = []
    observed_pairs: list[tuple[int, int]] = []
    for edge in edges:
        a, b = index.get(edge.source), index.get(edge.target)
        if a is None or b is None:
            continue
        pairs.append((a, b))
        # fr rejects non-positive weights; a zero-weight edge still says "these
        # two are related", so it is floored rather than dropped.
        weights.append(max(float(edge.weight), 1e-6))
        if is_observed_edge(edge):
            observed_pairs.append((a, b))

    if not kept:
        return GraphLayout((), "", "", 0, 0, 0, omitted)

    graph = ig.Graph(n=len(kept), edges=pairs, directed=False)
    previous = random.getstate()
    try:
        random.seed(seed)
        ig.set_random_number_generator(random.Random(seed))
        raw = graph.layout("fr", weights=weights if weights else None)
    finally:
        # Leave the module-global RNGs exactly as they were found: a stage that
        # re-seeds them silently makes the next stage's output depend on whether
        # this one ran.
        ig.set_random_number_generator(random)
        random.setstate(previous)

    xs = _normalise([float(p[0]) for p in raw])
    ys = _normalise([float(p[1]) for p in raw])

    observed_set = frozenset(observed_pairs)
    nodes: list[LaidOutNode] = []
    for i, pid in enumerate(kept):
        degree = degrees.get(pid, 0)
        radius = min(_MAX_R, _MIN_R + math.sqrt(degree) * 1.6)
        if pid in ranks:
            radius = max(radius, 9.0)
        nodes.append(
            LaidOutNode(
                person_id=pid,
                x=round(xs[i]),
                y=round(ys[i]),
                r=max(2, round(radius)),
                cluster=cluster_index.get(pid, -1),
                rank=ranks.get(pid),
                observed=pid in observed_people,
            )
        )

    def _d(items: Sequence[tuple[int, int]]) -> str:
        out: list[str] = []
        for a, b in sorted(items):
            out.append(f"M{round(xs[a])} {round(ys[a])}L{round(xs[b])} {round(ys[b])}")
        return "".join(out)

    inferred_pairs = [p for p in pairs if p not in observed_set]
    return GraphLayout(
        nodes=tuple(nodes),
        inferred_d=_d(inferred_pairs),
        observed_d=_d(observed_pairs),
        n_nodes=len(kept),
        n_edges=len(pairs),
        n_observed_edges=len(observed_pairs),
        omitted_nodes=omitted,
    )
