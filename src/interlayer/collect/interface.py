"""The acquisition boundary: the ``Collector`` protocol and its record types.

Everything in this package reads **files**. No collector opens a socket, and no
collector accepts a credential of any kind — not as an argument, not from an
environment variable, not from a config file. Enforcement precedent tracks *where
the code runs*, not what data is collected, so the file boundary is an
architectural invariant here rather than a policy statement.

Two properties of this layer are load-bearing downstream:

* **Provenance on every record.** ``source``, ``collected_at``, ``posture`` and
  ``truncated``. A capped result set means the absence of an edge is not evidence
  of no edge, and the analysis layer must be able to tell the difference.
* **Five capture states, not two.** ``complete``, ``truncated``, ``empty``,
  ``unavailable`` and ``skipped_by_degree`` are all distinct. Collapsing "we
  looked and there was nothing" into "we never looked" is the single easiest way
  to turn a thin harvest into a confident, wrong answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from interlayer.core import ids
from interlayer.core.models import (
    CompliancePosture,
    Edge,
    EdgeOrigin,
    Firm,
    Member,
    Provenance,
    Target,
)

#: Bumped when the record shapes this module emits change.
COLLECTOR_SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """How badly a diagnostic should be taken."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"
    """The input was refused outright; no records were produced from it."""


class Code(str, Enum):
    """Catalogue of every diagnostic this package can emit.

    Kept in one enum so the CLI can render them, tests can assert on them, and
    the operator runbook can document them without going hunting.
    """

    INPUT_MISSING = "input.missing"
    INPUT_EMPTY = "input.empty"
    INPUT_NOT_JSON = "input.not_json"
    INPUT_UNREADABLE = "input.unreadable"

    HAR_UNSANITISED = "har.unsanitised"
    HAR_MISSING_ENTRIES = "har.missing_entries"
    HAR_NO_MATCHING_ENTRIES = "har.no_matching_entries"
    HAR_NO_RESPONSE_BODY = "har.no_response_body"
    HAR_BODY_NOT_DECODABLE = "har.body_not_decodable"
    HAR_BODY_NOT_JSON = "har.body_not_json"
    HAR_BAD_TIMESTAMP = "har.bad_timestamp"

    VOYAGER_SHAPE_UNRECOGNISED = "voyager.shape_unrecognised"
    VOYAGER_FALLBACK_USED = "voyager.fallback_used"
    VOYAGER_NETWORK_FILTER_MISSING = "voyager.network_filter_missing"
    VOYAGER_NO_CONNECTION_OF = "voyager.no_connection_of"
    VOYAGER_ENTITY_UNIDENTIFIED = "voyager.entity_unidentified"

    CSV_MISSING_HEADER = "csv.missing_header"
    CSV_UNKNOWN_COLUMNS = "csv.unknown_columns"
    CSV_MISSING_REQUIRED_FIELD = "csv.missing_required_field"
    CSV_BAD_ENUM = "csv.bad_enum"
    CSV_BAD_TIMESTAMP = "csv.bad_timestamp"
    CSV_BAD_INT = "csv.bad_int"
    CSV_CONFLICTING_TARGET_FIELD = "csv.conflicting_target_field"

    TARGET_UNRESOLVED = "target.unresolved"
    TARGET_UNKNOWN_FIRM = "target.unknown_firm"
    TARGET_IN_OWN_MUTUALS = "target.in_own_mutuals"

    CAPTURE_TRUNCATED = "capture.truncated"
    CAPTURE_DEGREE_PRUNED = "capture.degree_pruned"
    CAPTURE_DEGREE_CONTRADICTION = "capture.degree_contradiction"

    POSTURE_NOT_ALLOWED = "posture.not_allowed"
    COLLECTOR_UNKNOWN = "collector.unknown"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A machine-readable, user-facing note about one input.

    ``schema_key`` is what makes a shape change legible: when a parser fails
    because LinkedIn moved a JSON path, the diagnostic names the exact
    ``schemas.py`` entry that no longer matches, so the fix is a one-line table
    edit rather than an investigation.
    """

    code: Code
    message: str
    severity: Severity = Severity.WARNING
    schema_key: str | None = None
    location: str | None = None
    remedy: str | None = None
    count: int = 1

    def __str__(self) -> str:
        head = f"[{self.severity.value}] {self.code.value}"
        if self.location:
            head += f" at {self.location}"
        parts = [f"{head}: {self.message}"]
        if self.schema_key:
            from interlayer.collect import schemas

            parts.append(f"  schema: {schemas.describe(self.schema_key)}")
        if self.remedy:
            parts.append(f"  try: {self.remedy}")
        return "\n".join(parts)


class CollectorError(RuntimeError):
    """Raised only for conditions the caller cannot recover from.

    Malformed *records* never raise — they become diagnostics. This is for
    programmer or configuration errors: an unknown collector name, a posture
    outside the allowlist.
    """


class CompliancePostureError(CollectorError):
    """A collector or record declared a posture outside the allowlist."""


# ---------------------------------------------------------------------------
# Identity and capture records
# ---------------------------------------------------------------------------


class Degree(str, Enum):
    """Shortest-path degree between the user and a target."""

    FIRST = "1"
    SECOND = "2"
    THIRD = "3"
    OUT = "out"
    UNKNOWN = "unknown"

    @property
    def can_have_mutuals(self) -> bool:
        """3rd degree and out-of-network have zero shared 1st-degree connections
        by the definition of shortest-path degree. This is the pruning oracle —
        free, lossless, and the reason a 300-target list becomes ~141 visits."""
        return self in (Degree.FIRST, Degree.SECOND, Degree.UNKNOWN)


class CaptureStatus(str, Enum):
    """What happened when a target's mutual list was looked at."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    SKIPPED_BY_DEGREE = "skipped_by_degree"
    """Pruned as 3rd/out. Sound, but an INFERENCE — never an observation."""


#: Statuses that put edges into the graph.
EDGE_YIELDING_STATUSES: frozenset[CaptureStatus] = frozenset(
    {CaptureStatus.COMPLETE, CaptureStatus.TRUNCATED}
)


@dataclass(frozen=True, slots=True)
class PersonRef:
    """Identity of a person as captured. At least one field must be set (V1)."""

    name: str | None = None
    profile_url: str | None = None
    urn: str | None = None
    headline: str | None = None
    member_distance: str | None = None

    @property
    def slug(self) -> str | None:
        """Canonical LinkedIn public identifier, if one is recoverable."""
        return ids.normalize_slug(self.profile_url)

    @property
    def key(self) -> str:
        """Join key, strongest identifier first. The prefix records which one
        matched, so resolution strength is reportable per edge."""
        if self.urn:
            return self.urn
        slug = self.slug
        if slug:
            return f"url:{slug}"
        return f"name:{ids.name_key(self.name)}"

    @property
    def is_identified(self) -> bool:
        return bool(self.urn or self.slug or (self.name or "").strip())

    @property
    def is_strongly_identified(self) -> bool:
        return bool(self.urn or self.slug)


@dataclass(frozen=True, slots=True)
class TargetRef(PersonRef):
    """A target-firm person, plus the two fields that gate collection."""

    id: str = ""
    firm: str = "other"
    degree: Degree = Degree.UNKNOWN
    opaque_id: str | None = None
    """The value of the ``connectionOf`` parameter, when the capture came from a
    search URL. Opaque by contract: never constructed, never interpreted."""

    @property
    def firm_enum(self) -> Firm | None:
        """``core.models.Firm`` member, or ``None`` for an out-of-registry firm."""
        try:
            return Firm(self.firm)
        except ValueError:
            return None


@dataclass(frozen=True, slots=True)
class MutualCapture:
    """One capture event for one target — the unit of everything downstream."""

    target: TargetRef
    status: CaptureStatus
    mutuals: tuple[PersonRef, ...] = ()
    reported_count: int | None = None
    pages_seen: tuple[int, ...] = ()
    collector: str = ""
    collector_version: str = COLLECTOR_SCHEMA_VERSION
    posture: CompliancePosture = CompliancePosture.MANUAL_CAPTURE
    captured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source_url: str | None = None
    source_artifact: str | None = None
    source_artifact_sha256: str | None = None
    parser_strategy: str | None = None
    network_filter: tuple[str, ...] | None = None
    notes: str = ""

    @property
    def observed_count(self) -> int:
        return len(self.mutuals)

    @property
    def is_truncated(self) -> bool:
        """V5: a reported count above the observed count forces truncation,
        whatever the collector claimed."""
        if self.status is CaptureStatus.TRUNCATED:
            return True
        return self.reported_count is not None and self.reported_count > self.observed_count

    @property
    def is_inferred(self) -> bool:
        return self.status is CaptureStatus.SKIPPED_BY_DEGREE

    @property
    def yields_edges(self) -> bool:
        return self.status in EDGE_YIELDING_STATUSES

    def provenance(self) -> Provenance:
        """The frozen-contract provenance stamped onto every emitted record."""
        return Provenance(
            source=self.collector,
            collected_at=self.captured_at,
            posture=self.posture,
            truncated=self.is_truncated,
        )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """Everything one collector produced from one input, diagnostics included."""

    collector: str = ""
    posture: CompliancePosture = CompliancePosture.MANUAL_CAPTURE
    members: tuple[Member, ...] = ()
    targets: tuple[Target, ...] = ()
    edges: tuple[Edge, ...] = ()
    captures: tuple[MutualCapture, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def ok(self) -> bool:
        """False when anything fatal happened."""
        return not any(d.severity is Severity.FATAL for d in self.diagnostics)

    def of_severity(self, *severities: Severity) -> tuple[Diagnostic, ...]:
        wanted = set(severities)
        return tuple(d for d in self.diagnostics if d.severity in wanted)

    def with_diagnostics(self, *extra: Diagnostic) -> CollectionResult:
        return replace(self, diagnostics=self.diagnostics + extra)

    @property
    def truncated_target_ids(self) -> tuple[str, ...]:
        """Targets whose capture was capped. Absence of an edge here is not
        evidence of no edge, and the report must say so."""
        return tuple(
            sorted({c.target.id for c in self.captures if c.is_truncated and c.target.id})
        )

    def coverage_states(self) -> dict[str, int]:
        """V12 accounting. A target absent from all inputs is ``unobserved`` and
        is counted by the caller, not here — this file only sees what was read."""
        counts = {
            "observed_complete": 0,
            "observed_truncated": 0,
            "observed_empty": 0,
            "unavailable": 0,
            "pruned_by_degree": 0,
        }
        for capture in self.captures:
            if capture.status is CaptureStatus.SKIPPED_BY_DEGREE:
                counts["pruned_by_degree"] += 1
            elif capture.status is CaptureStatus.UNAVAILABLE:
                counts["unavailable"] += 1
            elif capture.status is CaptureStatus.EMPTY:
                counts["observed_empty"] += 1
            elif capture.is_truncated:
                counts["observed_truncated"] += 1
            else:
                counts["observed_complete"] += 1
        return counts


def merge_results(
    results: Iterable[CollectionResult],
    *,
    collector: str = "merged",
    posture: CompliancePosture = CompliancePosture.MANUAL_CAPTURE,
) -> CollectionResult:
    """Concatenate results, de-duplicating members, targets and edges.

    Edge dedupe follows ``ids.edge_key``. On conflict the record with the most
    recent ``collected_at`` wins, and a truncated capture never overwrites a
    complete one's ``truncated=False``... it does the opposite: truncation is
    sticky, because a single capped observation makes the pair's completeness
    unknowable.
    """
    members: dict[str, Member] = {}
    targets: dict[str, Target] = {}
    edges: dict[tuple[str, str], Edge] = {}
    captures: list[MutualCapture] = []
    diagnostics: list[Diagnostic] = []

    for result in results:
        for member in result.members:
            existing = members.get(member.member_id)
            if existing is None or _newer(member.provenance, existing.provenance):
                members[member.member_id] = member
        for target in result.targets:
            existing_t = targets.get(target.target_id)
            if existing_t is None or (target.harvested and not existing_t.harvested):
                targets[target.target_id] = target
        for edge in result.edges:
            key = ids.edge_key(edge.member_id, edge.target_id)
            existing_e = edges.get(key)
            edges[key] = _merge_edge(existing_e, edge)
        captures.extend(result.captures)
        diagnostics.extend(result.diagnostics)

    return CollectionResult(
        collector=collector,
        posture=posture,
        members=tuple(members[k] for k in sorted(members)),
        targets=tuple(targets[k] for k in sorted(targets)),
        edges=tuple(edges[k] for k in sorted(edges)),
        captures=tuple(captures),
        diagnostics=tuple(diagnostics),
    )


def _newer(left: Provenance | None, right: Provenance | None) -> bool:
    if left is None:
        return False
    if right is None:
        return True
    return left.collected_at > right.collected_at


def _merge_edge(existing: Edge | None, incoming: Edge) -> Edge:
    if existing is None:
        return incoming
    winner = incoming if _newer(incoming.provenance, existing.provenance) else existing
    truncated = any(
        p is not None and p.truncated for p in (existing.provenance, incoming.provenance)
    )
    observed = existing.observed or incoming.observed
    provenance = winner.provenance
    if provenance is not None and provenance.truncated != truncated:
        provenance = replace(provenance, truncated=truncated)
    return replace(
        winner,
        origin=EdgeOrigin.OBSERVED if observed else winner.origin,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Capture -> frozen-contract records
# ---------------------------------------------------------------------------


def split_display_name(name: str | None) -> tuple[str, str]:
    """Split a rendered display name into (first, last) for presentation only.

    This uses the crude "last whitespace-separated token is the family name"
    convention, which is wrong for a great many names. That is tolerable here
    **only** because identity never depends on it: ``core.ids`` keys on the slug
    when there is one and on an order-free sorted-token key when there is not.
    Nothing in this package decides which token is a surname for matching.
    """
    tokens = (name or "").split()
    if not tokens:
        return ("", "")
    if len(tokens) == 1:
        return (tokens[0], "")
    return (" ".join(tokens[:-1]), tokens[-1])


def member_from_ref(ref: PersonRef, provenance: Provenance) -> Member:
    """Build a ``Member`` from a captured person reference."""
    first, last = split_display_name(ref.name)
    slug = ref.slug or ref.urn
    return Member(
        member_id=ids.member_id(slug=slug, first_name=first, last_name=last),
        first_name=first,
        last_name=last,
        linkedin_slug=ref.slug,
        position=ref.headline,
        provenance=provenance,
    )


def target_from_ref(ref: TargetRef, provenance: Provenance, *, harvested: bool) -> Target | None:
    """Build a ``Target``, or ``None`` when the firm is outside the registry.

    A firm we do not recognise is not silently coerced: the caller turns the
    ``None`` into a ``TARGET_UNKNOWN_FIRM`` diagnostic. Inventing a firm would
    put a person into the wrong per-firm result set.
    """
    firm = ref.firm_enum
    if firm is None:
        return None
    first, last = split_display_name(ref.name)
    return Target(
        target_id=ref.id or ids.target_id(firm=firm.value, slug=ref.slug or ref.opaque_id),
        firm=firm,
        first_name=first,
        last_name=last,
        linkedin_slug=ref.slug,
        harvested=harvested,
        provenance=provenance,
    )


def records_from_capture(capture: MutualCapture) -> CollectionResult:
    """Convert one capture event into ``Member`` / ``Target`` / ``Edge`` records.

    Both shipped collectors funnel through here, so provenance and truncation
    semantics cannot drift between them.
    """
    diagnostics: list[Diagnostic] = []
    provenance = capture.provenance()
    target_ref = capture.target

    target = target_from_ref(
        target_ref,
        provenance,
        harvested=capture.status is not CaptureStatus.SKIPPED_BY_DEGREE,
    )
    if target is None:
        diagnostics.append(
            Diagnostic(
                code=Code.TARGET_UNKNOWN_FIRM,
                message=(
                    f"target {target_ref.id or target_ref.key!r} declares firm "
                    f"{target_ref.firm!r}, which is not one of the registry firms; "
                    "its edges are kept but no Target record is emitted"
                ),
                severity=Severity.WARNING,
                schema_key="csv.firm_values",
                location=target_ref.id or target_ref.key,
            )
        )
    target_id = target.target_id if target is not None else target_ref.id

    if capture.is_truncated:
        diagnostics.append(
            Diagnostic(
                code=Code.CAPTURE_TRUNCATED,
                message=(
                    f"target {target_id!r}: captured {capture.observed_count} of "
                    f"{capture.reported_count if capture.reported_count is not None else '?'} "
                    "reported mutual connections — absence of an edge here is not "
                    "evidence of no edge"
                ),
                severity=Severity.WARNING,
                location=target_id,
                remedy="re-open the mutual-connections list and page to the end",
            )
        )
    if capture.status is CaptureStatus.SKIPPED_BY_DEGREE:
        diagnostics.append(
            Diagnostic(
                code=Code.CAPTURE_DEGREE_PRUNED,
                message=(
                    f"target {target_id!r} pruned at degree {target_ref.degree.value}: "
                    "zero shared 1st-degree connections by definition. This is a sound "
                    "inference, not an observation, and is reported separately."
                ),
                severity=Severity.INFO,
                location=target_id,
            )
        )
    if (
        not target_ref.degree.can_have_mutuals
        and capture.status is CaptureStatus.COMPLETE
        and capture.observed_count > 0
    ):
        diagnostics.append(
            Diagnostic(
                code=Code.CAPTURE_DEGREE_CONTRADICTION,
                message=(
                    f"target {target_id!r} is recorded at degree "
                    f"{target_ref.degree.value} but {capture.observed_count} mutual "
                    "connections were observed; trusting the observation and treating "
                    "the degree badge as stale"
                ),
                severity=Severity.WARNING,
                location=target_id,
                schema_key="csv.degree_values",
            )
        )

    members: list[Member] = []
    edges: list[Edge] = []
    seen: set[str] = set()
    for ref in capture.mutuals:
        if not ref.is_identified:
            diagnostics.append(
                Diagnostic(
                    code=Code.VOYAGER_ENTITY_UNIDENTIFIED,
                    message=(
                        "dropped a captured person carrying no name, profile URL or "
                        "URN — nothing to match on"
                    ),
                    severity=Severity.WARNING,
                    location=target_id,
                )
            )
            continue
        if _is_same_person(ref, target_ref):
            diagnostics.append(
                Diagnostic(
                    code=Code.TARGET_IN_OWN_MUTUALS,
                    message=(
                        f"target {target_id!r} appeared in its own mutual-connections "
                        "list; dropped"
                    ),
                    severity=Severity.WARNING,
                    location=target_id,
                )
            )
            continue
        if ref.key in seen:
            continue
        seen.add(ref.key)
        member = member_from_ref(ref, provenance)
        members.append(member)
        if capture.yields_edges and target_id:
            edges.append(
                Edge(
                    member_id=member.member_id,
                    target_id=target_id,
                    origin=EdgeOrigin.OBSERVED,
                    confidence=1.0,
                    evidence=(
                        f"{capture.collector}: shared-connections surface for "
                        f"{target_id} ({capture.status.value})"
                    ),
                    provenance=provenance,
                )
            )

    if capture.yields_edges and not target_id:
        diagnostics.append(
            Diagnostic(
                code=Code.TARGET_UNRESOLVED,
                message=(
                    f"{len(members)} captured mutual connections could not be attached: "
                    "the target could not be resolved to a known person. Supply a "
                    "targets file, or pass a default firm so the capture can be keyed."
                ),
                severity=Severity.ERROR,
                location=target_ref.opaque_id or target_ref.key,
            )
        )

    return CollectionResult(
        collector=capture.collector,
        posture=capture.posture,
        members=tuple(members),
        targets=(target,) if target is not None else (),
        edges=tuple(edges),
        captures=(capture,),
        diagnostics=tuple(diagnostics),
    )


def _is_same_person(left: PersonRef, right: PersonRef) -> bool:
    """V4 check: is this captured person the target itself?"""
    if left.urn and right.urn and left.urn == right.urn:
        return True
    left_slug, right_slug = left.slug, right.slug
    if left_slug and right_slug and left_slug == right_slug:
        return True
    if isinstance(right, TargetRef) and right.opaque_id:
        opaque = right.opaque_id.casefold()
        if left.urn and opaque in left.urn.casefold():
            return True
        if left_slug and left_slug == opaque:
            return True
    return False


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Collector(Protocol):
    """Every acquisition adapter.

    Adapters read files. No adapter opens a socket, and none accepts a session
    credential. ``collect`` must not raise on a malformed record: it returns what
    parsed and reports the rest as diagnostics, because a 5,000-row hand-edited
    CSV with one bad date should not cost the user the other 4,999 rows.
    """

    name: str
    version: str
    compliance_posture: CompliancePosture

    def accepts(self, path: Path) -> bool:
        """Cheap check — extension plus a small header sniff. No full parse."""
        ...

    def collect(self, path: Path) -> CollectionResult:
        """Parse one input file into records plus diagnostics."""
        ...


def check_posture(
    posture: CompliancePosture,
    allowed: Sequence[CompliancePosture] | frozenset[CompliancePosture],
    *,
    what: str,
) -> None:
    """Raise unless ``posture`` is in the allowlist.

    Called twice per run — once on the collector, once on every record it
    produces — because a collector that declares one posture and emits another
    is exactly the failure this gate exists to catch.
    """
    if posture not in set(allowed):
        raise CompliancePostureError(
            f"{what} declares compliance posture {posture.value!r}, which is not in "
            f"the enabled set {sorted(p.value for p in allowed)}. Enabling a posture "
            "is an explicit, logged user act; it is never inferred from an input file."
        )


__all__ = [
    "COLLECTOR_SCHEMA_VERSION",
    "EDGE_YIELDING_STATUSES",
    "CaptureStatus",
    "Code",
    "CollectionResult",
    "Collector",
    "CollectorError",
    "CompliancePostureError",
    "Degree",
    "Diagnostic",
    "MutualCapture",
    "PersonRef",
    "Severity",
    "TargetRef",
    "check_posture",
    "member_from_ref",
    "merge_results",
    "records_from_capture",
    "split_display_name",
    "target_from_ref",
]
