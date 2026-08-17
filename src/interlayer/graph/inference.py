"""The inference fallback: co-affiliation cues -> a calibrated confidence.

When a target's mutual-connections list cannot be harvested, adjacency can be
*guessed* from shared attributes. It must never be presented as an observation
(PRIV-19), so every edge this module produces carries
``origin=EdgeOrigin.INFERRED``, a confidence below 1.0, and a mandatory
``evidence`` string — an unexplained score is indistinguishable from a fabrication.

**The model.** Additive log-odds over inverse-frequency-damped cues::

    z = -3.0                                            # base rate, P ~ 0.047
    if same_employer and tenure_overlap > 0:
        z += 2.2 * min(overlap, 4)/4 * sqrt(log(100)/log(max(employer_size, e)))
    if same_school and year_overlap > 0:
        z += 1.4 * min(overlap, 4)/4 * sqrt(log(500)/log(max(cohort_size, e)))
    if same_city:     z += 0.5 * city_pop_bucket
    if shared_groups: z += 0.3 * min(shared_groups, 3)
    p = sigmoid(z)

Log-odds because the cues are conditionally near-independent and it keeps the
output a probability. Inverse-frequency damping because "we both worked at a
50,000-person company" is nearly worthless while "we both worked at an 80-person
startup" is strong evidence — the same rare-feature logic as RA weighting.
Employer overlap gets roughly double the weight of school overlap, which is
itself far stronger than co-location, following the homophily literature on focal
closure.

The coefficients are a documented prior, not a fit: we have no labelled data.
They are chosen so that **no single weak cue crosses the default
``min_confidence=0.5`` gate** — only a genuine multi-cue coincidence does. City
alone scores 0.076, i.e. near-noise. Tenure saturates at 4 years because marginal
returns beyond "we overlapped for a few years" are negligible.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from interlayer.core.models import Edge, EdgeOrigin, Provenance

BASE_LOG_ODDS = -3.0
EMPLOYER_COEFFICIENT = 2.2
SCHOOL_COEFFICIENT = 1.4
CITY_COEFFICIENT = 0.5
GROUPS_COEFFICIENT = 0.3
TENURE_SATURATION_YEARS = 4.0
GROUPS_SATURATION = 3

#: Reference sizes for the inverse-frequency damping. A 100-person employer and a
#: 500-person cohort score 1.0 damping; larger organisations score less.
EMPLOYER_REFERENCE = 100.0
SCHOOL_REFERENCE = 500.0

DEFAULT_EMPLOYER_SIZE = 100.0
DEFAULT_COHORT_SIZE = 500.0


@dataclass(frozen=True, slots=True)
class Cues:
    """Co-affiliation evidence for one (member, target) pair.

    A separate record rather than fields on ``Member``/``Target`` because the
    frozen contract deliberately does not persist school, city or tenure — see the
    field allowlists in ``core.models``. Cues are computed at inference time from
    whatever the caller has, and are never stored.
    """

    same_employer: bool = False
    tenure_overlap_years: float = 0.0
    employer_size: int | None = None
    same_school: bool = False
    school_overlap_years: float = 0.0
    cohort_size: int | None = None
    same_city: bool = False
    city_pop_bucket: float = 1.0
    shared_groups: int = 0

    @property
    def any_cue(self) -> bool:
        return bool(
            (self.same_employer and self.tenure_overlap_years > 0)
            or (self.same_school and self.school_overlap_years > 0)
            or self.same_city
            or self.shared_groups
        )


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _damping(size: float | None, default: float, reference: float) -> float:
    """``sqrt(log(reference) / log(max(size, e)))``.

    ``max(size, e)`` guards ``log`` at and below 1; the value is monotonically
    decreasing in ``size``, which is the property T6.4 asserts.
    """
    value = float(default if size is None else size)
    return math.sqrt(math.log(reference) / math.log(max(value, math.e)))


def infer_edge_confidence(cues: Cues) -> float:
    """Probability that this member and target are connected, given the cues."""
    z = BASE_LOG_ODDS

    if cues.same_employer and cues.tenure_overlap_years > 0:
        saturated = min(cues.tenure_overlap_years, TENURE_SATURATION_YEARS)
        z += (
            EMPLOYER_COEFFICIENT
            * (saturated / TENURE_SATURATION_YEARS)
            * _damping(cues.employer_size, DEFAULT_EMPLOYER_SIZE, EMPLOYER_REFERENCE)
        )

    if cues.same_school and cues.school_overlap_years > 0:
        saturated = min(cues.school_overlap_years, TENURE_SATURATION_YEARS)
        z += (
            SCHOOL_COEFFICIENT
            * (saturated / TENURE_SATURATION_YEARS)
            * _damping(cues.cohort_size, DEFAULT_COHORT_SIZE, SCHOOL_REFERENCE)
        )

    if cues.same_city:
        z += CITY_COEFFICIENT * cues.city_pop_bucket

    if cues.shared_groups:
        z += GROUPS_COEFFICIENT * min(cues.shared_groups, GROUPS_SATURATION)

    return _sigmoid(z)


def evidence_for(cues: Cues) -> str:
    """Human-readable justification. Mandatory on every inferred edge."""
    parts: list[str] = []
    if cues.same_employer and cues.tenure_overlap_years > 0:
        size = "unknown size" if cues.employer_size is None else f"~{cues.employer_size} staff"
        parts.append(f"shared employer, {cues.tenure_overlap_years:g}y overlap ({size})")
    if cues.same_school and cues.school_overlap_years > 0:
        size = "unknown cohort" if cues.cohort_size is None else f"~{cues.cohort_size} cohort"
        parts.append(f"shared school, {cues.school_overlap_years:g}y overlap ({size})")
    if cues.same_city:
        parts.append("same city")
    if cues.shared_groups:
        parts.append(f"{cues.shared_groups} shared group(s)")
    if not parts:
        return "no co-affiliation cues; base rate only"
    return "inferred from " + "; ".join(parts)


def infer_edge(
    member_id: str,
    target_id: str,
    cues: Cues,
    *,
    provenance: Provenance | None = None,
) -> Edge:
    """Build one INFERRED edge. Never returns an observed one."""
    return Edge(
        member_id=member_id,
        target_id=target_id,
        origin=EdgeOrigin.INFERRED,
        confidence=infer_edge_confidence(cues),
        evidence=evidence_for(cues),
        provenance=provenance,
    )


def infer_edges(
    candidates: Iterable[tuple[str, str, Cues]],
    *,
    min_confidence: float = 0.0,
    provenance: Provenance | None = None,
) -> tuple[Edge, ...]:
    """Batch inference, sorted by ``(member_id, target_id)`` for determinism.

    ``min_confidence`` here is an optional pre-filter; the authoritative gate is
    the one in :func:`interlayer.graph.build.select_edges`, so that a caller
    cannot slip a low-confidence guess past the pipeline by constructing edges
    itself.
    """
    edges = [
        infer_edge(member_id, target_id, cues, provenance=provenance)
        for member_id, target_id, cues in candidates
    ]
    kept = [e for e in edges if e.confidence >= min_confidence]
    kept.sort(key=lambda e: (e.member_id, e.target_id))
    return tuple(kept)


__all__ = [
    "BASE_LOG_ODDS",
    "CITY_COEFFICIENT",
    "EMPLOYER_COEFFICIENT",
    "EMPLOYER_REFERENCE",
    "GROUPS_COEFFICIENT",
    "SCHOOL_COEFFICIENT",
    "SCHOOL_REFERENCE",
    "TENURE_SATURATION_YEARS",
    "Cues",
    "evidence_for",
    "infer_edge",
    "infer_edge_confidence",
    "infer_edges",
]
