"""Stage 4 -- assemble the weighted, undirected person-person graph.

Reads ``affiliations.jsonl`` + ``orgs.jsonl`` (Agent 2) and, when they exist,
``observations.jsonl`` + ``targets.jsonl`` (Agent 3). Writes ``edges.jsonl`` and
``graph_stats.json``.

Order of operations, and why it is this order:

1. **Project** affiliations onto people, co-tenure filtered and Newman/size
   weighted (:mod:`interlayer.graph.projection`). Weighting first, because a
   filter applied to badly-weighted edges just keeps the wrong ones.
2. **Backbone** the inferred graph with the disparity filter, then rescue each
   node's top-k edges (:mod:`interlayer.graph.disparity`).
3. **Union in the observed bridges** (:mod:`interlayer.graph.observed`) *after*
   filtering, so ground truth is structurally incapable of being pruned. They
   are also excluded from the strength sums the filter tests against: an edge
   weighted 2.5 among inferred edges weighted 0.02 would make every one of that
   person's inferred edges look insignificant and delete them all.

``observations.jsonl`` and ``targets.jsonl`` are treated as optional rather than
required inputs. Tier 1 is designed to work with zero manual collection, so an
operator who has not recorded any mutual-connection readings must still get a
graph; the missing-input error is reserved for running before ``normalize``,
which genuinely cannot produce anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import networkx as nx
from pydantic import BaseModel

from interlayer.config import Settings
from interlayer.errors import GraphError
from interlayer.graph.disparity import Backbone, backbone
from interlayer.graph.observed import ObservedResult, observed_pairs
from interlayer.graph.projection import Projection, project
from interlayer.graph.sizes import load_firm_sizes
from interlayer.graph.weights import (
    DEFAULT_SIZE_DAMPING,
    MAX_INFERRED_WEIGHT,
    OBSERVED_FLOOR,
    SizeDamping,
    resolve_size_damping,
)
from interlayer.io import dump_json, read_jsonl, write_jsonl
from interlayer.models import (
    SCHEMA_VERSION,
    Affiliation,
    AffiliationKind,
    GraphEdge,
    GraphStats,
    MutualObservation,
    Org,
    TargetPerson,
)

__all__ = ["STATS_FILENAME", "GraphResult", "build_graph", "run"]

M = TypeVar("M", bound=BaseModel)

STATS_FILENAME = "graph_stats.json"
"""Artifact owned by this stage. Shape: ``{schema_version, stats, diagnostics}``.

``stats`` validates as :class:`~interlayer.models.GraphStats` on its own;
``diagnostics`` is free-form and exists so a human can see *why* the graph came
out the shape it did without re-running the stage.
"""

WEIGHT_PRECISION = 9
"""Weights are rounded before serialisation so artifacts diff cleanly."""


@dataclass(frozen=True)
class GraphResult:
    """Everything this stage computes, before it touches the filesystem."""

    edges: tuple[GraphEdge, ...]
    stats: GraphStats
    diagnostics: dict[str, Any]


def _validate(cfg: Settings) -> None:
    """Fail loudly on knobs that would silently produce a nonsense graph."""
    if cfg.max_overlap_years <= 0:
        raise GraphError(f"max_overlap_years must be > 0, got {cfg.max_overlap_years}")
    if not 0.0 < cfg.disparity_alpha <= 1.0:
        raise GraphError(f"disparity_alpha must be in (0, 1], got {cfg.disparity_alpha}")
    if cfg.rescue_top_k < 0:
        raise GraphError(f"rescue_top_k must be >= 0, got {cfg.rescue_top_k}")
    if not 0.0 <= cfg.missing_date_factor <= 1.0:
        raise GraphError(f"missing_date_factor must be in [0, 1], got {cfg.missing_date_factor}")


def _read_optional(path: Path, model: type[M]) -> list[M]:
    """Read an artifact that legitimately may not exist yet."""
    return read_jsonl(path, model) if path.is_file() else []


def _target_org_ids(targets: Iterable[TargetPerson], orgs: Iterable[Org]) -> dict[str, str]:
    """target_person_id -> org_id, so an observed edge can name its shared context.

    Several orgs can carry the same ``target_firm`` (aliases resolved to separate
    records); the lowest ``org_id`` wins so the mapping never depends on file
    order.
    """
    firm_org: dict[str, str] = {}
    for org in sorted(orgs, key=lambda o: o.org_id):
        if org.target_firm is not None:
            firm_org.setdefault(str(org.target_firm), org.org_id)
    return {
        target.target_person_id: firm_org[str(target.firm)]
        for target in sorted(targets, key=lambda t: t.target_person_id)
        if str(target.firm) in firm_org
    }


def _inferred_graph(projection: Projection) -> nx.Graph:
    """Inferred pairs as a networkx graph, inserted in sorted order.

    Insertion order is dict order is iteration order in networkx, so sorting here
    is what makes every downstream traversal reproducible.
    """
    graph = nx.Graph()
    graph.add_nodes_from(sorted(projection.people))
    for source, target in sorted(projection.pairs):
        graph.add_edge(source, target, weight=projection.pairs[(source, target)].weight)
    return graph


def _concentration(projection: Projection, *, redact: bool) -> list[dict[str, Any]]:
    """Top orgs by share of projected weight -- the metric this stage is judged on."""
    total = sum(profile.weight for profile in projection.orgs.values())
    ranked = sorted(projection.orgs.values(), key=lambda p: (-p.weight, -p.members, p.org_id))[:5]
    return [
        {
            "org_id": profile.org_id,
            "name": None if redact else profile.name,
            "members_in_data": profile.members,
            "headcount": profile.headcount,
            "headcount_from_gazetteer": profile.headcount_from_gazetteer,
            "size_damping": round(profile.damping, 6),
            "weight_share": round(profile.weight / total, 6) if total > 0 else 0.0,
        }
        for profile in ranked
    ]


def build_graph(
    cfg: Settings,
    *,
    affiliations: Sequence[Affiliation],
    orgs: Sequence[Org],
    observations: Sequence[MutualObservation] = (),
    targets: Sequence[TargetPerson] = (),
    size_damping: str | SizeDamping | None = None,
) -> GraphResult:
    """Build the graph in memory. Pure: no reads, no writes, no clock, no RNG."""
    _validate(cfg)
    damping = resolve_size_damping(size_damping)
    sizes = load_firm_sizes(cfg.gazetteer)
    org_by_id: Mapping[str, Org] = {org.org_id: org for org in orgs}

    projection = project(
        affiliations,
        orgs=org_by_id,
        sizes=sizes,
        size_damping=damping,
        require_cotenure=cfg.require_cotenure,
        missing_date_factor=cfg.missing_date_factor,
        max_overlap_years=cfg.max_overlap_years,
    )
    observed = observed_pairs(observations, target_orgs=_target_org_ids(targets, orgs))

    inferred = _inferred_graph(projection)
    kept = backbone(inferred, alpha=cfg.disparity_alpha, rescue_top_k=cfg.rescue_top_k)

    edges = _assemble(projection, observed, kept.kept)
    stats, diagnostics = _summarise(projection, observed, kept, edges, cfg, size_damping)
    return GraphResult(edges=edges, stats=stats, diagnostics=diagnostics)


def _assemble(
    projection: Projection,
    observed: ObservedResult,
    kept: Sequence[tuple[str, str]],
) -> tuple[GraphEdge, ...]:
    """Merge surviving inferred edges with observed ones into sorted GraphEdges.

    A pair that is both co-worked and observed keeps the sum: it is at least as
    strong as either alone, and still above every purely inferred edge because
    the observed part alone starts at ``OBSERVED_FLOOR``.

    ``GraphEdge`` canonicalises ``source < target`` in its own validator, so the
    endpoints are handed over as-is rather than sorted here twice.
    """
    merged: dict[tuple[str, str], tuple[float, set[str], set[AffiliationKind]]] = {}
    for pair in kept:
        evidence = projection.pairs[pair]
        merged[pair] = (evidence.weight, set(evidence.org_ids), set(evidence.kinds))
    for pair, obs in observed.pairs.items():
        weight, org_ids, kinds = merged.get(pair, (0.0, set(), set()))
        merged[pair] = (weight + obs.weight, org_ids | obs.org_ids, kinds)

    out: list[GraphEdge] = []
    for pair in sorted(merged):
        weight, org_ids, kinds = merged[pair]
        if weight <= 0.0:
            continue
        out.append(
            GraphEdge(
                source=pair[0],
                target=pair[1],
                weight=round(weight, WEIGHT_PRECISION),
                shared_org_ids=tuple(sorted(org_ids)),
                kinds=tuple(sorted(kinds)),
            )
        )
    return tuple(out)


def _summarise(
    projection: Projection,
    observed: ObservedResult,
    kept: Backbone,
    edges: Sequence[GraphEdge],
    cfg: Settings,
    size_damping: str | SizeDamping | None,
) -> tuple[GraphStats, dict[str, Any]]:
    """GraphStats plus the diagnostics that explain the numbers in it."""
    nodes = sorted(projection.people | observed.people)
    final = nx.Graph()
    final.add_nodes_from(nodes)
    final.add_edges_from((edge.source, edge.target) for edge in edges)

    observed_only = sum(1 for pair in observed.pairs if pair not in projection.pairs)
    considered = projection.pairs_considered
    stats = GraphStats(
        n_nodes=final.number_of_nodes(),
        n_edges=final.number_of_edges(),
        n_components=nx.number_connected_components(final) if nodes else 0,
        density=nx.density(final) if final.number_of_nodes() > 1 else 0.0,
    )
    components = sorted(nx.connected_components(final), key=len, reverse=True)
    diagnostics: dict[str, Any] = {
        "affiliation_records": projection.n_affiliations,
        "orgs_seen": len(projection.orgs),
        "orgs_with_gazetteer_headcount": sum(
            1 for p in projection.orgs.values() if p.headcount_from_gazetteer
        ),
        "orgs_using_n_f_fallback": sum(
            1 for p in projection.orgs.values() if not p.headcount_from_gazetteer
        ),
        "size_damping": (size_damping if isinstance(size_damping, str) else DEFAULT_SIZE_DAMPING),
        "require_cotenure": cfg.require_cotenure,
        "org_pairs_considered": considered,
        "org_pairs_dropped_no_cotenure": projection.pairs_dropped_cotenure,
        "org_pairs_dropped_zero_affiliation_weight": projection.pairs_dropped_zero_weight,
        "cotenure_drop_rate": (
            round(projection.pairs_dropped_cotenure / considered, 6) if considered else 0.0
        ),
        "affiliation_stints_with_invalid_dates": projection.invalid_dates,
        "inferred_pairs_before_filter": len(projection.pairs),
        "disparity_alpha": cfg.disparity_alpha,
        "disparity_significant_edges": kept.n_significant,
        "rescued_edges": kept.n_rescued,
        "rescue_top_k": cfg.rescue_top_k,
        "pruned_edges": kept.n_pruned,
        "observations": observed.n_observations,
        "observations_truncated": observed.n_truncated,
        "observations_without_a_pair": observed.n_unpaired,
        "observed_edges": len(observed.pairs),
        "observed_edges_not_also_inferred": observed_only,
        "observed_floor": OBSERVED_FLOOR,
        "max_inferred_weight": MAX_INFERRED_WEIGHT,
        "nodes_with_edges": sum(1 for _, degree in final.degree() if degree > 0),
        "isolated_nodes": sum(1 for _, degree in final.degree() if degree == 0),
        "largest_component": len(components[0]) if components else 0,
        "projected_weight_share_by_org": _concentration(projection, redact=cfg.redact),
    }
    return stats, diagnostics


def run(cfg: Settings, *, size_damping: str | SizeDamping | None = None) -> None:
    """Stage entry point: read Agent 2/3 artifacts, write edges + graph stats.

    ``size_damping`` is the swap point for the firm-size term, which the research
    flagged as a judgement call needing real-data tuning. It is a keyword
    argument rather than a ``Settings`` field because ``config.py`` is scaffold
    this stage does not own -- adding ``cfg.size_damping`` is a scaffold request.
    """
    affiliations = read_jsonl(cfg.affiliations_path, Affiliation, produced_by="normalize")
    orgs = read_jsonl(cfg.orgs_path, Org, produced_by="normalize")
    observations = _read_optional(cfg.observations_path, MutualObservation)
    targets = _read_optional(cfg.targets_path, TargetPerson)

    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=observations,
        targets=targets,
        size_damping=size_damping,
    )
    write_jsonl(cfg.edges_path, result.edges)
    dump_json(
        cfg.artifact(STATS_FILENAME),
        {
            "schema_version": SCHEMA_VERSION,
            "stats": result.stats.model_dump(mode="json"),
            "diagnostics": result.diagnostics,
        },
    )
