"""``interlayer report`` — render the self-contained HTML report and the manifest.

The stage is deliberately paranoid about two things that are cheap to check here
and expensive to discover later:

* **Egress.** :func:`assert_self_contained` scans the finished document for any
  reference that could cause a fetch and refuses to write the file if it finds
  one. A report that phones home is worse than no report — it hands the entire
  analysed list to the company the analysis is about.
* **Redaction.** :meth:`Redactor.audit` runs over the assembled payload before a
  single byte is written, so a leaked name is a crash rather than a file on disk
  that somebody has already forwarded.

Both checks run on every render, redacted or not, because the failure they guard
against is silent.
"""

from __future__ import annotations

import hashlib
import platform
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from interlayer import __version__
from interlayer.config import Settings
from interlayer.errors import InterlayerError
from interlayer.io import read_jsonl, secure_dir, secure_write, sha256_file
from interlayer.models import (
    SCHEMA_VERSION,
    Cluster,
    GraphEdge,
    Org,
    Person,
    RunManifest,
    ScoredCluster,
    ScoredPerson,
    stable_id,
)
from interlayer.report import text
from interlayer.report.layout import build_layout
from interlayer.report.redact import Redactor
from interlayer.report.view import build_context

__all__ = ["assert_self_contained", "run"]

REPORT_STAGE_VERSION = "1.0.0"

#: Exactly the tag the build contract mandates, byte for byte. It is emitted
#: from this constant rather than typed into the template so there is one place
#: to look and nothing to drift.
#:
#: Note what it does *not* contain: a ``script-src``. Under ``default-src
#: 'none'`` that means no JavaScript of any kind runs in this document, inline
#: or otherwise. The report therefore ships zero script and gets its
#: interactivity from ``<details>``, ``:target`` and CSS sibling selectors over
#: real form controls. Widening the policy with ``script-src 'unsafe-inline'``
#: was the alternative -- it would have allowed the canvas renderer verbatim --
#: and was rejected: the mandated policy is a contract other agents and the
#: Wave 3 tests match against, adding a directive weakens the strongest
#: guarantee this file makes, and the interaction that actually matters here
#: (read a row, open its evidence, find a person in the picture) does not need
#: a scripting engine. Inline SVG is markup rather than a fetched sub-resource,
#: so the graph renders with no directive relaxed at all.
CSP_META = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; img-src data:; font-src data:\">"
)

#: Anything that could make the browser talk to a third party when the file is
#: opened, plus profile-photo hosts by name so the failure is legible.
_EGRESS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("absolute url", re.compile(r"https?://", re.IGNORECASE)),
    ("protocol-relative url", re.compile(r'(?:src|href|content|url)\s*=?\s*["\'(]//')),
    ("linkedin media host", re.compile(r"licdn|media\.licdn|linkedin\.com", re.IGNORECASE)),
    ("script element", re.compile(r"<\s*script\b", re.IGNORECASE)),
    ("iframe element", re.compile(r"<\s*iframe\b", re.IGNORECASE)),
    ("external stylesheet", re.compile(r"<\s*link\b[^>]*\brel\s*=\s*[\"']?stylesheet", re.I)),
    ("webfont", re.compile(r"@font-face|fonts\.(?:googleapis|gstatic)", re.IGNORECASE)),
    ("remote image", re.compile(r"<\s*img\b(?![^>]*\bsrc\s*=\s*[\"']data:)", re.IGNORECASE)),
    ("css remote fetch", re.compile(r"url\(\s*[\"']?(?!data:)(?:https?:)?//", re.IGNORECASE)),
)


def assert_self_contained(html: str) -> None:
    """Refuse to emit a document that could fetch anything when opened."""
    hits = [name for name, pattern in _EGRESS_PATTERNS if pattern.search(html)]
    if hits:
        raise InterlayerError(
            "report would make outbound requests when opened ("
            + ", ".join(sorted(hits))
            + "); refusing to write it"
        )


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _cluster_palette(
    clusters: list[Cluster], scored_clusters: list[ScoredCluster]
) -> tuple[dict[str, int], dict[str, int]]:
    """Palette index per cluster and per person.

    Indices follow cluster rank so the highest-scoring cluster is always colour
    0; unranked clusters follow in id order. Both loops iterate sorted sequences
    because a colour that depends on ``PYTHONHASHSEED`` is a diff on every run.
    """
    order = [c.cluster_id for c in sorted(scored_clusters, key=lambda c: (-c.score, c.cluster_id))]
    seen = set(order)
    order += sorted(c.cluster_id for c in clusters if c.cluster_id not in seen)
    by_cluster = {cid: index for index, cid in enumerate(order)}
    by_person: dict[str, int] = {}
    for cluster in sorted(clusters, key=lambda c: c.cluster_id):
        index = by_cluster.get(cluster.cluster_id, -1)
        for person_id in sorted(cluster.member_ids):
            by_person[person_id] = index
    return by_cluster, by_person


def _inputs_digest(paths: list[Path]) -> str:
    """One hash over every artifact this stage consumed, in a fixed order."""
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(sha256_file(path).encode("ascii") if path.is_file() else b"-")
        digest.update(b"\x1e")
    return digest.hexdigest()


def run(cfg: Settings) -> None:
    """Render ``report.html`` and ``manifest.json`` into ``cfg.artifact_dir``."""
    people = read_jsonl(cfg.people_path, Person, produced_by="ingest")
    orgs = read_jsonl(cfg.orgs_path, Org, produced_by="normalize")
    edges = read_jsonl(cfg.edges_path, GraphEdge, produced_by="build")
    clusters = read_jsonl(cfg.clusters_path, Cluster, produced_by="cluster")
    scored_people = read_jsonl(cfg.scored_people_path, ScoredPerson, produced_by="score")
    scored_clusters = read_jsonl(cfg.scored_clusters_path, ScoredCluster, produced_by="score")

    # Sorted at the boundary: everything downstream may assume a stable order.
    people.sort(key=lambda p: p.person_id)
    orgs.sort(key=lambda o: o.org_id)
    edges.sort(key=lambda e: (e.source, e.target))
    clusters.sort(key=lambda c: c.cluster_id)
    scored_people.sort(key=lambda p: p.person_id)
    scored_clusters.sort(key=lambda c: c.cluster_id)

    redactor = Redactor(cfg, people)

    inputs_sha = _inputs_digest(
        [
            cfg.people_path,
            cfg.orgs_path,
            cfg.edges_path,
            cfg.clusters_path,
            cfg.scored_people_path,
            cfg.scored_clusters_path,
        ]
    )
    mode = "redacted" if cfg.redact else "identified"
    run_id = stable_id("run", inputs_sha, str(cfg.seed), mode, __version__)

    degrees: dict[str, int] = {}
    for edge in edges:
        degrees[edge.source] = degrees.get(edge.source, 0) + 1
        degrees[edge.target] = degrees.get(edge.target, 0) + 1

    scored_by_id = {p.person_id: p for p in scored_people}
    node_ids = sorted(
        {edge.source for edge in edges}
        | {edge.target for edge in edges}
        | {p.person_id for p in scored_people if p.score != 0.0}
    )
    palette_by_cluster, palette_by_person = _cluster_palette(clusters, scored_clusters)
    ranks = {
        p.person_id: p.rank
        for p in sorted(scored_people, key=lambda p: (-p.score, p.person_id))[: cfg.top_n]
    }

    layout = build_layout(
        edges,
        seed=cfg.seed,
        node_ids=node_ids,
        degrees=degrees,
        cluster_index=palette_by_person,
        ranks=ranks,
        observed_people=frozenset(p.person_id for p in scored_people if p.has_observed_evidence),
        priority={pid: sp.score for pid, sp in scored_by_id.items()},
    )

    context = build_context(
        cfg,
        redactor,
        people=people,
        orgs=orgs,
        edges=edges,
        clusters=clusters,
        scored_people=scored_people,
        scored_clusters=scored_clusters,
        layout=layout,
        cluster_palette=palette_by_cluster,
        run_id=run_id,
    )

    # Gate three of the redaction design: nothing reaches the template until the
    # assembled payload is provably free of known names.
    redactor.audit(
        {
            key: value
            for key, value in context.items()
            # Fixed report copy is authored in this repository and contains no
            # export data; scanning it would only produce collisions with
            # ordinary English words that happen to be somebody's name.
            if key not in {"evidence_labels", "band_legend", "provenance_legend", "limitations"}
        }
    )

    html = _environment().get_template("report.html.j2").render(
        csp_meta=CSP_META,
        title=text.REPORT_TITLE,
        subtitle=text.REPORT_SUBTITLE,
        header_line=text.HEADER_LINE,
        firm_note=text.FIRM_NOTE,
        sources_note=text.SOURCES_NOTE,
        redaction_note=text.REDACTION_NOTE,
        not_built_for=text.NOT_BUILT_FOR,
        rule_of_thumb=text.RULE_OF_THUMB,
        tool_version=__version__,
        schema_version=SCHEMA_VERSION,
        **context,
    )
    assert_self_contained(html)

    secure_dir(cfg.artifact_dir)
    secure_write(cfg.report_path, html)
    _write_manifest(cfg, run_id, inputs_sha, mode, context["counts"], len(html.encode("utf-8")))


def _write_manifest(
    cfg: Settings,
    run_id: str,
    inputs_sha: str,
    mode: str,
    counts: dict[str, int],
    report_bytes: int,
) -> None:
    """The provenance record: what ran, on what, with which knobs.

    This is the only artifact that carries a clock. ``report.html`` deliberately
    does not, so that the same input and seed produce a byte-identical document
    and a diff between two reports is a diff in the data.
    """
    config_sha = hashlib.sha256(cfg.model_dump_json().encode("utf-8")).hexdigest()
    input_sha: str | None = None
    input_name: str | None = None
    if cfg.input_csv is not None and Path(cfg.input_csv).is_file():
        input_sha = sha256_file(Path(cfg.input_csv))
        input_name = Path(cfg.input_csv).name

    manifest = RunManifest(
        run_id=run_id,
        created_at=datetime.now(UTC),
        seed=cfg.seed,
        tool_version=__version__,
        python_version=platform.python_version() or sys.version.split()[0],
        input_sha256=input_sha,
        input_filename=input_name,
        config_sha256=config_sha,
        stage_versions={
            "report": REPORT_STAGE_VERSION,
            "schema": SCHEMA_VERSION,
            # RunManifest has no field for the redaction mode or for a digest
            # over the stage's own inputs (both required by research P-20), and
            # models.py is scaffold-owned. Recorded here rather than dropped.
            "report.mode": mode,
            "report.inputs_sha256": inputs_sha,
        },
        counts={**{k: int(v) for k, v in sorted(counts.items())}, "report_bytes": report_bytes},
    )
    secure_write(cfg.manifest_path, manifest.model_dump_json(indent=2) + "\n")
