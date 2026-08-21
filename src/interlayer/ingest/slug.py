"""Normalise a LinkedIn profile URL down to one stable join key.

WHY this module exists at all: in a ``Connections.csv`` export the URL column is
the *only* reliable identity. Names repeat, emails are almost always blank, and
company/title churn between exports. So every spelling of one profile has to
fold to a single key, and -- more importantly -- two different profiles must
never fold together. Wave 1 (``docs/research/04-entity-resolution/slugs.py``)
settled the rules; they are encoded here:

* **Host is noise.** Locale subdomains (``uk.``, ``de.``) and ``www.`` identify a
  rendering of the page, not the member, so the host is validated and discarded.
* **Query strings and fragments are noise.** ``?trk=...`` is a tracking token and
  ``?originalSubdomain=uk`` is a locale hint; neither identifies anyone.
* **Trailing path segments are routes, not identity.** ``/in/jane/en`` and
  ``/in/jane/overlay/contact-info/`` are the same human as ``/in/jane``.
* **``/in/ACoA...`` member URNs are CASE-SENSITIVE.** They are base64-ish, so
  ``ACoAAb`` and ``ACoAAB`` are two different people. Casefolding them silently
  merges strangers, which is the worst failure this tool can have.
* **Ordinary vanity slugs are case-insensitive** on LinkedIn's side, so they are
  casefolded -- otherwise ``/in/Jane-Doe`` and ``/in/jane-doe`` split one person
  in two.
* **Legacy ``/pub/first-last/1/2/3`` needs the whole path.** The discriminating
  digits live in the *later* segments: keeping only the last segment merges
  unrelated people who happen to share a final digit group, and keeping only the
  first merges every namesake. So the full path is retained.

The returned key is deliberately the *bare* slug (no ``urn:``/``pub:`` tag) for
``/in/`` URLs, because ``models.TargetPerson.make`` derives its own slug as the
last path segment. Matching that convention is what lets a later stage join
hand-collected Tier 2 profiles against ingested people without importing this
module (stages may not import each other).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit

__all__ = ["PUB_PREFIX", "canonical_url", "is_member_urn", "normalize_slug"]

PUB_PREFIX = "pub/"
"""Marker kept on legacy ``/pub/`` keys; it embeds a ``/``, so such a key can
never collide with a modern single-segment ``/in/`` slug."""

# ``/in/<slug>`` or ``/pub/<slug>`` anywhere in the path -- LinkedIn also serves
# ``/mwlite/in/<slug>`` and other prefixed routes.
_PROFILE_RE = re.compile(r"(?:^|/)(in|pub)/([^/?#]+)", re.IGNORECASE)

# Member URNs. The ``ACoA`` prefix is matched case-sensitively on purpose: it is
# the literal LinkedIn prefix, and a vanity slug that merely looks like one
# should not be granted case-sensitive treatment.
_URN_RE = re.compile(r"^ACoA[A-Za-z0-9_-]+$")

# ``en``, ``en-us`` -- a locale suffix segment, never part of an identity.
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)

# A bare token in the URL column (no scheme, no slash) is treated as a vanity
# slug only if it looks like one; this rejects stray emails and free text.
_BARE_SLUG_RE = re.compile(r"^[\w.-]{2,}$", re.UNICODE)

_MULTI_SLASH_RE = re.compile(r"/{2,}")
_LINKEDIN_HOST_RE = re.compile(r"(?:^|\.)linkedin\.(?:com|cn)$", re.IGNORECASE)


def _prepare(raw: str) -> str:
    """Coerce whatever was in the URL cell into something ``urlsplit`` understands.

    Exports in the wild carry ``https://…``, ``http://…``, ``www.linkedin.com/…``,
    a bare ``/in/x`` path and occasionally just the slug, so all five shapes are
    normalised to a splittable string before any parsing happens.
    """
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return ""
    if "://" in text or text.startswith("/"):
        return text
    if "linkedin." in text.casefold():
        return "https://" + text
    if "/" not in text and _BARE_SLUG_RE.match(text):
        return "https://www.linkedin.com/in/" + text
    return ""


def is_member_urn(slug: str) -> bool:
    """True for an ``ACoA…`` member URN, whose case carries information."""
    return bool(_URN_RE.match(slug))


def normalize_slug(raw: str | None) -> str | None:
    """Fold a profile URL to its join key, or ``None`` if it is not a profile.

    ``None`` covers blank cells, company/school pages and anything that is not a
    LinkedIn member URL -- callers treat that as an unusable row rather than
    inventing an identity for it.
    """
    if raw is None:
        return None
    prepared = _prepare(raw)
    if not prepared:
        return None
    try:
        parts = urlsplit(prepared)
    except ValueError:  # malformed IPv6 literal, bad port, etc.
        return None

    host = (parts.hostname or "").casefold()
    if host and not _LINKEDIN_HOST_RE.search(host):
        return None

    path = unicodedata.normalize("NFKC", unquote(parts.path))
    path = _MULTI_SLASH_RE.sub("/", path)
    match = _PROFILE_RE.search(path)
    if match is None:
        return None

    if match.group(1).casefold() == "pub":
        return _pub_key(path[match.start(2) :])

    slug = match.group(2).strip()
    if not slug:
        return None
    return slug if is_member_urn(slug) else slug.casefold()


def _pub_key(tail: str) -> str | None:
    """Key for a legacy ``/pub/`` URL, retaining every identifying segment."""
    segments = [seg for seg in tail.strip("/").split("/") if seg]
    while len(segments) > 1 and _LOCALE_SEGMENT_RE.match(segments[-1]):
        segments.pop()
    if not segments:
        return None
    return PUB_PREFIX + "/".join(seg.casefold() for seg in segments)


def canonical_url(slug: str) -> str:
    """Rebuild one canonical URL for a key, so two spellings serialise identically."""
    tail = slug if slug.startswith(PUB_PREFIX) else f"in/{slug}"
    return f"https://www.linkedin.com/{tail}"
