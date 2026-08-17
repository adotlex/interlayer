"""Provider payload → :class:`EnrichedPerson`. The adapter boundary itself.

Three jobs, in order of importance:

1. **Drop contact fields.** Email, phone, address and birth date are discarded
   here even when a provider volunteers them, unless ``allow_contact_fields`` is
   explicitly ``True``. They are not needed to find who bridges into a firm, and
   holding them is what turns a local research tool into a data-protection
   liability. What was dropped is *recorded* on the record, so the user can see
   that the provider offered contact data and the boundary refused it.
2. **Canonicalise field names.** Every provider maps onto the same names, so
   nothing downstream ever branches on which vendor supplied a fact.
3. **Refuse to launder precision.** ``"2019"`` becomes ``date(2019, 1, 1)`` with
   :attr:`DatePrecision.YEAR` attached, never a bare date that looks like someone
   knows the day. An overlap computed from year-only dates is much weaker
   evidence and the scorer has to be able to tell.

Raw provider payloads never leave this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from interlayer.collect import schemas
from interlayer.core import ids
from interlayer.enrichment.interface import (
    DatePrecision,
    EducationSpan,
    EmploymentSpan,
    EnrichedPerson,
    EnrichmentProvenance,
    LookupKey,
    LookupKeyType,
    Seniority,
    cache_id_for,
)

#: Substrings that mark a payload key as contact data. Matched case-insensitively
#: against the key name at any depth, so ``contact_info.work_email`` is caught as
#: surely as ``email``. Deliberately broad: a false positive costs a field nobody
#: needed, a false negative costs a category of personal data we promised not to
#: keep.
CONTACT_KEY_MARKERS: tuple[str, ...] = (
    "email",
    "phone",
    "mobile",
    "telephone",
    "fax",
    "address",
    "birth",
    "dob",
)

#: The censoring threshold the sources apply to connection counts. At or above
#: this the value carries no information and is recorded as unknown.
CONNECTIONS_CENSORED_AT = 500

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_PRESENT_TOKENS = frozenset({"present", "current", "now", "ongoing", "till date", "-"})

#: Ordered ladder. ``phrases`` match as substrings; ``tokens`` must match a whole
#: word. The split matters: "Senior Director" contains the letters "cto", and a
#: naive substring rule promotes it to CXO — exactly the kind of quiet mis-grading
#: that makes a seniority field worse than no seniority field at all.
_SENIORITY_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], Seniority], ...] = (
    (("intern", "apprentice"), (), Seniority.INTERN),
    (
        ("chief ", "c-level", "c level"),
        ("cto", "ceo", "cfo", "coo", "cio", "cmo", "cxo"),
        Seniority.CXO,
    ),
    (("founder", "owner", "proprietor", "managing partner"), (), Seniority.OWNER),
    (("vice president", "vice-president"), ("vp", "svp", "evp", "avp"), Seniority.VP),
    (("director", "head of", "partner"), ("head",), Seniority.DIRECTOR),
    (("principal",), ("lead", "leader", "staff", "manager"), Seniority.LEAD),
    (("senior",), ("sr",), Seniority.SENIOR),
    (("junior", "graduate", "trainee", "entry-level"), ("jr",), Seniority.JUNIOR),
    (("associate", "mid-level", "mid level"), (), Seniority.MID),
)

_TOKEN_RE = re.compile(r"[\w+#]+", flags=re.UNICODE)

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+", flags=re.UNICODE)


# ---------------------------------------------------------------------------
# Field maps
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldMap:
    """Declarative provider payload → canonical field mapping.

    Values are candidate dotted paths, tried in order; the first that resolves to
    something non-empty wins. Keeping this declarative means adding a provider is
    a table entry rather than a new code path with its own bugs.
    """

    provider: str
    person: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    employment: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    education: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    employment_list: tuple[str, ...] = ()
    education_list: tuple[str, ...] = ()
    skills_list: tuple[str, ...] = ()


#: Identity map for payloads that already use canonical names — the user-supplied
#: CSV/JSONL provider, and the shape our own cache round-trips.
CANONICAL_FIELD_MAP = FieldMap(
    provider="canonical",
    person={
        "linkedin_public_id": ("linkedin_public_id", "public_id", "slug"),
        "profile_url": ("profile_url", "linkedin_url", "url"),
        "full_name": ("full_name", "name"),
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "headline": ("headline", "title"),
        "location_country": ("location_country", "country"),
        "location_region": ("location_region", "region", "state"),
        "location_city": ("location_city", "city", "locality"),
        "current_employer_name": ("current_employer_name", "company", "employer"),
        "current_title": ("current_title", "position"),
        "current_seniority": ("current_seniority", "seniority"),
        "current_start_date": ("current_start_date", "start_date"),
        "connections_count": ("connections_count",),
        "followers_count": ("followers_count",),
    },
    employment={
        "employer_name": ("employer_name", "company", "company_name"),
        "employer_domain": ("employer_domain", "domain"),
        "title": ("title", "position"),
        "start_date": ("start_date", "starts_at", "start"),
        "end_date": ("end_date", "ends_at", "end"),
        "location": ("location",),
    },
    education={
        "school_name": ("school_name", "school", "institution"),
        "degree": ("degree", "degree_name"),
        "field_of_study": ("field_of_study", "field"),
        "start_year": ("start_year",),
        "end_year": ("end_year",),
    },
    employment_list=("employment_history", "experience"),
    education_list=("education_history", "education"),
    skills_list=("skills",),
)


# ---------------------------------------------------------------------------
# Contact-field dropping
# ---------------------------------------------------------------------------


def is_contact_key(name: str) -> bool:
    """True when a payload key names contact data."""
    lowered = name.casefold()
    return any(marker in lowered for marker in CONTACT_KEY_MARKERS)


def drop_contact_fields(
    payload: Any, *, allow_contact_fields: bool = False, _prefix: str = ""
) -> tuple[Any, tuple[str, ...]]:
    """Strip contact fields at any depth; return the cleaned payload and the names.

    With ``allow_contact_fields=True`` the payload passes through untouched and
    the dropped list is empty — the opt-in is real, but it has to be asked for.
    """
    if allow_contact_fields:
        return payload, ()

    dropped: list[str] = []

    def walk(node: Any, prefix: str) -> Any:
        if isinstance(node, Mapping):
            clean: dict[str, Any] = {}
            for key, value in node.items():
                name = str(key)
                path = f"{prefix}{name}"
                if is_contact_key(name):
                    dropped.append(path)
                    continue
                clean[name] = walk(value, f"{path}.")
            return clean
        if isinstance(node, list):
            return [walk(item, prefix) for item in node]
        return node

    cleaned = walk(payload, _prefix)
    return cleaned, tuple(sorted(set(dropped)))


# ---------------------------------------------------------------------------
# Scalar normalisers
# ---------------------------------------------------------------------------


def normalize_public_id(value: str | None) -> str | None:
    """Canonical LinkedIn public identifier from a URL, path or bare slug."""
    return ids.normalize_slug(value)


def canonical_profile_url(value: str | None) -> str | None:
    """Canonical profile URL, assembled from the ``collect.schemas`` table.

    Nothing in this package hard-codes the host or path; there is one table and
    this reads from it, exactly like the collectors do.
    """
    slug = normalize_public_id(value)
    if not slug:
        return None
    return schemas.profile_url(slug)


def normalize_connections_count(value: Any) -> int | None:
    """Return the count, or ``None`` when the source censored it.

    Bright Data's field literally named ``connections`` is an INTEGER count, not a
    list of connections — verified against the vendor's published sample. Nobody
    sells the list. See ``docs/research/R2-third-party-providers.md``.
    """
    number = _as_int(value)
    if number is None or number < 0:
        return None
    if number >= CONNECTIONS_CENSORED_AT:
        return None
    return number


def normalize_seniority(raw: Any) -> Seniority:
    """Map a free-text title or seniority string onto the fixed vocabulary."""
    if isinstance(raw, Seniority):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return Seniority.UNKNOWN
    text = f" {raw.casefold().strip()} "
    tokens = set(_TOKEN_RE.findall(text))
    for phrases, words, seniority in _SENIORITY_RULES:
        if any(phrase in text for phrase in phrases) or (tokens & set(words)):
            return seniority
    return Seniority.UNKNOWN


def normalize_title(raw: Any) -> str | None:
    """Lowercased, punctuation-stripped title for comparison purposes."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _PUNCT.sub(" ", raw.casefold())
    return _WS.sub(" ", text).strip() or None


def normalize_skills(raw: Any) -> tuple[str, ...]:
    """Casefold, strip, dedupe and sort. Sorted for determinism, not aesthetics."""
    if raw is None:
        return ()
    items: Iterable[Any]
    items = raw if isinstance(raw, (list, tuple, set)) else [raw]
    cleaned = {
        _WS.sub(" ", str(item).strip().casefold())
        for item in items
        if item is not None and str(item).strip()
    }
    return tuple(sorted(cleaned))


def parse_partial_date(raw: Any) -> tuple[date | None, DatePrecision]:
    """Normalise a provider date, keeping the precision it actually had.

    Handles ``"2019"``, ``"Jan 2019"``, ``"2019-01"``, ``"2019-01-15"``,
    ``"present"``, a ``date``/``datetime``, and ``{"year":…, "month":…}``.
    """
    if raw is None:
        return None, DatePrecision.NONE
    if isinstance(raw, datetime):
        return raw.date(), DatePrecision.DAY
    if isinstance(raw, date):
        return raw, DatePrecision.DAY
    if isinstance(raw, Mapping):
        year = _as_int(raw.get("year"))
        month = _as_int(raw.get("month"))
        day = _as_int(raw.get("day"))
        if year is None:
            return None, DatePrecision.NONE
        if month is None:
            return date(year, 1, 1), DatePrecision.YEAR
        if day is None:
            return date(year, month, 1), DatePrecision.MONTH
        return date(year, month, day), DatePrecision.DAY

    text = str(raw).strip()
    if not text or text.casefold() in _PRESENT_TOKENS:
        return None, DatePrecision.NONE

    iso = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if iso:
        return _safe_date(iso.group(1), iso.group(2), iso.group(3)), DatePrecision.DAY

    ym = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if ym:
        return _safe_date(ym.group(1), ym.group(2), "1"), DatePrecision.MONTH

    named = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{4})", text)
    if named:
        month = _MONTHS.get(named.group(1).casefold())
        if month is not None:
            return date(int(named.group(2)), month, 1), DatePrecision.MONTH

    named_rev = re.fullmatch(r"(\d{4})\s+([A-Za-z]{3,9})", text)
    if named_rev:
        month = _MONTHS.get(named_rev.group(2).casefold())
        if month is not None:
            return date(int(named_rev.group(1)), month, 1), DatePrecision.MONTH

    year_only = re.fullmatch(r"(\d{4})", text)
    if year_only:
        return date(int(year_only.group(1)), 1, 1), DatePrecision.YEAR

    return None, DatePrecision.NONE


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """First/last split for display only — identity never depends on it."""
    tokens = (full_name or "").split()
    if not tokens:
        return None, None
    if len(tokens) == 1:
        return tokens[0], None
    return " ".join(tokens[:-1]), tokens[-1]


# ---------------------------------------------------------------------------
# Record assembly
# ---------------------------------------------------------------------------


def person_key_for(
    *,
    public_id: str | None,
    full_name: str | None,
    employer: str | None,
) -> str:
    """The interlayer-internal stable id.

    Deliberately the same derivation the ingest layer uses for a member, so an
    enriched record joins onto the connection set without a second, subtly
    different identity scheme.
    """
    first, last = split_name(full_name)
    return ids.member_id(
        slug=public_id, first_name=first, last_name=last, company=employer
    )


def normalize_person(
    payload: Mapping[str, Any],
    *,
    provider: str,
    retrieved_at: datetime,
    field_map: FieldMap = CANONICAL_FIELD_MAP,
    allow_contact_fields: bool = False,
    source_record_id: str | None = None,
    source_url: str | None = None,
    confidence: float = 1.0,
    is_inferred: bool = False,
    license_note: str | None = None,
) -> EnrichedPerson:
    """Turn one provider payload into the canonical record."""
    clean, dropped = drop_contact_fields(
        payload, allow_contact_fields=allow_contact_fields
    )

    get = _getter(clean, field_map.person)
    public_id = normalize_public_id(
        _as_str(get("linkedin_public_id")) or _as_str(get("profile_url"))
    )
    profile_url = canonical_profile_url(
        _as_str(get("profile_url")) or public_id
    )

    full_name = _as_str(get("full_name"))
    first_name = _as_str(get("first_name"))
    last_name = _as_str(get("last_name"))
    if full_name is None and (first_name or last_name):
        full_name = " ".join(part for part in (first_name, last_name) if part)
    if first_name is None and last_name is None:
        first_name, last_name = split_name(full_name)

    employment = _employment_history(clean, field_map, allow_contact_fields)
    education = _education_history(clean, field_map, allow_contact_fields)

    current = next((span for span in employment if span.is_current), None)
    current_employer = _as_str(get("current_employer_name")) or (
        current.employer_name if current else None
    )
    current_title = _as_str(get("current_title")) or (current.title if current else None)
    start_raw = get("current_start_date")
    current_start, current_precision = parse_partial_date(start_raw)
    if current_start is None and current is not None:
        current_start, current_precision = current.start_date, current.start_precision

    seniority_raw = get("current_seniority")
    seniority = normalize_seniority(seniority_raw)
    if seniority is Seniority.UNKNOWN:
        seniority = normalize_seniority(current_title)

    return EnrichedPerson(
        person_key=person_key_for(
            public_id=public_id, full_name=full_name, employer=current_employer
        ),
        linkedin_public_id=public_id,
        profile_url=profile_url,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=_as_str(get("headline")),
        location_country=_as_str(get("location_country")),
        location_region=_as_str(get("location_region")),
        location_city=_as_str(get("location_city")),
        current_employer_name=current_employer,
        current_title=current_title,
        current_seniority=seniority,
        current_start_date=current_start,
        current_start_precision=current_precision,
        employment_history=employment,
        education_history=education,
        skills=normalize_skills(_first_list(clean, field_map.skills_list)),
        connections_count=normalize_connections_count(get("connections_count")),
        followers_count=_as_int(get("followers_count")),
        provenance=EnrichmentProvenance(
            provider=provider,
            retrieved_at=retrieved_at,
            source_record_id=source_record_id,
            source_url=source_url,
            confidence=confidence,
            is_inferred=is_inferred,
            license_note=license_note,
        ),
        dropped_fields=dropped,
    )


def _employment_history(
    payload: Mapping[str, Any], field_map: FieldMap, allow_contact_fields: bool
) -> tuple[EmploymentSpan, ...]:
    spans: list[EmploymentSpan] = []
    for entry in _first_list(payload, field_map.employment_list):
        if not isinstance(entry, Mapping):
            continue
        clean, _ = drop_contact_fields(entry, allow_contact_fields=allow_contact_fields)
        get = _getter(clean, field_map.employment)
        employer = _as_str(get("employer_name"))
        if not employer:
            continue
        start, start_precision = parse_partial_date(get("start_date"))
        end_raw = get("end_date")
        end, end_precision = parse_partial_date(end_raw)
        is_current = end is None
        title = _as_str(get("title"))
        spans.append(
            EmploymentSpan(
                employer_name=employer,
                employer_domain=_as_str(get("employer_domain")),
                employer_id=_as_str(get("employer_id")),
                title=title,
                title_normalized=normalize_title(title),
                seniority=normalize_seniority(title),
                start_date=start,
                end_date=end,
                is_current=is_current,
                location=_as_str(get("location")),
                start_precision=start_precision,
                end_precision=end_precision,
            )
        )
    return tuple(spans)


def _education_history(
    payload: Mapping[str, Any], field_map: FieldMap, allow_contact_fields: bool
) -> tuple[EducationSpan, ...]:
    spans: list[EducationSpan] = []
    for entry in _first_list(payload, field_map.education_list):
        if not isinstance(entry, Mapping):
            continue
        clean, _ = drop_contact_fields(entry, allow_contact_fields=allow_contact_fields)
        get = _getter(clean, field_map.education)
        school = _as_str(get("school_name"))
        if not school:
            continue
        start_year = _as_int(get("start_year"))
        end_year = _as_int(get("end_year"))
        if start_year is None:
            start_date, _ = parse_partial_date(get("start_date"))
            start_year = start_date.year if start_date else None
        if end_year is None:
            end_date, _ = parse_partial_date(get("end_date"))
            end_year = end_date.year if end_date else None
        spans.append(
            EducationSpan(
                school_name=school,
                school_normalized=ids.normalize_text(school) or None,
                degree=_as_str(get("degree")),
                field_of_study=_as_str(get("field_of_study")),
                start_year=start_year,
                end_year=end_year,
            )
        )
    return tuple(spans)


# ---------------------------------------------------------------------------
# Lookup-key helpers
# ---------------------------------------------------------------------------


def lookup_key_for_slug(slug: str) -> LookupKey:
    return LookupKey(key_type=LookupKeyType.LINKEDIN_PUBLIC_ID, value=slug)


def lookup_key_for_url(url: str) -> LookupKey:
    return LookupKey(key_type=LookupKeyType.LINKEDIN_URL, value=url)


def lookup_key_for_name(name: str, employer: str | None = None) -> LookupKey:
    return LookupKey(
        key_type=LookupKeyType.NAME_AND_EMPLOYER, value=name, employer_hint=employer
    )


def match_tokens(key: LookupKey) -> tuple[str, ...]:
    """Every index token a local provider should file a record under."""
    if key.key_type is LookupKeyType.NAME_AND_EMPLOYER:
        return (
            f"name:{ids.name_key(key.value)}|{ids.normalize_text(key.employer_hint)}",
            f"name:{ids.name_key(key.value)}",
        )
    slug = normalize_public_id(key.value)
    return (f"slug:{slug}",) if slug else ()


def cache_id(key: LookupKey) -> str:
    """Content address for one lookup key (re-exported from ``interface``)."""
    return cache_id_for(key.key_type, key.value, key.employer_hint)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _getter(payload: Mapping[str, Any], paths: Mapping[str, tuple[str, ...]]) -> Any:
    def get(canonical: str) -> Any:
        for dotted in paths.get(canonical, ()):
            found = _dig(payload, dotted)
            if found not in (None, "", [], {}):
                return found
        return None

    return get


def _dig(payload: Any, dotted: str) -> Any:
    node: Any = payload
    for segment in dotted.split("."):
        if not isinstance(node, Mapping) or segment not in node:
            return None
        node = node[segment]
    return node


def _first_list(payload: Mapping[str, Any], paths: Sequence[str]) -> list[Any]:
    for dotted in paths:
        found = _dig(payload, dotted)
        if isinstance(found, list):
            return found
    return []


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[,\s]", "", value.strip())
        try:
            return int(digits)
        except ValueError:
            return None
    return None


def _safe_date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


__all__ = [
    "CANONICAL_FIELD_MAP",
    "CONNECTIONS_CENSORED_AT",
    "CONTACT_KEY_MARKERS",
    "FieldMap",
    "cache_id",
    "canonical_profile_url",
    "drop_contact_fields",
    "is_contact_key",
    "lookup_key_for_name",
    "lookup_key_for_slug",
    "lookup_key_for_url",
    "match_tokens",
    "normalize_connections_count",
    "normalize_person",
    "normalize_public_id",
    "normalize_seniority",
    "normalize_skills",
    "normalize_title",
    "parse_partial_date",
    "person_key_for",
    "split_name",
]
