"""Slug normalisation -- the primary key the whole pipeline joins on.

Two failure modes, and they are not symmetric. Splitting one person across two
keys costs an edge. *Merging* two people into one key invents an edge between
strangers and there is no downstream stage that can detect it, so the
case-sensitivity rules around member URNs are tested harder than anything else
in this file.
"""

from __future__ import annotations

import pytest

from interlayer.ingest.slug import (
    PUB_PREFIX,
    canonical_url,
    is_member_urn,
    normalize_slug,
)

# Every one of these is the same human. Scheme, host, locale subdomain, mobile
# route, query, fragment, casing, encoding and trailing route segments are all
# rendering detail, not identity.
ONE_PROFILE = [
    "https://www.linkedin.com/in/ada-lovelace",
    "https://www.linkedin.com/in/ada-lovelace/",
    "http://www.linkedin.com/in/ada-lovelace",
    "http://linkedin.com/in/ada-lovelace",
    "https://linkedin.com/in/ada-lovelace",
    "www.linkedin.com/in/ada-lovelace",
    "linkedin.com/in/ada-lovelace",
    "https://uk.linkedin.com/in/ada-lovelace",
    "https://de.linkedin.com/in/ada-lovelace/",
    "https://m.linkedin.com/in/ada-lovelace",
    "https://www.linkedin.com/mwlite/in/ada-lovelace",
    "https://www.linkedin.com/in/ada-lovelace?trk=contacts-contacts-list",
    "https://www.linkedin.com/in/ada-lovelace/?originalSubdomain=uk",
    "https://www.linkedin.com/in/ada-lovelace#experience",
    "https://www.linkedin.com//in//ada-lovelace",
    "https://www.linkedin.com/in/ada%2Dlovelace",
    "https://www.linkedin.com/in/ada-lovelace/en",
    "https://www.linkedin.com/in/ada-lovelace/overlay/contact-info/",
    "https://www.linkedin.com/in/ada-lovelace/detail/contact-info/",
    "/in/ada-lovelace",
    "ada-lovelace",
    "  https://www.linkedin.com/in/Ada-Lovelace/  ",
    "HTTPS://WWW.LINKEDIN.COM/IN/ADA-LOVELACE",
]


@pytest.mark.parametrize("raw", ONE_PROFILE)
def test_every_spelling_folds_to_the_same_key(raw: str) -> None:
    assert normalize_slug(raw) == "ada-lovelace"


def test_the_whole_set_collapses_to_exactly_one_key() -> None:
    assert len({normalize_slug(raw) for raw in ONE_PROFILE}) == 1


# ---------------------------------------------------------------------------
# member URNs: case carries information
# ---------------------------------------------------------------------------


def test_member_urns_differing_only_in_case_stay_distinct() -> None:
    """``ACoAAB123`` and ``acoaab123`` are two people, not one.

    Member URNs are base64-ish, so casefolding them merges strangers -- the
    worst failure this tool can have.
    """
    upper = normalize_slug("https://www.linkedin.com/in/ACoAAB123")
    lower = normalize_slug("https://www.linkedin.com/in/acoaab123")

    assert upper == "ACoAAB123"
    assert lower == "acoaab123"
    assert upper != lower


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/ACoAAB123",
        "https://uk.linkedin.com/in/ACoAAB123/",
        "https://www.linkedin.com/in/ACoAAB123?trk=x",
        "https://www.linkedin.com/in/ACoAAB123/en",
        "https://www.linkedin.com/mwlite/in/ACoAAB123",
        "ACoAAB123",
    ],
)
def test_urn_decorations_are_stripped_without_touching_case(raw: str) -> None:
    assert normalize_slug(raw) == "ACoAAB123"


def test_two_urns_differing_in_one_letter_case_stay_distinct() -> None:
    keys = {
        normalize_slug(f"https://www.linkedin.com/in/{urn}")
        for urn in ("ACoAABcDeF", "ACoAABcDef", "ACoAABCDEF", "ACoAAbcdef")
    }
    assert len(keys) == 4


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("ACoAAB123", True),
        ("ACoAA-_x9", True),
        ("acoaab123", False),  # only the literal prefix earns case-sensitivity
        ("ACOAAB123", False),
        ("ACoA", False),  # prefix alone, no payload
        ("ada-lovelace", False),
        ("acola-bianchi", False),
    ],
)
def test_is_member_urn(slug: str, expected: bool) -> None:
    assert is_member_urn(slug) is expected


def test_ordinary_vanity_slugs_do_casefold() -> None:
    assert normalize_slug("https://www.linkedin.com/in/Ada-Lovelace") == normalize_slug(
        "https://www.linkedin.com/in/ada-lovelace"
    )
    assert normalize_slug("https://www.linkedin.com/in/ADA-LOVELACE") == "ada-lovelace"


# ---------------------------------------------------------------------------
# legacy /pub/ URLs
# ---------------------------------------------------------------------------


def test_legacy_pub_url_keeps_its_whole_path() -> None:
    """The discriminating digits live in the later segments."""
    assert normalize_slug("https://www.linkedin.com/pub/jane-doe/1/2a/3b") == "pub/jane-doe/1/2a/3b"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/pub/jane-doe/1/2a/3b",
        "https://uk.linkedin.com/pub/jane-doe/1/2a/3b/",
        "http://www.linkedin.com/pub/jane-doe/1/2a/3b?trk=pub-pbmap",
        "https://www.linkedin.com/pub/Jane-Doe/1/2A/3B",
        "https://www.linkedin.com/pub/jane-doe/1/2a/3b/en",
    ],
)
def test_pub_spellings_fold_together(raw: str) -> None:
    assert normalize_slug(raw) == "pub/jane-doe/1/2a/3b"


def test_pub_urls_are_not_merged_by_a_shared_segment() -> None:
    keys = {
        normalize_slug(f"https://www.linkedin.com/pub/{tail}")
        for tail in ("jane-doe/1/2a/3b", "jane-doe/9/2a/3b", "john-doe/1/2a/3b", "jane-doe/1/2a/9b")
    }
    assert len(keys) == 4


def test_a_pub_key_can_never_collide_with_an_in_slug() -> None:
    pub = normalize_slug("https://www.linkedin.com/pub/jane-doe/1/2a/3b")
    modern = normalize_slug("https://www.linkedin.com/in/jane-doe")

    assert pub is not None and pub.startswith(PUB_PREFIX)
    assert "/" in pub and modern == "jane-doe"
    assert pub != modern


# ---------------------------------------------------------------------------
# everything that is not a member profile
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/company/jane-street",
        "https://www.linkedin.com/company/jane-street/people/",
        "https://www.linkedin.com/school/mit/",
        "https://www.linkedin.com/feed/",
        "https://www.linkedin.com/jobs/view/123456",
        "https://www.linkedin.com/in/",
        "https://www.linkedin.com/in//",
    ],
)
def test_non_member_linkedin_pages_are_none(raw: str) -> None:
    assert normalize_slug(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://example.com/in/ada-lovelace",
        "https://twitter.com/ada",
        "https://linkedin.com.evil.example/in/ada-lovelace",
        "https://notlinkedin.com/in/ada-lovelace",
        "https://www.linkedln.com/in/ada-lovelace",  # typosquat: l-n, not i-n
    ],
)
def test_non_linkedin_hosts_are_none(raw: str) -> None:
    assert normalize_slug(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "\t\n",
        "n/a",
        "N/A",
        "-",
        "???",
        "not on linkedin",
        "ada@example.com",
        "javascript:alert(1)",
        "http://[bad:ipv6/in/ada",
    ],
)
def test_empty_and_junk_are_none(raw: str | None) -> None:
    assert normalize_slug(raw) is None


# ---------------------------------------------------------------------------
# canonical_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://uk.linkedin.com/in/Ada-Lovelace/?x=1",
            "https://www.linkedin.com/in/ada-lovelace",
        ),
        ("https://www.linkedin.com/in/ACoAAB123/en", "https://www.linkedin.com/in/ACoAAB123"),
        (
            "https://www.linkedin.com/pub/jane-doe/1/2a/3b/",
            "https://www.linkedin.com/pub/jane-doe/1/2a/3b",
        ),
    ],
)
def test_canonical_url_is_one_spelling_per_key(raw: str, expected: str) -> None:
    slug = normalize_slug(raw)
    assert slug is not None
    assert canonical_url(slug) == expected


@pytest.mark.parametrize("raw", [*ONE_PROFILE, "https://www.linkedin.com/in/ACoAAB123/en"])
def test_canonical_url_round_trips_through_normalisation(raw: str) -> None:
    slug = normalize_slug(raw)
    assert slug is not None
    assert normalize_slug(canonical_url(slug)) == slug
