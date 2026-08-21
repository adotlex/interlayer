"""Stage 5b -- rank people and clusters by adjacency to the target firms.

Reads the graph, the partition and the affiliation evidence; writes
``scored_people.jsonl`` and ``scored_clusters.jsonl``.

The ranking is a personalized PageRank seeded on everyone known to be at a target
firm, plus additive components from ``cfg.weights``. Three things about it are
deliberate:

* **Clusters rank by the mean PPR of their NON-seed members.** Seeds scoring
  themselves highly is not information -- it is the restart vector reading itself
  back. Ranking by the sum would just rank big clusters first, which is also not
  information.
* **Observed evidence dominates.** ``observed_mutual`` is weighted 10.0 against
  ``direct_employment``'s 6.0, because a human who read a mutual-connections list
  knows something this tool can only guess at. Only such a reading is ever
  labelled ``Provenance.OBSERVED``.
* **Per-firm scores stay per firm.** Citadel LLC and Citadel Securities are
  separate companies with separate staff; the two are never summed into one
  "Citadel" number, and there is no code path that could.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from interlayer.config import Settings
from interlayer.io import read_jsonl, secure_dir, write_jsonl
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    Cluster,
    EvidenceItem,
    GraphEdge,
    MutualObservation,
    Org,
    Person,
    Provenance,
    ScoreComponents,
    ScoredCluster,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
    normalize_key,
)
from interlayer.score.evidence import Bucket, Signal, condense, damped
from interlayer.score.network import Network, personalized_pagerank

__all__ = ["FIRM_LABELS", "TITLE_KEYWORDS", "run", "score_all"]

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

FIRM_LABELS: Mapping[TargetFirm, str] = {
    TargetFirm.JANE_STREET: "Jane Street",
    TargetFirm.CITADEL_LLC: "Citadel LLC",
    TargetFirm.CITADEL_SECURITIES: "Citadel Securities",
}
"""Display names. Citadel LLC and Citadel Securities are spelled out in full
everywhere a human will read them, because "Citadel" alone is the ambiguity that
corrupts this whole domain."""

TITLE_KEYWORDS: tuple[str, ...] = (
    "quant",
    "trader",
    "trading",
    "researcher",
    "research scientist",
    "software engineer",
    "developer",
    "portfolio manager",
    "data scientist",
    "machine learning",
    "algorithm",
    "strategist",
    "risk",
)
"""Roles these firms recruit from. A heuristic, weighted 0.5 and only ever applied
on top of an existing link -- a job title says nothing about whose network someone
is in, so on its own it must not put anybody on the operator's list."""

MAX_EVIDENTIAL_ORG_MEMBERS = 500
"""Above this an employer stops being evidence of acquaintance. Wave 1 measured a
single large employer producing 65% of all naive edges; enumerating colleague
pairs inside one is expensive and says almost nothing."""

TOP_CLUSTER_MEMBERS = 5


# ---------------------------------------------------------------------------
# input loading
# ---------------------------------------------------------------------------


def _read_optional(path: Path, model: type[M], *, produced_by: str, need: str) -> list[M]:
    """Read an artifact that enriches the score but is not required to compute one."""
    if not path.is_file():
        logger.warning("%s not found; %s. Run `interlayer %s` first.", path, need, produced_by)
        return []
    return read_jsonl(path, model, produced_by=produced_by)


def _primary_partition(clusters: Sequence[Cluster], resolution: float) -> list[Cluster]:
    """Keep one resolution, so nobody belongs to three overlapping clusters.

    ``cluster`` writes only the primary partition to ``clusters.jsonl`` and puts
    the sweep in a separate file, but filtering here means a hand-assembled or
    concatenated artifact cannot silently produce a person with three cluster ids.
    """
    available = sorted({c.resolution for c in clusters})
    if len(available) <= 1:
        return list(clusters)
    chosen = resolution if resolution in available else available[0]
    logger.warning(
        "clusters.jsonl mixes resolutions %s; scoring the gamma=%s partition only",
        available,
        chosen,
    )
    return [c for c in clusters if c.resolution == chosen]


# ---------------------------------------------------------------------------
# derived context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    """Everything derived once, before any person is scored."""

    cfg: Settings
    network: Network
    display: Callable[[str], str]
    org_names: Mapping[str, str]
    firm_by_org: Mapping[str, TargetFirm]
    affiliations_by_person: Mapping[str, tuple[Affiliation, ...]]
    target_members_by_org: Mapping[str, tuple[str, ...]]
    org_member_counts: Mapping[str, int]
    firms_by_person: Mapping[str, tuple[TargetFirm, ...]]
    observations_by_bridge: Mapping[str, tuple[MutualObservation, ...]]
    firm_by_target_person: Mapping[str, TargetFirm]
    seeds: frozenset[str]
    ppr: Mapping[str, float]
    ppr_by_firm: Mapping[TargetFirm, Mapping[str, float]]
    max_nonseed_ppr: float
    hops: Mapping[str, int | None]
    cluster_by_person: Mapping[str, Cluster]
    cluster_stats: Mapping[str, "_ClusterStats"]
    max_cluster_score: float
    weights_by_firm: Mapping[str, Mapping[TargetFirm, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class _ClusterStats:
    """Proximity summary for one cluster, computed before any person is ranked."""

    cluster: Cluster
    mean_nonseed_ppr: float
    per_firm: dict[TargetFirm, float]
    seed_members: int
    adjacent_members: int
    rank: int = 0

    @property
    def target_density(self) -> float:
        size = self.cluster.size
        return round(self.seed_members / size, 6) if size else 0.0


def _display_name(people: Sequence[Person], targets: Sequence[TargetPerson], *, redact: bool):
    """Resolve an id to something a human reads -- or to the id itself when redacting.

    ``EvidenceItem.detail`` is a pre-rendered sentence, so the report cannot strip
    a name out of it later. Redaction therefore has to happen where the sentence is
    written, and ``person_id`` is exactly the pseudonymous stable id the redacted
    mode calls for.
    """
    names = {p.person_id: p.full_name for p in people}
    names.update({t.target_person_id: t.full_name for t in targets})

    def resolve(identifier: str) -> str:
        if redact:
            return identifier
        return names.get(identifier, identifier)

    return resolve


def _build_context(
    cfg: Settings,
    *,
    people: Sequence[Person],
    edges: Sequence[GraphEdge],
    clusters: Sequence[Cluster],
    orgs: Sequence[Org],
    affiliations: Sequence[Affiliation],
    observations: Sequence[MutualObservation],
    targets: Sequence[TargetPerson],
) -> _Context:
    network = Network.from_edges(
        (e.source, e.target, float(e.weight)) for e in edges
    ).with_nodes(p.person_id for p in people)

    firm_by_org = {
        o.org_id: o.target_firm for o in orgs if o.is_target and o.target_firm is not None
    }
    org_names = {o.org_id: o.name for o in orgs}

    by_person: dict[str, list[Affiliation]] = {}
    by_org: dict[str, list[str]] = {}
    for aff in sorted(
        affiliations, key=lambda a: (a.person_id, a.org_id, str(a.kind), str(a.start or ""))
    ):
        by_person.setdefault(aff.person_id, []).append(aff)
        members = by_org.setdefault(aff.org_id, [])
        if aff.person_id not in members:
            members.append(aff.person_id)

    firms_by_person: dict[str, list[TargetFirm]] = {}
    for person_id, affs in by_person.items():
        firms = sorted({firm_by_org[a.org_id] for a in affs if a.org_id in firm_by_org})
        if firms:
            firms_by_person[person_id] = firms

    target_members_by_org = {
        org_id: tuple(sorted(m for m in members if m in firms_by_person))
        for org_id, members in sorted(by_org.items())
        if org_id not in firm_by_org
    }

    firm_by_target_person = {t.target_person_id: t.firm for t in targets}
    observations_by_bridge: dict[str, list[MutualObservation]] = {}
    for obs in sorted(observations, key=lambda o: o.target_person_id):
        for bridge in obs.bridge_person_ids:
            observations_by_bridge.setdefault(bridge, []).append(obs)

    known_nodes = set(network.node_ids)
    seeds = frozenset(
        node
        for node in (
            set(firms_by_person)
            | {t.target_person_id for t in targets}
            | {o.target_person_id for o in observations}
        )
        if node in known_nodes
    )
    seed_positions = sorted(network.index[s] for s in seeds)
    if not seeds:
        logger.warning(
            "no target-firm people found in the graph; proximity scores will be zero. "
            "Check that `normalize` marked the target orgs and that `enrich` ran."
        )

    ppr_vector = personalized_pagerank(network, seed_positions, alpha=cfg.pagerank_alpha)
    ppr = dict(zip(network.node_ids, ppr_vector, strict=True))

    ppr_by_firm: dict[TargetFirm, Mapping[str, float]] = {}
    for firm in sorted(TargetFirm):
        firm_seeds = sorted(
            {p for p, firms in firms_by_person.items() if firm in firms and p in known_nodes}
            | {t for t, f in firm_by_target_person.items() if f is firm and t in known_nodes}
        )
        if not firm_seeds:
            continue
        vector = personalized_pagerank(
            network, [network.index[s] for s in firm_seeds], alpha=cfg.pagerank_alpha
        )
        ppr_by_firm[firm] = dict(zip(network.node_ids, vector, strict=True))

    nonseed_values = [v for node, v in sorted(ppr.items()) if node not in seeds]
    max_nonseed_ppr = max(nonseed_values) if nonseed_values else 0.0
    hops = dict(zip(network.node_ids, network.hops_from(seed_positions), strict=True))

    cluster_by_person: dict[str, Cluster] = {}
    for cluster in clusters:
        for member in cluster.member_ids:
            cluster_by_person[member] = cluster
    stats = _cluster_stats(clusters, ppr=ppr, ppr_by_firm=ppr_by_firm, seeds=seeds, network=network)
    max_cluster_score = max((s.mean_nonseed_ppr for s in stats.values()), default=0.0)

    return _Context(
        cfg=cfg,
        network=network,
        display=_display_name(people, targets, redact=cfg.redact),
        org_names=org_names,
        firm_by_org=firm_by_org,
        affiliations_by_person={p: tuple(a) for p, a in by_person.items()},
        target_members_by_org=target_members_by_org,
        org_member_counts={org_id: len(members) for org_id, members in by_org.items()},
        firms_by_person={p: tuple(f) for p, f in firms_by_person.items()},
        observations_by_bridge={b: tuple(o) for b, o in observations_by_bridge.items()},
        firm_by_target_person=firm_by_target_person,
        seeds=seeds,
        ppr=ppr,
        ppr_by_firm=ppr_by_firm,
        max_nonseed_ppr=max_nonseed_ppr,
        hops=hops,
        cluster_by_person=cluster_by_person,
        cluster_stats=stats,
        max_cluster_score=max_cluster_score,
    )


def _cluster_stats(
    clusters: Sequence[Cluster],
    *,
    ppr: Mapping[str, float],
    ppr_by_firm: Mapping[TargetFirm, Mapping[str, float]],
    seeds: frozenset[str],
    network: Network,
) -> dict[str, _ClusterStats]:
    """Mean non-seed PPR per cluster, plus the counts that explain it.

    Mean and not sum: summing ranks the biggest cluster first regardless of
    proximity. Non-seed and not all members: a cluster full of Jane Street people
    scores high on its own seeds, which tells the operator nothing they did not
    already know.
    """
    unranked: list[_ClusterStats] = []
    for cluster in clusters:
        members = list(cluster.member_ids)
        nonseeds = [m for m in members if m not in seeds]
        mean = (
            round(sum(ppr.get(m, 0.0) for m in nonseeds) / len(nonseeds), 12) if nonseeds else 0.0
        )
        per_firm = {}
        for firm in sorted(ppr_by_firm):
            vector = ppr_by_firm[firm]
            per_firm[firm] = (
                round(sum(vector.get(m, 0.0) for m in nonseeds) / len(nonseeds), 12)
                if nonseeds
                else 0.0
            )
        adjacent = sum(
            1
            for m in nonseeds
            if any(network.node_ids[j] in seeds for j, _ in network.neighbours(m))
        )
        unranked.append(
            _ClusterStats(
                cluster=cluster,
                mean_nonseed_ppr=mean,
                per_firm=per_firm,
                seed_members=sum(1 for m in members if m in seeds),
                adjacent_members=adjacent,
            )
        )
    ordered = sorted(unranked, key=lambda s: (-s.mean_nonseed_ppr, s.cluster.cluster_id))
    return {
        s.cluster.cluster_id: _ClusterStats(
            cluster=s.cluster,
            mean_nonseed_ppr=s.mean_nonseed_ppr,
            per_firm=s.per_firm,
            seed_members=s.seed_members,
            adjacent_members=s.adjacent_members,
            rank=position,
        )
        for position, s in enumerate(ordered, start=1)
    }
