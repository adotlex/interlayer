"""JSON renderer, plus the round-trippable analysis snapshot.

Two different jobs live here and they are deliberately kept apart:

* :func:`render_json` produces a *report* — redacted, carrying the PRIV-18
  notices as data, with inference marked by an explicit ``inferred`` flag on
  every row (PRIV-19 in a format that has no visual channel).
* :func:`dump_result` / :func:`load_result` produce a *snapshot* — the analysis
  result written to disk so that ``interlayer report`` and ``interlayer next``
  can run without recomputing the graph. It round-trips exactly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from interlayer.report import (
    ANALYSIS_SCHEMA_VERSION,
    PROVENANCE_NOTICE,
    PURPOSE_NOTICE,
    REDISTRIBUTION_NOTICE,
    RETENTION_TEMPLATE,
    SCHEMA_VERSION,
    THIRD_PARTY_NOTICE,
    UI_DRIFT_NOTICE,
    ReportContext,
    basis_label,
    coverage_caveat,
    harvest_is_complete,
    inference_banner,
    is_inferred,
    reach_display,
    review_statement,
    sorted_bridges,
    sorted_clusters,
    sorted_next_targets,
)
from interlayer.report.redact import redact_value

if TYPE_CHECKING:  # pragma: no cover - typing only
    from interlayer.core.models import AnalysisResult


def build_payload(result: AnalysisResult, ctx: ReportContext | None = None) -> dict[str, Any]:
    """The report as a plain dict, redacted and ready to serialise."""
    ctx = ctx or ReportContext()
    cov = result.coverage
    complete = harvest_is_complete(cov)

    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": ctx.generated_at.isoformat(),
        "tool": f"interlayer {ctx.tool_version}",
        # PRIV-18: the notices are first-class fields, not a comment, so a
        # downstream consumer cannot render this without them.
        "notice": {
            "purpose": PURPOSE_NOTICE,
            "retention": RETENTION_TEMPLATE.format(days=ctx.retention_days),
            "retention_days": ctx.retention_days,
            "third_party_personal_data": THIRD_PARTY_NOTICE,
            "redistribution": REDISTRIBUTION_NOTICE,
            "provenance": PROVENANCE_NOTICE,
            "ui_drift": UI_DRIFT_NOTICE,
        },
        # PRIV-20
        "review": {
            "unadjudicated_count": result.unadjudicated_count,
            "statement": review_statement(result),
        },
        # PRIV-19
        "inference": {
            "used_inferred_edges": result.used_inferred_edges,
            "statement": inference_banner(result),
        },
        "coverage": {
            "n_targets_total": cov.n_targets_total,
            "n_targets_harvested": cov.n_targets_harvested,
            "n_members_total": cov.n_members_total,
            "n_bridges": cov.n_bridges,
            "fraction": cov.fraction,
            "harvest_complete": complete,
            "reach_is_lower_bound": not complete,
            "caveat": coverage_caveat(cov),
        },
        "bridges": [],
        "clusters": [],
        "next_targets": [],
        "fingerprint": result.fingerprint,
        "params": dict(result.params),
    }

    for rank, b in enumerate(sorted_bridges(result), start=1):
        inferred = is_inferred(result, ctx, b.member_id)
        payload["bridges"].append(
            {
                "rank": rank,
                "member_id": b.member_id,
                "display_name": ctx.display_name(b.member_id),
                "inferred": inferred,
                "basis": basis_label(inferred),
                "reach": b.reach,
                "reach_is_lower_bound": not complete,
                "reach_display": reach_display(b.reach, complete),
                "rarity": b.rarity,
                "betweenness": b.betweenness,
                "effective_size": b.effective_size,
                "constraint": b.constraint,
                "autonomy": b.autonomy,
                "composite": b.composite,
            }
        )

    for c in sorted_clusters(result):
        payload["clusters"].append(
            {
                "cluster_id": c.cluster_id,
                "label": c.label,
                "stability": c.stability,
                "score_sum": c.score_sum,
                "members": [
                    {
                        "member_id": mid,
                        "display_name": ctx.display_name(mid),
                        "inferred": is_inferred(result, ctx, mid),
                    }
                    for mid in c.member_ids
                ],
                "top_targets": [
                    {"target_id": tid, "display_name": ctx.display_name(tid)}
                    for tid in c.top_targets
                ],
            }
        )

    for rank, n in enumerate(sorted_next_targets(result), start=1):
        payload["next_targets"].append(
            {
                "rank": rank,
                "target_id": n.target_id,
                "display_name": ctx.display_name(n.target_id),
                "priority": n.priority,
                "n_known_bridges": n.n_known_bridges,
                "n_clusters_touched": n.n_clusters_touched,
                "novelty": n.novelty,
            }
        )

    return redact_value(payload)


def render_json(
    result: AnalysisResult, ctx: ReportContext | None = None, *, indent: int = 2
) -> str:
    """Render *result* as JSON, with every contact detail stripped."""
    payload = build_payload(result, ctx)
    return json.dumps(payload, indent=indent, ensure_ascii=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Analysis snapshot — machine round-trip, not a human artefact
# ---------------------------------------------------------------------------


def dump_result(
    result: AnalysisResult, names: dict[str, str] | None = None
) -> dict[str, Any]:
    """Serialise an ``AnalysisResult`` losslessly.

    Written by ``interlayer analyse`` so that ``report`` and ``next`` are pure
    rendering commands over a stored result rather than re-runs of the graph
    pipeline. Contact details are stripped here too: this file sits on disk and
    the cost of the extra pass is nil.
    """
    payload: dict[str, Any] = {
        "schema": ANALYSIS_SCHEMA_VERSION,
        "clusters": [
            {
                "cluster_id": c.cluster_id,
                "label": c.label,
                "member_ids": list(c.member_ids),
                "top_targets": list(c.top_targets),
                "stability": c.stability,
                "score_sum": c.score_sum,
            }
            for c in result.clusters
        ],
        "brokerage": [
            {
                "member_id": b.member_id,
                "reach": b.reach,
                "rarity": b.rarity,
                "betweenness": b.betweenness,
                "effective_size": b.effective_size,
                "constraint": b.constraint,
                "autonomy": b.autonomy,
                "composite": b.composite,
            }
            for b in result.brokerage
        ],
        "coverage": {
            "n_targets_total": result.coverage.n_targets_total,
            "n_targets_harvested": result.coverage.n_targets_harvested,
            "n_members_total": result.coverage.n_members_total,
            "n_bridges": result.coverage.n_bridges,
        },
        "next_targets": [
            {
                "target_id": n.target_id,
                "priority": n.priority,
                "n_known_bridges": n.n_known_bridges,
                "n_clusters_touched": n.n_clusters_touched,
                "novelty": n.novelty,
            }
            for n in result.next_targets
        ],
        "used_inferred_edges": result.used_inferred_edges,
        "unadjudicated_count": result.unadjudicated_count,
        "fingerprint": result.fingerprint,
        "params": dict(result.params),
        "names": dict(names or {}),
    }
    return redact_value(payload)


def load_result(payload: dict[str, Any]) -> tuple[AnalysisResult, dict[str, str]]:
    """Inverse of :func:`dump_result`. Returns the result and its name map."""
    from interlayer.core.models import (
        AnalysisResult,
        Brokerage,
        Cluster,
        Coverage,
        NextTarget,
    )

    schema = payload.get("schema")
    if schema != ANALYSIS_SCHEMA_VERSION:
        raise ValueError(
            f"not an interlayer analysis snapshot: expected schema "
            f"{ANALYSIS_SCHEMA_VERSION!r}, found {schema!r}"
        )

    result = AnalysisResult(
        clusters=tuple(
            Cluster(
                cluster_id=int(c["cluster_id"]),
                label=str(c["label"]),
                member_ids=tuple(c.get("member_ids", ())),
                top_targets=tuple(c.get("top_targets", ())),
                stability=float(c.get("stability", 1.0)),
                score_sum=float(c.get("score_sum", 0.0)),
            )
            for c in payload.get("clusters", [])
        ),
        brokerage=tuple(
            Brokerage(
                member_id=str(b["member_id"]),
                reach=int(b.get("reach", 0)),
                rarity=float(b.get("rarity", 0.0)),
                betweenness=float(b.get("betweenness", 0.0)),
                effective_size=float(b.get("effective_size", 0.0)),
                constraint=float(b.get("constraint", 1.0)),
                autonomy=float(b.get("autonomy", 0.0)),
                composite=float(b.get("composite", 0.0)),
            )
            for b in payload.get("brokerage", [])
        ),
        coverage=Coverage(**{k: int(v) for k, v in payload.get("coverage", {}).items()}),
        next_targets=tuple(
            NextTarget(
                target_id=str(n["target_id"]),
                priority=float(n.get("priority", 0.0)),
                n_known_bridges=int(n.get("n_known_bridges", 0)),
                n_clusters_touched=int(n.get("n_clusters_touched", 0)),
                novelty=float(n.get("novelty", 0.0)),
            )
            for n in payload.get("next_targets", [])
        ),
        used_inferred_edges=bool(payload.get("used_inferred_edges", False)),
        unadjudicated_count=int(payload.get("unadjudicated_count", 0)),
        fingerprint=str(payload.get("fingerprint", "")),
        params=dict(payload.get("params", {})),
    )
    return result, dict(payload.get("names", {}))


__all__ = ["build_payload", "dump_result", "load_result", "render_json"]
