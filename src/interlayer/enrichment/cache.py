"""Content-addressed SQLite cache for enrichment lookups.

Checked **before** the rate limiter and **before** the budget guard, so a fully
cached run makes zero network calls and costs zero. Misses are cached too, on a
shorter TTL: a target list with 40% coverage would otherwise re-pay for the
missing 60% on every single run.

The cache key is ``sha256(provider|schema_version|key.cache_id())``. Bumping
``schema_version`` therefore invalidates everything cleanly without deleting the
file or serving records the current reader cannot parse.

Deletion is a first-class operation, not an afterthought: GDPR Article 17 is not
optional, and the enforcement action that shut down one of the vendors surveyed in
Wave 1 was specifically a deletion order.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from interlayer.enrichment.interface import (
    ENRICHMENT_SCHEMA_VERSION,
    DatePrecision,
    EducationSpan,
    EmploymentSpan,
    EnrichedPerson,
    EnrichmentProvenance,
    LookupKey,
    Seniority,
)

DEFAULT_POSITIVE_TTL_DAYS = 30
DEFAULT_NEGATIVE_TTL_DAYS = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichment_cache (
    cache_id       TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json   TEXT,
    is_miss        INTEGER NOT NULL,
    retrieved_at   TEXT NOT NULL,
    cost_usd       REAL NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS enrichment_cache_provider
    ON enrichment_cache (provider, retrieved_at);
"""


@dataclass(frozen=True)
class CacheEntry:
    """One stored lookup outcome."""

    cache_id: str
    provider: str
    person: EnrichedPerson | None
    retrieved_at: datetime
    cost_usd: float = 0.0

    @property
    def is_miss(self) -> bool:
        return self.person is None


def cache_key(provider: str, key: LookupKey, schema_version: int) -> str:
    """The content address for one (provider, key, schema version)."""
    basis = f"{provider}|{schema_version}|{key.cache_id()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


class EnrichmentCache:
    """SQLite-backed cache. Safe to construct against a path that does not exist."""

    def __init__(
        self,
        path: Path | str,
        *,
        schema_version: int = ENRICHMENT_SCHEMA_VERSION,
        positive_ttl_days: int = DEFAULT_POSITIVE_TTL_DAYS,
        negative_ttl_days: int = DEFAULT_NEGATIVE_TTL_DAYS,
    ) -> None:
        self.path = Path(path)
        self.schema_version = schema_version
        self.positive_ttl = timedelta(days=positive_ttl_days)
        self.negative_ttl = timedelta(days=negative_ttl_days)
        if self.path.parent and str(self.path.parent) not in {"", "."}:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        with self._write() as conn:
            if str(self.path) != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> EnrichmentCache:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connection:
            yield self._connection

    # -- reads -------------------------------------------------------------

    def get(
        self, provider: str, key: LookupKey, *, now: datetime | None = None
    ) -> CacheEntry | None:
        """Return a live entry, or ``None`` when absent or expired."""
        moment = now or _now()
        row = self._connection.execute(
            "SELECT * FROM enrichment_cache WHERE cache_id = ?",
            (cache_key(provider, key, self.schema_version),),
        ).fetchone()
        if row is None:
            return None
        retrieved_at = _parse_dt(row["retrieved_at"])
        is_miss = bool(row["is_miss"])
        ttl = self.negative_ttl if is_miss else self.positive_ttl
        if moment - retrieved_at > ttl:
            return None
        person = None if is_miss else _decode_person(json.loads(row["payload_json"]))
        return CacheEntry(
            cache_id=row["cache_id"],
            provider=row["provider"],
            person=person,
            retrieved_at=retrieved_at,
            cost_usd=float(row["cost_usd"]),
        )

    def __len__(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM enrichment_cache").fetchone()
        return int(row["n"])

    # -- writes ------------------------------------------------------------

    def put(
        self,
        provider: str,
        key: LookupKey,
        person: EnrichedPerson | None,
        *,
        cost_usd: float = 0.0,
        now: datetime | None = None,
    ) -> str:
        """Store a hit or a miss. Returns the cache id."""
        identifier = cache_key(provider, key, self.schema_version)
        payload = None if person is None else json.dumps(
            _encode_person(person), sort_keys=True, separators=(",", ":")
        )
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO enrichment_cache "
                "(cache_id, provider, schema_version, payload_json, is_miss, "
                " retrieved_at, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    provider,
                    self.schema_version,
                    payload,
                    int(person is None),
                    _format_dt(now or _now()),
                    float(cost_usd),
                ),
            )
        return identifier

    def purge(
        self,
        *,
        provider: str | None = None,
        older_than_days: int | None = None,
        now: datetime | None = None,
    ) -> int:
        """Delete entries; returns the number removed. No filters means all."""
        clauses: list[str] = []
        params: list[Any] = []
        if provider is not None:
            clauses.append("provider = ?")
            params.append(provider)
        if older_than_days is not None:
            cutoff = (now or _now()) - timedelta(days=older_than_days)
            clauses.append("retrieved_at < ?")
            params.append(_format_dt(cutoff))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._write() as conn:
            cursor = conn.execute(f"DELETE FROM enrichment_cache{where}", params)
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Drop everything past its TTL. Cheap to run at startup."""
        moment = now or _now()
        with self._write() as conn:
            cursor = conn.execute(
                "DELETE FROM enrichment_cache WHERE "
                "(is_miss = 0 AND retrieved_at < ?) OR (is_miss = 1 AND retrieved_at < ?)",
                (
                    _format_dt(moment - self.positive_ttl),
                    _format_dt(moment - self.negative_ttl),
                ),
            )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _encode_person(person: EnrichedPerson) -> dict[str, Any]:
    return {
        "person_key": person.person_key,
        "linkedin_public_id": person.linkedin_public_id,
        "profile_url": person.profile_url,
        "full_name": person.full_name,
        "first_name": person.first_name,
        "last_name": person.last_name,
        "headline": person.headline,
        "location_country": person.location_country,
        "location_region": person.location_region,
        "location_city": person.location_city,
        "current_employer_name": person.current_employer_name,
        "current_title": person.current_title,
        "current_seniority": person.current_seniority.value,
        "current_start_date": _format_date(person.current_start_date),
        "current_start_precision": person.current_start_precision.value,
        "employment_history": [
            {
                "employer_name": span.employer_name,
                "employer_domain": span.employer_domain,
                "employer_id": span.employer_id,
                "title": span.title,
                "title_normalized": span.title_normalized,
                "seniority": span.seniority.value,
                "start_date": _format_date(span.start_date),
                "end_date": _format_date(span.end_date),
                "is_current": span.is_current,
                "location": span.location,
                "start_precision": span.start_precision.value,
                "end_precision": span.end_precision.value,
            }
            for span in person.employment_history
        ],
        "education_history": [
            {
                "school_name": span.school_name,
                "school_normalized": span.school_normalized,
                "degree": span.degree,
                "field_of_study": span.field_of_study,
                "start_year": span.start_year,
                "end_year": span.end_year,
            }
            for span in person.education_history
        ],
        "skills": list(person.skills),
        "connections_count": person.connections_count,
        "followers_count": person.followers_count,
        "dropped_fields": list(person.dropped_fields),
        "provenance": None
        if person.provenance is None
        else {
            "provider": person.provenance.provider,
            "retrieved_at": _format_dt(person.provenance.retrieved_at),
            "source_record_id": person.provenance.source_record_id,
            "source_url": person.provenance.source_url,
            "confidence": person.provenance.confidence,
            "is_inferred": person.provenance.is_inferred,
            "license_note": person.provenance.license_note,
        },
    }


def _decode_person(payload: dict[str, Any]) -> EnrichedPerson:
    provenance_payload = payload.get("provenance")
    provenance = (
        None
        if not provenance_payload
        else EnrichmentProvenance(
            provider=provenance_payload["provider"],
            retrieved_at=_parse_dt(provenance_payload["retrieved_at"]),
            source_record_id=provenance_payload.get("source_record_id"),
            source_url=provenance_payload.get("source_url"),
            confidence=float(provenance_payload.get("confidence", 1.0)),
            is_inferred=bool(provenance_payload.get("is_inferred", False)),
            license_note=provenance_payload.get("license_note"),
        )
    )
    return EnrichedPerson(
        person_key=payload["person_key"],
        linkedin_public_id=payload.get("linkedin_public_id"),
        profile_url=payload.get("profile_url"),
        full_name=payload.get("full_name"),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name"),
        headline=payload.get("headline"),
        location_country=payload.get("location_country"),
        location_region=payload.get("location_region"),
        location_city=payload.get("location_city"),
        current_employer_name=payload.get("current_employer_name"),
        current_title=payload.get("current_title"),
        current_seniority=Seniority(payload.get("current_seniority", "unknown")),
        current_start_date=_parse_date(payload.get("current_start_date")),
        current_start_precision=DatePrecision(payload.get("current_start_precision", "none")),
        employment_history=tuple(
            EmploymentSpan(
                employer_name=span["employer_name"],
                employer_domain=span.get("employer_domain"),
                employer_id=span.get("employer_id"),
                title=span.get("title"),
                title_normalized=span.get("title_normalized"),
                seniority=Seniority(span.get("seniority", "unknown")),
                start_date=_parse_date(span.get("start_date")),
                end_date=_parse_date(span.get("end_date")),
                is_current=bool(span.get("is_current", False)),
                location=span.get("location"),
                start_precision=DatePrecision(span.get("start_precision", "none")),
                end_precision=DatePrecision(span.get("end_precision", "none")),
            )
            for span in payload.get("employment_history", [])
        ),
        education_history=tuple(
            EducationSpan(
                school_name=span["school_name"],
                school_normalized=span.get("school_normalized"),
                degree=span.get("degree"),
                field_of_study=span.get("field_of_study"),
                start_year=span.get("start_year"),
                end_year=span.get("end_year"),
            )
            for span in payload.get("education_history", [])
        ),
        skills=tuple(payload.get("skills", [])),
        connections_count=payload.get("connections_count"),
        followers_count=payload.get("followers_count"),
        provenance=provenance,
        dropped_fields=tuple(payload.get("dropped_fields", [])),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_date(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


__all__ = [
    "DEFAULT_NEGATIVE_TTL_DAYS",
    "DEFAULT_POSITIVE_TTL_DAYS",
    "CacheEntry",
    "EnrichmentCache",
    "cache_key",
]
