"""Parse LinkedIn's ``Connections.csv`` into :class:`Member` records.

This is the one parser that must be bulletproof: it is the only source of ``M``,
and every downstream number is computed over whatever it returns.

Three things it does that a naive reader does not:

* **It sniffs the header row.** The export prepends a "Notes:" preamble whose
  length has changed over time (0, 2, 3 and 5 lines are all attested). A fixed
  ``skiprows=3`` is a silent-corruption bug — it either eats the header or
  leaves preamble in the data, and both look like a successful parse. Ruling C1
  settles this: scan for the row that actually carries the columns.
* **It keeps only whitelisted fields** (PRIV-05), and **discards the email
  column at parse time** unless the caller opts in (PRIV-06). Not "loads then
  filters" — the string never reaches a record.
* **It never raises on a bad date.** ``Connected On`` becomes ``None`` and the
  row survives; losing a connection because LinkedIn changed a date format
  would be a far worse failure than a missing date.

Standard library only. No pandas: a hand-rolled ``csv`` pass is what lets the
header sniffing and the field whitelist be exact, and ``M`` is a few thousand
rows at most.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from interlayer.core.ids import member_id, normalize_slug
from interlayer.core.models import CompliancePosture, Member, Provenance
from interlayer.ingest import ConnectionsCsvError

#: Verbatim LinkedIn header, in order.
CANONICAL_COLUMNS: tuple[str, ...] = (
    "First Name",
    "Last Name",
    "URL",
    "Email Address",
    "Company",
    "Position",
    "Connected On",
)

#: A row missing any of these is not a connections header.
REQUIRED_COLUMNS: tuple[str, ...] = ("First Name", "Last Name", "URL")

#: Columns whose values survive into a ``Member`` (PRIV-05). ``Email Address``
#: is deliberately absent: it is admitted only via ``retain_emails=True``.
RETAINED_COLUMNS: tuple[str, ...] = (
    "First Name",
    "Last Name",
    "URL",
    "Company",
    "Position",
    "Connected On",
)

#: How far to scan for the header before giving up. Observed preambles are
#: 0-5 lines; 15 leaves room for a longer one without swallowing a whole file.
MAX_PREAMBLE_LINES = 15

#: Tried in order. LinkedIn has shipped BOMs, and older exports are not UTF-8.
ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "latin-1")

DEFAULT_SOURCE = "first_party_export"

_HEADER_KEY = re.compile(r"[^a-z0-9]+")

#: Renamed columns seen in the wild, plus the obvious near-misses. Unknown
#: columns are reported as a warning, never a crash.
_COLUMN_ALIASES: Mapping[str, str] = {
    "firstname": "First Name",
    "first": "First Name",
    "givenname": "First Name",
    "lastname": "Last Name",
    "last": "Last Name",
    "surname": "Last Name",
    "familyname": "Last Name",
    "url": "URL",
    "profileurl": "URL",
    "linkedinurl": "URL",
    "publicprofileurl": "URL",
    "emailaddress": "Email Address",
    "email": "Email Address",
    "emailaddresses": "Email Address",
    "company": "Company",
    "currentcompany": "Company",
    "organization": "Company",
    "organisation": "Company",
    "employer": "Company",
    "position": "Position",
    "title": "Position",
    "jobtitle": "Position",
    "connectedon": "Connected On",
    "connected": "Connected On",
    "connecteddate": "Connected On",
    "connectiondate": "Connected On",
}

_MONTHS: Mapping[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}

#: "14 Mar 2021" — LinkedIn's own format, and the two-digit-year variant.
_DMY = re.compile(r"^(\d{1,2})[\s./-]+([^\s./\d-]{3,})[\s./,-]+(\d{2,4})$")
#: "Mar 14, 2021"
_MDY = re.compile(r"^([^\s./\d-]{3,})[\s./-]+(\d{1,2})[\s,./-]+(\d{2,4})$")
#: ISO, and the slashed variant.
_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeaderScan:
    """Where the header was, and what it looked like."""

    line_index: int
    """0-based index of the header line within the decoded file."""
    preamble_lines: int
    raw_columns: tuple[str, ...]
    """Header cells exactly as written in the file."""
    columns: tuple[str | None, ...]
    """Per position: the canonical column it resolved to, or ``None``."""

    @property
    def resolved(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c is not None)

    @property
    def missing(self) -> tuple[str, ...]:
        found = set(self.resolved)
        return tuple(c for c in CANONICAL_COLUMNS if c not in found)

    @property
    def unrecognised(self) -> tuple[str, ...]:
        return tuple(
            raw for raw, canon in zip(self.raw_columns, self.columns, strict=True) if canon is None
        )


@dataclass(frozen=True, slots=True)
class ConnectionsParseResult:
    """Everything the parse learned. The counts are the audit trail."""

    members: tuple[Member, ...] = ()
    header: HeaderScan | None = None
    encoding: str = ""
    path: Path | None = None

    rows_read: int = 0
    rows_skipped: int = 0
    """Blank or entirely empty rows."""
    duplicates_merged: int = 0
    weak_identity_ids: tuple[str, ...] = ()
    """Members with no ``URL``: keyed on a name+company hash, so two different
    people with the same name at the same employer would collide. Downstream
    code should treat these as lower confidence."""
    unparsed_dates: int = 0
    emails_retained: bool = False
    warnings: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.members)

    def __iter__(self) -> Iterable[Member]:
        return iter(self.members)


# ---------------------------------------------------------------------------
# Header sniffing (ruling C1)
# ---------------------------------------------------------------------------


def _header_key(cell: str) -> str:
    return _HEADER_KEY.sub("", cell.strip().lstrip("﻿").casefold())


def _resolve_column(cell: str) -> str | None:
    return _COLUMN_ALIASES.get(_header_key(cell))


def sniff_header(text: str, *, max_lines: int = MAX_PREAMBLE_LINES) -> HeaderScan:
    """Find the header row, however long the preamble is.

    Selects the first line that, CSV-split, resolves all of ``First Name``,
    ``Last Name`` and ``URL``. Everything above it is preamble and is dropped.

    Raises :class:`ConnectionsCsvError` naming what it did find, rather than
    silently mis-parsing — a wrong header is the failure that produces a
    plausible-looking but wholly incorrect graph.
    """
    lines = text.splitlines()
    best: tuple[int, HeaderScan] | None = None

    for index, line in enumerate(lines[:max_lines]):
        if not line.strip():
            continue
        try:
            cells = next(csv.reader([line]))
        except csv.Error:
            continue
        columns = tuple(_resolve_column(cell) for cell in cells)
        scan = HeaderScan(
            line_index=index,
            preamble_lines=index,
            raw_columns=tuple(cells),
            columns=columns,
        )
        found = set(scan.resolved)
        if all(required in found for required in REQUIRED_COLUMNS):
            return scan
        if found and (best is None or len(found) > best[0]):
            best = (len(found), scan)

    scanned = min(len(lines), max_lines)
    detail = ""
    if best is not None:
        # Only echo a line that already looks like a header (it resolved at
        # least one known column), so a diagnostic never prints a person's row.
        detail = (
            f" The closest candidate was line {best[1].line_index + 1} with columns "
            f"{list(best[1].raw_columns)}, missing {list(best[1].missing)}."
        )
    raise ConnectionsCsvError(
        f"no Connections.csv header found in the first {scanned} line(s): no row resolves "
        f"all of {list(REQUIRED_COLUMNS)}. Expected the LinkedIn header "
        f"{','.join(CANONICAL_COLUMNS)!r} after a 'Notes:' preamble." + detail
    )


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def parse_connected_on(value: str | None) -> date | None:
    """Parse ``Connected On``. Returns ``None`` rather than raising.

    LinkedIn writes ``14 Mar 2021``. The month table is explicit rather than
    ``strptime("%d %b %Y")`` because ``%b`` reads the process locale, and a
    user with ``LC_TIME=de_DE`` would silently lose every date.
    """
    if not value:
        return None
    text = value.strip().strip(",")
    if not text:
        return None

    match = _DMY.match(text)
    if match:
        day, month_name, year = match.group(1), match.group(2), match.group(3)
        return _build(year, _MONTHS.get(month_name.casefold().rstrip(".")), day)

    match = _MDY.match(text)
    if match:
        month_name, day, year = match.group(1), match.group(2), match.group(3)
        return _build(year, _MONTHS.get(month_name.casefold().rstrip(".")), day)

    match = _ISO.match(text)
    if match:
        return _build(match.group(1), int(match.group(2)), match.group(3))

    return None


def _build(year: str, month: int | None, day: str) -> date | None:
    if month is None:
        return None
    y = int(year)
    if len(year) == 2:
        # POSIX two-digit-year rule. LinkedIn launched in 2003, so in practice
        # everything lands in the 2000s anyway.
        y += 2000 if y < 69 else 1900
    try:
        return date(y, month, int(day))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def decode_bytes(payload: bytes) -> tuple[str, str]:
    """Decode with LinkedIn's attested encodings. Returns ``(text, encoding)``."""
    for encoding in ENCODINGS:
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ConnectionsCsvError(
        f"could not decode the file with any of {list(ENCODINGS)}; "
        "it does not look like a LinkedIn CSV export"
    )


def find_in_archive(path: Path) -> bytes:
    """Pull ``Connections.csv`` out of the archive ``.zip``, case-insensitively."""
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if Path(name).name.casefold() == "connections.csv":
                return archive.read(name)
    raise ConnectionsCsvError(
        f"{path} is a zip archive but contains no Connections.csv "
        f"(found {len(zipfile.ZipFile(path).namelist())} entries)"
    )


def parse_connections_csv(
    source: str | Path | bytes,
    *,
    retain_emails: bool = False,
    provenance: Provenance | None = None,
    adapter: str = DEFAULT_SOURCE,
    collected_at: datetime | None = None,
    posture: CompliancePosture = CompliancePosture.FIRST_PARTY_EXPORT,
    max_preamble_lines: int = MAX_PREAMBLE_LINES,
) -> ConnectionsParseResult:
    """Parse a ``Connections.csv`` (loose file, archive zip, or raw bytes).

    ``retain_emails`` is a plain parameter, not a config lookup: PRIV-06 makes
    the default ``False``, and taking it as an argument keeps this module
    independent of ``core.config``. Pass ``config.retain_emails`` at the call
    site. When it is ``False`` the email string is dropped here and never
    written to a ``Member`` at all.
    """
    path: Path | None = None
    if isinstance(source, bytes):
        payload = source
    else:
        path = Path(source)
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ConnectionsCsvError(f"cannot read {path}: {exc}") from exc
        if zipfile.is_zipfile(path):
            payload = find_in_archive(path)

    text, encoding = decode_bytes(payload)
    if not text.strip():
        raise ConnectionsCsvError(f"{path or '<bytes>'} is empty")

    header = sniff_header(text, max_lines=max_preamble_lines)

    warnings: list[str] = []
    if header.preamble_lines:
        warnings.append(
            f"discarded a {header.preamble_lines}-line preamble above the header "
            f"(do not hard-code skiprows; ruling C1)"
        )
    if header.missing:
        warnings.append(
            f"header is missing the optional column(s) {list(header.missing)}; "
            "those values will be empty on every record"
        )
    if header.unrecognised:
        warnings.append(
            f"header carries unrecognised column(s) {list(header.unrecognised)}; "
            "they are ignored (PRIV-05 field whitelist)"
        )

    provenance = provenance or Provenance(
        source=adapter,
        collected_at=collected_at or datetime.now(UTC),
        posture=posture,
    )

    # Re-join from the header line so quoted fields spanning newlines survive,
    # while the preamble (which may contain its own quotes) is isolated.
    body_lines = text.splitlines(keepends=True)[header.line_index :]
    reader = csv.reader(io.StringIO("".join(body_lines)))
    next(reader, None)  # the header itself

    index: dict[str, int] = {}
    for position, canonical in enumerate(header.columns):
        if canonical is not None and canonical not in index:
            index[canonical] = position

    members: dict[str, Member] = {}
    order: list[str] = []
    weak: list[str] = []
    rows_read = 0
    rows_skipped = 0
    duplicates = 0
    unparsed_dates = 0

    for row in reader:
        if not row or not any(cell.strip() for cell in row):
            rows_skipped += 1
            continue
        rows_read += 1

        def cell(column: str, _row: Sequence[str] = row) -> str:
            position = index.get(column)
            if position is None or position >= len(_row):
                return ""
            return _row[position].strip()

        first_name = cell("First Name")
        last_name = cell("Last Name")
        url = cell("URL")
        company = cell("Company")
        position_title = cell("Position")
        connected_raw = cell("Connected On")

        connected_on = parse_connected_on(connected_raw)
        if connected_raw and connected_on is None:
            unparsed_dates += 1

        slug = normalize_slug(url)
        identifier = member_id(
            slug=url or None,
            first_name=first_name,
            last_name=last_name,
            company=company,
        )

        # PRIV-06: the email string is read only when explicitly opted in.
        email = cell("Email Address") if retain_emails else ""

        member = Member(
            member_id=identifier,
            first_name=first_name,
            last_name=last_name,
            linkedin_slug=slug,
            company_raw=company or None,
            position=position_title or None,
            connected_on=connected_on,
            email=email or None,
            provenance=provenance,
        )

        existing = members.get(identifier)
        if existing is None:
            members[identifier] = member
            order.append(identifier)
            if slug is None:
                weak.append(identifier)
        else:
            duplicates += 1
            members[identifier] = _merge(existing, member)

    if unparsed_dates:
        warnings.append(
            f"{unparsed_dates} row(s) had an unparseable 'Connected On'; "
            "the rows were kept with connected_on=None"
        )
    if weak:
        warnings.append(
            f"{len(weak)} row(s) had no URL and were keyed on a name+company hash "
            "(weak identity: name collisions at one employer will merge)"
        )

    return ConnectionsParseResult(
        members=tuple(members[key] for key in order),
        header=header,
        encoding=encoding,
        path=path,
        rows_read=rows_read,
        rows_skipped=rows_skipped,
        duplicates_merged=duplicates,
        weak_identity_ids=tuple(weak),
        unparsed_dates=unparsed_dates,
        emails_retained=retain_emails,
        warnings=tuple(warnings),
    )


def _merge(existing: Member, incoming: Member) -> Member:
    """Fold a duplicate row into the record we already have.

    Keeps the earliest ``Connected On`` (R1: dedupe on the public id, keep the
    earliest date) and backfills any field the first row left blank.
    """
    connected = existing.connected_on
    incoming_date = incoming.connected_on
    if incoming_date is not None and (connected is None or incoming_date < connected):
        connected = incoming_date
    return Member(
        member_id=existing.member_id,
        first_name=existing.first_name or incoming.first_name,
        last_name=existing.last_name or incoming.last_name,
        linkedin_slug=existing.linkedin_slug or incoming.linkedin_slug,
        company_raw=existing.company_raw or incoming.company_raw,
        position=existing.position or incoming.position,
        connected_on=connected,
        email=existing.email or incoming.email,
        provenance=existing.provenance or incoming.provenance,
    )


__all__ = [
    "CANONICAL_COLUMNS",
    "ENCODINGS",
    "MAX_PREAMBLE_LINES",
    "REQUIRED_COLUMNS",
    "RETAINED_COLUMNS",
    "ConnectionsParseResult",
    "HeaderScan",
    "decode_bytes",
    "find_in_archive",
    "parse_connected_on",
    "parse_connections_csv",
    "sniff_header",
]
