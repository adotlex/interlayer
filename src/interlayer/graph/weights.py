"""Edge weighting -- the part of this stage that actually decides the answer.

Wave 1 measured that a *naive* bipartite projection is decided entirely by
whichever employer happens to be biggest: on a simulated 1,200-person export a
single 200,000-person firm produced **65% of all edges and 60.8% of all edge
weight**, at density 0.209 -- an order of magnitude denser than any real social
network. Clustering that graph recovers "the people who worked at big
companies", which is worthless. Three multiplicative corrections, applied once
per shared organisation, cut that firm's weight share to **5.4%** -- a 33x
reweighting toward small, informative employers:

``w_ij(f) = 1/(n_f - 1)  x  cotenure(i, j, f)  x  1/log(size_f + e)``

* ``1/(n_f - 1)`` -- Newman collaboration weighting (Newman 2001). An
  affiliation shared by n people is a clique of n(n-1)/2 edges, so without this
  term group size enters the projection quadratically.
* ``cotenure`` -- two people who worked somewhere five years apart have
  essentially no chance of knowing each other. Requiring interval intersection
  removed ~62% of candidate edges in the research with no loss of real signal.
* ``1/log(size_f + e)`` -- ``n_f`` counts people *in this export*, which is the
  wrong denominator: 20 connections at a 40-person startup all know each other,
  20 at a 200,000-person firm mostly do not. ``log`` rather than ``1/size``
  because ``1/size`` is too brutal -- it would give a 200k-person firm a weight
  of 5e-6 and delete an edge between two people who may have shared a 10-person
  team. ``1/log`` compresses a 5,000x size range into roughly a 2x weight range.

**The damping form is a judgement call, not a derived result** (research 02
Sec. 1.4 says so in as many words), so it is a named, swappable strategy rather
than an expression buried inside the projection loop. Swap it by passing
``size_damping=`` to the builder; ``Settings`` has no field for it yet, and
adding one is a scaffold change this stage may not make unilaterally.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import date
from typing import NamedTuple

from interlayer.errors import GraphError
from interlayer.models import Affiliation, ApproxDate

__all__ = [
    "DAYS_PER_YEAR",
    "DEFAULT_SIZE_DAMPING",
    "MAX_INFERRED_WEIGHT",
    "OBSERVED_FLOOR",
    "OBSERVED_SPAN",
    "SIZE_DAMPINGS",
    "SizeDamping",
    "Stint",
    "cotenure_factor",
    "end_exclusive_ordinal",
    "newman_factor",
    "observed_bonus",
    "resolve_size_damping",
]

DAYS_PER_YEAR = 365.25

MAX_INFERRED_WEIGHT = 1.0
"""Ceiling on any single inferred edge.

Each per-org term is bounded by 1.0 (``n_f >= 2`` so Newman <= 1, cotenure <= 1,
damping <= 1), but a pair sharing several orgs sums several terms. Clamping here
is what makes ``OBSERVED_FLOOR`` a *provable* separation rather than a hope.
"""

OBSERVED_FLOOR = 1.5
"""Floor for an edge backed by a human-read mutual-connections list.

Strictly above ``MAX_INFERRED_WEIGHT`` so that observed evidence outranks every
possible inference, mirroring ``ScoreWeights.observed_mutual`` (10.0) sitting
above ``direct_employment`` (6.0) downstream.
"""

OBSERVED_SPAN = 1.0
"""Per-observation bonus scale; see :func:`observed_bonus`."""

DEFAULT_SIZE_DAMPING = "log"

SizeDamping = Callable[[float], float]
"""A firm-size damping strategy: real-world headcount -> multiplier in (0, 1]."""


def log_damping(size: float) -> float:
    """``1/log(size + e)`` -- the researched default.

    ``+e`` keeps the denominator at or above 1, so the multiplier never exceeds
    1.0 and a size of 0 (unknown or defunct) degrades to "no damping" instead of
    exploding.
    """
    return 1.0 / math.log(max(size, 0.0) + math.e)


def inverse_damping(size: float) -> float:
    """``1/(1 + size)``. Kept as the deliberately brutal end of the range."""
    return 1.0 / (1.0 + max(size, 0.0))


def sqrt_damping(size: float) -> float:
    """``1/sqrt(1 + size)``. Between :func:`log_damping` and :func:`inverse_damping`."""
    return 1.0 / math.sqrt(1.0 + max(size, 0.0))


def no_damping(_size: float) -> float:
    """Newman weighting only -- the ablation the research measured at 15.9%."""
    return 1.0


SIZE_DAMPINGS: Mapping[str, SizeDamping] = {
    "log": log_damping,
    "sqrt": sqrt_damping,
    "inverse": inverse_damping,
    "none": no_damping,
}


def resolve_size_damping(name: str | SizeDamping | None) -> SizeDamping:
    """Look a damping strategy up by name, or pass a callable straight through."""
    if name is None:
        return SIZE_DAMPINGS[DEFAULT_SIZE_DAMPING]
    if callable(name):
        return name
    try:
        return SIZE_DAMPINGS[name]
    except KeyError:
        known = ", ".join(sorted(SIZE_DAMPINGS))
        raise GraphError(f"unknown size damping {name!r}; known strategies: {known}") from None


def newman_factor(n_f: int) -> float:
    """``1/(n_f - 1)``: Newman's group-size correction, 0 for a group of one."""
    return 1.0 / (n_f - 1) if n_f > 1 else 0.0


def observed_bonus(list_size: int) -> float:
    """Weight one mutual-connections *reading* contributes to each of its pairs.

    A reading of k mutuals asserts k(k-1)/2 pairwise "shared context" edges, so
    it has exactly the clique problem the inferred side is corrected for: two
    people who are the only two mutuals of a target share far more context than
    two of ninety. Damping is logarithmic rather than ``1/(k-1)`` because these
    edges must stay above every inferred edge no matter how long the list is --
    Newman damping on a 90-name list would push ground truth below inference.
    """
    if list_size < 2:
        return 0.0
    return OBSERVED_SPAN / math.log(list_size + math.e)


def _start_ordinal(value: ApproxDate) -> int:
    return value.to_date().toordinal()


def end_exclusive_ordinal(value: ApproxDate) -> int:
    """Half-open end of a coarse date: ``2019`` ends 2020-01-01, not 2019-01-01.

    LinkedIn gives year-only dates far more often than full ones. Treating
    ``2019`` as the instant 2019-01-01 would make two people who both worked
    somewhere throughout 2019 share zero days, and drop a real edge.
    """
    if value.month is None:
        return date(value.year + 1, 1, 1).toordinal()
    if value.day is None:
        year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
        return date(year, month, 1).toordinal()
    return value.to_date().toordinal() + 1


OPEN_ENDED = date.max.toordinal()
"""Right bound for a role that is still current and has no as-of date.

Not ``date.today()``: reading the clock here would mean the same input produced
a different graph tomorrow, and reproducibility is the property the pipeline is
built on.
"""


class Stint(NamedTuple):
    """An affiliation plus its precomputed day-ordinal span.

    The span is computed once per affiliation rather than once per candidate
    pair: the projection is quadratic in the size of each org, so anything done
    inside the pair loop is done a hundred thousand times on a real export.
    """

    affiliation: Affiliation
    span: tuple[int, int] | None
    invalid_dates: bool = False

    @classmethod
    def of(cls, affiliation: Affiliation, *, as_of: int | None = None) -> Stint:
        """Build a stint, closing an open current role at ``as_of``.

        A role marked current has no end date by definition, which is not the
        same as having an unknown one. Treating the two alike damped two people
        who work at the same firm *right now* as though nobody knew when either
        was there -- and those are precisely the highest-value edges this tool
        exists to surface.

        ``as_of`` is supplied by the caller from the data rather than read from
        the clock, so the same input keeps producing the same graph tomorrow.
        """
        start, end = affiliation.start, affiliation.end
        if start is not None and end is None and affiliation.is_current:
            try:
                begin = _start_ordinal(start)
            except ValueError:
                return cls(affiliation, None, invalid_dates=True)
            # With no as-of supplied, a current role is simply open on the
            # right: it has not ended. The overlap it produces is then bounded by
            # max_overlap_years rather than by a guess at today's date.
            close = OPEN_ENDED if as_of is None else max(begin + 1, as_of)
            return cls(affiliation, (begin, close))
        if start is None or end is None:
            return cls(affiliation, None)
        try:
            return cls(affiliation, (_start_ordinal(start), end_exclusive_ordinal(end)))
        except ValueError:
            # e.g. day=31 in a 30-day month: ApproxDate validates ranges, not
            # calendars. One malformed stint must not abort the whole run, so it
            # is demoted to "undated" and counted in the diagnostics.
            return cls(affiliation, None, invalid_dates=True)


def cotenure_factor(
    a: Stint,
    b: Stint,
    *,
    require_cotenure: bool,
    missing_date_factor: float,
    max_overlap_years: float,
) -> float:
    """Co-tenure multiplier in [0, 1]; 0 means "emit no edge from this pair".

    Saturating rather than linear: eight years together is not eight times
    stronger evidence than one, so overlap is capped at
    ``cfg.max_overlap_years``. Missing dates are *damped, never dropped* --
    dropping loses real people, and full credit re-creates the clique blowup
    this module exists to prevent.

    ``Affiliation.overlaps`` is the authoritative gate; the span arithmetic only
    sizes an overlap that helper has already admitted. The two differ in one
    direction only: ``overlaps`` compares ``sort_key`` instants, so it reads
    year-only ``2019`` as the instant 2019-01-01 and can therefore reject a pair
    whose spans really did intersect (``2019`` against ``2019-06``..``2020``).
    Positive span overlap is implied by ``overlaps``, never the reverse, so
    deferring to the shared helper is the conservative choice -- it drops a
    handful of mixed-precision edges rather than inventing any. The cheap integer
    test runs first purely to skip the helper on pairs it must reject anyway.
    """
    if not require_cotenure:
        return 1.0
    if a.span is not None and b.span is not None:
        overlap_days = min(a.span[1], b.span[1]) - max(a.span[0], b.span[0])
        if overlap_days <= 0 or not a.affiliation.overlaps(b.affiliation):
            return 0.0
        return min(overlap_days / DAYS_PER_YEAR, max_overlap_years) / max_overlap_years
    if not a.affiliation.overlaps(b.affiliation):
        return 0.0
    return missing_date_factor
