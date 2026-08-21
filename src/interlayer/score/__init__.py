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
from dataclasses import dataclass
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
from interlayer.score.evidence import Bucket, EvidenceKind, Signal, condense, damped
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
    cluster_stats: Mapping[str, _ClusterStats]
    max_cluster_score: float


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


def _display_name(
    people: Sequence[Person], targets: Sequence[TargetPerson], *, redact: bool
) -> Callable[[str], str]:
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
    network = Network.from_edges((e.source, e.target, float(e.weight)) for e in edges).with_nodes(
        p.person_id for p in people
    )

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


# ---------------------------------------------------------------------------
# evidence builders -- one per weight in ScoreWeights
# ---------------------------------------------------------------------------


def _firm_label(firm: TargetFirm) -> str:
    return FIRM_LABELS.get(firm, str(firm.value))


def _span(affiliation: Affiliation) -> str:
    """Human phrasing for a tenure, honest about missing dates."""
    if affiliation.start and affiliation.end:
        return f"{affiliation.start} to {affiliation.end}"
    if affiliation.start:
        return f"since {affiliation.start}"
    if affiliation.end:
        return f"until {affiliation.end}"
    return "dates not recorded"


def _is_current(person: Person | None, affiliation: Affiliation) -> bool:
    """Whether a stint is still running.

    Prefers the export's explicit ``is_current`` flag and falls back to an open
    end date, which is how LinkedIn renders a present role.
    """
    if person is not None:
        for position in person.positions:
            if position.company_org_id == affiliation.org_id and position.is_current:
                return True
    return affiliation.end is None


def _observed_signals(ctx: _Context, person_id: str) -> list[Signal]:
    """Tier 2 evidence: a human read this person's name off a target's profile.

    The only signal in the whole tool that is not an inference, which is why it
    carries ``Provenance.OBSERVED`` and ten times the weight of a guess. A
    truncated reading still counts -- the names that *were* seen are real; it is
    only the absence of a name from a truncated list that proves nothing, and
    absence is never used as evidence here.
    """
    signals: list[Signal] = []
    for rank, observation in enumerate(ctx.observations_by_bridge.get(person_id, ()), start=1):
        firm = ctx.firm_by_target_person.get(observation.target_person_id)
        contribution = round(damped(ctx.cfg.weights.observed_mutual, rank), 6)
        where = f" at {_firm_label(firm)}" if firm else ""
        when = f", recorded {observation.observed_on}" if observation.observed_on else ""
        caveat = (
            f" That list was truncated at {len(observation.bridge_person_ids)} of "
            f"{observation.stated_count} names, so it is a floor, not a full reading."
            if observation.truncated
            else ""
        )
        signals.append(
            Signal(
                item=EvidenceItem(
                    kind="observed_mutual",
                    detail=(
                        f"Observed on LinkedIn: this person appears in the shared-connections "
                        f"list on {ctx.display(observation.target_person_id)}'s profile"
                        f"{where}{when}. That is a real connection, not an inference.{caveat}"
                    ),
                    contribution=contribution,
                    via_person_id=observation.target_person_id,
                    hops=1,
                    provenance=Provenance.OBSERVED,
                ),
                bucket="direct",
                per_firm=((firm, contribution),) if firm else (),
            )
        )
    return condense(
        signals,
        summary=lambda n, total: (
            f"And {n} further observed shared connections into the target firms, "
            f"adding {total:.2f} between them."
        ),
    )


def _employment_signals(ctx: _Context, person: Person) -> list[Signal]:
    """This person is, or was, inside a target firm."""
    signals: list[Signal] = []
    ranks: dict[str, int] = {}
    affiliations = [
        a
        for a in ctx.affiliations_by_person.get(person.person_id, ())
        if a.org_id in ctx.firm_by_org
    ]
    for affiliation in sorted(
        affiliations, key=lambda a: (ctx.org_names.get(a.org_id, a.org_id), str(a.start or ""))
    ):
        firm = ctx.firm_by_org[affiliation.org_id]
        current = _is_current(person, affiliation)
        kind: EvidenceKind = "direct_employment" if current else "past_employment"
        weight = ctx.cfg.weights.direct_employment if current else ctx.cfg.weights.past_employment
        ranks[kind] = ranks.get(kind, 0) + 1
        contribution = round(damped(weight, ranks[kind]), 6)
        title = f" as {affiliation.title}" if affiliation.title else ""
        detail = (
            f"Works at {_firm_label(firm)}{title} ({_span(affiliation)}) -- inside the target firm."
            if current
            else f"Worked at {_firm_label(firm)}{title}, {_span(affiliation)} -- a former insider."
        )
        signals.append(
            Signal(
                item=EvidenceItem(
                    kind=kind,
                    detail=detail,
                    contribution=contribution,
                    org_id=affiliation.org_id,
                    hops=0,
                ),
                bucket="direct",
                per_firm=((firm, contribution),),
            )
        )
    return signals


def _colleague_signals(ctx: _Context, person: Person) -> list[Signal]:
    """Shared an employer or a school, at the same time, with a target-firm person.

    Co-tenure is required rather than assumed: ``Affiliation.overlaps`` rejects two
    people who passed through the same place years apart, which Wave 1 measured as
    removing roughly 62% of candidate links while losing no real signal.
    """
    employment: list[Signal] = []
    education: list[Signal] = []
    own = ctx.affiliations_by_person.get(person.person_id, ())
    seen: set[tuple[str, str]] = set()

    def sort_key(affiliation: Affiliation) -> tuple[int, str]:
        # Smallest shared organisation first: two people from a 40-person shop
        # almost certainly know each other, two from a 200,000-person firm do not,
        # and harmonic damping gives the first link listed the largest share.
        return (
            ctx.org_member_counts.get(affiliation.org_id, 0),
            ctx.org_names.get(affiliation.org_id, affiliation.org_id),
        )

    for affiliation in sorted(own, key=sort_key):
        members = ctx.target_members_by_org.get(affiliation.org_id, ())
        if not members:
            continue
        if ctx.org_member_counts.get(affiliation.org_id, 0) > MAX_EVIDENTIAL_ORG_MEMBERS:
            continue
        schooling = affiliation.kind is AffiliationKind.EDUCATION
        bucket: Bucket = "alumni" if schooling else "direct"
        weight = ctx.cfg.weights.alumni_overlap if schooling else ctx.cfg.weights.shared_employer
        kind: EvidenceKind = "shared_school" if schooling else "shared_employer"
        target = education if schooling else employment
        place = ctx.org_names.get(affiliation.org_id, affiliation.org_id)
        for colleague in members:
            if colleague == person.person_id or (affiliation.org_id, colleague) in seen:
                continue
            theirs = [
                a
                for a in ctx.affiliations_by_person.get(colleague, ())
                if a.org_id == affiliation.org_id and affiliation.overlaps(a)
            ]
            if not theirs:
                continue
            seen.add((affiliation.org_id, colleague))
            firms = ctx.firms_by_person.get(colleague, ())
            contribution = round(damped(weight, len(target) + 1), 6)
            firm_names = " and ".join(_firm_label(f) for f in firms)
            verb = "Studied at" if schooling else "Worked at"
            target.append(
                Signal(
                    item=EvidenceItem(
                        kind=kind,
                        detail=(
                            f"{verb} {place} at the same time as "
                            f"{ctx.display(colleague)} ({_span(theirs[0])}), who is at "
                            f"{firm_names or 'a target firm'}."
                        ),
                        contribution=contribution,
                        org_id=affiliation.org_id,
                        via_person_id=colleague,
                        hops=1,
                    ),
                    bucket=bucket,
                    per_firm=tuple((f, contribution) for f in firms),
                )
            )

    return condense(
        employment,
        summary=lambda n, total: (
            f"And {n} further colleagues at shared employers who are now at target firms, "
            f"adding {total:.2f} between them."
        ),
    ) + condense(
        education,
        summary=lambda n, total: (
            f"And {n} further classmates who are now at target firms, "
            f"adding {total:.2f} between them."
        ),
    )


def _split_by_firm(
    contribution: float, masses: Mapping[TargetFirm, float]
) -> tuple[tuple[TargetFirm, float], ...]:
    """Divide one contribution between firms in proportion to their own mass.

    Splitting rather than duplicating, because a single random walk cannot be
    claimed in full by two different firms -- and the split keeps Citadel LLC and
    Citadel Securities as two separate numbers that are never added together.
    """
    total = sum(masses.values())
    if total <= 0:
        return ()
    return tuple(
        (firm, round(contribution * mass / total, 6))
        for firm, mass in sorted(masses.items())
        if mass > 0
    )


def _proximity_signals(ctx: _Context, person_id: str) -> list[Signal]:
    """Personalized-PageRank mass arriving from the target-firm seed set."""
    mass = ctx.ppr.get(person_id, 0.0)
    if mass <= 0.0 or ctx.max_nonseed_ppr <= 0.0:
        return []
    share = min(mass / ctx.max_nonseed_ppr, 1.0)
    contribution = round(ctx.cfg.weights.proximity * share, 6)
    if contribution <= 0.0:
        return []
    multiple = mass * ctx.network.size
    hops = ctx.hops.get(person_id)
    if hops == 0:
        reach = "; they are themselves at a target firm"
    elif hops is not None:
        reach = f"; {hops} introduction{'s' if hops != 1 else ''} from the nearest one"
    else:
        reach = ""
    return [
        Signal(
            item=EvidenceItem(
                kind="path_proximity",
                detail=(
                    f"Network proximity to the target firms: a random walk starting from their "
                    f"employees reaches this person {multiple:.1f} times as often as it reaches "
                    f"the average person in your network{reach}."
                ),
                contribution=contribution,
                hops=hops,
            ),
            bucket="proximity",
            per_firm=_split_by_firm(
                contribution,
                {firm: vector.get(person_id, 0.0) for firm, vector in ctx.ppr_by_firm.items()},
            ),
        )
    ]


def _cluster_signals(ctx: _Context, person_id: str) -> list[Signal]:
    """Membership of a community that sits close to the target firms."""
    cluster = ctx.cluster_by_person.get(person_id)
    if cluster is None or ctx.max_cluster_score <= 0.0:
        return []
    stats = ctx.cluster_stats.get(cluster.cluster_id)
    if stats is None or stats.mean_nonseed_ppr <= 0.0:
        return []
    contribution = round(
        ctx.cfg.weights.cluster * (stats.mean_nonseed_ppr / ctx.max_cluster_score), 6
    )
    if contribution <= 0.0:
        return []
    return [
        Signal(
            item=EvidenceItem(
                kind="cluster_membership",
                detail=(
                    f'In the "{cluster.label}" group, ranked #{stats.rank} of '
                    f"{len(ctx.cluster_stats)} groups for proximity to the target firms: "
                    f"{stats.seed_members} of {cluster.size} members are at a target firm and "
                    f"{stats.adjacent_members} more are one introduction away."
                ),
                contribution=contribution,
            ),
            bucket="cluster",
            per_firm=_split_by_firm(contribution, stats.per_firm),
        )
    ]


def _title_signals(ctx: _Context, person: Person, *, has_link: bool) -> list[Signal]:
    """A role these firms recruit from -- a tie-breaker, never a reason on its own."""
    if not has_link:
        return []
    titles = [p.title_raw.strip() for p in person.positions if p.title_raw.strip()]
    current = [
        p.title_raw.strip() for p in person.positions if p.is_current and p.title_raw.strip()
    ]
    for title in [*current, *titles]:
        folded = normalize_key(title)
        if any(keyword in folded for keyword in TITLE_KEYWORDS):
            contribution = round(ctx.cfg.weights.title_signal, 6)
            return [
                Signal(
                    item=EvidenceItem(
                        kind="title_signal",
                        detail=(
                            f'Their role, "{title}", is the kind these firms recruit from. '
                            "On its own a job title says nothing about whose network someone "
                            "is in, so this only adds on top of the links above."
                        ),
                        contribution=contribution,
                    ),
                    bucket="title",
                )
            ]
    return []


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def _score_person(ctx: _Context, person: Person) -> ScoredPerson:
    """Turn one person's signals into a score, its components and its audit trail.

    Deliberately built evidence-first: the numeric components are *sums of the
    evidence items*, so there is no arrangement of this code in which the score
    moves without a sentence in ``evidence`` explaining the movement.
    """
    signals: list[Signal] = []
    signals += _observed_signals(ctx, person.person_id)
    signals += _employment_signals(ctx, person)
    signals += _colleague_signals(ctx, person)
    signals += _proximity_signals(ctx, person.person_id)
    signals += _cluster_signals(ctx, person.person_id)
    signals += _title_signals(ctx, person, has_link=any(s.item.contribution > 0.0 for s in signals))

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


def _rank_clusters(ctx: _Context, people: Sequence[ScoredPerson]) -> list[ScoredCluster]:
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
    ctx = _build_context(
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
