"""The budget guard. It stops the run; it does not log a warning and continue."""

from __future__ import annotations

import pytest

from interlayer.enrichment.budget import (
    BudgetExceeded,
    BudgetGuard,
    CostEstimate,
    format_estimates,
)


def test_call_ceiling_stops_before_the_breaching_call() -> None:
    guard = BudgetGuard(max_calls_per_run=2)
    guard.reserve()
    guard.reserve()
    with pytest.raises(BudgetExceeded) as excinfo:
        guard.reserve()
    assert guard.calls_made == 2  # the third was never booked
    assert "call limit" in str(excinfo.value)
    assert "stopped before making the call" in str(excinfo.value)


def test_cost_ceiling_stops_before_the_breaching_call() -> None:
    guard = BudgetGuard(max_estimated_cost_usd=1.0)
    guard.reserve(cost_usd=0.9)
    with pytest.raises(BudgetExceeded, match="cost limit"):
        guard.reserve(cost_usd=0.2)
    assert guard.cost_spent_usd == pytest.approx(0.9)


def test_spending_exactly_the_ceiling_is_allowed() -> None:
    guard = BudgetGuard(max_estimated_cost_usd=1.0)
    guard.reserve(cost_usd=1.0)
    assert guard.cost_remaining_usd == pytest.approx(0.0)
    with pytest.raises(BudgetExceeded):
        guard.reserve(cost_usd=0.01)


def test_would_exceed_reports_without_raising() -> None:
    guard = BudgetGuard(max_calls_per_run=1)
    assert guard.would_exceed() is None
    guard.record()
    assert guard.would_exceed() is not None


def test_dry_run_still_enforces_but_books_nothing() -> None:
    guard = BudgetGuard(max_calls_per_run=1, dry_run=True)
    guard.reserve()
    guard.reserve()  # nothing was booked, so this is fine
    assert guard.calls_made == 0

    tight = BudgetGuard(max_calls_per_run=0, dry_run=True)
    with pytest.raises(BudgetExceeded):
        tight.reserve()


def test_preflight_validates_the_whole_plan_up_front() -> None:
    guard = BudgetGuard(max_calls_per_run=100, max_estimated_cost_usd=1.0)
    plan = [CostEstimate("brightdata", 500, 0.5), CostEstimate("coresignal", 10, 0.2)]
    with pytest.raises(BudgetExceeded, match="call limit"):
        guard.preflight(plan)

    ok = BudgetGuard(max_calls_per_run=1000, max_estimated_cost_usd=1.0)
    ok.preflight([CostEstimate("brightdata", 100, 0.1)])


def test_remaining_never_goes_negative() -> None:
    guard = BudgetGuard(max_calls_per_run=1, max_estimated_cost_usd=1.0)
    guard.record(calls=5, cost_usd=5.0)
    assert guard.calls_remaining == 0
    assert guard.cost_remaining_usd == 0.0


def test_summary_is_reportable() -> None:
    guard = BudgetGuard()
    guard.record(calls=3, cost_usd=0.25)
    summary = guard.summary()
    assert summary["calls_made"] == 3
    assert summary["cost_spent_usd"] == pytest.approx(0.25)
    assert summary["dry_run"] is False


def test_estimate_table_shows_free_providers_too() -> None:
    rendered = format_estimates(
        [CostEstimate("null", 100, 0.0), CostEstimate("brightdata", 100, 0.10)]
    )
    assert "null" in rendered
    assert "brightdata" in rendered
    assert "total" in rendered
    assert format_estimates([]) == "no providers enabled; nothing to estimate"
