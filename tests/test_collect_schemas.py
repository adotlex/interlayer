"""The schema table is the project's honesty mechanism; test it as such."""

from __future__ import annotations

import pytest

from interlayer.collect import schemas


def test_every_entry_is_documented_and_stamped() -> None:
    for entry in schemas.iter_entries():
        assert entry.values, f"{entry.key} has no values"
        assert all(isinstance(v, str) and v for v in entry.values), entry.key
        assert entry.note.strip(), f"{entry.key} has no note explaining what it is"
        assert entry.version, f"{entry.key} is not version-stamped"


def test_keys_are_namespaced_and_unique() -> None:
    keys = schemas.keys()
    assert len(keys) == len(set(keys))
    assert all("." in key for key in keys)


def test_no_linkedin_shape_claims_to_be_verified() -> None:
    """No agent on this project could reach linkedin.com.

    Every shape describing LinkedIn's own surfaces is therefore second-hand and
    must ship ``verified=False``. Only shapes we can actually confirm — the HAR
    container format and our own CSV column specs — may claim otherwise.
    """
    linkedin_prefixes = ("url.", "param.", "enum.network", "enum.member_distance", "json.", "token.")
    for entry in schemas.iter_entries():
        if entry.key.startswith("har.") or entry.key.startswith("csv."):
            continue
        assert entry.key.startswith(linkedin_prefixes), f"unclassified entry {entry.key}"
        assert entry.verified is False, (
            f"{entry.key} claims to be verified, but nothing in this project has "
            "ever seen a LinkedIn response"
        )


def test_verified_entries_are_only_the_ones_we_can_see() -> None:
    for key in schemas.verified_keys():
        assert key.startswith(("har.", "csv."))


def test_unverified_set_is_not_empty_and_is_reported() -> None:
    unverified = schemas.unverified_keys()
    assert unverified, "the table must admit what it could not confirm"
    assert "json.search_clusters" in unverified
    assert "param.connection_of" in unverified


def test_staleness_warning_names_the_stamp_date() -> None:
    assert schemas.SCHEMA_VERSION in schemas.STALENESS_WARNING


def test_unknown_key_raises_rather_than_returning_none() -> None:
    with pytest.raises(schemas.UnknownSchemaKey) as excinfo:
        schemas.entry("json.no_such_path")
    assert "json.no_such_path" in str(excinfo.value)
    assert "json.search_clusters" in str(excinfo.value)  # lists the real keys


def test_describe_is_useful_in_a_diagnostic() -> None:
    described = schemas.describe("json.search_clusters")
    assert "json.search_clusters" in described
    assert "UNVERIFIED" in described
    assert schemas.SCHEMA_VERSION in described
    assert "data.searchDashClustersByAll.elements" in described


def test_dig_walks_a_path_and_returns_none_for_a_miss() -> None:
    document = {"data": {"searchDashClustersByAll": {"elements": [1, 2]}}}
    assert schemas.dig(document, "json.search_clusters") == [1, 2]
    assert schemas.dig({}, "json.search_clusters") is None
    assert schemas.dig({"data": "not-a-mapping"}, "json.search_clusters") is None


def test_field_tries_every_alias_in_order() -> None:
    assert schemas.field({"entityUrn": "a"}, "json.entity_urn") == "a"
    assert schemas.field({"trackingUrn": "b"}, "json.entity_tracking_urn") == "b"
    assert schemas.field({"nope": 1}, "json.entity_urn") is None


def test_query_values_accepts_the_legacy_parameter_spelling() -> None:
    """The facet prefix was dropped once already; both spellings must work."""
    current = {"connectionOf": ['["ACoAA1"]']}
    legacy = {"facetConnectionOf": ['["ACoAA1"]']}
    assert schemas.query_values(current, "param.connection_of") == ('["ACoAA1"]',)
    assert schemas.query_values(legacy, "param.connection_of") == ('["ACoAA1"]',)
    assert schemas.query_values({}, "param.connection_of") == ()


def test_profile_url_is_assembled_from_the_table() -> None:
    url = schemas.profile_url("alexrivera")
    assert url.startswith("https://" + schemas.value("url.host"))
    assert url.endswith("/in/alexrivera")


def test_locale_subdomains_are_recognised_as_the_same_host() -> None:
    assert schemas.is_linkedin_host("www.linkedin.com")
    assert schemas.is_linkedin_host("uk.linkedin.com")
    assert schemas.is_linkedin_host("LINKEDIN.COM")
    assert not schemas.is_linkedin_host("linkedin.com.evil.test")
    assert not schemas.is_linkedin_host(None)


def test_sensitive_header_detection_is_generic_http_vocabulary() -> None:
    assert schemas.is_sensitive_header("Cookie")
    assert schemas.is_sensitive_header("set-cookie")
    assert schemas.is_sensitive_header("Authorization")
    assert not schemas.is_sensitive_header("content-type")


def test_capture_status_vocabulary_has_all_five_states() -> None:
    """Four is not enough: 'observed and empty' is not 'never looked at'."""
    assert set(schemas.values("csv.capture_status_values")) == {
        "complete",
        "truncated",
        "empty",
        "unavailable",
        "skipped_by_degree",
    }


def test_csv_column_specs_are_verbatim_from_the_research() -> None:
    assert schemas.values("csv.mutuals_columns") == (
        "target_id",
        "mutual_name",
        "mutual_profile_url",
        "mutual_headline",
        "capture_status",
        "reported_count",
        "captured_at",
        "source_url",
        "collector",
        "notes",
    )
    assert schemas.values("csv.targets_columns") == (
        "target_id",
        "name",
        "profile_url",
        "firm",
        "degree",
        "degree_observed_at",
        "enumeration_source",
        "enumeration_truncated",
        "notes",
    )


def test_table_report_covers_every_entry() -> None:
    assert len(schemas.table_report()) == len(schemas.SCHEMA)
