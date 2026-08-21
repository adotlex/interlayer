"""Stage 5a -- group the network into communities that do not move between runs.

Reads ``edges.jsonl`` and writes ``clusters.jsonl`` (the partition at
``cfg.resolution``) plus ``clusters_sweep.jsonl`` (every resolution in
``cfg.resolution_sweep``, so an operator can trade group size against
granularity without a re-run).

Three decisions here are load-bearing and were settled by measurement in Wave 1,
not by preference:

* **Consensus, not a single Leiden run.** See ``consensus`` for the numbers; the
  short version is that one seeded run is reproducible but not stable, and at our
  scale two runs disagreed enough that an operator would see different groups.
* **gamma = 2.0, not the modularity optimum.** Q peaks at gamma=1.0, which
  returned clusters of 52-118 people -- roughly four times too coarse to act on.
  Usefulness and modularity do not peak in the same place, so resolution is a
  product decision, not an argmax.
* **Clusters are split back to connected pieces.** Consensus components can chain
  two communities through a node that ends up in neither, so the Leiden
  connectivity guarantee is re-imposed rather than assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

import igraph as ig
from pydantic import BaseModel

from interlayer.cluster.consensus import build_igraph, consensus_membership
from interlayer.cluster.labeling import label_for, orgs_by_person, titles_by_person
from interlayer.cluster.quality import cohesion, is_connected, quality_warnings
from interlayer.config import Settings
from interlayer.io import read_jsonl, secure_dir, write_jsonl
from interlayer.models import Affiliation, Cluster, GraphEdge, Org, Person, stable_id

__all__ = ["ALGORITHM", "SWEEP_FILENAME", "cluster_graph", "run"]

logger = logging.getLogger(__name__)

ALGORITHM = "consensus-leiden"
SWEEP_FILENAME = "clusters_sweep.jsonl"
"""The multi-resolution view. ``clusters.jsonl`` stays a single partition so that
downstream stages never have to guess which granularity a person belongs to."""

M = TypeVar("M", bound=BaseModel)


def _read_optional(path: Path, model: type[M], *, produced_by: str, need: str) -> list[M]:
    """Read an artifact that improves the output but is not required to produce it.

    Missing labelling inputs degrade cluster *names*, not cluster *membership*, so
    they warn instead of raising: a partition with generic names still beats no
    partition, and refusing to run would make ``cluster`` untestable in isolation.
    """
    if not path.is_file():
        logger.warning("%s not found; %s. Run `interlayer %s` first.", path, need, produced_by)
        return []
    return read_jsonl(path, model, produced_by=produced_by)


def _node_index(edges: Sequence[GraphEdge]) -> list[str]:
    """Every person mentioned by an edge, in a fixed order.

    Sorted rather than first-seen: ``PYTHONHASHSEED`` produced four distinct
    set-iteration orders in Wave 1, and node order decides leidenalg's
    tie-breaking, so an unsorted index would quietly denature the seed.
    """
    seen: set[str] = set()
    for edge in edges:
        seen.add(edge.source)
        seen.add(edge.target)
    return sorted(seen)


def _index_triples(
    edges: Sequence[GraphEdge], position: dict[str, int]
) -> list[tuple[int, int, float]]:
    """Collapse edges to sorted ``(u, v, weight)`` index triples, summing duplicates.

    The graph stage should not emit an endpoint pair twice, but if it ever does,
    summing is the only interpretation that keeps total weight conserved.
    """
    merged: dict[tuple[int, int], float] = {}
    for edge in edges:
        u, v = position[edge.source], position[edge.target]
        key = (u, v) if u < v else (v, u)
        merged[key] = merged.get(key, 0.0) + float(edge.weight)
    return sorted((u, v, weight) for (u, v), weight in merged.items())


def _groups(membership: Sequence[int]) -> list[list[int]]:
    """Communities as ascending index lists, ordered by their lowest member."""
    buckets: dict[int, list[int]] = {}
    for node, community in enumerate(membership):
        buckets.setdefault(community, []).append(node)
    return sorted(buckets.values(), key=lambda members: members[0])


def _split_disconnected(graph: ig.Graph, members: Sequence[int]) -> list[list[int]]:
    """Break a member set into the connected pieces it actually forms.

    Consensus components are unions of overlapping Leiden communities, and the
    path joining two members can run through a node that landed in neither, so a
    consensus cluster is not automatically connected even though every Leiden
    community is. Showing an operator "this group" when the group has no internal
    path between its halves would be a lie, so the pieces are separated.
    """
    if is_connected(graph, members):
        return [list(members)]
    sub = graph.subgraph(list(members))
    pieces: dict[int, list[int]] = {}
    for local, component in enumerate(sub.connected_components().membership):
        pieces.setdefault(component, []).append(members[local])
    return sorted(pieces.values(), key=lambda piece: piece[0])


def cluster_graph(
    node_ids: Sequence[str],
    graph: ig.Graph,
    cfg: Settings,
    *,
    resolution: float,
    people: Sequence[Person] = (),
    orgs: Sequence[Org] = (),
    affiliations: Sequence[Affiliation] = (),
) -> list[Cluster]:
    """Consensus-cluster one graph at one resolution and label the result.

    Separated from ``run`` so the sweep, the tests and any future interactive
    granularity slider all take the identical code path.
    """
    result = consensus_membership(
        graph,
        runs=cfg.consensus_runs,
        threshold=cfg.consensus_threshold,
        resolution=resolution,
        seed=cfg.seed,
    )
    if not result.converged:
        logger.warning(
            "consensus did not reach block structure at gamma=%s after %d iterations; "
            "the partition is the final consensus-graph clustering, not a fixed point",
            resolution,
            result.iterations,
        )

    person_orgs = orgs_by_person(affiliations)
    person_titles = titles_by_person(people, affiliations)
    org_names = {org.org_id: org.name for org in orgs}
    global_org_counts: dict[str, int] = {}
    for org_set in person_orgs.values():
        for org_id in sorted(org_set):
            global_org_counts[org_id] = global_org_counts.get(org_id, 0) + 1
    n_people = max(len(person_orgs), len(node_ids))

    clusters: list[Cluster] = []
    for community in _groups(result.membership):
        for piece in _split_disconnected(graph, community):
            if len(piece) < cfg.min_cluster_size:
                continue
            member_ids = sorted(node_ids[i] for i in piece)
            named = label_for(
                member_ids,
                person_orgs=person_orgs,
                person_titles=person_titles,
                org_names=org_names,
                global_org_counts=global_org_counts,
                n_people=n_people,
            )
            clusters.append(
                Cluster(
                    cluster_id=stable_id("cluster", f"{resolution:g}", *member_ids),
                    label=named.label,
                    member_ids=tuple(member_ids),
                    top_orgs=named.top_orgs,
                    top_titles=named.top_titles,
                    cohesion=cohesion(graph, piece),
                    algorithm=ALGORITHM,
                    resolution=resolution,
                )
            )

    for warning in quality_warnings(
        graph,
        result.membership,
        {c.cluster_id: c.member_ids for c in clusters},
        n_nodes=len(node_ids),
    ):
        logger.warning("cluster quality (gamma=%s): %s", resolution, warning)

    return sorted(clusters, key=lambda c: (-c.size, c.cluster_id))


def run(cfg: Settings) -> None:
    """Cluster the person graph and write the primary partition plus the sweep."""
    edges = read_jsonl(cfg.edges_path, GraphEdge, produced_by="build")
    people = _read_optional(
        cfg.people_path, Person, produced_by="ingest", need="cluster titles will be missing"
    )
    orgs = _read_optional(
        cfg.orgs_path, Org, produced_by="normalize", need="cluster labels will show raw org ids"
    )
    affiliations = _read_optional(
        cfg.affiliations_path,
        Affiliation,
        produced_by="normalize",
        need="cluster labels will be generic",
    )

    node_ids = _node_index(edges)
    graph = build_igraph(
        len(node_ids), _index_triples(edges, {n: i for i, n in enumerate(node_ids)})
    )
    if not node_ids:
        logger.warning("no edges in %s; writing an empty partition", cfg.edges_path)

    resolutions = sorted({*cfg.resolution_sweep, cfg.resolution})
    sweep: list[Cluster] = []
    primary: list[Cluster] = []
    for resolution in resolutions:
        found = cluster_graph(
            node_ids,
            graph,
            cfg,
            resolution=resolution,
            people=people,
            orgs=orgs,
            affiliations=affiliations,
        )
        logger.info(
            "gamma=%s: %d clusters of >=%d members covering %d of %d people",
            resolution,
            len(found),
            cfg.min_cluster_size,
            sum(c.size for c in found),
            len(node_ids),
        )
        sweep.extend(found)
        if resolution == cfg.resolution:
            primary = found

    secure_dir(cfg.artifact_dir)
    write_jsonl(cfg.clusters_path, primary)
    write_jsonl(cfg.artifact(SWEEP_FILENAME), sweep)
