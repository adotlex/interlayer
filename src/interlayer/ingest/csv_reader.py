"""Find the real header row of a ``Connections.csv`` and read it by column name.

Two things about this file bite every naive parser:

* **The ``Notes:`` preamble is not a fixed height.** It has been two, three and
  four lines across export versions, so ``skiprows=3`` is a latent bug that
  silently eats the header (or a person) on the next export change. The header
  is located by scanning for the row that actually contains the name columns.
* **Column order is not guaranteed.** Positional access is a silent corruption
  waiting to happen -- reading ``Company`` out of the ``Position`` slot produces
  plausible-looking garbage that nothing downstream can detect. Everything here
  is keyed by header name, case- and whitespace-insensitively.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from interlayer.errors import IngestError

__all__ = [
    "CANONICAL_HEADER",
    "COLUMN_ALIASES",
    "OPTIONAL_FIELDS",
    "REQUIRED_FIELDS",
    "Header",
    "RawRow",
    "RowIssue",
    "find_header",
    "map_columns",
    "normalize_header_cell",
    "read_rows",
]

CANONICAL_HEADER = (
    "First Name",
    "Last Name",
    "URL",
    "Email Address",
    "Company",
    "Position",
    "Connected On",
)

# Field key -> header spellings, best first. The aliases only apply when the
# canonical name is absent; they cost nothing and rescue hand-edited or
# older-format exports rather than failing the whole file.
COLUMN_ALIASES: Mapping[str, tuple[str, ...]] = {
    "first_name": ("first name", "firstname", "given name"),
    "last_name": ("last name", "lastname", "surname", "family name"),
    "url": ("url", "profile url", "public profile url", "linkedin url", "member url"),
    "email": ("email address", "emailaddress", "email", "e-mail address", "e-mail"),
    "company": ("company", "current company", "organization", "organisation"),
    "position": ("position", "title", "job title", "current position"),
    "connected_on": ("connected on", "connectedon", "connection date", "connected"),
}

REQUIRED_FIELDS = ("first_name", "last_name", "url", "company", "position", "connected_on")

OPTIONAL_FIELDS = ("email",)
"""``Email Address`` is optional *unless* ``keep_emails`` is on.

Refusing to parse an otherwise-valid export because of a column whose contents
we are about to discard (emails are dropped by default) would be a bad trade.
When the operator explicitly asked to keep emails, its absence is fatal instead.
"""

# The preamble is 2-4 lines in every export seen so far; the cap keeps a stray
# data row that happens to contain the words from being mistaken for a header.
MAX_PREAMBLE_LINES = 50

_WS_RE = re.compile(r"\s+")
# U+FEFF survives NFKC and is not whitespace, so a BOM that ends up mid-file --
# two exports concatenated, or a spreadsheet re-save -- would otherwise make
# 'First Name' unrecognisable and fail an otherwise-valid file.
_ZERO_WIDTH_RE = re.compile(r"[\ufeff\u200b-\u200d]")


@dataclass(frozen=True)
class RowIssue:
    """One row that could not be used, or was used only after repair.

    ``reason`` is a stable machine key (it becomes a count in the run summary);
    ``fatal`` distinguishes "row dropped" from "row kept with a caveat".
    ``detail`` is free text for stderr only and must never quote row contents.
    """

    lineno: int
    reason: str
    fatal: bool = True
    detail: str = ""


@dataclass(frozen=True)
class RawRow:
    """One data row, addressed by field key rather than by column index."""

    lineno: int
    values: Mapping[str, str]

    def get(self, field: str) -> str:
        return self.values.get(field, "")


@dataclass(frozen=True)
class Header:
    """Where the header was found and how its columns map to field keys."""

    lineno: int
    cells: tuple[str, ...]
    columns: Mapping[str, int]
    duplicates: tuple[str, ...] = ()


def normalize_header_cell(cell: str) -> str:
    """Fold a header cell for comparison: NFKC, collapse whitespace, casefold.

    NFKC is what turns a non-breaking space (seen in exports opened and re-saved
    by a spreadsheet) into an ordinary one, so ``Connected\xa0On`` still matches;
    zero-width characters are dropped for the same reason.
    """
    folded = _ZERO_WIDTH_RE.sub("", unicodedata.normalize("NFKC", cell))
    return _WS_RE.sub(" ", folded).strip().casefold()


_KNOWN_HEADER_CELLS: frozenset[str] = frozenset(
    alias for aliases in COLUMN_ALIASES.values() for alias in aliases
)

_HEADER_RECOGNITION_RATIO = 0.6
"""Fraction of a row's cells that must be known column names for it to be a header.

A real header is almost entirely recognised cells, even when one column is
missing (6 of 6 known). Preamble prose that happens to name the columns
("the exported columns are, First Name, Last Name, and five others") is mostly
unrecognised (2 of 4). The ratio separates the two without demanding a complete
header, which would cost the specific "missing column X" diagnostic.
"""


_MIN_KNOWN_HEADER_CELLS = 3
"""Recognised cells a row needs before the ratio is even consulted, so a short
prose fragment that happens to be mostly column names cannot qualify."""


def _looks_like_header(cells: Sequence[str]) -> bool:
    """Whether a row is the header, as opposed to prose that mentions the columns.

    Deliberately does NOT require the full set of columns: a header missing one
    is still a header, and recognising it is what produces the specific "missing
    column X" diagnostic instead of a useless "no header found".
    """
    present = [c for c in (normalize_header_cell(c) for c in cells) if c]
    if not present:
        return False
    known = sum(1 for c in present if c in _KNOWN_HEADER_CELLS)
    return known >= _MIN_KNOWN_HEADER_CELLS and known / len(present) >= _HEADER_RECOGNITION_RATIO


def find_header(lines: Sequence[str]) -> tuple[int, tuple[str, ...]]:
    """Return ``(index, cells)`` of the header row within ``lines``.

    Scanning physical lines rather than feeding the whole file to ``csv.reader``
    is deliberate: the preamble is free prose that has contained unbalanced
    quotes, and a quote-confused reader would swallow the header into a field.
    """
    for index, line in enumerate(lines[:MAX_PREAMBLE_LINES]):
        if not line.strip():
            continue
        try:
            cells = next(csv.reader([line]), [])
        except csv.Error:
            continue
        if _looks_like_header(cells):
            return index, tuple(cells)
    # Report SHAPE, never content. Pointed at messages.csv or Contacts.csv from
    # the same download, echoing the first lines would write third-party emails
    # and message bodies to stderr -- precisely what the rest of this stage
    # refuses to do.
    shape = (
        ", ".join(
            f"line {i + 1}: {len(next(csv.reader([line]), []))} field(s)"
            for i, line in enumerate(lines[:5])
            if line.strip()
        )
        or "<empty file>"
    )
    raise IngestError(
        "no header row found: expected a line naming the export's columns "
        f"({', '.join(CANONICAL_HEADER)}) within the first {MAX_PREAMBLE_LINES} "
        f"lines. Saw {shape}. Is this a LinkedIn Connections.csv, rather than "
        "another file from the same export?"
    )


def map_columns(cells: Sequence[str], *, require_email: bool = False) -> Header:
    """Bind field keys to column indices, or say precisely what is missing."""
    folded = [normalize_header_cell(c) for c in cells]
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for index, name in enumerate(folded):
        if not name:
            continue
        if name in seen:
            duplicates.append(name)  # first wins; mapping by name must stay 1:1
            continue
        seen[name] = index

    columns: dict[str, int] = {}
    for field, spellings in COLUMN_ALIASES.items():
        for spelling in spellings:
            if spelling in seen:
                columns[field] = seen[spelling]
                break

    required = list(REQUIRED_FIELDS) + (["email"] if require_email else [])
    missing = [f for f in required if f not in columns]
    if missing:
        wanted = ", ".join(COLUMN_ALIASES[f][0] for f in missing)
        found = ", ".join(repr(c) for c in cells) or "<no columns>"
        extra = (
            " (keep_emails is on, which requires the email column)"
            if require_email and "email" in missing
            else ""
        )
        raise IngestError(
            f"Connections.csv is missing required column(s): {wanted}{extra}. "
            f"Header row contained: {found}. Expected: {', '.join(CANONICAL_HEADER)}"
        )
    return Header(
        lineno=0, cells=tuple(cells), columns=columns, duplicates=tuple(sorted(set(duplicates)))
    )


def read_rows(
    path: Path, *, require_email: bool = False
) -> tuple[Header, list[RawRow], list[RowIssue]]:
    """Read every data row of ``path``, keyed by field name.

    ``utf-8-sig`` strips the BOM that Excel-touched exports carry, and
    ``newline=""`` hands CRLF and quoted embedded newlines to ``csv`` intact
    instead of mangling them. The whole file is read up front: a Connections
    export is a few thousand rows, and having the lines in hand is what makes
    header sniffing robust.
    """
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            lines = handle.readlines()
    except UnicodeDecodeError as exc:
        raise IngestError(f"{path} is not UTF-8 text: {exc}") from exc
    except OSError as exc:
        raise IngestError(f"cannot read {path}: {exc}") from exc

    index, cells = find_header(lines)
    header = replace(map_columns(cells, require_email=require_email), lineno=index + 1)

    rows: list[RawRow] = []
    issues: list[RowIssue] = []
    width = len(cells)
    reader = csv.reader(lines[index + 1 :])
    try:
        for record in reader:
            # Physical line number, so an operator can open the file and look.
            lineno = index + 1 + reader.line_num
            if not any(cell.strip() for cell in record):
                issues.append(RowIssue(lineno, "blank_row"))
                continue
            fields = list(record)
            while len(fields) > width and not fields[-1].strip():
                fields.pop()  # trailing empty cells are padding, not data
            if len(fields) > width:
                issues.append(RowIssue(lineno, "too_many_fields"))
                continue
            if len(fields) < width:
                # Short rows usually mean a broken quote upstream. Pad and carry
                # on: the missing columns simply read as empty, and the row is
                # dropped later if that cost it the URL.
                issues.append(RowIssue(lineno, "short_row", fatal=False))
                fields.extend([""] * (width - len(fields)))
            rows.append(
                RawRow(
                    lineno=lineno,
                    values={f: fields[i].strip() for f, i in header.columns.items()},
                )
            )
    except csv.Error as exc:
        raise IngestError(f"{path}: malformed CSV after line {index + 1}: {exc}") from exc
    return header, rows, issues
