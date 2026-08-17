"""The analysis core: R3 pipeline steps 5-14.

Pure, offline, deterministic. Nothing under this package may import ``httpx``,
``requests``, ``urllib.request`` or ``socket`` (setup guide §3, boundary 1).

``analyse`` is the single entry point::

    result = analyse(members, targets, edges)

Everything below it is a pure function over the frozen dataclasses in
``core.models``, which is what makes the determinism tests possible.

Pipeline order, and why each step is where it is:

5.  restrict to bridges (``d_m > 0``)     — ``|B| << |M|``; drop the rest early
6.  build bipartite graph, sorted         — sorted order *is* the determinism contract
7.  RA projection ``A @ diags(1/d_T) @ Aᵀ`` — kills the hub pathology
8.  deterministic igraph conversion       — sorted names, sorted edges, 12 dp weights
9.  consensus Leiden, 25 seeds, tau=0.5   — one seed is an arbitrary pick among near-ties
10. brokerage on the right graph each time
11. percentile-ranked composite
12. TF-IDF labels with the sentinel guard
13. rank clusters by score sum
14. next-harvest priorities
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from interlayer.core.models import (
    AnalysisResult,
    Brokerage,
    Cluster,
    Coverage,
    Edge,
    Member,
    Target,
)
from interlayer.graph.brokerage import RawMetrics, brokerage_metrics
from interlayer.graph.build import BipartiteGraph, bridge_members, build_bipartite, select_edges
from interlayer.graph.cluster import (
    DEFAULT_BASE_SEED,
    DEFAULT_GAMMA,
    DEFAULT_MAX_CONSENSUS_NODES,
    DEFAULT_N_RUNS,
    DEFAULT_TAU,
    ConsensusPartition,
    leiden_consensus,
    leiden_once,
    modularity,
    select_gamma,
    stability_scores,
    to_igraph,
)
from interlayer.graph.fingerprint import membership_fingerprint, result_fingerprint
from interlayer.graph.inference import Cues, infer_edge, infer_edge_confidence, infer_edges
from interlayer.graph.label import (
    AttributeFn,
    default_attributes,
    label_clusters,
    top_shared_targets,
)
from interlayer.graph.nextsteps import next_targets as compute_next_targets
from interlayer.graph.project import Projection, Weighting, project
from interlayer.graph.score import DEFAULT_WEIGHTS, composite_score, percentile_rank, wilson_lb

__all__ = [
    "DEFAULT_BASE_SEED",
    "DEFAULT_GAMMA",
    "DEFAULT_N_RUNS",
    "DEFAULT_TAU",
    "DEFAULT_WEIGHTS",
    "AnalysisContext",
    "BipartiteGraph",
    "ConsensusPartition",
    "Cues",
    "Projection",
    "RawMetrics",
    "analyse",
    "bridge_members",
    "brokerage_metrics",
    "build_bipartite",
    "composite_score",
    "default_attributes",
    "infer_edge",
    "infer_edge_confidence",
    "infer_edges",
    "label_clusters",
    "leiden_consensus",
    "leiden_once",
    "membership_fingerprint",
    "modularity",
    "percentile_rank",
    "project",
    "result_fingerprint",
    "select_edges",
    "select_gamma",
    "stability_scores",
    "to_igraph",
    "top_shared_targets",
    "wilson_lb",
]


class AnalysisContext:
    """Intermediate artefacts of one :func:`analyse` call.

    Exposed so tests and the report layer can inspect the projection, the igraph
    object and the consensus matrix without re-running the pipeline.
    """

    __slots__ = ("bipartite", "igraph", "metrics", "partition", "projection", "result", "scores")

    def __init__(
        self,
        *,
        bipartite: BipartiteGraph,
        projection: Projection,
        igraph: Any,
        partition: ConsensusPartition,
        metrics: dict[str, RawMetrics],
        scores: dict[str, float],
        result: AnalysisResult,
    ) -> None:
        self.bipartite = bipartite
        self.projection = projection
        self.igraph = igraph
        self.partition = partition
        self.metrics = metrics
        self.scores = scores
        self.result = result


def analyse(
    members: Sequence[Member],
    targets: Sequence[Target],
    edges: Sequence[Edge],
    *,
    include_inferred: bool = False,
    min_confidence: float = 0.5,
    weighting: Weighting = "ra",
    use_confidence: bool = True,
    gamma: float = DEFAULT_GAMMA,
    base_seed: int = DEFAULT_BASE_SEED,
    n_runs: int = DEFAULT_N_RUNS,
    tau: float = DEFAULT_TAU,
    consensus: bool = True,
    max_consensus_nodes: int = DEFAULT_MAX_CONSENSUS_NODES,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
    label_attributes: AttributeFn = default_attributes,
    top_next: int = 20,
    unadjudicated_count: int = 0,
    return_context: bool = False,
) -> AnalysisResult | AnalysisContext:
    """Run steps 5-14 and return an :class:`~interlayer.core.models.AnalysisResult`.

    ``include_inferred`` defaults to ``False``: an inferred edge is a guess and
    must not silently move a headline number (PRIV-19). With it enabled,
    ``min_confidence`` gates inferred edges only.

    Set ``return_context=True`` to get the intermediates alongside the result.
    """
    # --- steps 5-6: bridge restriction and bipartite construction -------------
    bg = build_bipartite(
        members,
        targets,
        edges,
        include_inferred=include_inferred,
        min_confidence=min_confidence,
    )

    # --- step 7: RA projection ------------------------------------------------
    projection = project(bg, weighting=weighting, use_confidence=use_confidence)

    # --- step 8: deterministic igraph ----------------------------------------
    g = to_igraph(projection.matrix, projection.member_ids)

    # --- step 9: consensus Leiden --------------------------------------------
    if consensus:
        partition = leiden_consensus(
            g,
            gamma=gamma,
            n_runs=n_runs,
            base_seed=base_seed,
            tau=tau,
            max_dense_nodes=max_consensus_nodes,
        )
    else:
        membership_list = leiden_once(g, gamma=gamma, seed=base_seed)
        partition = ConsensusPartition(
            membership=tuple(membership_list),
            stability=tuple([1.0] * g.vcount()),
            names=tuple(g.vs["name"]) if g.vcount() else (),
            n_runs=1,
            tau=tau,
            gamma=gamma,
            base_seed=base_seed,
            modularity=modularity(g, membership_list),
            consensus_applied=False,
        )

    membership = partition.by_member()
    stability = partition.stability_by_member()

    # --- steps 10-11: brokerage and composite ---------------------------------
    metrics = brokerage_metrics(bg, projection, g)
    scores = composite_score(metrics, weights)

    brokerage_rows = tuple(
        Brokerage(
            member_id=m,
            reach=metrics[m].reach,
            rarity=metrics[m].rarity,
            betweenness=metrics[m].betweenness,
            effective_size=metrics[m].effective_size,
            constraint=metrics[m].constraint,
            autonomy=metrics[m].autonomy,
            composite=scores.get(m, 0.0),
        )
        # step 13: members ranked by score descending, ties by member_id
        for m in sorted(bg.member_ids, key=lambda x: (-scores.get(x, 0.0), x))
    )

    # --- step 12: labels and shared targets -----------------------------------
    groups: dict[int, list[str]] = {}
    for member_id, cluster_id in membership.items():
        groups.setdefault(cluster_id, []).append(member_id)
    for member_id_list in groups.values():
        member_id_list.sort()

    members_by_id = {m.member_id: m for m in members}
    labels = label_clusters(members_by_id, groups, attributes=label_attributes)
    shared = top_shared_targets(bg, groups)

    # --- step 13: clusters ranked by summed score -----------------------------
    cluster_rows = []
    for cluster_id in sorted(groups):
        member_ids = groups[cluster_id]
        ordered = tuple(sorted(member_ids, key=lambda x: (-scores.get(x, 0.0), x)))
        mean_stability = (
            sum(stability.get(m, 1.0) for m in member_ids) / len(member_ids)
            if member_ids
            else 1.0
        )
        cluster_rows.append(
            Cluster(
                cluster_id=cluster_id,
                label=labels.get(cluster_id, f"mixed (n={len(member_ids)})"),
                member_ids=ordered,
                top_targets=shared.get(cluster_id, ()),
                stability=round(mean_stability, 12),
                score_sum=round(sum(scores.get(m, 0.0) for m in member_ids), 12),
            )
        )
    cluster_rows.sort(key=lambda c: (-c.score_sum, c.cluster_id))
    clusters = tuple(cluster_rows)

    coverage = Coverage(
        n_targets_total=len(targets),
        n_targets_harvested=bg.n_targets_harvested,
        n_members_total=len(members),
        n_bridges=len(bg.member_ids),
    )

    # --- step 14: next-harvest priorities -------------------------------------
    harvested_ids = {t.target_id for t in targets if t.harvested}
    signal_edges = tuple(e for e in edges if e.target_id not in harvested_ids)
    next_rows = compute_next_targets(
        targets,
        signal_edges,
        bg.edges,
        membership,
        top=top_next,
    )

    params: dict[str, Any] = {
        "include_inferred": include_inferred,
        "min_confidence": min_confidence,
        "weighting": weighting,
        "use_confidence": use_confidence,
        "gamma": gamma,
        "base_seed": base_seed,
        "n_runs": partition.n_runs,
        "tau": tau,
        "consensus_applied": partition.consensus_applied,
        "weights": {k: weights[k] for k in sorted(weights)},
        "modularity": partition.modularity,
        "k": partition.k,
    }

    fingerprint = result_fingerprint(
        membership=membership,
        clusters=clusters,
        brokerage=brokerage_rows,
        coverage=coverage,
        next_targets=next_rows,
        params=params,
    )

    result = AnalysisResult(
        clusters=clusters,
        brokerage=brokerage_rows,
        coverage=coverage,
        next_targets=next_rows,
        used_inferred_edges=bg.used_inferred_edges,
        unadjudicated_count=unadjudicated_count,
        fingerprint=fingerprint,
        params=params,
    )

    if return_context:
        return AnalysisContext(
            bipartite=bg,
            projection=projection,
            igraph=g,
            partition=partition,
            metrics=metrics,
            scores=scores,
            result=result,
        )
    return result
