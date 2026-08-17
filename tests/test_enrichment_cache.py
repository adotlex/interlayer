"""The content-addressed cache. A fully cached run must cost nothing."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from interlayer.enrichment.cache import (
    DEFAULT_NEGATIVE_TTL_DAYS,
    DEFAULT_POSITIVE_TTL_DAYS,
    EnrichmentCache,
    cache_key,
)
from interlayer.enrichment.interface import (
    DatePrecision,
    EducationSpan,
    EmploymentSpan,
    EnrichedPerson,
    EnrichmentProvenance,
    LookupKey,
    LookupKeyType,
    Seniority,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=timezone.utc)
KEY = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "danawu-7f3a2")


def person() -> EnrichedPerson:
    return EnrichedPerson(
        person_key="m_abc",
        linkedin_public_id="danawu-7f3a2",
        profile_url="https://www.linkedin.com/in/danawu-7f3a2",
        full_name="Dana Wu",
        first_name="Dana",
        last_name="Wu",
        headline="Quant Researcher",
        location_city="London",
        current_employer_name="Globex",
        current_title="Quant Researcher",
        current_seniority=Seniority.SENIOR,
        current_start_date=date(2021, 3, 1),
        current_start_precision=DatePrecision.MONTH,
        employment_history=(
            EmploymentSpan(
                employer_name="Globex",
                title="Quant Researcher",
                seniority=Seniority.SENIOR,
                start_date=date(2021, 3, 1),
                is_current=True,
                start_precision=DatePrecision.MONTH,
            ),
        ),
        education_history=(EducationSpan(school_name="Imperial", degree="MSc"),),
        skills=("options", "python"),
        connections_count=431,
        provenance=EnrichmentProvenance(provider="local_file", retrieved_at=NOW),
        dropped_fields=("email",),
    )


def cache(tmp_path: Path, **kwargs: object) -> EnrichmentCache:
    return EnrichmentCache(tmp_path / "enrichment.sqlite", **kwargs)  # type: ignore[arg-type]


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    with cache(tmp_path) as store:
        store.put("local_file", KEY, person(), cost_usd=0.0, now=NOW)
        entry = store.get("local_file", KEY, now=NOW)

    assert entry is not None
    assert entry.person == person()


def test_key_is_content_addressed_and_provider_scoped() -> None:
    a = cache_key("brightdata", KEY, 1)
    b = cache_key("coresignal", KEY, 1)
    c = cache_key("brightdata", KEY, 2)
    assert len({a, b, c}) == 3
    assert a == cache_key("brightdata", KEY, 1)


def test_bumping_the_schema_version_invalidates_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "enrichment.sqlite"
    with EnrichmentCache(path, schema_version=1) as store:
        store.put("local_file", KEY, person(), now=NOW)
    with EnrichmentCache(path, schema_version=2) as store:
        assert store.get("local_file", KEY, now=NOW) is None
        assert len(store) == 1  # the old row is still there, just unreachable


def test_misses_are_cached_too(tmp_path: Path) -> None:
    """Otherwise a 40%-coverage run re-pays for the missing 60% every time."""
    with cache(tmp_path) as store:
        store.put("brightdata", KEY, None, cost_usd=0.001, now=NOW)
        entry = store.get("brightdata", KEY, now=NOW)
        assert entry is not None
        assert entry.is_miss
        assert entry.cost_usd == 0.001


def test_positive_and_negative_ttls_differ(tmp_path: Path) -> None:
    with cache(tmp_path) as store:
        store.put("brightdata", KEY, person(), now=NOW)
        miss_key = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "nobody")
        store.put("brightdata", miss_key, None, now=NOW)

        just_inside_negative = NOW + timedelta(days=DEFAULT_NEGATIVE_TTL_DAYS - 1)
        just_outside_negative = NOW + timedelta(days=DEFAULT_NEGATIVE_TTL_DAYS + 1)
        assert store.get("brightdata", miss_key, now=just_inside_negative) is not None
        assert store.get("brightdata", miss_key, now=just_outside_negative) is None

        # The hit is still live long after the miss has expired.
        assert store.get("brightdata", KEY, now=just_outside_negative) is not None
        expired = NOW + timedelta(days=DEFAULT_POSITIVE_TTL_DAYS + 1)
        assert store.get("brightdata", KEY, now=expired) is None


def test_purge_by_provider(tmp_path: Path) -> None:
    with cache(tmp_path) as store:
        store.put("brightdata", KEY, person(), now=NOW)
        store.put("coresignal", KEY, person(), now=NOW)
        assert store.purge(provider="brightdata") == 1
        assert store.get("brightdata", KEY, now=NOW) is None
        assert store.get("coresignal", KEY, now=NOW) is not None


def test_purge_by_age_and_purge_all(tmp_path: Path) -> None:
    with cache(tmp_path) as store:
        store.put("brightdata", KEY, person(), now=NOW - timedelta(days=40))
        store.put("coresignal", KEY, person(), now=NOW)
        assert store.purge(older_than_days=30, now=NOW) == 1
        assert len(store) == 1
        assert store.purge() == 1
        assert len(store) == 0


def test_purge_expired_respects_both_ttls(tmp_path: Path) -> None:
    with cache(tmp_path) as store:
        store.put("brightdata", KEY, person(), now=NOW - timedelta(days=10))
        store.put(
            "brightdata",
            LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "nobody"),
            None,
            now=NOW - timedelta(days=10),
        )
        assert store.purge_expired(now=NOW) == 1  # the miss only
        assert len(store) == 1


def test_cache_file_is_created_with_parents(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "enrichment.sqlite"
    with EnrichmentCache(path) as store:
        store.put("local_file", KEY, None, now=NOW)
    assert path.exists()
