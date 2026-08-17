"""Markdown renderer.

The plain-text format, and the one most likely to be pasted somewhere. That is
precisely why the redaction pass at the end is unconditional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from interlayer.report import (
    UI_DRIFT_NOTICE,
    ReportContext,
    basis_label,
    coverage_caveat,
    fmt,
    fmt_pct,
    harvest_is_complete,
    header_rows,
    inference_banner,
    is_inferred,
    reach_display,
    review_statement,
    sorted_bridges,
    sorted_clusters,
    sorted_next_targets,
)
from interlayer.report.redact import redact_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from interlayer.core.models import AnalysisResult


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out.extend("| " + " | ".join(r) + " |" for r in rows)
    out.append("")
    return out


def render_markdown(result: AnalysisResult, ctx: ReportContext | None = None) -> str:
    """Render *result* as Markdown, with every contact detail stripped."""
    ctx = ctx or ReportContext()
    cov = result.coverage
    complete = harvest_is_complete(cov)

    lines: list[str] = [f"# {ctx.title}", ""]

    # --- PRIV-18 header block -------------------------------------------------
    lines.append("## Read this first")
    lines.append("")
    for label, statement in header_rows(ctx):
        lines.append(f"- **{label}.** {statement}")
    lines.append("")

    # --- PRIV-19 inference banner --------------------------------------------
    banner = inference_banner(result)
    lines.append(f"> **Basis of ties.** {banner}")
    lines.append("")

    # --- PRIV-20 review count -------------------------------------------------
    lines.append(f"> **Review queue.** {review_statement(result)}")
    lines.append("")

    # --- Coverage, before any finding ----------------------------------------
    lines.append("## Coverage")
    lines.append("")
    lines.extend(
        _table(
            ["Measure", "Value"],
            [
                ["Targets harvested", f"{cov.n_targets_harvested} of {cov.n_targets_total}"],
                ["Coverage", fmt_pct(cov.fraction)],
                ["Connections in scope", str(cov.n_members_total)],
                ["Bridges found", str(cov.n_bridges)],
                ["Harvest complete", "yes" if complete else "no"],
            ],
        )
    )
    lines.append(f"> {coverage_caveat(cov)}")
    lines.append("")

    # --- Bridge ranking -------------------------------------------------------
    lines.append("## Bridge ranking")
    lines.append("")
    rows = []
    for rank, b in enumerate(sorted_bridges(result), start=1):
        inferred = is_inferred(result, ctx, b.member_id)
        name = ctx.display_name(b.member_id)
        # An inferred row must not be able to pass for an observed one, so the
        # marking is redundant on purpose: label column, bracketed name, and an
        # explicit tag on the reach value itself.
        label = f"**[{basis_label(inferred)}]** _{name}_" if inferred else name
        reach = reach_display(b.reach, complete)
        reach_cell = f"{reach} ({basis_label(inferred)})" if inferred else reach
        rows.append(
            [
                str(rank),
                label,
                reach_cell,
                fmt(b.rarity),
                fmt(b.betweenness),
                fmt(b.effective_size, 2),
                fmt(b.autonomy),
                fmt(b.composite),
                basis_label(inferred),
            ]
        )
    lines.extend(
        _table(
            [
                "#",
                "Bridge",
                "Reach",
                "Rarity",
                "Betweenness",
                "Eff. size",
                "Autonomy",
                "Composite",
                "Basis",
            ],
            rows,
        )
    )

    # --- Clusters -------------------------------------------------------------
    lines.append("## Clusters")
    lines.append("")
    clusters = sorted_clusters(result)
    if not clusters:
        lines.extend(["_No clusters. Either no bridges were found, or the harvest is too thin "
                      "to partition._", ""])
    for c in clusters:
        lines.append(f"### {c.label}  —  cluster {c.cluster_id}")
        lines.append("")
        lines.append(
            f"- Members: {len(c.member_ids)} · stability {fmt(c.stability)} "
            f"· score sum {fmt(c.score_sum)}"
        )
        member_labels = []
        for mid in c.member_ids:
            inferred = is_inferred(result, ctx, mid)
            name = ctx.display_name(mid)
            member_labels.append(f"_{name}_ [{basis_label(inferred)}]" if inferred else name)
        lines.append(f"- Bridges: {', '.join(member_labels) if member_labels else 'none'}")
        if c.top_targets:
            lines.append(
                "- Reaches into: "
                + ", ".join(ctx.display_name(t) for t in c.top_targets)
            )
        lines.append("")

    # --- Who to look at next --------------------------------------------------
    lines.append("## Who to look at next")
    lines.append("")
    lines.append(
        "Ranked by how much each unharvested target would change the picture, not by how "
        "senior the person is."
    )
    lines.append("")
    lines.extend(
        _table(
            ["#", "Target", "Priority", "Known bridges", "Clusters touched", "Novelty"],
            [
                [
                    str(rank),
                    ctx.display_name(n.target_id),
                    fmt(n.priority),
                    str(n.n_known_bridges),
                    str(n.n_clusters_touched),
                    fmt(n.novelty),
                ]
                for rank, n in enumerate(sorted_next_targets(result), start=1)
            ],
        )
    )

    # --- Provenance footer ----------------------------------------------------
    lines.append("## Run")
    lines.append("")
    lines.append(f"- Fingerprint: `{result.fingerprint or 'not recorded'}`")
    lines.append(f"- Inferred edges used: {'yes' if result.used_inferred_edges else 'no'}")
    for key in sorted(result.params):
        lines.append(f"- {key}: `{result.params[key]}`")
    lines.append("")
    lines.append(f"_{UI_DRIFT_NOTICE}_")
    lines.append("")

    return redact_text("\n".join(lines))


__all__ = ["render_markdown"]
