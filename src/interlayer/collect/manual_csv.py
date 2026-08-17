"""Hand-fillable CSV collector — the guaranteed floor.

This is the collector that must work when everything else has broken: no HAR, no
DevTools, no cooperation from the response shape. A person with a text editor and
a browser can fill in ``mutuals.csv`` by hand and get a correct answer out of the
tool. Every acquisition mode is capped at the same ~150-250 targets/month by the
Commercial Use Limit, so the "slow" manual path costs coverage nothing; it costs
only the user's wall-clock time, once.

Two files are understood, both from R4's proposed input schemas, columns verbatim
(see ``schemas.py`` entries ``csv.targets_columns`` and ``csv.mutuals_columns``):

``targets.csv``
    The roster. ``degree`` is load-bearing — 3rd-degree and out-of-network targets
    have zero shared 1st-degree connections by definition, so they are pruned and
    recorded as ``skipped_by_degree``: an inference, labelled as one.

``mutuals.csv``
    Long/tidy edge list, one row per (target, mutual). A target with no mutuals
    gets one row with an empty ``mutual_name`` and ``capture_status=empty``, which
    is how the format says "I looked, there were none" rather than "I never
    looked".

Partial data is the expected case, not the error case. A row missing a profile URL
still yields an edge; a bad timestamp costs that row and nothing else.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interlayer.collect import schemas
from interlayer.collect.interface import (
    COLLECTOR_SCHEMA_VERSION,
    CaptureStatus,
    Code,
    CollectionResult,
    Degree,
    Diagnostic,
    MutualCapture,
    PersonRef,
    Severity,
    TargetRef,
    merge_results,
    records_from_capture,
    target_from_ref,
)
from interlayer.core.models import CompliancePosture, Firm, Provenance, Target

COLLECTOR_NAME = "manual_csv"


class ManualCsvCollector:
    """Reads R4's hand-fillable ``targets.csv`` and ``mutuals.csv``.

    ``targets`` maps ``target_id`` to a known target and is normally the output of
    :func:`read_targets_csv`. ``default_firm`` is the fallback for a mutuals file
    supplied on its own: without either, rows whose firm cannot be determined are
    still parsed, still produce edges, and produce a diagnostic saying so.
    """

    name = COLLECTOR_NAME
    version = COLLECTOR_SCHEMA_VERSION
    compliance_posture = CompliancePosture.MANUAL_CAPTURE

    def __init__(
        self,
        *,
        targets: Mapping[str, TargetRef] | None = None,
        default_firm: Firm | None = None,
    ) -> None:
        self.targets: dict[str, TargetRef] = dict(targets or {})
        self.default_firm = default_firm

    # -- discovery ---------------------------------------------------------

    def accepts(self, path: Path) -> bool:
        """Extension plus a header sniff. Never a full parse."""
        if path.suffix.casefold() != ".csv":
            return False
        header = _sniff_header(path)
        if header is None:
            return False
        return _looks_like(header, "csv.mutuals_columns") or _looks_like(
            header, "csv.targets_columns"
        )

    def kind_of(self, path: Path) -> str | None:
        """``"mutuals"``, ``"targets"`` or ``None`` — strict, all required columns."""
        header = _sniff_header(path)
        if header is None:
            return None
        if _looks_like(header, "csv.mutuals_columns"):
            return "mutuals"
        if _looks_like(header, "csv.targets_columns"):
            return "targets"
        return None

    def probable_kind(self, path: Path) -> str | None:
        """Best guess for a header that is close but incomplete.

        A file missing one required column should get "you are missing
        ``capture_status``", not "unrecognised file". The guess only decides which
        reader produces the diagnostic; it never relaxes what the reader requires.
        """
        header = _sniff_header(path)
        if header is None:
            return None
        present = {cell.strip() for cell in header}
        mutuals = set(schemas.values("csv.mutuals_columns"))
        targets = set(schemas.values("csv.targets_columns"))
        mutuals_score = len(present & (mutuals - targets))
        targets_score = len(present & (targets - mutuals))
        if mutuals_score == targets_score == 0:
            return None
        return "mutuals" if mutuals_score >= targets_score else "targets"

    # -- collection --------------------------------------------------------

    def collect(self, path: Path) -> CollectionResult:
        kind = self.kind_of(path) or self.probable_kind(path)
        if kind == "targets":
            table, result = self.read_targets(path)
            self.targets.update(table)
            return result
        if kind == "mutuals":
            return self.read_mutuals(path)
        return _empty(
            Diagnostic(
                code=Code.CSV_MISSING_HEADER,
                message=(
                    f"{path.name}: header does not match either hand-fillable layout. "
                    "Expected the mutuals columns or the targets columns."
                ),
                severity=Severity.FATAL,
                schema_key="csv.mutuals_columns",
                location=str(path),
                remedy=(
                    "copy the header row from docs; the first column must be "
                    f"{schemas.values('csv.mutuals_columns')[0]!r}"
                ),
            )
        )

    def collect_pair(self, targets_path: Path, mutuals_path: Path) -> CollectionResult:
        """Read a roster and its edge list together — the normal invocation."""
        table, target_result = self.read_targets(targets_path)
        self.targets.update(table)
        mutual_result = self.read_mutuals(mutuals_path)
        return merge_results(
            (target_result, mutual_result),
            collector=self.name,
            posture=self.compliance_posture,
        )

    # -- targets.csv -------------------------------------------------------

    def read_targets(self, path: Path) -> tuple[dict[str, TargetRef], CollectionResult]:
        """Parse ``targets.csv`` into a lookup table plus ``Target`` records."""
        rows, diagnostics, fatal = _read_rows(path, "csv.targets_columns")
        if fatal:
            return {}, _empty(*diagnostics)

        table: dict[str, TargetRef] = {}
        targets: list[Target] = []
        captures: list[MutualCapture] = []
        results: list[CollectionResult] = []

        for line_no, row in rows:
            where = f"{path.name}:{line_no}"
            target_id = _clean(row.get("target_id"))
            if not target_id:
                diagnostics.append(
                    _missing_field("target_id", where, "csv.targets_required_columns")
                )
                continue

            firm_raw = _clean(row.get("firm")) or "other"
            if firm_raw not in schemas.values("csv.firm_values"):
                diagnostics.append(_bad_enum("firm", firm_raw, where, "csv.firm_values"))

            degree_raw = _clean(row.get("degree")) or Degree.UNKNOWN.value
            degree, degree_diag = _parse_degree(degree_raw, where)
            if degree_diag is not None:
                diagnostics.append(degree_diag)

            observed_at, ts_diag = _parse_timestamp(
                _clean(row.get("degree_observed_at")), where, "degree_observed_at"
            )
            if ts_diag is not None:
                diagnostics.append(ts_diag)

            truncated = _parse_bool(_clean(row.get("enumeration_truncated")))
            ref = TargetRef(
                name=_clean(row.get("name")),
                profile_url=_clean(row.get("profile_url")),
                id=target_id,
                firm=firm_raw,
                degree=degree,
            )
            table[target_id] = ref

            provenance = Provenance(
                source=self.name,
                collected_at=observed_at or _now(),
                posture=self.compliance_posture,
                truncated=truncated,
            )
            if truncated:
                diagnostics.append(
                    Diagnostic(
                        code=Code.CAPTURE_TRUNCATED,
                        message=(
                            f"target {target_id!r} came from an enumeration that hit the "
                            "search result cap; the roster itself is incomplete"
                        ),
                        severity=Severity.WARNING,
                        location=where,
                        remedy="slice the enumeration search by title, location or school",
                    )
                )

            if degree.can_have_mutuals:
                target = target_from_ref(ref, provenance, harvested=False)
                if target is not None:
                    targets.append(target)
                else:
                    diagnostics.append(_unknown_firm(target_id, firm_raw, where))
                continue

            capture = MutualCapture(
                target=ref,
                status=CaptureStatus.SKIPPED_BY_DEGREE,
                collector=self.name,
                collector_version=self.version,
                posture=self.compliance_posture,
                captured_at=observed_at or _now(),
                notes=f"degree {degree.value}: no shared 1st-degree connections possible",
            )
            captures.append(capture)
            results.append(records_from_capture(capture))

        # ``captures`` is not repeated here: each pruned capture already travels
        # inside its own result from ``records_from_capture``.
        base = CollectionResult(
            collector=self.name,
            posture=self.compliance_posture,
            targets=tuple(targets),
            diagnostics=tuple(diagnostics),
        )
        merged = merge_results(
            [base, *results], collector=self.name, posture=self.compliance_posture
        )
        return table, merged

    # -- mutuals.csv -------------------------------------------------------

    def read_mutuals(self, path: Path) -> CollectionResult:
        """Parse ``mutuals.csv`` into captures, then into records."""
        rows, diagnostics, fatal = _read_rows(path, "csv.mutuals_columns")
        if fatal:
            return _empty(*diagnostics)

        grouped: dict[str, _MutualGroup] = {}
        order: list[str] = []

        for line_no, row in rows:
            where = f"{path.name}:{line_no}"
            target_id = _clean(row.get("target_id"))
            if not target_id:
                diagnostics.append(
                    _missing_field("target_id", where, "csv.mutuals_required_columns")
                )
                continue
            group = grouped.get(target_id)
            if group is None:
                group = _MutualGroup(target_id=target_id)
                grouped[target_id] = group
                order.append(target_id)
            diagnostics.extend(group.absorb(row, where))

        results: list[CollectionResult] = []
        for target_id in order:
            group = grouped[target_id]
            capture, group_diagnostics = group.to_capture(
                collector=self,
                known=self.targets.get(target_id),
            )
            diagnostics.extend(group_diagnostics)
            results.append(records_from_capture(capture))

        base = CollectionResult(
            collector=self.name,
            posture=self.compliance_posture,
            diagnostics=tuple(diagnostics),
        )
        return merge_results(
            [base, *results], collector=self.name, posture=self.compliance_posture
        )


# ---------------------------------------------------------------------------
# Row grouping
# ---------------------------------------------------------------------------

_TARGET_LEVEL = schemas.values("csv.mutuals_target_level_columns")


class _MutualGroup:
    """Accumulates every row belonging to one ``target_id``."""

    def __init__(self, target_id: str) -> None:
        self.target_id = target_id
        self.fields: dict[str, str] = {}
        self.mutuals: list[PersonRef] = []
        self.first_location: str | None = None
        self.rows_seen = 0

    def absorb(self, row: Mapping[str, Any], where: str) -> list[Diagnostic]:
        """Take target-level fields first-non-empty (V14) and collect the mutual."""
        diagnostics: list[Diagnostic] = []
        self.rows_seen += 1
        if self.first_location is None:
            self.first_location = where

        for column in _TARGET_LEVEL:
            incoming = _clean(row.get(column))
            if not incoming:
                continue
            existing = self.fields.get(column)
            if existing is None:
                self.fields[column] = incoming
            elif existing != incoming:
                diagnostics.append(
                    Diagnostic(
                        code=Code.CSV_CONFLICTING_TARGET_FIELD,
                        message=(
                            f"target {self.target_id!r}: column {column!r} says "
                            f"{existing!r} on an earlier row and {incoming!r} here; "
                            "keeping the first non-empty value"
                        ),
                        severity=Severity.WARNING,
                        location=where,
                        schema_key="csv.mutuals_target_level_columns",
                    )
                )

        name = _clean(row.get("mutual_name"))
        url = _clean(row.get("mutual_profile_url"))
        headline = _clean(row.get("mutual_headline"))
        if name or url:
            self.mutuals.append(
                PersonRef(name=name, profile_url=url, headline=headline)
            )
        return diagnostics

    def to_capture(
        self, *, collector: ManualCsvCollector, known: TargetRef | None
    ) -> tuple[MutualCapture, list[Diagnostic]]:
        diagnostics: list[Diagnostic] = []
        where = self.first_location or self.target_id

        status_raw = self.fields.get("capture_status", "")
        status, status_diag = _parse_status(status_raw, where, bool(self.mutuals))
        if status_diag is not None:
            diagnostics.append(status_diag)

        reported, reported_diag = _parse_int(self.fields.get("reported_count"), where)
        if reported_diag is not None:
            diagnostics.append(reported_diag)

        captured_at, ts_diag = _parse_timestamp(
            self.fields.get("captured_at"), where, "captured_at"
        )
        if ts_diag is not None:
            diagnostics.append(ts_diag)

        firm = known.firm if known is not None else _firm_name(collector.default_firm)
        degree = known.degree if known is not None else Degree.UNKNOWN
        target = TargetRef(
            name=known.name if known is not None else None,
            profile_url=known.profile_url if known is not None else None,
            id=self.target_id,
            firm=firm,
            degree=degree,
        )
        if known is None and collector.default_firm is None:
            diagnostics.append(
                Diagnostic(
                    code=Code.TARGET_UNRESOLVED,
                    message=(
                        f"target {self.target_id!r} is not in the roster and no default "
                        "firm was given, so its firm is unknown"
                    ),
                    severity=Severity.WARNING,
                    location=where,
                    remedy="pass the matching targets.csv, or set a default firm",
                )
            )

        capture = MutualCapture(
            target=target,
            status=status,
            mutuals=tuple(self.mutuals),
            reported_count=reported,
            collector=collector.name,
            collector_version=collector.version,
            posture=collector.compliance_posture,
            captured_at=captured_at or _now(),
            source_url=self.fields.get("source_url"),
            source_artifact=None,
            parser_strategy="manual_csv",
            notes=self.fields.get("collector", ""),
        )
        return capture, diagnostics


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _clean(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, list):  # csv gives a list for rows longer than the header
        raw = next((part for part in raw if part), "")
    return str(raw).strip()


def _sniff_header(path: Path) -> tuple[str, ...] | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                if any(cell.strip() for cell in row):
                    return tuple(cell.strip() for cell in row)
            return None
    except OSError:
        return None


def _looks_like(header: Sequence[str], schema_key: str) -> bool:
    expected = set(schemas.values(schema_key))
    present = {cell.strip() for cell in header}
    required_key = schema_key.replace("_columns", "_required_columns")
    try:
        required = set(schemas.values(required_key))
    except schemas.UnknownSchemaKey:  # pragma: no cover - both keys exist today
        required = expected
    return required.issubset(present) and bool(expected & present)


def _read_rows(
    path: Path, schema_key: str
) -> tuple[list[tuple[int, dict[str, Any]]], list[Diagnostic], bool]:
    """Read a CSV into (line number, row) pairs plus header diagnostics."""
    diagnostics: list[Diagnostic] = []
    if not path.exists():
        return [], [
            Diagnostic(
                code=Code.INPUT_MISSING,
                message=f"{path} does not exist",
                severity=Severity.FATAL,
                location=str(path),
            )
        ], True

    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            rows = [(index, dict(row)) for index, row in enumerate(reader, start=2)]
    except (OSError, UnicodeDecodeError) as exc:
        return [], [
            Diagnostic(
                code=Code.INPUT_UNREADABLE,
                message=f"{path} could not be read: {exc}",
                severity=Severity.FATAL,
                location=str(path),
            )
        ], True

    if not header:
        return [], [
            Diagnostic(
                code=Code.CSV_MISSING_HEADER,
                message=f"{path.name} is empty — no header row",
                severity=Severity.FATAL,
                schema_key=schema_key,
                location=str(path),
            )
        ], True

    expected = schemas.values(schema_key)
    required_key = schema_key.replace("_columns", "_required_columns")
    required = set(schemas.values(required_key))
    missing = sorted(required - set(header))
    if missing:
        return rows, [
            Diagnostic(
                code=Code.CSV_MISSING_HEADER,
                message=(
                    f"{path.name} is missing required column(s) {missing}; "
                    f"found {list(header)}"
                ),
                severity=Severity.FATAL,
                schema_key=required_key,
                location=str(path),
                remedy=f"the full column set is: {', '.join(expected)}",
            )
        ], True

    unknown = sorted(set(header) - set(expected))
    if unknown:
        diagnostics.append(
            Diagnostic(
                code=Code.CSV_UNKNOWN_COLUMNS,
                message=f"{path.name} has extra column(s) {unknown}; ignored",
                severity=Severity.INFO,
                schema_key=schema_key,
                location=str(path),
            )
        )
    if not rows:
        diagnostics.append(
            Diagnostic(
                code=Code.INPUT_EMPTY,
                message=f"{path.name} has a valid header but no data rows",
                severity=Severity.WARNING,
                location=str(path),
            )
        )
    return rows, diagnostics, False


def _parse_status(
    raw: str, where: str, has_mutuals: bool
) -> tuple[CaptureStatus, Diagnostic | None]:
    allowed = schemas.values("csv.capture_status_values")
    if not raw:
        fallback = CaptureStatus.COMPLETE if has_mutuals else CaptureStatus.UNAVAILABLE
        return fallback, Diagnostic(
            code=Code.CSV_MISSING_REQUIRED_FIELD,
            message=(
                f"no capture_status given; assuming {fallback.value!r}. Only this "
                "column separates 'observed, none' from 'not yet looked at'."
            ),
            severity=Severity.WARNING,
            schema_key="csv.capture_status_values",
            location=where,
        )
    value = raw.casefold()
    if value not in allowed:
        fallback = CaptureStatus.COMPLETE if has_mutuals else CaptureStatus.UNAVAILABLE
        return fallback, _bad_enum(
            "capture_status", raw, where, "csv.capture_status_values"
        )
    return CaptureStatus(value), None


def _parse_degree(raw: str, where: str) -> tuple[Degree, Diagnostic | None]:
    value = raw.casefold()
    if value in schemas.values("csv.degree_values"):
        return Degree(value), None
    return Degree.UNKNOWN, _bad_enum("degree", raw, where, "csv.degree_values")


def _parse_int(raw: str | None, where: str) -> tuple[int | None, Diagnostic | None]:
    if not raw:
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, Diagnostic(
            code=Code.CSV_BAD_INT,
            message=f"reported_count {raw!r} is not a whole number; ignored",
            severity=Severity.WARNING,
            location=where,
        )


def _parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().casefold() in {"true", "yes", "1", "y"}


def _parse_timestamp(
    raw: str | None, where: str, column: str
) -> tuple[datetime | None, Diagnostic | None]:
    """Parse an RFC 3339 timestamp, tolerating the ``Z`` suffix and naive input."""
    if not raw:
        return None, None
    text = raw.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, Diagnostic(
            code=Code.CSV_BAD_TIMESTAMP,
            message=(
                f"{column} {raw!r} is not an RFC 3339 timestamp; falling back to the "
                "time of this run, which makes retention accounting less accurate"
            ),
            severity=Severity.WARNING,
            location=where,
            remedy="use e.g. 2026-08-17T10:04:00Z",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), None


def _firm_name(firm: Firm | None) -> str:
    return firm.value if firm is not None else "other"


def _missing_field(column: str, where: str, schema_key: str) -> Diagnostic:
    return Diagnostic(
        code=Code.CSV_MISSING_REQUIRED_FIELD,
        message=f"row has no {column!r}; skipped",
        severity=Severity.ERROR,
        schema_key=schema_key,
        location=where,
    )


def _bad_enum(column: str, raw: str, where: str, schema_key: str) -> Diagnostic:
    return Diagnostic(
        code=Code.CSV_BAD_ENUM,
        message=(
            f"{column} {raw!r} is not one of "
            f"{list(schemas.values(schema_key))}; treated as unknown"
        ),
        severity=Severity.WARNING,
        schema_key=schema_key,
        location=where,
    )


def _unknown_firm(target_id: str, firm: str, where: str) -> Diagnostic:
    return Diagnostic(
        code=Code.TARGET_UNKNOWN_FIRM,
        message=(
            f"target {target_id!r} has firm {firm!r}, which is outside the registry; "
            "no Target record emitted"
        ),
        severity=Severity.WARNING,
        schema_key="csv.firm_values",
        location=where,
    )


def _empty(*diagnostics: Diagnostic) -> CollectionResult:
    return CollectionResult(
        collector=COLLECTOR_NAME,
        posture=CompliancePosture.MANUAL_CAPTURE,
        diagnostics=tuple(diagnostics),
    )


def read_targets_csv(path: Path) -> tuple[dict[str, TargetRef], CollectionResult]:
    """Module-level convenience wrapper around :meth:`ManualCsvCollector.read_targets`."""
    return ManualCsvCollector().read_targets(path)


def write_template(path: Path, schema_key: str = "csv.mutuals_columns") -> None:
    """Write an empty hand-fillable file with the correct header row.

    Users should never have to retype a ten-column header from documentation;
    getting one column name wrong is a fatal parse for no good reason.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(schemas.values(schema_key))


__all__ = [
    "COLLECTOR_NAME",
    "ManualCsvCollector",
    "read_targets_csv",
    "write_template",
]
