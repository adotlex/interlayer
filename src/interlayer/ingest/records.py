"""Turn one raw CSV row into a ``Person`` plus the ``Connection`` edge to the ego.

The judgement calls live here, and each one is a place where being clever would
lose data or invent it:

* A row we cannot key to a profile URL is dropped rather than keyed by name --
  two people called John Smith are two people, and merging them is exactly the
  failure mode the slug rules exist to prevent.
* An unparseable ``Connected On`` costs the date, not the row.
* Emails are dropped before they can reach an artifact unless the operator asked
  for them, and then only as an HMAC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from dateutil import parser as dateutil_parser
from pydantic import ValidationError

from interlayer.ingest.csv_reader import RawRow, RowIssue
from interlayer.ingest.slug import canonical_url, normalize_slug
from interlayer.io import hash_email
from interlayer.models import Connection, Person, Position

__all__ = ["ParsedRow", "parse_connected_on", "parse_row"]

# A plausibility band, set well wider than LinkedIn's own history (2003-) so it
# only ever catches parse artefacts: a stray number read as a year, or a typo
# rolling a date into 3021. Outside it, the date is dropped, not the row.
MIN_YEAR = 1990
MAX_YEAR = 2100

# Two probe defaults: if a string does not pin down every component, the two
# parses disagree and we know the date was partial rather than trusting today's
# clock to fill it in -- which would make the artifact non-reproducible.
_PROBE_A = datetime(2001, 1, 1)
_PROBE_B = datetime(2002, 2, 2)

# A leading four-digit year means the string is ISO-ordered (Y-M-D), where
# ``dayfirst=True`` would silently read 2021-03-01 as the 3rd of January.
_YEAR_FIRST_RE = re.compile(r"^\d{4}[-/.]")


@dataclass(frozen=True)
class ParsedRow:
    """A usable row: the person, their edge to the ego, and the key they dedupe on."""

    slug: str
    person: Person
    connection: Connection
    notes: tuple[str, ...] = ()


def parse_connected_on(raw: str) -> date | None:
    """Parse ``Connected On``; return ``None`` rather than failing the row.

    ``dateutil`` rather than ``strptime``: ``%b`` is locale-dependent, so a
    strptime-based parser silently stops recognising ``01 Mar 2021`` on a machine
    whose ``LC_TIME`` is not English. Ambiguous all-numeric forms are read
    day-first, matching the export's own convention, *except* when the string
    leads with a four-digit year and is therefore ISO-ordered.

    One caveat: a two-digit year (``01-Mar-21``) is resolved by dateutil's
    century pivot, which is relative to the current year.
    """
    text = raw.strip()
    if not text:
        return None
    day_first = _YEAR_FIRST_RE.match(text) is None
    try:
        first = dateutil_parser.parse(
            text, dayfirst=day_first, yearfirst=not day_first, ignoretz=True, default=_PROBE_A
        )
        second = dateutil_parser.parse(
            text, dayfirst=day_first, yearfirst=not day_first, ignoretz=True, default=_PROBE_B
        )
    except (ValueError, OverflowError, TypeError):
        return None
    if first != second:
        return None  # partial date (e.g. "Mar 2021"); a guessed day is a lie
    if not MIN_YEAR <= first.year <= MAX_YEAR:
        return None
    return first.date()


def _email_field(raw: str, *, keep_emails: bool, hmac_key: str | None) -> tuple[str | None, str]:
    """Return the value to store plus a count key.

    Normalisation before hashing is ``strip()`` + ``casefold()`` and nothing else
    (``io.hash_email`` does exactly that). Stripping Gmail dots or ``+tags``
    would deliberately link identities the address owner chose to keep apart.
    """
    email = raw.strip()
    if not email:
        return None, ""
    if not keep_emails:
        return None, "email_dropped"
    if hmac_key is None:  # Settings enforces this; belt and braces
        return None, "email_dropped"
    return hash_email(email, hmac_key), "email_hashed"


def _positions(company: str, title: str) -> tuple[tuple[Position, ...], str]:
    """Build the single current position the export carries.

    The export has no dates for the role, only the current employer, so
    ``is_current=True`` with no span. ``company_org_id`` stays ``None``: resolving
    it is the normalize stage's job and guessing here would pre-empt the review
    path that keeps The Citadel out of Citadel LLC.
    """
    if not company:
        # A title with no employer cannot be resolved to an org or co-tenured
        # with anyone, and an empty ``company_raw`` would mint a junk Org.
        return (), "position_without_company" if title else ""
    return (Position(company_raw=company, title_raw=title, is_current=True),), ""


def parse_row(row: RawRow, *, keep_emails: bool, hmac_key: str | None) -> ParsedRow | RowIssue:
    """Build the records for one row, or say why the row is unusable."""
    url = row.get("url")
    slug = normalize_slug(url)
    if slug is None:
        reason = "missing_url" if not url else "unrecognised_url"
        return RowIssue(row.lineno, reason)

    notes: list[str] = []
    email, email_note = _email_field(row.get("email"), keep_emails=keep_emails, hmac_key=hmac_key)
    if email_note:
        notes.append(email_note)

    positions, position_note = _positions(row.get("company"), row.get("position"))
    if position_note:
        notes.append(position_note)

    connected_on = parse_connected_on(row.get("connected_on"))
    if connected_on is None and row.get("connected_on"):
        notes.append("unparseable_date")

    first, last = row.get("first_name"), row.get("last_name")
    if not first and not last:
        notes.append("missing_name")

    try:
        person = Person.make(
            first,
            last,
            linkedin_slug=slug,
            linkedin_url=canonical_url(slug),
            email=email,
            positions=positions,
            source="connections_csv",
        )
        connection = Connection(person_id=person.person_id, connected_on=connected_on, degree=1)
    except (ValidationError, ValueError) as exc:
        # Never echo the row itself: it is third-party personal data and stderr
        # gets captured. The line number is enough to find it in the source.
        return RowIssue(row.lineno, "invalid_record", detail=type(exc).__name__)

    return ParsedRow(slug=slug, person=person, connection=connection, notes=tuple(notes))
