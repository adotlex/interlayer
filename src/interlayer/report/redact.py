"""Unconditional redaction of contact details from rendered output (PRIV-17).

This module is deliberately the *first* thing written in the report layer and
every renderer routes its output through it as a final pass. The control is
unconditional on purpose: ``config.retain_emails`` governs whether an address is
kept in the local store, and says nothing about whether it may leave the machine
inside a document. A report is the one artefact that gets forwarded, pasted into
a chat window and attached to an email, so the two decisions are separated and
the export side is not configurable.

Two shapes are removed:

* **Email addresses** — a conventional address regex, including a ``mailto:``
  prefix if one is present.
* **Phone numbers** — heuristic, because a bare digit run is ambiguous. The
  heuristic is tuned to over-match contact details rather than under-match them,
  but is guarded against the numbers this tool legitimately prints: percentages,
  scores, ISO dates, version strings, small integer ranges and SHA fingerprints.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

EMAIL_PLACEHOLDER = "[redacted-email]"
PHONE_PLACEHOLDER = "[redacted-phone]"

#: Matches an email address, optionally prefixed with ``mailto:``.
EMAIL_RE = re.compile(
    r"(?:mailto:\s*)?"
    r"[A-Za-z0-9._%+\-]+"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+"
)

#: One digit group, either bare or parenthesised.
_GROUP = r"(?:\(\s?\d{1,4}\s?\)|\d{1,4})"

#: Separators between digit groups: space, dot, and the ASCII/Unicode dashes.
#: The dash range is written as escapes rather than literals so that the class
#: cannot be misread as a single hyphen. U+2010..U+2015 covers hyphen through
#: horizontal bar, which is what a pasted phone number tends to arrive with.
_SEP = "[\\s.\\u2010-\\u2015\\-]{1,2}"

#: Candidate phone shapes. Every hit is then validated by :func:`_phone_repl`;
#: the regex is intentionally loose and the callback does the real filtering.
PHONE_RE = re.compile(
    r"(?<!\w)(?:tel:\s*)?"
    r"(?:"
    rf"\+?\d{{10,15}}"  # bare international run, e.g. 12125550142
    rf"|\+?{_GROUP}(?:{_SEP}{_GROUP}){{1,6}}"  # grouped, e.g. +44 20 7123 4567
    r")"
    r"(?!\w)"
)

#: A plain decimal number — 3.14159265 is not a phone number.
_DECIMAL_RE = re.compile(r"\d+[.,]\d+")

#: An ISO-8601 date opening — 2026-08-17 is not a phone number.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

#: A dotted version string — 1.17.1 is not a phone number.
_VERSION_RE = re.compile(r"\d+(?:\.\d+){2,}")


def _looks_like_a_phone_number(text: str) -> bool:
    """Decide whether a loose regex hit is really a phone number.

    The guards matter more than the match: this tool prints coverage fractions,
    composite scores, generation timestamps and dependency pins, and redacting
    any of those would be a visible defect rather than a privacy win.
    """
    if _DECIMAL_RE.fullmatch(text) or _VERSION_RE.fullmatch(text):
        return False
    if _ISO_DATE_RE.match(text):
        return False

    digits = sum(1 for ch in text if ch.isdigit())
    if not 7 <= digits <= 15:
        return False

    stripped = text.removeprefix("tel:").strip()
    bare = stripped.lstrip("+")
    if bare.isdigit():
        # A run of bare digits is a phone number only at plausible length.
        return 10 <= digits <= 15

    groups = re.findall(r"\d+", stripped)
    if stripped.startswith("+") or "(" in stripped:
        return True
    if len(groups) >= 3:
        return True
    # Two groups are ambiguous with a numeric range ("1000-2500"), so demand
    # enough digits that a range becomes implausible.
    return len(groups) == 2 and digits >= 9


def _phone_repl(match: re.Match[str]) -> str:
    text = match.group(0)
    return PHONE_PLACEHOLDER if _looks_like_a_phone_number(text) else text


def redact_text(value: str) -> str:
    """Strip email addresses and phone numbers from a string.

    Idempotent: the placeholders contain no contact shape, so re-running this
    over already-redacted output is a no-op.
    """
    out = EMAIL_RE.sub(EMAIL_PLACEHOLDER, value)
    return PHONE_RE.sub(_phone_repl, out)


def redact_value(value: Any) -> Any:
    """Recursively redact strings inside mappings, sequences and scalars.

    Mapping *keys* are redacted too — a dict keyed by email address is exactly
    the accident this control exists to catch.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {redact_value(k): redact_value(v) for k, v in value.items()}
    if isinstance(value, bytes | bytearray):
        return value
    if isinstance(value, Sequence):
        return [redact_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return {redact_value(item) for item in value}
    return value


def has_contact_details(value: str) -> bool:
    """True when *value* still contains an email address or a phone number.

    The assertion helper for PRIV-17 tests, and cheap enough to use as a
    belt-and-braces check at the end of a renderer.
    """
    if EMAIL_RE.search(value):
        return True
    return any(_looks_like_a_phone_number(m.group(0)) for m in PHONE_RE.finditer(value))


__all__ = [
    "EMAIL_PLACEHOLDER",
    "EMAIL_RE",
    "PHONE_PLACEHOLDER",
    "PHONE_RE",
    "has_contact_details",
    "redact_text",
    "redact_value",
]
