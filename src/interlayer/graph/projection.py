"""Bipartite person-org projection, corrected so that size does not decide it.

The projection is ``P = B B^T`` restricted to people, with each pairwise
contribution weighted by :mod:`interlayer.graph.weights` rather than counted.
It iterates **per org over its own members**, never over all pairs of people:
the cost is ``sum_f n_f^2 / 2``, which for a realistic export (a power-law firm
distribution over a few thousand people) is small, where the all-pairs form is
not.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from interlayer.graph.sizes import FirmSizes
from interlayer.graph.weights import (
    MAX_INFERRED_WEIGHT,
    SizeDamping,
    Stint,
    cotenure_factor,
    end_exclusive_ordinal,
    newman_factor,
)
from interlayer.models import Affiliation, AffiliationKind, Org

__all__ = ["OrgProfile", "PairEvidence", "Projection", "project"]

Pair = tuple[str, str]


@dataclass
class PairEvidence:
    """Accumulated inferred evidence for one unordered pair of people."""

    weight: float = 0.0
    org_ids: set[str] = field(default_factory=set)
    kinds: set[AffiliationKind] = field(default_factory=set)


@dataclass(frozen=True)
class OrgProfile:
    """What one org contributed, for the weight-concentration diagnostics."""

    org_id: str
    name: str
    members: int
    headcount: int | None
    headcount_from_gazetteer: bool
    damping: float
    weight: float


@dataclass
class Projection:
    """Inferred pairs plus the counters that explain how they were reached."""

    pairs: dict[Pair, PairEvidence] = field(default_factory=dict)
    people: set[str] = field(default_factory=set)
    orgs: dict[str, OrgProfile] = field(default_factory=dict)
    n_affiliations: int = 0
    pairs_considered: int = 0
    pairs_dropped_cotenure: int = 0
    pairs_dropped_zero_weight: int = 0
    invalid_dates: int = 0

    @property
    def total_weight(self) -> float:
        return sum(e.weight for e in self.pairs.values())


def _as_of_ordinal(affiliations: Iterable[Affiliation]) -> int | None:
    """The dataset's own "now": the latest date anyone recorded.

    Derived from the data rather than the clock so that a run is reproducible --
    reading ``date.today()`` here would make yesterday's artifacts fail to
    reproduce today, which is the property the whole pipeline is built around.
    """
    latest: int | None = None
    for affiliation in affiliations:
        for bound in (affiliation.end, affiliation.start):
            if bound is None:
                continue
            try:
                ordinal = end_exclusive_ordinal(bound)
            except ValueError:
                continue
            latest = ordinal if latest is None else max(latest, ordinal)
    return latest


def _group_by_org(
    affiliations: Iterable[Affiliation],
) -> tuple[dict[str, dict[str, list[Stint]]], int]:
    """org_id -> person_id -> stints, built in sorted order so ties are stable.

    Grouping by person inside the org is what makes self-loops impossible: a
    person with two stints at one employer is one key, and pairs are drawn from
    distinct keys only. ``GraphEdge`` raises on a self-loop, so this is the
    guard, not a nicety.
    """
    groups: defaultdict[str, defaultdict[str, list[Stint]]] = defaultdict(lambda: defaultdict(list))
    invalid = 0
    ordered = sorted(
        affiliations,
        key=lambda a: (a.org_id, a.person_id, str(a.start or ""), str(a.end or ""), str(a.kind)),
    )
    as_of = _as_of_ordinal(ordered)
    for affiliation in ordered:
        stint = Stint.of(affiliation, as_of=as_of)
        invalid += stint.invalid_dates
        groups[affiliation.org_id][affiliation.person_id].append(stint)
    return {org: dict(people) for org, people in groups.items()}, invalid


def project(
    affiliations: Iterable[Affiliation],
    *,
    orgs: Mapping[str, Org],
    sizes: FirmSizes,
    size_damping: SizeDamping,
    require_cotenure: bool,
    missing_date_factor: float,
    max_overlap_years: float,
) -> Projection:
    """Weighted projection of the affiliation bipartite graph onto people.

    Every pair that survives contributes
    ``1/(n_f - 1) x cotenure x 1/log(size_f + e) x min(affiliation weights)``
    from each org they share. The affiliation weights are Agent 2's confidence in
    the underlying match, and a pair is only as strong as its weaker side, so the
    minimum is taken rather than the product -- two independently 0.5-confident
    matches are not 0.25-confident evidence.
    """
    grouped, invalid_dates = _group_by_org(affiliations)
    out = Projection(invalid_dates=invalid_dates)
    out.n_affiliations = sum(len(s) for people in grouped.values() for s in people.values())

    for org_id in sorted(grouped):
        members = grouped[org_id]
        person_ids = sorted(members)
        out.people.update(person_ids)
        n_f = len(person_ids)
        org = orgs.get(org_id)
        gazetteer_size = sizes.headcount(org) if org is not None else None
        # Fallback bias is documented in sizes.py: n_f is a lower bound on the
        # true headcount, so unknown orgs are under-damped, never over-damped.
        size = float(gazetteer_size if gazetteer_size is not None else n_f)
        damping = size_damping(size)
        base = newman_factor(n_f) * damping
        org_weight = 0.0

        if n_f > 1 and base > 0.0:
            for i in range(n_f):
                left = person_ids[i]
                left_stints = members[left]
                for j in range(i + 1, n_f):
                    right = person_ids[j]
                    out.pairs_considered += 1
                    best = 0.0
                    kinds: set[AffiliationKind] = set()
                    for a in left_stints:
                        for b in members[right]:
                            factor = cotenure_factor(
                                a,
                                b,
                                require_cotenure=require_cotenure,
                                missing_date_factor=missing_date_factor,
                                max_overlap_years=max_overlap_years,
                            )
                            if factor <= 0.0:
                                continue
                            factor *= min(a.affiliation.weight, b.affiliation.weight)
                            best = max(best, factor)
                            # Only the stint pairs that actually co-occurred name
                            # the edge: two people at one org, one employed and
                            # one enrolled, really do share both contexts.
                            kinds.add(a.affiliation.kind)
                            kinds.add(b.affiliation.kind)
                    if best <= 0.0:
                        # ``kinds`` is only touched by a stint pair that did
                        # co-tenure, so it distinguishes "never overlapped" from
                        # "overlapped but Agent 2 zero-weighted the match".
                        if kinds:
                            out.pairs_dropped_zero_weight += 1
                        else:
                            out.pairs_dropped_cotenure += 1
                        continue
                    contribution = base * best
                    org_weight += contribution
                    evidence = out.pairs.get((left, right))
                    if evidence is None:
                        evidence = PairEvidence()
                        out.pairs[(left, right)] = evidence
                    evidence.weight += contribution
                    evidence.org_ids.add(org_id)
                    evidence.kinds |= kinds

        out.orgs[org_id] = OrgProfile(
            org_id=org_id,
            name=org.name if org is not None else "",
            members=n_f,
            headcount=gazetteer_size,
            headcount_from_gazetteer=gazetteer_size is not None,
            damping=damping,
            weight=org_weight,
        )

    for evidence in out.pairs.values():
        evidence.weight = min(evidence.weight, MAX_INFERRED_WEIGHT)
    return out
