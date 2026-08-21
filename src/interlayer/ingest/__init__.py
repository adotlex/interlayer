"""Stage 1 -- parse a LinkedIn ``Connections.csv`` into people and ego edges.

This is the only stage that touches the operator's raw export, so it is also the
stage that decides what is allowed to exist downstream:

* **Identity.** Every person is keyed by a normalised profile slug (see
  ``slug.py``). A row without one is dropped, because an identity we invent here
  becomes a wrong edge four stages later.
* **Privacy.** Email addresses never reach an artifact unless ``keep_emails`` is
  set, and then only as an HMAC. Warnings name line numbers, never row contents.
* **Determinism.** Output is sorted by ``person_id`` before it is written, so the
  same export always produces byte-identical artifacts.

Nothing is dropped quietly: every skipped or repaired row is counted by reason,
printed to stderr and persisted next to the artifacts.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from interlayer.config import Settings
from interlayer.errors import ConfigError, IngestError
from interlayer.ingest.csv_reader import RawRow, RowIssue, read_rows
from interlayer.ingest.records import ParsedRow, parse_row
from interlayer.ingest.slug import canonical_url, normalize_slug
from interlayer.io import dump_json, write_jsonl
from interlayer.models import Connection, Person

__all__ = [
    "CONNECTIONS_FILENAME",
    "STATS_FILENAME",
    "IngestResult",
    "canonical_url",
    "ingest_csv",
    "normalize_slug",
    "run",
]

CONNECTIONS_FILENAME = "connections.jsonl"
"""``Settings`` has a property for every other artifact but not this one, so the
name is published here for the stages that need to read it back."""

STATS_FILENAME = "ingest_stats.json"
"""Row counts by reason. ``manifest.json`` belongs to the report stage, so the
drop accounting is persisted separately rather than written into someone else's
artifact."""

MAX_LOGGED_ISSUES = 10
"""Per reason. A broken export can produce thousands of identical warnings; the
aggregate counts carry the rest."""

# soft_wrap: these lines are diagnostics that get piped into logs, and rich
# otherwise hard-wraps them at 80 columns, splitting paths mid-token.
console = Console(stderr=True, soft_wrap=True)


@dataclass(frozen=True)
class IngestResult:
    """What the stage produced, including everything it refused to produce."""

    people: tuple[Person, ...] = ()
    connections: tuple[Connection, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)
    issues: tuple[RowIssue, ...] = ()
    source: Path | None = None
    header_line: int = 0

    @property
    def skipped(self) -> tuple[RowIssue, ...]:
        return tuple(i for i in self.issues if i.fatal)


def _input_path(cfg: Settings) -> Path:
    if cfg.input_csv is None:
        raise ConfigError(
            "ingest needs an input file: run `interlayer ingest <Connections.csv>` "
            "or set input_csv in the config"
        )
    path = Path(cfg.input_csv)
    if not path.is_file():
        raise IngestError(f"input CSV not found: {path}")
    return path


def _dedupe(
    rows: list[RawRow], *, keep_emails: bool, hmac_key: str | None
) -> tuple[list[ParsedRow], list[RowIssue], Counter[str]]:
    """Parse rows in file order, keeping the first record for each identity."""
    kept: list[ParsedRow] = []
    issues: list[RowIssue] = []
    notes: Counter[str] = Counter()
    by_slug: dict[str, int] = {}
    by_person_id: dict[str, str] = {}

    for row in rows:
        parsed = parse_row(row, keep_emails=keep_emails, hmac_key=hmac_key)
        if isinstance(parsed, RowIssue):
            issues.append(parsed)
            continue
        if parsed.slug in by_slug:
            first = by_slug[parsed.slug]
            issues.append(
                RowIssue(row.lineno, "duplicate_slug", detail=f"first seen on line {first}")
            )
            continue
        clash = by_person_id.get(parsed.person.person_id)
        if clash is not None:
            # models.stable_id casefolds its inputs, so two member URNs that
            # differ only in case collapse to one id even though they are two
            # people. Keeping both would corrupt every downstream join, so the
            # second is dropped loudly instead.
            issues.append(
                RowIssue(row.lineno, "person_id_collision", detail=f"collides with slug {clash!r}")
            )
            continue
        by_slug[parsed.slug] = row.lineno
        by_person_id[parsed.person.person_id] = parsed.slug
        notes.update(parsed.notes)
        kept.append(parsed)
    return kept, issues, notes


def ingest_csv(cfg: Settings) -> IngestResult:
    """Parse the configured export into records without writing anything."""
    path = _input_path(cfg)
    header, rows, structural_issues = read_rows(path, require_email=cfg.keep_emails)
    kept, row_issues, notes = _dedupe(
        rows, keep_emails=cfg.keep_emails, hmac_key=cfg.email_hmac_key
    )

    issues = sorted(structural_issues + row_issues, key=lambda i: (i.lineno, i.reason))
    if header.duplicates:
        notes["duplicate_header_column"] += len(header.duplicates)

    # Sorting by person_id, not by file order: the id is content-addressed, so
    # the same export always serialises in the same order regardless of how the
    # rows happened to be laid out.
    kept.sort(key=lambda p: p.person.person_id)

    counts: dict[str, int] = {
        # ``short_row`` issues describe rows that were repaired and kept, so only
        # the fatal structural ones are additional to ``rows``.
        "rows_total": len(rows) + sum(1 for i in structural_issues if i.fatal),
        "people": len(kept),
        "connections": len(kept),
        "skipped_total": sum(1 for i in issues if i.fatal),
    }
    for issue in issues:
        prefix = "skipped" if issue.fatal else "noted"
        counts[f"{prefix}_{issue.reason}"] = counts.get(f"{prefix}_{issue.reason}", 0) + 1
    for reason, n in notes.items():
        counts[f"noted_{reason}"] = counts.get(f"noted_{reason}", 0) + n

    return IngestResult(
        people=tuple(p.person for p in kept),
        connections=tuple(p.connection for p in kept),
        counts=dict(sorted(counts.items())),
        issues=tuple(issues),
        source=path,
        header_line=header.lineno,
    )


def _report(result: IngestResult) -> None:
    """Print the run summary. Line numbers only -- rows are third-party data."""
    console.print(
        f"[dim]ingest[/] {escape(str(result.source))}: header on line {result.header_line}, "
        f"{result.counts.get('people', 0)} people, "
        f"{result.counts.get('connections', 0)} connections"
    )
    shown: Counter[str] = Counter()
    for issue in result.issues:
        shown[issue.reason] += 1
        if shown[issue.reason] > MAX_LOGGED_ISSUES:
            continue
        style = "yellow" if issue.fatal else "dim"
        verb = "skipped" if issue.fatal else "repaired"
        detail = f" ({escape(issue.detail)})" if issue.detail else ""
        console.print(f"[{style}]ingest: line {issue.lineno} {verb}: {issue.reason}{detail}[/]")
    for reason, total in sorted(shown.items()):
        if total > MAX_LOGGED_ISSUES:
            console.print(f"[yellow]ingest: … and {total - MAX_LOGGED_ISSUES} more {reason}[/]")
    skipped = result.counts.get("skipped_total", 0)
    if skipped:
        breakdown = ", ".join(
            f"{k.removeprefix('skipped_')}={v}"
            for k, v in sorted(result.counts.items())
            if k.startswith("skipped_") and k != "skipped_total"
        )
        console.print(f"[yellow]ingest: skipped {skipped} row(s): {breakdown}[/]")


def run(cfg: Settings) -> None:
    """Entry point: read ``cfg.input_csv``, write people and connections."""
    result = ingest_csv(cfg)
    write_jsonl(cfg.people_path, result.people)
    write_jsonl(cfg.artifact(CONNECTIONS_FILENAME), result.connections)
    dump_json(cfg.artifact(STATS_FILENAME), result.counts)
    _report(result)
    if not result.people:
        console.print("[yellow]ingest: no usable rows -- check that this is a Connections.csv[/]")
