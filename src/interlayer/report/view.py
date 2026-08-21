"""Turn artifacts into the template context.

Nothing here touches disk and nothing here formats HTML; the template receives
plain sorted lists of plain dicts, every one of which has been through
:meth:`Redactor.emit`. Keeping the shaping separate from the rendering is what
makes the redaction allowlist a single auditable place rather than a property of
however many ``{% if not redact %}`` branches a template happened to grow.

Sort order is explicit at every boundary. ``PYTHONHASHSEED`` produced four
distinct set-iteration orders across four values in Wave 1, so any collection
derived from a set is sorted before it is emitted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from interlayer.config import Settings
from interlayer.models import (
    Cluster,
    EvidenceItem,
    GraphEdge,
    Org,
    Person,
    Position,
    Provenance,
    ScoredCluster,
    ScoredPerson,
    TargetFirm,
)
from interlayer.report import text
from interlayer.report.confidence import (
    SPECULATIVE_MAX,
    band_for,
    confidence_for,
    mean_confidence,
)
from interlayer.report.layout import GraphLayout, is_observed_edge
from interlayer.report.redact import Redactor

__all__ = ["FIRM_LABELS", "FIRM_ORDER", "build_context"]

#: Fixed column order. Citadel LLC and Citadel Securities are separate columns
#: and are never summed; see ``text.FIRM_NOTE``, which is rendered next to them.
FIRM_ORDER: tuple[TargetFirm, ...] = (
    TargetFirm.JANE_STREET,
    TargetFirm.CITADEL_LLC,
    TargetFirm.CITADEL_SECURITIES,
)

FIRM_LABELS: dict[TargetFirm, str] = {
    TargetFirm.JANE_STREET: "Jane Street",
    TargetFirm.CITADEL_LLC: "Citadel LLC",
    TargetFirm.CITADEL_SECURITIES: "Citadel Securities",
}

#: P-17: a redacted cluster smaller than this re-identifies its members.
MIN_REDACTED_CLUSTER = 3


def _primary_position(person: Person) -> Position | None:
    """The position a staleness note should be about: current, else most recent."""
    if not person.positions:
        return None
    current = [p for p in person.positions if p.is_current]
    pool = current or list(person.positions)
    return max(pool, key=lambda p: ((p.start.sort_key if p.start else 0), p.company_raw))


def _stale_note(person: Person, red: Redactor) -> str:
    """§3.8 item 4, expressed as a date rather than an age.

    The spec asks for "employer data is 2y 4m old", which needs a clock; a clock
    in the document body would make two runs of the same input differ byte for
    byte. The underlying date says the same thing and is reproducible.
    """
    position = _primary_position(person)
    if position is None:
        return "no employer field in your export"
    if position.is_current and position.start is not None:
        return (
            f"employer field still says “current”, since {red.year(position.start)} — "
            "LinkedIn profiles lag reality by months or years"
        )
    when = red.year(position.end) or red.year(position.start)
    if when:
        return f"employer field dated {when} — may be out of date"
    return "employer field is undated — may be out of date"


def _org_label(org: Org | None, red: Redactor) -> str | None:
    """Org name, or a generic stand-in when redacting a non-target org.

    A named target firm is public and enormous. A three-person consultancy is
    not, and naming it alongside a cluster would undo the pseudonyms, so in
    redacted mode only gazetteer targets are named.
    """
    if org is None:
        return None
    if red.enabled and not org.is_target:
        return {"school": "a shared school", "company": "a shared employer"}.get(
            str(org.kind), "an organisation"
        )
    return red.scrub(org.name) or None


def _synth_detail(item: EvidenceItem, org_label: str | None, via_label: str | None) -> str:
    """Rebuild an evidence line from structured fields only.

    ``EvidenceItem.detail`` is unbounded prose written by the scoring stage and
    is *expected* to contain names — "mutual connection with Jane Doe" — some of
    which belong to target-firm employees who never appear in ``people.jsonl``
    and therefore cannot be scrubbed by name. In redacted mode it is dropped
    outright and this replaces it.
    """
    parts: list[str] = []
    if org_label:
        parts.append(org_label)
    if via_label:
        parts.append(f"via {via_label}")
    if item.hops is not None:
        parts.append(f"{item.hops} hop{'s' if item.hops != 1 else ''} away")
    return " · ".join(parts)


def _evidence_rows(
    person: ScoredPerson,
    red: Redactor,
    orgs: Mapping[str, Org],
    names: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ordered = sorted(person.evidence, key=lambda e: (-e.contribution, e.kind, e.detail))
    for item in ordered:
        org_label = _org_label(orgs.get(item.org_id or ""), red)
        via_label = (
            red.person_label(item.via_person_id, names.get(item.via_person_id, ""))
            if item.via_person_id
            else None
        )
        detail = (
            _synth_detail(item, org_label, via_label) if red.enabled else red.scrub(item.detail)
        )
        rows.append(
            red.emit(
                "evidence",
                {
                    "kind": item.kind,
                    "observed": item.provenance is Provenance.OBSERVED,
                    "contribution": round(item.contribution, 4),
                    "detail": detail,
                    "org_label": org_label,
                    "via_label": via_label,
                    "hops": item.hops,
                },
            )
        )
    return rows


def _per_firm(values: Mapping[TargetFirm, float]) -> list[float]:
    """Fixed-order firm columns. Never a total — the firms are different companies."""
    return [round(float(values.get(firm, 0.0)), 4) for firm in FIRM_ORDER]


def _person_rows(
    scored: Sequence[ScoredPerson],
    red: Redactor,
    people: Mapping[str, Person],
    orgs: Mapping[str, Org],
    cluster_labels: Mapping[str, str],
) -> list[dict[str, Any]]:
    names = {pid: p.full_name for pid, p in people.items()}
    rows: list[dict[str, Any]] = []
    for person in scored:
        confidence = confidence_for(person.evidence)
        slug, _label = band_for(confidence)
        record = people.get(person.person_id)
        raw: dict[str, Any] = {
            "id": red.anchor(person.person_id),
            "anchor": f"p-{red.anchor(person.person_id)}",
            "label": red.person_label(person.person_id, person.full_name),
            "rank": person.rank,
            "score": round(person.score, 4),
            "confidence": confidence,
            "band": slug,
            "observed": person.has_observed_evidence,
            "speculative": confidence < SPECULATIVE_MAX,
            "per_firm": _per_firm(person.per_firm),
            "components": {
                "direct": round(person.components.direct, 4),
                "alumni": round(person.components.alumni, 4),
                "proximity": round(person.components.proximity, 4),
                "cluster": round(person.components.cluster, 4),
                "title": round(person.components.title, 4),
            },
            "cluster_id": person.cluster_id or "",
            "cluster_label": cluster_labels.get(person.cluster_id or "", ""),
            "cluster_anchor": f"c-{person.cluster_id}" if person.cluster_id else "",
            "hops": person.hops_to_target,
            "n_evidence": len(person.evidence),
            "evidence": _evidence_rows(person, red, orgs, names),
            "stale_note": _stale_note(record, red) if record else "not in your export",
            # Free text, deliberately outside the redacted allowlist.
            "employer": red.scrub(record.current_company) if record else "",
            "headline": red.scrub(record.headline) if record else "",
        }
        # Profile URLs are never emitted, in either mode. An <a href> is not a
        # sub-resource load, so it would not breach the CSP, but it would put
        # linkedin.com strings in a file the operator may forward, and clicking
        # one sends a referrer to LinkedIn from a page listing everyone this
        # tool flagged. The upside -- saving one search -- does not pay for that.
        rows.append(red.emit("person", raw))
    return rows


def _cluster_label(
    cluster: Cluster | None,
    scored: ScoredCluster,
    red: Redactor,
    orgs: Mapping[str, Org],
) -> str:
    """Hedged cluster label (§3.8 item 5), rebuilt from structure when redacting."""
    if not red.enabled:
        return red.scrub(scored.label or (cluster.label if cluster else "")) or "Unlabelled cluster"
    targets = [
        orgs[org_id].name
        for org_id, _count in (cluster.top_orgs if cluster else ())
        if org_id in orgs and orgs[org_id].is_target
    ]
    if targets:
        return "Possible " + " / ".join(sorted(set(targets))) + " cluster"
    return "Unlabelled cluster"


def _cluster_rows(
    scored_clusters: Sequence[ScoredCluster],
    clusters: Mapping[str, Cluster],
    red: Redactor,
    orgs: Mapping[str, Org],
    people: Mapping[str, Person],
    person_confidence: Mapping[str, float],
    observed_people: frozenset[str],
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    suppressed = 0
    for scored in scored_clusters:
        cluster = clusters.get(scored.cluster_id)
        size = scored.size or (cluster.size if cluster else 0)
        if red.enabled and size < MIN_REDACTED_CLUSTER:
            suppressed += 1
            continue
        member_ids = sorted(cluster.member_ids) if cluster else sorted(scored.top_person_ids)
        confidences = [person_confidence.get(pid, 0.0) for pid in member_ids]
        mean_conf = mean_confidence(confidences)
        top_ids = sorted(
            scored.top_person_ids,
            key=lambda pid: (-person_confidence.get(pid, 0.0), pid),
        )
        members = [
            red.emit(
                "member",
                {
                    "anchor": f"p-{red.anchor(pid)}",
                    "label": red.person_label(pid, people[pid].full_name if pid in people else ""),
                    "observed": pid in observed_people,
                },
            )
            for pid in top_ids
        ]
        top_orgs = [
            red.emit("org", {"label": label, "count": count})
            for label, count in (
                (_org_label(orgs.get(org_id), red), count)
                for org_id, count in (cluster.top_orgs if cluster else ())
            )
            if label
        ]
        rationale = (
            f"{size} people · target density {scored.target_density:.2f} · "
            f"mean confidence {mean_conf:.2f}"
            if red.enabled
            else red.scrub(scored.rationale)
        )
        rows.append(
            red.emit(
                "cluster",
                {
                    "id": scored.cluster_id,
                    "anchor": f"c-{scored.cluster_id}",
                    "label": _cluster_label(cluster, scored, red, orgs),
                    "rank": scored.rank,
                    "score": round(scored.score, 4),
                    "size": size,
                    "target_density": round(scored.target_density, 4),
                    "per_firm": _per_firm(scored.per_firm),
                    "mean_confidence": mean_conf,
                    "band": band_for(mean_conf)[0],
                    "observed_members": sum(1 for pid in member_ids if pid in observed_people),
                    "members": members,
                    "top_orgs": top_orgs,
                    "rationale": rationale,
                },
            )
        )
    return rows, suppressed


def build_context(
    cfg: Settings,
    red: Redactor,
    *,
    people: Sequence[Person],
    orgs: Sequence[Org],
    edges: Sequence[GraphEdge],
    clusters: Sequence[Cluster],
    scored_people: Sequence[ScoredPerson],
    scored_clusters: Sequence[ScoredCluster],
    layout: GraphLayout,
    cluster_palette: Mapping[str, int],
    run_id: str,
) -> dict[str, Any]:
    """Assemble everything the template renders. Every list is explicitly sorted."""
    by_id = {p.person_id: p for p in people}
    org_by_id = {o.org_id: o for o in orgs}
    cluster_by_id = {c.cluster_id: c for c in clusters}

    person_confidence = {p.person_id: confidence_for(p.evidence) for p in scored_people}
    observed_people = frozenset(p.person_id for p in scored_people if p.has_observed_evidence)

    # §3.8 item 6: confidence descending is the default sort, and the score is
    # never hidden to make the page look tidier.
    ordered = sorted(
        scored_people,
        key=lambda p: (-person_confidence[p.person_id], -p.score, p.person_id),
    )

    labels_for_clusters = {
        sc.cluster_id: _cluster_label(cluster_by_id.get(sc.cluster_id), sc, red, org_by_id)
        for sc in scored_clusters
    }

    observed_rows = _person_rows(
        [p for p in ordered if p.has_observed_evidence], red, by_id, org_by_id, labels_for_clusters
    )
    inferred_pool = [
        p
        for p in ordered
        if not p.has_observed_evidence and person_confidence[p.person_id] >= SPECULATIVE_MAX
    ]
    speculative_pool = [
        p
        for p in ordered
        if not p.has_observed_evidence and person_confidence[p.person_id] < SPECULATIVE_MAX
    ]
    inferred_rows = _person_rows(
        inferred_pool[: cfg.top_n], red, by_id, org_by_id, labels_for_clusters
    )
    speculative_rows = _person_rows(
        speculative_pool[: cfg.top_n], red, by_id, org_by_id, labels_for_clusters
    )

    ordered_clusters = sorted(scored_clusters, key=lambda c: (-c.score, c.cluster_id))
    cluster_rows, suppressed = _cluster_rows(
        ordered_clusters[: cfg.top_n],
        cluster_by_id,
        red,
        org_by_id,
        by_id,
        person_confidence,
        observed_people,
    )

    node_rows = [
        red.emit(
            "node",
            {
                "id": f"p-{red.anchor(node.person_id)}",
                "label": red.person_label(
                    node.person_id,
                    by_id[node.person_id].full_name if node.person_id in by_id else "",
                ),
                "x": node.x,
                "y": node.y,
                "r": node.r,
                "cluster": node.cluster,
                "rank": node.rank,
                "observed": node.observed,
            },
        )
        for node in layout.nodes
    ]
    # Grouped by palette index so a circle carries no class attribute of its
    # own; at a few thousand nodes that is tens of kilobytes of the file.
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in node_rows:
        grouped.setdefault(int(row.get("cluster", -1)), []).append(row)
    node_groups = [
        {"cluster": index, "nodes": grouped[index]} for index in sorted(grouped, key=int)
    ]

    n_observed_edges = sum(1 for e in edges if is_observed_edge(e))
    counts = {
        "people": len(people),
        "orgs": len(orgs),
        "edges": len(edges),
        "observed_edges": n_observed_edges,
        "inferred_edges": len(edges) - n_observed_edges,
        "clusters": len(clusters),
        "scored_people": len(scored_people),
        "scored_clusters": len(scored_clusters),
        "nonzero_people": sum(1 for p in scored_people if p.score != 0.0),
        "observed_people": len(observed_people),
        "inferred_people": len(inferred_pool),
        "speculative_people": len(speculative_pool),
        "clusters_suppressed": suppressed,
        "graph_nodes": layout.n_nodes,
        "graph_edges": layout.n_edges,
        "graph_omitted": layout.omitted_nodes,
    }

    palette_legend = [
        {"index": index, "label": labels_for_clusters.get(cid, "Unlabelled cluster")}
        for cid, index in sorted(cluster_palette.items(), key=lambda kv: (kv[1], kv[0]))
        if index >= 0
    ][:12]

    return {
        "run_id": run_id,
        "redact": red.enabled,
        "seed": cfg.seed,
        "top_n": cfg.top_n,
        "counts": counts,
        "firm_columns": [FIRM_LABELS[f] for f in FIRM_ORDER],
        "observed_rows": observed_rows,
        "inferred_rows": inferred_rows,
        "speculative_rows": speculative_rows,
        "cluster_rows": cluster_rows,
        "node_groups": node_groups,
        "inferred_d": layout.inferred_d,
        "observed_d": layout.observed_d,
        "palette_legend": palette_legend,
        "evidence_labels": text.EVIDENCE_KIND_LABELS,
        "band_labels": {slug: label for slug, label, _d in text.BAND_LEGEND},
        "band_legend": text.BAND_LEGEND,
        "provenance_legend": text.PROVENANCE_LEGEND,
        "limitations": text.LIMITATIONS,
    }
