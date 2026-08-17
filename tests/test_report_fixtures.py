"""Shared fixtures for the report and CLI tests.

Built directly from ``core.models`` rather than by running the pipeline: the
graph engine is a separate work package, and the renderers are contracted
against ``AnalysisResult`` alone. A renderer test that needed a real harvest
would be testing the wrong thing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from interlayer.core.models import (
    AnalysisResult,
    Brokerage,
    Cluster,
    Coverage,
    NextTarget,
)
from interlayer.report import ReportContext

FIXED_TIME = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)

#: Contact details deliberately smuggled into display names, which is the only
#: route by which they can reach a renderer: ``AnalysisResult`` has no contact
#: field at all. PRIV-17 must strip them regardless of ``retain_emails``.
LEAKY_NAMES = {
    "m_alpha": "Dana Wu <dana.wu@example.com>",
    "m_beta": "Alex Rivera, +1 (212) 555-0142",
    "m_gamma": "Priya Nair  mailto:priya@sub.example.co.uk  tel:+44 20 7123 4567",
    "t_one": "J. Doe / j.doe@janestreet.example",
    "t_two": "K. Chen 212-555-9987",
}


def make_result(
    *,
    used_inferred_edges: bool = False,
    unadjudicated_count: int = 0,
    n_targets_total: int = 300,
    n_targets_harvested: int = 141,
    empty: bool = False,
) -> AnalysisResult:
    """A small but structurally complete analysis result."""
    if empty:
        return AnalysisResult(
            coverage=Coverage(
                n_targets_total=n_targets_total,
                n_targets_harvested=n_targets_harvested,
                n_members_total=2000,
                n_bridges=0,
            ),
            unadjudicated_count=unadjudicated_count,
            used_inferred_edges=used_inferred_edges,
        )

    return AnalysisResult(
        clusters=(
            Cluster(
                cluster_id=1,
                label="options | market making",
                member_ids=("m_alpha", "m_beta"),
                top_targets=("t_one",),
                stability=0.92,
                score_sum=3.21,
            ),
            Cluster(
                cluster_id=2,
                label="infrastructure | systems",
                member_ids=("m_gamma",),
                top_targets=("t_two",),
                stability=0.61,
                score_sum=1.04,
            ),
        ),
        brokerage=(
            Brokerage("m_alpha", 7, 0.41, 0.22, 3.10, 0.31, 0.69, 0.812),
            Brokerage("m_beta", 4, 0.88, 0.05, 2.00, 0.55, 0.45, 0.507),
            Brokerage("m_gamma", 2, 0.93, 0.00, 1.00, 0.90, 0.10, 0.318),
        ),
        coverage=Coverage(
            n_targets_total=n_targets_total,
            n_targets_harvested=n_targets_harvested,
            n_members_total=2000,
            n_bridges=3,
        ),
        next_targets=(
            NextTarget("t_two", 0.771, 2, 1, 0.50),
            NextTarget("t_three", 0.402, 0, 0, 0.91),
        ),
        used_inferred_edges=used_inferred_edges,
        unadjudicated_count=unadjudicated_count,
        fingerprint="9f" * 32,
        params={"base_seed": 42, "tau": 0.5},
    )


def make_ctx(
    *,
    names: dict[str, str] | None = None,
    inferred_members: frozenset[str] = frozenset(),
    retention_days: int = 90,
) -> ReportContext:
    return ReportContext(
        retention_days=retention_days,
        generated_at=FIXED_TIME,
        names=names if names is not None else {},
        inferred_members=inferred_members,
        tool_version="0.1.0-test",
    )


def test_fixture_is_structurally_complete() -> None:
    """Guard the fixtures themselves, so a later edit cannot hollow them out."""
    result = make_result(used_inferred_edges=True, unadjudicated_count=2)
    assert result.clusters and result.brokerage and result.next_targets
    assert result.coverage.n_targets_harvested < result.coverage.n_targets_total
    assert 0.0 < result.coverage.fraction < 1.0
