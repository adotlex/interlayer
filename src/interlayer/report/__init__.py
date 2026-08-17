"""Rendering layer: turn an :class:`~interlayer.core.models.AnalysisResult` into
something a human reads.

Four rules bind every renderer in this package, and each is enforced by a test:

* **PRIV-17** — email addresses and phone numbers are stripped from output
  unconditionally, whatever ``config.retain_emails`` says. See :mod:`.redact`.
* **PRIV-18** — every rendered artefact opens with a header block stating the
  purpose, the retention period in force, that the document contains
  third-party personal data, and that it must not be redistributed.
* **PRIV-19** — an inferred value never renders the way an observed one does.
  Inference is a guess about a person, and a guess that looks like an
  observation is worse than no output at all.
* **PRIV-20** — the count of unadjudicated ``needs_review`` records is stated,
  because those records are held *out* of the graph and their absence would
  otherwise read as a finding.

There is a fifth rule that is not a numbered privacy control but matters just as
much to whether the output is honest: **coverage is reported prominently, and
reach is rendered as a lower bound whenever the harvest is incomplete.** An
unharvested target cannot produce an edge, so a thin harvest and a thin network
produce identical numbers. The renderers refuse to let those two look alike.

The shared vocabulary lives here, at the top of the module, and the three
renderers are imported below it — they import back from this package, so the
definition order is load-bearing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from interlayer.core.models import AnalysisResult, Coverage

SCHEMA_VERSION = "interlayer.report/v1"
ANALYSIS_SCHEMA_VERSION = "interlayer.analysis/v1"

# ---------------------------------------------------------------------------
# PRIV-18 — the mandatory header block
# ---------------------------------------------------------------------------
#
# These strings are asserted on verbatim by the PRIV-18 test and are embedded in
# HTML, so they deliberately avoid characters that an escaping template engine
# would rewrite (``< > & " '``). A notice that survives Markdown but not HTML is
# not a notice.

PURPOSE_NOTICE = (
    "Identify which of your own LinkedIn connections bridge into a named target firm, so "
    "that an introduction can be requested from someone who already knows the person, "
    "rather than approaching a stranger."
)

THIRD_PARTY_NOTICE = (
    "This document contains personal data about third parties. They have not consented to "
    "this analysis and are not aware that it exists."
)

REDISTRIBUTION_NOTICE = (
    "Do not redistribute. This document is for the operator alone; passing it on is a "
    "further disclosure of personal data belonging to other people."
)

RETENTION_TEMPLATE = (
    "Retention period in force: {days} days from collection. Records older than that are "
    "removed by the retention sweep, and this document is a snapshot that the sweep cannot "
    "reach once it has been saved elsewhere."
)

PROVENANCE_NOTICE = (
    "Structure in this report comes from mutual-connection surfaces the operator viewed in "
    "their own authenticated session. No connection graph was purchased, scraped or "
    "otherwise obtained from a third party."
)

# ---------------------------------------------------------------------------
# PRIV-19 — inference vocabulary
# ---------------------------------------------------------------------------

INFERRED_MARKER = "INFERRED"
OBSERVED_MARKER = "observed"

INFERRED_BANNER = (
    "This report includes INFERRED values. An inferred tie was never seen: it is a "
    "co-affiliation guess with a confidence below 1.0. Inferred values are marked "
    "throughout and must not be relied on as evidence that two people know each other."
)

OBSERVED_ONLY_BANNER = (
    "All ties in this report are observed. No inferred edges were used."
)

# ---------------------------------------------------------------------------
# PRIV-20 — review vocabulary
# ---------------------------------------------------------------------------

REVIEW_TEMPLATE = (
    "Unadjudicated needs_review items: {count}. These records are held OUT of the graph "
    "until a human adjudicates them, so any of them could add bridges that this report "
    "does not show. A count above zero means the result is provisional."
)

# ---------------------------------------------------------------------------
# Coverage vocabulary
# ---------------------------------------------------------------------------

COVERAGE_CAVEAT_INCOMPLETE = (
    "Coverage is incomplete, so every reach figure below is a LOWER BOUND and is written "
    "with a leading sign. An unharvested target cannot produce an edge; a thin harvest and "
    "a thin network are indistinguishable from the numbers alone. Harvest more targets "
    "before concluding that anyone lacks reach."
)

COVERAGE_CAVEAT_COMPLETE = (
    "Every known target has been harvested, so reach figures are exact with respect to the "
    "target set. They remain bounded by the completeness of the target enumeration itself."
)

UI_DRIFT_NOTICE = (
    "The acquisition procedure described in the runbook was documented on 2026-08-17 and "
    "could not be verified against the live site. LinkedIn may have changed its interface "
    "since then."
)


def _tool_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib always present
        return "unknown"
    try:
        return version("interlayer")
    except PackageNotFoundError:  # pragma: no cover - editable installs carry metadata
        return "unknown"


@dataclass(frozen=True)
class ReportContext:
    """Everything a renderer needs that is not in the ``AnalysisResult``.

    Deliberately small. The result object is the source of truth for findings;
    this carries only presentation facts and the config values the header block
    is obliged to state.
    """

    retention_days: int = 90
    """Stated in the PRIV-18 header. Comes from ``config.retention_days``."""

    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    names: Mapping[str, str] = field(default_factory=dict)
    """Optional ``id -> display name`` lookup. Absent by default: the analysis
    layer works in ids, and a report of ids is still a usable report."""

    inferred_members: frozenset[str] = frozenset()
    """Member ids whose contribution is inferred rather than observed. When the
    caller cannot localise inference but ``used_inferred_edges`` is set, every
    row is marked instead — over-marking is the safe direction for PRIV-19."""

    title: str = "interlayer bridge report"
    tool_version: str = field(default_factory=_tool_version)

    def display_name(self, entity_id: str) -> str:
        """Human label for an id, falling back to the id itself."""
        return self.names.get(entity_id, entity_id)


def header_rows(ctx: ReportContext) -> tuple[tuple[str, str], ...]:
    """The PRIV-18 header block as ``(label, statement)`` pairs.

    One definition, three renderers. If a notice is missing from one format but
    present in another, that is a bug the shared source makes impossible.
    """
    return (
        ("Purpose", PURPOSE_NOTICE),
        ("Retention", RETENTION_TEMPLATE.format(days=ctx.retention_days)),
        ("Third-party data", THIRD_PARTY_NOTICE),
        ("Redistribution", REDISTRIBUTION_NOTICE),
        ("Provenance", PROVENANCE_NOTICE),
        ("Generated", f"{ctx.generated_at.isoformat()} by interlayer {ctx.tool_version}"),
    )


def harvest_is_complete(coverage: Coverage) -> bool:
    """True only when every known target has actually been looked at.

    A zero-target coverage record is *not* complete: nothing has been harvested
    because nothing is known, which is the weakest possible evidential state.
    """
    return coverage.n_targets_total > 0 and coverage.n_targets_harvested >= coverage.n_targets_total


def reach_display(reach: int, complete: bool) -> str:
    """Reach as an exact count, or as ``>= n`` when the harvest is incomplete."""
    return str(reach) if complete else f"≥ {reach}"


def coverage_caveat(coverage: Coverage) -> str:
    return COVERAGE_CAVEAT_COMPLETE if harvest_is_complete(coverage) else COVERAGE_CAVEAT_INCOMPLETE


def is_inferred(result: AnalysisResult, ctx: ReportContext, member_id: str) -> bool:
    """Whether a bridge row must render as inferred.

    Precise when the caller supplied ``inferred_members``; otherwise every row
    is marked whenever the result used inferred edges at all. Marking a
    genuinely observed row as inferred costs the reader a caveat. Failing to
    mark an inferred one presents a guess as a fact, which is the failure
    PRIV-19 exists to prevent.
    """
    if member_id in ctx.inferred_members:
        return True
    return result.used_inferred_edges and not ctx.inferred_members


def basis_label(inferred: bool) -> str:
    return INFERRED_MARKER if inferred else OBSERVED_MARKER


def inference_banner(result: AnalysisResult) -> str:
    return INFERRED_BANNER if result.used_inferred_edges else OBSERVED_ONLY_BANNER


def review_statement(result: AnalysisResult) -> str:
    return REVIEW_TEMPLATE.format(count=result.unadjudicated_count)


def sorted_bridges(result: AnalysisResult) -> list:
    """Bridges ranked by composite descending, ties broken by ascending id.

    The tie-break is the project-wide determinism rule; applying it here keeps
    two renderings of one result in the same order.
    """
    return sorted(result.brokerage, key=lambda b: (-b.composite, b.member_id))


def sorted_next_targets(result: AnalysisResult) -> list:
    return sorted(result.next_targets, key=lambda n: (-n.priority, n.target_id))


def sorted_clusters(result: AnalysisResult) -> list:
    return sorted(result.clusters, key=lambda c: (-c.score_sum, c.cluster_id))


def fmt(value: float, dp: int = 3) -> str:
    """Fixed-precision number formatting, shared so formats agree."""
    return f"{value:.{dp}f}"


def fmt_pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


# Imported last: the renderers below import the vocabulary defined above, so the
# names must already exist on this partially-initialised module.
from .html import render_html  # noqa: E402
from .json_out import dump_result, load_result, render_json  # noqa: E402
from .markdown import render_markdown  # noqa: E402
from .redact import has_contact_details, redact_text, redact_value  # noqa: E402

RENDERERS = {
    "markdown": render_markdown,
    "md": render_markdown,
    "html": render_html,
    "json": render_json,
}


def render(fmt_name: str, result: AnalysisResult, ctx: ReportContext | None = None) -> str:
    """Render *result* in the named format.

    Raises ``KeyError`` with the valid names on an unknown format, which the CLI
    turns into a usage error rather than a traceback.
    """
    try:
        renderer = RENDERERS[fmt_name.lower()]
    except KeyError:
        raise KeyError(
            f"unknown format {fmt_name!r}; expected one of markdown, html, json"
        ) from None
    return renderer(result, ctx)


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "COVERAGE_CAVEAT_COMPLETE",
    "COVERAGE_CAVEAT_INCOMPLETE",
    "INFERRED_BANNER",
    "INFERRED_MARKER",
    "OBSERVED_MARKER",
    "PROVENANCE_NOTICE",
    "PURPOSE_NOTICE",
    "REDISTRIBUTION_NOTICE",
    "RENDERERS",
    "RETENTION_TEMPLATE",
    "REVIEW_TEMPLATE",
    "SCHEMA_VERSION",
    "THIRD_PARTY_NOTICE",
    "UI_DRIFT_NOTICE",
    "ReportContext",
    "basis_label",
    "coverage_caveat",
    "dump_result",
    "fmt",
    "fmt_pct",
    "harvest_is_complete",
    "has_contact_details",
    "header_rows",
    "inference_banner",
    "is_inferred",
    "load_result",
    "reach_display",
    "redact_text",
    "redact_value",
    "render",
    "render_html",
    "render_json",
    "render_markdown",
    "review_statement",
    "sorted_bridges",
    "sorted_clusters",
    "sorted_next_targets",
]
