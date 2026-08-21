"""Unit tests for :mod:`interlayer.graph.projection`.

The headline test in this file is
:func:`test_one_huge_employer_stops_dominating_the_edge_weight`. Research 02
Sec. 1.2-1.4 measured a naive bipartite projection handing **65% of all edges**
and **60.8% of all edge weight** to a single 200,000-person employer, and the
three corrections cutting that to 5.4%. Everything else here pins one
correction at a time so that a regression names its own cause.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from itertools import pairwise

import pytest
from pydantic import ValidationError

from interlayer.graph.projection import Projection, project
from interlayer.graph.sizes import FirmSizes
from interlayer.graph.weights import (
    MAX_INFERRED_WEIGHT,
    SizeDamping,
    log_damping,
    no_damping,
)
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Org,
    OrgKind,
    normalize_key,
)

CAP = 5.0
MISSING = 0.3

# The whole tenure window every synthetic person shares unless a test says
# otherwise, so co-tenure is a constant and cannot confound a weighting test.
SHARED_START = ApproxDate(year=2010, month=1, day=1)
SHARED_END = ApproxDate(year=2019, month=12, day=31)


def _org(name: str) -> Org:
    return Org.make(name, OrgKind.COMPANY)


def _aff(
    person: str,
    org: Org,
    *,
    start: ApproxDate | None = SHARED_START,
    end: ApproxDate | None = SHARED_END,
    kind: AffiliationKind = AffiliationKind.EMPLOYMENT,
    weight: float = 1.0,
) -> Affiliation:
    return Affiliation(
        person_id=person,
        org_id=org.org_id,
        kind=kind,
        start=start,
        end=end,
        weight=weight,
    )


def _sizes(**headcounts: int) -> FirmSizes:
    """Gazetteer headcounts keyed by org *name*, the way ``FirmSizes`` resolves them."""
    return FirmSizes({}, {normalize_key(name): size for name, size in headcounts.items()})


def _project(
    affiliations: list[Affiliation],
    orgs: list[Org],
    *,
    sizes: FirmSizes | None = None,
    size_damping: SizeDamping = log_damping,
    require_cotenure: bool = True,
    missing_date_factor: float = MISSING,
    max_overlap_years: float = CAP,
) -> Projection:
    return project(
        affiliations,
        orgs={org.org_id: org for org in orgs},
        sizes=sizes if sizes is not None else FirmSizes({}, {}),
        size_damping=size_damping,
        require_cotenure=require_cotenure,
        missing_date_factor=missing_date_factor,
        max_overlap_years=max_overlap_years,
    )


def _pair_weight(projection: Projection, left: str, right: str) -> float:
    key = (left, right) if left < right else (right, left)
    return projection.pairs[key].weight


def _staff(org: Org, count: int, *, prefix: str) -> list[Affiliation]:
    return [_aff(f"{prefix}{i:03d}", org) for i in range(count)]


# ---------------------------------------------------------------------------
# Newman 1/(n_f - 1)
# ---------------------------------------------------------------------------

GROUP_SIZES = (2, 3, 5, 10, 50, 200)


def test_an_edge_from_a_two_person_firm_outweighs_one_from_a_two_hundred_person_firm() -> None:
    """Newman weighting, isolated: same headcount, same tenure, only ``n_f`` differs."""
    orgs = [_org(f"Firm{n:03d}") for n in GROUP_SIZES]
    affiliations: list[Affiliation] = []
    for org, size in zip(orgs, GROUP_SIZES, strict=True):
        affiliations += _staff(org, size, prefix=f"{org.name}_p")

    # Fixed headcount for every firm, so the size-damping term is a constant.
    projection = _project(
        affiliations,
        orgs,
        sizes=_sizes(**{org.name: 500 for org in orgs}),
    )

    weights = [_pair_weight(projection, f"{org.name}_p000", f"{org.name}_p001") for org in orgs]
    assert all(x > y for x, y in pairwise(weights)), dict(zip(GROUP_SIZES, weights, strict=True))
    assert weights[0] / weights[-1] == pytest.approx(199.0, rel=1e-6)


def test_a_firm_of_one_contributes_no_pair_but_still_contributes_a_node() -> None:
    solo = _org("Solo")
    projection = _project(_staff(solo, 1, prefix="s"), [solo])
    assert projection.pairs == {}
    assert projection.people == {"s000"}
    assert projection.orgs[solo.org_id].members == 1
    assert projection.orgs[solo.org_id].weight == 0.0


def test_total_weight_a_firm_contributes_does_not_grow_quadratically() -> None:
    """The clique blowup is exactly what Newman weighting removes.

    Naive projection gives a firm ``n(n-1)/2`` unit edges; Newman gives it
    ``n/2`` total. A 200-person firm must not be 100x a 20-person firm.
    """
    totals: list[float] = []
    for size in (20, 200):
        org = _org(f"Firm{size}")
        projection = _project(_staff(org, size, prefix="p"), [org], sizes=_sizes(**{org.name: 500}))
        totals.append(projection.orgs[org.org_id].weight)
    naive_ratio = (200 * 199) / (20 * 19)
    assert totals[1] / totals[0] == pytest.approx(10.0, rel=1e-6)
    assert totals[1] / totals[0] < naive_ratio / 5


# ---------------------------------------------------------------------------
# firm-size damping
# ---------------------------------------------------------------------------

HEADCOUNTS = (40, 400, 3_700, 40_000, 200_000)


def test_edge_weight_falls_as_the_real_firm_gets_bigger() -> None:
    """Twenty people at a 40-person startup all know each other; at a bank they do not.

    Every firm here has the same number of people *in the export*, so Newman
    weighting is identical and only the gazetteer headcount moves.
    """
    orgs = [_org(f"Firm{h}") for h in HEADCOUNTS]
    affiliations: list[Affiliation] = []
    for org in orgs:
        affiliations += _staff(org, 6, prefix=f"{org.name}_p")

    projection = _project(
        affiliations,
        orgs,
        sizes=_sizes(**{org.name: h for org, h in zip(orgs, HEADCOUNTS, strict=True)}),
    )
    weights = [_pair_weight(projection, f"{org.name}_p000", f"{org.name}_p001") for org in orgs]
    assert all(x > y for x, y in pairwise(weights)), dict(zip(HEADCOUNTS, weights, strict=True))
    assert weights[0] > weights[-1]


def test_an_org_the_gazetteer_does_not_know_falls_back_to_members_in_export() -> None:
    """Documented one-directional bias: unknown orgs are under-damped, never over-damped."""
    known = _org("KnownCo")
    unknown = _org("UnknownCo")
    affiliations = _staff(known, 8, prefix="k") + _staff(unknown, 8, prefix="u")
    projection = _project(affiliations, [known, unknown], sizes=_sizes(KnownCo=200_000))

    known_profile = projection.orgs[known.org_id]
    unknown_profile = projection.orgs[unknown.org_id]
    assert known_profile.headcount_from_gazetteer is True
    assert known_profile.headcount == 200_000
    assert unknown_profile.headcount_from_gazetteer is False
    assert unknown_profile.headcount is None
    assert unknown_profile.damping == pytest.approx(log_damping(8.0))
    assert unknown_profile.damping > known_profile.damping


# ---------------------------------------------------------------------------
# co-tenure filtering, at projection level
# ---------------------------------------------------------------------------


def test_non_overlapping_colleagues_get_no_edge_and_are_counted() -> None:
    org = _org("Sequential")
    affiliations = [
        _aff("early", org, start=ApproxDate(year=2001), end=ApproxDate(year=2003)),
        _aff("late", org, start=ApproxDate(year=2015), end=ApproxDate(year=2018)),
    ]
    projection = _project(affiliations, [org])
    assert projection.pairs == {}
    assert projection.pairs_considered == 1
    assert projection.pairs_dropped_cotenure == 1
    assert projection.pairs_dropped_zero_weight == 0
    # The people still exist; only the relationship was rejected.
    assert projection.people == {"early", "late"}


def test_overlapping_colleagues_get_an_edge_naming_the_shared_org() -> None:
    org = _org("Overlapping")
    projection = _project(_staff(org, 2, prefix="p"), [org])
    evidence = projection.pairs[("p000", "p001")]
    assert evidence.weight > 0.0
    assert evidence.org_ids == {org.org_id}
    assert evidence.kinds == {AffiliationKind.EMPLOYMENT}


def test_undated_colleagues_get_a_damped_edge_rather_than_none() -> None:
    org = _org("Undated")
    dated = _project(_staff(org, 2, prefix="p"), [org])
    undated = _project(
        [_aff("p000", org, start=None, end=None), _aff("p001", org, start=None, end=None)],
        [org],
    )
    assert ("p000", "p001") in undated.pairs
    assert undated.pairs[("p000", "p001")].weight == pytest.approx(
        dated.pairs[("p000", "p001")].weight * MISSING
    )


def test_require_cotenure_false_admits_the_pairs_the_filter_would_reject() -> None:
    org = _org("Sequential")
    affiliations = [
        _aff("early", org, start=ApproxDate(year=2001), end=ApproxDate(year=2003)),
        _aff("late", org, start=ApproxDate(year=2015), end=ApproxDate(year=2018)),
    ]
    projection = _project(affiliations, [org], require_cotenure=False)
    assert ("early", "late") in projection.pairs
    assert projection.pairs_dropped_cotenure == 0


def test_a_zero_weighted_affiliation_match_is_counted_separately_from_no_cotenure() -> None:
    """ "Agent 2 had no confidence in this match" is a different failure from
    "these two were never there together", and the diagnostics must not conflate them."""
    org = _org("LowConfidence")
    affiliations = [
        _aff("p000", org, weight=0.0),
        _aff("p001", org, weight=1.0),
    ]
    projection = _project(affiliations, [org])
    assert projection.pairs == {}
    assert projection.pairs_dropped_zero_weight == 1
    assert projection.pairs_dropped_cotenure == 0


def test_a_pair_is_only_as_strong_as_its_weaker_affiliation() -> None:
    org = _org("Mixed")
    strong = _project(_staff(org, 2, prefix="p"), [org])
    weak = _project(
        [_aff("p000", org, weight=0.5), _aff("p001", org, weight=1.0)],
        [org],
    )
    assert weak.pairs[("p000", "p001")].weight == pytest.approx(
        strong.pairs[("p000", "p001")].weight * 0.5
    )


def test_calendar_impossible_dates_are_rejected_before_the_projection() -> None:
    """31 February can no longer be constructed, so the counter stays at zero.

    ``ApproxDate`` rejects it at construction now; the projection's invalid-date
    counter remains for artifacts written by older versions.
    """
    with pytest.raises(ValidationError):
        ApproxDate(year=2019, month=2, day=31)

    org = _org("BadDates")
    affiliations = [
        _aff(
            "p000",
            org,
            start=ApproxDate(year=2019, month=2, day=28),
            end=ApproxDate(year=2019, month=6, day=1),
        ),
        _aff("p001", org),
    ]
    projection = _project(affiliations, [org])
    assert projection.invalid_dates == 0
    assert ("p000", "p001") in projection.pairs


def test_overlap_years_saturate_inside_the_projection() -> None:
    """Ten years of shared tenure is not twice the edge of five."""
    org = _org("LongHaul")
    five = _project(
        [
            _aff(
                p,
                org,
                start=ApproxDate(year=2000, month=1, day=1),
                end=ApproxDate(year=2004, month=12, day=31),
            )
            for p in ("p000", "p001")
        ],
        [org],
    )
    ten = _project(
        [
            _aff(
                p,
                org,
                start=ApproxDate(year=2000, month=1, day=1),
                end=ApproxDate(year=2009, month=12, day=31),
            )
            for p in ("p000", "p001")
        ],
        [org],
    )
    assert ten.pairs[("p000", "p001")].weight == pytest.approx(five.pairs[("p000", "p001")].weight)


# ---------------------------------------------------------------------------
# structural guarantees
# ---------------------------------------------------------------------------


def test_a_person_with_several_stints_at_one_org_never_pairs_with_themselves() -> None:
    """``GraphEdge`` raises on a self-loop, so grouping by person is the guard."""
    org = _org("Boomerang")
    affiliations = [
        _aff("a", org, start=ApproxDate(year=2010), end=ApproxDate(year=2012)),
        _aff("a", org, start=ApproxDate(year=2015), end=ApproxDate(year=2018)),
        _aff("a", org, start=None, end=None),
        _aff("b", org, start=ApproxDate(year=2011), end=ApproxDate(year=2016)),
    ]
    projection = _project(affiliations, [org])
    assert all(left != right for left, right in projection.pairs)
    assert set(projection.pairs) == {("a", "b")}
    assert projection.n_affiliations == 4


def test_identical_duplicate_affiliation_records_do_not_create_a_self_loop() -> None:
    org = _org("Duplicated")
    one = _aff("a", org)
    projection = _project([one, one, one], [org])
    assert projection.pairs == {}
    assert projection.people == {"a"}


def test_multiple_shared_orgs_accumulate_and_name_every_context() -> None:
    employer = _org("SharedEmployer")
    school = _org("SharedSchool")
    affiliations = [
        _aff("a", employer),
        _aff("b", employer),
        _aff("a", school, kind=AffiliationKind.EDUCATION),
        _aff("b", school, kind=AffiliationKind.EDUCATION),
    ]
    projection = _project(affiliations, [employer, school])
    evidence = projection.pairs[("a", "b")]
    assert evidence.org_ids == {employer.org_id, school.org_id}
    assert evidence.kinds == {AffiliationKind.EMPLOYMENT, AffiliationKind.EDUCATION}


def test_no_inferred_pair_can_exceed_the_max_inferred_weight() -> None:
    """The clamp is what makes ``OBSERVED_FLOOR`` a provable separation, not a hope."""
    orgs = [_org(f"Shared{i:02d}") for i in range(12)]
    affiliations = [_aff(p, org) for org in orgs for p in ("a", "b")]
    projection = _project(affiliations, orgs, size_damping=no_damping)
    assert projection.pairs[("a", "b")].weight == MAX_INFERRED_WEIGHT
    assert all(e.weight <= MAX_INFERRED_WEIGHT for e in projection.pairs.values())


def test_projection_does_not_depend_on_the_order_affiliations_arrive_in() -> None:
    orgs = [_org(f"Firm{i:02d}") for i in range(6)]
    affiliations = [_aff(f"p{i:03d}", orgs[i % len(orgs)]) for i in range(60)]
    baseline = _project(affiliations, orgs)
    shuffled = list(affiliations)
    random.Random(20240101).shuffle(shuffled)
    other = _project(shuffled, orgs)
    assert {k: v.weight for k, v in baseline.pairs.items()} == {
        k: v.weight for k, v in other.pairs.items()
    }


@pytest.mark.parametrize(
    "affiliations_factory",
    [
        pytest.param(lambda org: [], id="empty"),
        pytest.param(lambda org: [_aff("solo", org)], id="single-person-firm"),
        pytest.param(
            lambda org: [_aff("a", org, start=None, end=None), _aff("b", org, weight=0.0)],
            id="zero-weight-and-undated",
        ),
        pytest.param(
            lambda org: [
                _aff("a", org, start=ApproxDate(year=2000), end=ApproxDate(year=2001)),
                _aff("b", org, start=ApproxDate(year=2090), end=ApproxDate(year=2091)),
            ],
            id="zero-overlap",
        ),
    ],
)
def test_degenerate_inputs_never_produce_nan_or_negative_weights(
    affiliations_factory: Callable[[Org], list[Affiliation]],
) -> None:
    org = _org("Degenerate")
    affiliations = affiliations_factory(org)
    for sizes in (FirmSizes({}, {}), _sizes(Degenerate=0), _sizes(Degenerate=200_000)):
        projection = _project(affiliations, [org], sizes=sizes)
        for evidence in projection.pairs.values():
            assert math.isfinite(evidence.weight)
            assert evidence.weight >= 0.0
        for profile in projection.orgs.values():
            assert math.isfinite(profile.weight)
            assert math.isfinite(profile.damping)
            assert profile.weight >= 0.0
            assert 0.0 < profile.damping <= 1.0


# ---------------------------------------------------------------------------
# the headline measurement
# ---------------------------------------------------------------------------

MEGACORP = ("MegaCorp", 200, 200_000)
"""One very large employer: 200 of the operator's connections, 200k staff."""

# A deliberately long, small-skewed tail: a real export is dominated by a few
# employers and a long tail of places two or three people passed through.
TAIL_MEMBERS = (2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 6, 7, 8, 10, 12, 15, 18, 22, 28, 35)
TAIL_HEADCOUNT_MULTIPLIER = (1, 1, 2, 3, 5, 8, 12, 20, 30, 50)
TAIL_PEOPLE = 1_000


def _export_firms(seed: int = 20240101) -> list[tuple[str, int, int]]:
    """``(name, members_in_export, real_headcount)`` for a ~1,200-person export."""
    rng = random.Random(seed)
    firms = [MEGACORP]
    remaining, index = TAIL_PEOPLE, 0
    while remaining > 0:
        index += 1
        members = min(remaining, rng.choice(TAIL_MEMBERS))
        remaining -= members
        firms.append((f"Firm{index:03d}", members, members * rng.choice(TAIL_HEADCOUNT_MULTIPLIER)))
    return firms


def _export_world(
    firms: list[tuple[str, int, int]], seed: int = 20240101
) -> tuple[list[Org], list[Affiliation], FirmSizes]:
    """Give every person a 1-5 year stint starting somewhere in a 12-year window.

    The tenure distribution is identical for every firm, so the co-tenure filter
    removes the same *fraction* of each firm's pairs and cannot be what produces
    the reweighting below.
    """
    rng = random.Random(seed)
    orgs: list[Org] = []
    affiliations: list[Affiliation] = []
    person = 0
    for name, members, _headcount in firms:
        org = _org(name)
        orgs.append(org)
        for _ in range(members):
            person += 1
            start = 2005 + rng.randrange(0, 12)
            affiliations.append(
                _aff(
                    f"p{person:05d}",
                    org,
                    start=ApproxDate(year=start),
                    end=ApproxDate(year=start + rng.randrange(1, 6)),
                )
            )
    sizes = _sizes(**{name: headcount for name, _members, headcount in firms})
    return orgs, affiliations, sizes


def _naive_clique_share(firms: list[tuple[str, int, int]], name: str) -> float:
    """Share of a naive ``P = B B^T`` projection's edges owned by one firm.

    Naive means what research 02 Sec. 1.2 means: every affiliation of size ``n``
    becomes a clique of ``n(n-1)/2`` unit-weight edges, no co-tenure, no
    corrections.
    """
    per_firm = {n: m * (m - 1) / 2 for n, m, _ in firms}
    return per_firm[name] / sum(per_firm.values())


def _weight_share(projection: Projection, name: str) -> float:
    total = sum(profile.weight for profile in projection.orgs.values())
    mine = next(p.weight for p in projection.orgs.values() if p.name == name)
    return mine / total


def test_one_huge_employer_stops_dominating_the_edge_weight() -> None:
    """The measurement this whole stage exists for.

    A single 200,000-person employer holding 200 of a 1,200-person export takes
    two thirds of a naive projection. Newman weighting alone is not enough --
    research 02 measured 15.9% -- and it is the size-damping term that finishes
    the job.
    """
    firms = _export_firms()
    orgs, affiliations, sizes = _export_world(firms)
    assert sum(m for _, m, _ in firms) == 1_200

    naive = _naive_clique_share(firms, "MegaCorp")
    newman_only = _weight_share(
        _project(affiliations, orgs, sizes=sizes, size_damping=no_damping), "MegaCorp"
    )
    full = _weight_share(_project(affiliations, orgs, sizes=sizes), "MegaCorp")

    measured = {
        "naive_clique_projection": round(naive, 4),
        "newman_only": round(newman_only, 4),
        "newman_plus_cotenure_plus_size_damping": round(full, 4),
    }
    assert naive > 0.50, measured
    assert full < 0.15, measured
    assert full < naive / 4, measured
    # Newman alone is a real improvement but leaves the firm over-represented;
    # the size term is not redundant with it.
    assert newman_only < naive / 2, measured
    assert full < newman_only, measured


def test_the_size_damping_term_is_what_finishes_the_job() -> None:
    """Ablation: the same export, with the size term switched off."""
    firms = _export_firms()
    orgs, affiliations, sizes = _export_world(firms)
    with_damping = _weight_share(_project(affiliations, orgs, sizes=sizes), "MegaCorp")
    without = _weight_share(
        _project(affiliations, orgs, sizes=sizes, size_damping=no_damping), "MegaCorp"
    )
    assert with_damping < without / 2


def test_the_cotenure_filter_removes_a_large_fraction_of_candidate_pairs() -> None:
    """Research 02 measured -62% of candidate edges; the fixture here is milder,
    but a filter that stopped removing anything would be silently broken."""
    firms = _export_firms()
    orgs, affiliations, sizes = _export_world(firms)
    projection = _project(affiliations, orgs, sizes=sizes)
    unfiltered = _project(affiliations, orgs, sizes=sizes, require_cotenure=False)

    drop_rate = projection.pairs_dropped_cotenure / projection.pairs_considered
    assert 0.25 < drop_rate < 0.95, drop_rate
    assert len(projection.pairs) < len(unfiltered.pairs)
    assert projection.pairs_considered == unfiltered.pairs_considered
