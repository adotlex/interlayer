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
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from interlayer.config import Settings
from interlayer.io import read_jsonl, secure_dir, write_jsonl
from interlayer.models import (
    Affiliation,
    Cluster,
    GraphEdge,
    MutualObservation,
    Org,
    Person,
    ScoreComponents,
    ScoredCluster,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
)
from interlayer.score.evidence import Bucket, Signal
from interlayer.score.signals import (
    FIRM_LABELS,
    TITLE_KEYWORDS,
    Context,
    build_context,
    cluster_signals,
    colleague_signals,
    employment_signals,
    observed_signals,
    proximity_signals,
    title_signals,
)

__all__ = ["FIRM_LABELS", "TITLE_KEYWORDS", "run", "score_all"]
"""``FIRM_LABELS`` and ``TITLE_KEYWORDS`` are re-exported from ``signals`` so the
stage's vocabulary has one import path."""

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

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
# assembly
# ---------------------------------------------------------------------------


def _score_person(ctx: Context, person: Person) -> ScoredPerson:
    """Turn one person's signals into a score, its components and its audit trail.

    Deliberately built evidence-first: the numeric components are *sums of the
    evidence items*, so there is no arrangement of this code in which the score
    moves without a sentence in ``evidence`` explaining the movement.
    """
    signals: list[Signal] = []
    signals += observed_signals(ctx, person.person_id)
    signals += employment_signals(ctx, person)
    signals += colleague_signals(ctx, person)
    signals += proximity_signals(ctx, person.person_id)
    signals += cluster_signals(ctx, person.person_id)
    signals += title_signals(ctx, person, has_link=any(s.item.contribution > 0.0 for s in signals))

    totals: dict[Bucket, float] = {
        "direct": 0.0,
        "alumni": 0.0,
        "proximity": 0.0,
        "cluster": 0.0,
        "title": 0.0,
    }
    per_firm: dict[TargetFirm, float] = {}
    for signal in signals:
        totals[signal.bucket] += signal.item.contribution
        for firm, amount in signal.per_firm:
            per_firm[firm] = per_firm.get(firm, 0.0) + amount

    components = ScoreComponents(
        direct=round(totals["direct"], 6),
        alumni=round(totals["alumni"], 6),
        proximity=round(totals["proximity"], 6),
        cluster=round(totals["cluster"], 6),
        title=round(totals["title"], 6),
    )
    cluster = ctx.cluster_by_person.get(person.person_id)
    return ScoredPerson(
        person_id=person.person_id,
        full_name=person.full_name,
        score=components.total,
        components=components,
        per_firm={firm: round(value, 6) for firm, value in sorted(per_firm.items())},
        cluster_id=cluster.cluster_id if cluster is not None else None,
        evidence=tuple(signal.item for signal in signals),
        hops_to_target=ctx.hops.get(person.person_id),
    )


def _rank_people(scored: Iterable[ScoredPerson]) -> list[ScoredPerson]:
    """1-based ranks, ties broken by ``person_id`` so two runs never disagree."""
    ordered = sorted(scored, key=lambda s: (-s.score, s.person_id))
    return [s.model_copy(update={"rank": position}) for position, s in enumerate(ordered, start=1)]


def _rank_clusters(ctx: Context, people: Sequence[ScoredPerson]) -> list[ScoredCluster]:
    """Rank clusters by the mean PPR of their non-seed members."""
    by_person = {p.person_id: p for p in people}
    rows: list[ScoredCluster] = []
    for stats in ctx.cluster_stats.values():
        cluster = stats.cluster
        members = sorted(
            (by_person[m] for m in cluster.member_ids if m in by_person),
            key=lambda s: (-s.score, s.person_id),
        )
        nonseeds = max(cluster.size - stats.seed_members, 0)
        multiple = stats.mean_nonseed_ppr * ctx.network.size
        context_bits = [
            f"{name} ({count})" for name, count in (*cluster.top_orgs[:2], *cluster.top_titles[:1])
        ]
        rows.append(
            ScoredCluster(
                cluster_id=cluster.cluster_id,
                label=cluster.label,
                score=stats.mean_nonseed_ppr,
                size=cluster.size,
                target_density=stats.target_density,
                per_firm=dict(sorted(stats.per_firm.items())),
                top_person_ids=tuple(p.person_id for p in members[:TOP_CLUSTER_MEMBERS]),
                rationale=(
                    f"{cluster.size} people. {stats.seed_members} already at a target firm "
                    f"({stats.target_density:.0%}); {stats.adjacent_members} of {nonseeds} others "
                    f"sit one introduction away. Average non-seed proximity is {multiple:.1f}x "
                    f"the network average. Shared context: "
                    f"{', '.join(context_bits) if context_bits else 'none recorded'}."
                ),
            )
        )
    ordered = sorted(rows, key=lambda r: (-r.score, r.cluster_id))
    return [r.model_copy(update={"rank": position}) for position, r in enumerate(ordered, start=1)]


def score_all(
    cfg: Settings,
    *,
    people: Sequence[Person],
    edges: Sequence[GraphEdge],
    clusters: Sequence[Cluster],
    orgs: Sequence[Org] = (),
    affiliations: Sequence[Affiliation] = (),
    observations: Sequence[MutualObservation] = (),
    targets: Sequence[TargetPerson] = (),
) -> tuple[list[ScoredPerson], list[ScoredCluster]]:
    """Score and rank every person and cluster. The stage's whole computation.

    Exposed separately from ``run`` so the same code path is exercised whether the
    inputs come off disk or out of a fixture.
    """
    partition = _primary_partition(clusters, cfg.resolution)
    ctx = build_context(
        cfg,
        people=people,
        edges=edges,
        clusters=partition,
        orgs=orgs,
        affiliations=affiliations,
        observations=observations,
        targets=targets,
    )
    # The ego is the operator. Ranking them against their own network is noise,
    # and "you are 1 hop from Jane Street" is not a lead.
    subjects = sorted((p for p in people if not p.is_ego), key=lambda p: p.person_id)
    scored_people = _rank_people(_score_person(ctx, person) for person in subjects)
    scored_clusters = _rank_clusters(ctx, scored_people)

    observed = sum(1 for p in scored_people if p.has_observed_evidence)
    logger.info(
        "scored %d people (%d carrying observed evidence) and %d clusters against %d seeds",
        len(scored_people),
        observed,
        len(scored_clusters),
        len(ctx.seeds),
    )
    if scored_people and observed == 0:
        logger.warning(
            "every score is INFERRED: no Tier 2 mutual-connection observations were available, "
            "so nothing in this output is confirmed fact"
        )
    return scored_people, scored_clusters


def run(cfg: Settings) -> None:
    """Rank people and clusters by adjacency to the target firms."""
    people = read_jsonl(cfg.people_path, Person, produced_by="ingest")
    edges = read_jsonl(cfg.edges_path, GraphEdge, produced_by="build")
    clusters = read_jsonl(cfg.clusters_path, Cluster, produced_by="cluster")
    orgs = _read_optional(
        cfg.orgs_path, Org, produced_by="normalize", need="no firm can be marked as a target"
    )
    affiliations = _read_optional(
        cfg.affiliations_path,
        Affiliation,
        produced_by="normalize",
        need="employment and alumni evidence will be missing",
    )
    observations = _read_optional(
        cfg.observations_path,
        MutualObservation,
        produced_by="enrich",
        need="every score will be INFERRED rather than observed",
    )
    targets = _read_optional(
        cfg.targets_path,
        TargetPerson,
        produced_by="enrich",
        need="observed evidence cannot be attributed to a specific firm",
    )

    scored_people, scored_clusters = score_all(
        cfg,
        people=people,
        edges=edges,
        clusters=clusters,
        orgs=orgs,
        affiliations=affiliations,
        observations=observations,
        targets=targets,
    )
    secure_dir(cfg.artifact_dir)
    write_jsonl(cfg.scored_people_path, scored_people)
    write_jsonl(cfg.scored_clusters_path, scored_clusters)
