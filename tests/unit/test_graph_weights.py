"""Unit tests for :mod:`interlayer.graph.weights` -- the co-tenure gate and the
three multiplicative corrections that decide which relationships exist at all.

The weighting is the whole ballgame for this stage: research 02 Sec. 1.2-1.4
measured a single 200,000-person employer producing 65% of all edges and 60.8%
of all edge weight under naive projection. Everything here asserts the *shape*
of the corrections -- orderings, saturation, boundedness -- rather than frozen
magic numbers, because the functional forms are explicitly documented as
judgement calls that will be retuned against real data.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest
from pydantic import ValidationError

from interlayer.errors import GraphError
from interlayer.graph.weights import (
    DAYS_PER_YEAR,
    MAX_INFERRED_WEIGHT,
    OBSERVED_FLOOR,
    SIZE_DAMPINGS,
    Stint,
    cotenure_factor,
    inverse_damping,
    log_damping,
    newman_factor,
    no_damping,
    observed_bonus,
    resolve_size_damping,
    sqrt_damping,
)
from interlayer.models import Affiliation, AffiliationKind, ApproxDate

ORG = "org_shared"
OTHER_ORG = "org_elsewhere"

MISSING = 0.3
CAP = 5.0


def _ad(year: int, month: int | None = None, day: int | None = None) -> ApproxDate:
    return ApproxDate(year=year, month=month, day=day)


def _aff(
    person: str,
    *,
    org: str = ORG,
    start: ApproxDate | None = None,
    end: ApproxDate | None = None,
    current: bool = False,
    kind: AffiliationKind = AffiliationKind.EMPLOYMENT,
    weight: float = 1.0,
) -> Affiliation:
    return Affiliation(
        person_id=person,
        org_id=org,
        kind=kind,
        start=start,
        end=end,
        is_current=current,
        weight=weight,
    )


def _factor(
    a: Affiliation,
    b: Affiliation,
    *,
    require: bool = True,
    missing: float = MISSING,
    cap: float = CAP,
) -> float:
    return cotenure_factor(
        Stint.of(a),
        Stint.of(b),
        require_cotenure=require,
        missing_date_factor=missing,
        max_overlap_years=cap,
    )


# ---------------------------------------------------------------------------
# co-tenure filtering
# ---------------------------------------------------------------------------


def test_non_overlapping_tenures_produce_no_edge() -> None:
    """Five years apart at the same firm is no evidence of acquaintance."""
    early = _aff("a", start=_ad(2010), end=_ad(2012))
    late = _aff("b", start=_ad(2018), end=_ad(2021))
    assert _factor(early, late) == 0.0
    assert _factor(late, early) == 0.0


def test_overlapping_tenures_produce_an_edge() -> None:
    a = _aff("a", start=_ad(2010), end=_ad(2015))
    b = _aff("b", start=_ad(2012), end=_ad(2018))
    factor = _factor(a, b)
    assert factor > 0.0
    # 2012-01-01 .. 2016-01-01 exclusive is just over four years of the five-year cap.
    assert factor == pytest.approx(4.0 / 5.0, abs=0.01)


def test_a_year_only_date_is_a_span_not_an_instant() -> None:
    """Regression: ``2019`` must overlap ``2019-06`` -> ``2020``.

    LinkedIn hands out year-only dates far more often than full ones. Reading
    ``2019`` as the instant 2019-01-01 at *both* ends of the comparison makes a
    real seven-month co-tenure vanish, and it silently dropped real edges until
    :meth:`interlayer.models.Affiliation.overlaps` was changed to compare spans.
    """
    year_only = _aff("a", start=_ad(2019), end=_ad(2019))
    mid_year = _aff("b", start=_ad(2019, 6), end=_ad(2020))

    assert _factor(year_only, mid_year) > 0.0
    # ... and the gate is symmetric, which the two-branch implementation makes
    # worth checking rather than assuming.
    assert _factor(mid_year, year_only) == _factor(year_only, mid_year)


def test_a_year_only_end_date_runs_to_the_end_of_that_year() -> None:
    """``2019``..``2019`` is a whole year of tenure, not a zero-length instant."""
    whole_2019 = _aff("a", start=_ad(2019), end=_ad(2019))
    also_2019 = _aff("b", start=_ad(2019), end=_ad(2019))
    factor = _factor(whole_2019, also_2019)
    assert factor > 0.0
    assert factor == pytest.approx(365 / DAYS_PER_YEAR / CAP, rel=0.01)


def test_a_month_only_end_date_runs_to_the_end_of_that_month() -> None:
    a = _aff("a", start=_ad(2019, 1), end=_ad(2019, 6))
    b = _aff("b", start=_ad(2019, 6), end=_ad(2019, 12))
    assert _factor(a, b) > 0.0


def test_december_month_only_end_rolls_into_the_next_year() -> None:
    """The month+1 arithmetic has to carry; a December end must not build month 13."""
    a = _aff("a", start=_ad(2019, 1), end=_ad(2019, 12))
    b = _aff("b", start=_ad(2019, 12), end=_ad(2020, 3))
    assert _factor(a, b) > 0.0


def test_missing_dates_are_damped_never_excluded() -> None:
    """Dropping undated stints loses real people; full credit re-creates the blowup."""
    undated = _aff("a")
    other = _aff("b")
    assert _factor(undated, other) == MISSING

    dated = _aff("b", start=_ad(2010), end=_ad(2012))
    assert _factor(undated, dated) == MISSING

    # The damping is the configured knob, not a constant baked into the module.
    assert _factor(undated, other, missing=0.05) == 0.05
    assert _factor(undated, other, missing=1.0) == 1.0


def test_both_parties_current_at_one_firm_are_not_treated_as_undated() -> None:
    """Two people both *still there* genuinely overlap -- today.

    ``Affiliation.is_current`` is documented in ``models.py`` as "distinct from
    an unrecorded end date, which is merely unknown: two people both currently
    at a firm genuinely overlap and must not be damped as though their dates
    were missing", and research 02 Sec. 1.5 says to "treat a still-current role
    as ending today". ``Stint.of`` looks only at ``start``/``end``, so a current
    stint has ``span is None`` and falls through to the missing-date branch.
    """
    a = _aff("a", start=_ad(2010), current=True)
    b = _aff("b", start=_ad(2011), current=True)
    factor = _factor(a, b)

    assert factor > 0.0, "two people currently at the same firm must produce an edge"
    assert factor > MISSING, (
        "both parties are still at the firm, so their tenures provably intersect; "
        f"damping to the missing-date factor ({MISSING}) discards known information"
    )


def test_require_cotenure_false_disables_the_filter() -> None:
    """The filter is a knob, and turning it off admits even disjoint tenures."""
    early = _aff("a", start=_ad(2000), end=_ad(2001))
    late = _aff("b", start=_ad(2020), end=_ad(2021))
    assert _factor(early, late, require=True) == 0.0
    assert _factor(early, late, require=False) == 1.0
    # Undated pairs are not damped either once the gate is off.
    assert _factor(_aff("a"), _aff("b"), require=False) == 1.0


def test_affiliations_at_different_orgs_never_co_tenure() -> None:
    a = _aff("a", start=_ad(2010), end=_ad(2015))
    b = _aff("b", org=OTHER_ORG, start=_ad(2012), end=_ad(2018))
    assert _factor(a, b) == 0.0
    assert _factor(_aff("a"), _aff("b", org=OTHER_ORG)) == 0.0


def test_touching_but_not_overlapping_intervals() -> None:
    """Half-open spans: a year-granular hand-off still leaves a shared year."""
    a = _aff("a", start=_ad(2010), end=_ad(2012))
    b = _aff("b", start=_ad(2012), end=_ad(2014))
    # ``2012`` as an end runs to 2013-01-01, so they share the whole of 2012.
    assert _factor(a, b) == pytest.approx(366 / DAYS_PER_YEAR / CAP, rel=0.01)

    exact = _aff("a", start=_ad(2010, 1, 1), end=_ad(2011, 12, 31))
    after = _aff("b", start=_ad(2012, 1, 1), end=_ad(2013, 12, 31))
    assert _factor(exact, after) == 0.0


def test_calendar_impossible_dates_cannot_reach_the_graph_at_all() -> None:
    """The guarantee now sits at construction rather than here.

    ``ApproxDate`` used to bound ``day`` at 1-31 without consulting the month, so
    31 February validated and only failed later, inside whichever stage first
    converted it. It is now rejected where it is built, which makes the graph
    stage's demote-to-undated branch unreachable from any validated model -- the
    branch stays as insurance against artifacts written by older versions.
    """
    with pytest.raises(ValidationError):
        _ad(2019, 2, 31)

    # A real leap day is not impossible and must still be accepted.
    stint = Stint.of(_aff("a", start=_ad(2020, 2, 29), end=_ad(2020, 3, 15)))
    assert stint.span is not None
    assert stint.invalid_dates is False


def test_cotenure_is_symmetric_over_a_grid() -> None:
    windows = [
        (None, None),
        (_ad(2010), _ad(2012)),
        (_ad(2011), _ad(2011)),
        (_ad(2011, 6), _ad(2013, 2)),
        (_ad(2005, 1, 1), _ad(2020, 12, 31)),
        (_ad(2019), None),
    ]
    for start_a, end_a in windows:
        for start_b, end_b in windows:
            a = _aff("a", start=start_a, end=end_a)
            b = _aff("b", start=start_b, end=end_b)
            assert _factor(a, b) == _factor(b, a)


# ---------------------------------------------------------------------------
# saturation
# ---------------------------------------------------------------------------


def test_overlap_saturates_at_the_configured_cap() -> None:
    """Eight years together is not eight times one year (research 02 Sec. 1.5)."""
    five = _factor(
        _aff("a", start=_ad(2000, 1, 1), end=_ad(2004, 12, 31)),
        _aff("b", start=_ad(2000, 1, 1), end=_ad(2004, 12, 31)),
    )
    ten = _factor(
        _aff("a", start=_ad(2000, 1, 1), end=_ad(2009, 12, 31)),
        _aff("b", start=_ad(2000, 1, 1), end=_ad(2009, 12, 31)),
    )
    twenty = _factor(
        _aff("a", start=_ad(2000, 1, 1), end=_ad(2019, 12, 31)),
        _aff("b", start=_ad(2000, 1, 1), end=_ad(2019, 12, 31)),
    )
    assert five == pytest.approx(1.0, abs=0.01)
    assert ten == pytest.approx(five, abs=1e-12)
    assert twenty == pytest.approx(five, abs=1e-12)
    assert ten < 2 * five


def test_overlap_is_linear_below_the_cap() -> None:
    half = _factor(
        _aff("a", start=_ad(2000, 1, 1), end=_ad(2002, 6, 30)),
        _aff("b", start=_ad(2000, 1, 1), end=_ad(2002, 6, 30)),
    )
    assert half == pytest.approx(0.5, abs=0.01)


def test_the_cap_is_configurable() -> None:
    span = (_ad(2000, 1, 1), _ad(2004, 12, 31))
    a = _aff("a", start=span[0], end=span[1])
    b = _aff("b", start=span[0], end=span[1])
    assert _factor(a, b, cap=10.0) == pytest.approx(0.5, abs=0.01)
    assert _factor(a, b, cap=1.0) == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Newman collaboration weighting
# ---------------------------------------------------------------------------


def test_newman_weight_of_a_two_person_firm_is_one() -> None:
    assert newman_factor(2) == 1.0


def test_newman_weight_falls_strictly_with_group_size() -> None:
    """A 2-person firm outweighs a 200-person firm, monotonically."""
    sizes = [2, 3, 5, 10, 25, 50, 100, 200, 1000]
    factors = [newman_factor(n) for n in sizes]
    assert factors == sorted(factors, reverse=True)
    assert all(x > y for x, y in pairwise(factors))
    assert newman_factor(2) > newman_factor(200)
    assert newman_factor(200) == pytest.approx(1.0 / 199)


def test_newman_weight_of_a_solo_group_is_zero_not_a_division_by_zero() -> None:
    assert newman_factor(1) == 0.0
    assert newman_factor(0) == 0.0
    assert newman_factor(-4) == 0.0


# ---------------------------------------------------------------------------
# firm-size damping
# ---------------------------------------------------------------------------


def test_size_damping_orders_real_firm_sizes() -> None:
    """Co-working at a 40-person shop beats co-working at a 200,000-person firm."""
    ladder = [0, 1, 40, 400, 3_700, 40_000, 200_000]
    damped = [log_damping(float(size)) for size in ladder]
    assert damped == sorted(damped, reverse=True)
    assert all(x > y for x, y in pairwise(damped))
    assert log_damping(40) > log_damping(200_000)


def test_log_damping_compresses_the_size_range() -> None:
    """``1/log`` was chosen because ``1/size`` deletes big-firm edges outright.

    Research 02 Sec. 1.4: a 5,000x size range must compress to roughly a 2x
    weight range so that two people who really did share a ten-person team at a
    huge employer keep an edge at all.
    """
    ratio_log = log_damping(40) / log_damping(200_000)
    ratio_inverse = inverse_damping(40) / inverse_damping(200_000)
    assert 1.0 < ratio_log < 5.0
    assert ratio_inverse > 1000.0
    assert ratio_log < ratio_inverse / 100


def test_size_damping_is_bounded_in_zero_to_one() -> None:
    for name, damping in sorted(SIZE_DAMPINGS.items()):
        for size in (0.0, 1.0, 40.0, 200_000.0, 1e12):
            value = damping(size)
            assert math.isfinite(value), name
            assert 0.0 < value <= 1.0, (name, size, value)


def test_unknown_headcount_of_zero_degrades_to_no_damping() -> None:
    """A size of 0 must not explode the multiplier -- the ``+e`` is load-bearing."""
    assert log_damping(0.0) == 1.0
    assert log_damping(-500.0) == 1.0
    assert sqrt_damping(-500.0) == 1.0
    assert inverse_damping(-500.0) == 1.0
    assert no_damping(200_000.0) == 1.0


def test_resolve_size_damping_accepts_names_and_callables() -> None:
    assert resolve_size_damping(None)(40.0) == log_damping(40.0)
    assert resolve_size_damping("log") is log_damping
    assert resolve_size_damping("none") is no_damping
    assert resolve_size_damping(inverse_damping) is inverse_damping


def test_resolve_size_damping_rejects_an_unknown_strategy() -> None:
    with pytest.raises(GraphError) as excinfo:
        resolve_size_damping("hyperbolic")
    assert "hyperbolic" in str(excinfo.value)
    for known in SIZE_DAMPINGS:
        assert known in str(excinfo.value)


# ---------------------------------------------------------------------------
# observed bridges
# ---------------------------------------------------------------------------


def test_observed_floor_is_strictly_above_every_possible_inferred_edge() -> None:
    assert OBSERVED_FLOOR > MAX_INFERRED_WEIGHT


def test_observed_bonus_needs_a_pair() -> None:
    assert observed_bonus(0) == 0.0
    assert observed_bonus(1) == 0.0
    assert observed_bonus(2) > 0.0


def test_observed_bonus_falls_with_list_length_but_never_to_zero() -> None:
    """Two of two mutuals share more context than two of ninety -- but a long
    list must never push ground truth below an inference."""
    lengths = [2, 5, 10, 30, 90, 1000, 10**6]
    bonuses = [observed_bonus(n) for n in lengths]
    assert all(x > y for x, y in pairwise(bonuses))
    for length, bonus in zip(lengths, bonuses, strict=True):
        assert bonus > 0.0, length
        assert OBSERVED_FLOOR + bonus > MAX_INFERRED_WEIGHT


# ---------------------------------------------------------------------------
# degenerate inputs must not produce NaN or inf anywhere
# ---------------------------------------------------------------------------


def test_no_weight_function_returns_nan_or_infinity() -> None:
    values: list[float] = []
    values += [newman_factor(n) for n in (-5, 0, 1, 2, 10**6)]
    values += [observed_bonus(n) for n in (-1, 0, 1, 2, 10**9)]
    for damping in SIZE_DAMPINGS.values():
        values += [damping(size) for size in (-1e9, 0.0, 1e-12, 1.0, 1e15)]

    spans: list[tuple[ApproxDate | None, ApproxDate | None]] = [
        (None, None),
        (_ad(1900), _ad(2100)),
        (_ad(2019, 5, 1), _ad(2019, 5, 1)),
        (_ad(2019, 2, 28), _ad(2019, 3, 1)),
        (_ad(2020), None),
    ]
    for start_a, end_a in spans:
        for start_b, end_b in spans:
            for require in (True, False):
                values.append(
                    _factor(
                        _aff("a", start=start_a, end=end_a),
                        _aff("b", start=start_b, end=end_b),
                        require=require,
                        missing=0.0,
                    )
                )

    for value in values:
        assert math.isfinite(value)
        assert value >= 0.0


def test_cotenure_factor_never_exceeds_one() -> None:
    """The per-org term is bounded by 1, which is what makes the observed floor provable."""
    a = _aff("a", start=_ad(1900), end=_ad(2100))
    b = _aff("b", start=_ad(1900), end=_ad(2100))
    for cap in (0.5, 1.0, 5.0, 40.0):
        assert 0.0 <= _factor(a, b, cap=cap) <= 1.0
    assert _factor(_aff("a"), _aff("b"), missing=1.0) <= 1.0
