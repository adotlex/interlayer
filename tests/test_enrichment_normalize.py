"""The adapter boundary: contact-field dropping, canonical names, honest precision."""

from __future__ import annotations

from datetime import UTC, date, datetime

from interlayer.collect import schemas
from interlayer.enrichment.interface import DatePrecision, Seniority
from interlayer.enrichment.normalize import (
    CANONICAL_FIELD_MAP,
    canonical_profile_url,
    drop_contact_fields,
    normalize_connections_count,
    normalize_person,
    normalize_public_id,
    normalize_seniority,
    normalize_skills,
    normalize_title,
    parse_partial_date,
    person_key_for,
)
from interlayer.enrichment.providers.brightdata import FIELD_MAP as BRIGHTDATA_MAP
from interlayer.enrichment.providers.coresignal import FIELD_MAP as CORESIGNAL_MAP

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Contact fields
# ---------------------------------------------------------------------------


def test_contact_fields_are_dropped_at_any_depth() -> None:
    payload = {
        "name": "Alex Rivera",
        "email": "alex@example.test",
        "work_email": "a.rivera@acme.test",
        "contact_info": {"phone_numbers": ["+1 555"], "twitter": "@alex"},
        "experience": [{"company": "Acme", "recruiter_email": "hr@acme.test"}],
    }
    clean, dropped = drop_contact_fields(payload)

    assert "email" not in clean
    assert "work_email" not in clean
    assert clean["contact_info"] == {"twitter": "@alex"}
    assert clean["experience"][0] == {"company": "Acme"}
    assert set(dropped) == {
        "email",
        "work_email",
        "contact_info.phone_numbers",
        "experience.recruiter_email",
    }


def test_opting_in_keeps_them_but_must_be_asked_for() -> None:
    payload = {"name": "Alex", "email": "alex@example.test"}
    clean, dropped = drop_contact_fields(payload, allow_contact_fields=True)
    assert clean == payload
    assert dropped == ()


def test_normalize_person_drops_contact_fields_and_records_the_drop() -> None:
    person = normalize_person(
        {
            "full_name": "Alex Rivera",
            "profile_url": "https://www.linkedin.com/in/AlexRivera/",
            "email": "alex@example.test",
            "phone": "+1 555 0100",
        },
        provider="local_file",
        retrieved_at=NOW,
    )
    assert person.full_name == "Alex Rivera"
    assert set(person.dropped_fields) == {"email", "phone"}
    assert not hasattr(person, "email")


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


def test_profile_url_is_built_from_the_schema_table() -> None:
    url = canonical_profile_url("https://uk.linkedin.com/in/AlexRivera/?originalSubdomain=uk")
    assert url == schemas.profile_url("alexrivera")


def test_public_id_extraction() -> None:
    assert normalize_public_id("https://www.linkedin.com/in/dana-wu-7f3a2/") == "dana-wu-7f3a2"
    assert normalize_public_id("dana-wu-7f3a2") == "dana-wu-7f3a2"
    assert normalize_public_id(None) is None


def test_connections_count_is_none_once_the_source_censors_it() -> None:
    """500+ is a display string, not a measurement."""
    assert normalize_connections_count(312) == 312
    assert normalize_connections_count("1,204") is None
    assert normalize_connections_count(500) is None
    assert normalize_connections_count(None) is None


def test_seniority_maps_onto_the_fixed_vocabulary() -> None:
    assert normalize_seniority("Senior Vice President, Trading") is Seniority.VP
    assert normalize_seniority("Senior Director of Research") is Seniority.DIRECTOR
    assert normalize_seniority("Senior Quantitative Researcher") is Seniority.SENIOR
    assert normalize_seniority("Summer Intern") is Seniority.INTERN
    assert normalize_seniority("Chief Technology Officer") is Seniority.CXO
    assert normalize_seniority("Founder") is Seniority.OWNER
    assert normalize_seniority("") is Seniority.UNKNOWN
    assert normalize_seniority("Trader") is Seniority.UNKNOWN


def test_title_normalisation_strips_punctuation() -> None:
    assert normalize_title("  Head, Quant-Research (EMEA) ") == "head quant research emea"


def test_skills_are_casefolded_deduped_and_sorted() -> None:
    assert normalize_skills([" Python ", "python", "C++"]) == ("c++", "python")
    assert normalize_skills(None) == ()


def test_partial_dates_keep_their_precision() -> None:
    assert parse_partial_date("2019") == (date(2019, 1, 1), DatePrecision.YEAR)
    assert parse_partial_date("Jan 2019") == (date(2019, 1, 1), DatePrecision.MONTH)
    assert parse_partial_date("January 2019") == (date(2019, 1, 1), DatePrecision.MONTH)
    assert parse_partial_date("2019-01") == (date(2019, 1, 1), DatePrecision.MONTH)
    assert parse_partial_date("2019-01-15") == (date(2019, 1, 15), DatePrecision.DAY)
    assert parse_partial_date({"year": 2019, "month": 3}) == (
        date(2019, 3, 1),
        DatePrecision.MONTH,
    )
    assert parse_partial_date("present") == (None, DatePrecision.NONE)
    assert parse_partial_date("") == (None, DatePrecision.NONE)
    assert parse_partial_date("sometime in the 90s") == (None, DatePrecision.NONE)


def test_person_key_matches_the_ingest_derivation() -> None:
    from interlayer.core import ids

    key = person_key_for(public_id="alexrivera", full_name="Alex Rivera", employer="Acme")
    assert key == ids.member_id(slug="alexrivera")


# ---------------------------------------------------------------------------
# Per-provider field maps
# ---------------------------------------------------------------------------


def test_canonical_payload_round_trips() -> None:
    person = normalize_person(
        {
            "full_name": "Dana Wu",
            "profile_url": "https://www.linkedin.com/in/danawu-7f3a2",
            "headline": "Quant Researcher at Globex",
            "current_employer_name": "Globex",
            "current_title": "Senior Quant Researcher",
            "location_city": "London",
            "connections_count": 431,
            "skills": ["Python", "python", "Options"],
            "employment_history": [
                {"company": "Globex", "title": "Senior Quant Researcher", "start_date": "Mar 2021"},
                {
                    "company": "Acme",
                    "title": "Analyst",
                    "start_date": "2018",
                    "end_date": "2021-02",
                },
            ],
            "education_history": [
                {"school": "Imperial College", "degree": "MSc", "start_year": "2014"}
            ],
        },
        provider="local_file",
        retrieved_at=NOW,
        field_map=CANONICAL_FIELD_MAP,
    )

    assert person.linkedin_public_id == "danawu-7f3a2"
    assert person.current_seniority is Seniority.SENIOR
    assert person.connections_count == 431
    assert person.skills == ("options", "python")
    assert len(person.employment_history) == 2
    current = person.employment_history[0]
    assert current.is_current is True
    assert current.start_date == date(2021, 3, 1)
    assert current.start_precision is DatePrecision.MONTH
    past = person.employment_history[1]
    assert past.is_current is False
    assert past.start_precision is DatePrecision.YEAR
    assert person.education_history[0].school_name == "Imperial College"
    assert person.provenance is not None and person.provenance.provider == "local_file"


def test_brightdata_sample_shape_maps_onto_canonical_names() -> None:
    """The vendor field named 'connections' is an integer count, not a list."""
    payload = {
        "name": "Jordan Smith",
        "url": "https://www.linkedin.com/in/jsmith-quant-9a1",
        "position": "Quantitative Trader",
        "city": "London",
        "country_code": "GB",
        "connections": 2,
        "profile_info": {"followers": 900},
        "current_company": {"name": "Jane Street", "title": "Quantitative Trader"},
        "experience": [
            {"company": "Jane Street", "title": "Quantitative Trader", "start_date": "Jun 2022"}
        ],
        "education": [{"title": "Oxford", "degree": "BA"}],
        "email": "leaked@example.test",
    }
    person = normalize_person(
        payload, provider="brightdata", retrieved_at=NOW, field_map=BRIGHTDATA_MAP
    )
    assert person.full_name == "Jordan Smith"
    assert person.current_employer_name == "Jane Street"
    assert person.location_country == "GB"
    assert person.connections_count == 2
    assert person.followers_count == 900
    assert person.dropped_fields == ("email",)
    assert person.employment_history[0].employer_name == "Jane Street"


def test_coresignal_shape_maps_onto_canonical_names() -> None:
    payload = {
        "id": 12345,
        "name": "Priya Okafor",
        "url": "https://www.linkedin.com/in/p-okafor-2c8",
        "title": "Software Engineer",
        "location": "New York",
        "connections_count": 501,
        "member_experience_collection": [
            {
                "company_name": "Citadel Securities",
                "title": "Software Engineer",
                "date_from": "2023-04",
            }
        ],
        "member_education_collection": [{"title": "NYU", "subtitle": "BSc"}],
    }
    person = normalize_person(
        payload, provider="coresignal", retrieved_at=NOW, field_map=CORESIGNAL_MAP
    )
    assert person.linkedin_public_id == "p-okafor-2c8"
    assert person.current_employer_name == "Citadel Securities"
    assert person.connections_count is None  # censored at 500
    assert person.employment_history[0].start_date == date(2023, 4, 1)
