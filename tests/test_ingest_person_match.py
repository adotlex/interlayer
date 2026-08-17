"""Person entity resolution — ruling C2.

Identity is an exact key. ``WRatio >= 92`` only ranks review candidates, only
inside a block, and never merges anything.
"""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime

import pytest

import interlayer.ingest
from interlayer.core.models import CompliancePosture, Firm, Member, Provenance, Target
from interlayer.ingest.person_match import (
    BLOCK_INITIALS,
    BLOCK_NAME,
    BLOCK_SLUG,
    WRATIO_REVIEW_MIN,
    PersonRecord,
    PersonResolver,
    blocking_keys,
    dedupe,
    from_member,
    from_target,
    identity_key,
    person_id,
)


def rec(
    record_id: str,
    first: str,
    last: str = "",
    *,
    slug: str | None = None,
    urn: str | None = None,
    company: str = "",
) -> PersonRecord:
    return PersonRecord(
        record_id=record_id,
        first_name=first,
        last_name=last,
        slug=slug,
        urn=urn,
        company_key=company,
    )


# ---------------------------------------------------------------------------
# The order-free key (and what it must not do)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (("Robert", "Smith"), ("Smith,", "Robert")),
        (("José", "García"), ("Jose", "Garcia")),
        (("Nguyễn Văn", "An"), ("Nguyen Van", "An")),
        (("Björn", "Åkesson"), ("Bjorn", "Akesson")),
        (("Li", "Ming"), ("Ming", "Li")),
        (("Kim", "Min-jun"), ("Min jun", "Kim")),
        (("Robert", "Smith Jr."), ("Robert", "Smith")),
    ],
)
def test_the_sorted_token_key_is_order_and_accent_free(a, b) -> None:
    assert rec("a", *a).name_key == rec("b", *b).name_key


def test_non_latin_names_are_never_transliterated() -> None:
    """``unidecode`` would render 李明 as "Li Ming". We keep the characters."""
    assert rec("a", "李明").name_key == "李明"
    assert rec("a", "محمد").name_key != ""
    assert rec("a", "李明").name_key != "li ming"


def test_no_surname_is_ever_guessed() -> None:
    """``nameparser`` inverts these two. Sorting the tokens means nobody has
    to decide which token is the family name."""
    vietnamese = rec("a", "Nguyễn", "Văn An")
    korean = rec("b", "Kim", "Min-jun")
    assert vietnamese.name_key == "an nguyen van"
    assert korean.name_key == "jun kim min"


def test_the_ingest_package_imports_neither_unidecode_nor_nameparser() -> None:
    forbidden = {"unidecode", "nameparser"}
    for info in pkgutil.iter_modules(interlayer.ingest.__path__):
        module = importlib.import_module(f"interlayer.ingest.{info.name}")
        source = (module.__file__ or "")
        assert source
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for name in forbidden:
            assert f"import {name}" not in text
            assert f"from {name}" not in text


# ---------------------------------------------------------------------------
# Blocking vs identity
# ---------------------------------------------------------------------------


def test_blocking_keys_cover_slug_name_and_initials() -> None:
    keys = blocking_keys(rec("a", "Robert", "Smith", slug="rsmith"))
    assert f"{BLOCK_SLUG}:rsmith" in keys
    assert f"{BLOCK_NAME}:robert smith" in keys
    assert any(k.startswith(f"{BLOCK_INITIALS}:") for k in keys)


def test_a_single_token_name_gets_no_initials_block() -> None:
    keys = blocking_keys(rec("a", "Cher"))
    assert not any(k.startswith(f"{BLOCK_INITIALS}:") for k in keys)


def test_identity_prefers_urn_then_slug_then_name_and_company() -> None:
    assert identity_key(rec("a", "A", "B", slug="ab", urn="urn:li:member:9")).startswith("urn:")
    assert identity_key(rec("b", "A", "B", slug="ab")) == "slug:ab"
    assert identity_key(rec("c", "A", "B", company="jane_street")).startswith("name:")


def test_person_ids_are_stable_and_never_recomputed_from_a_changed_slug() -> None:
    first = person_id("slug:ada-lovelace")
    assert first == person_id("slug:ada-lovelace")
    assert first != person_id("slug:ada-lovelace-1")
    assert first.startswith("p_")


# ---------------------------------------------------------------------------
# Dedupe merges on exact keys only
# ---------------------------------------------------------------------------


def test_the_same_slug_merges() -> None:
    result = dedupe(
        [
            rec("m1", "Ada", "Lovelace", slug="ada-lovelace"),
            rec("m2", "A.", "Lovelace", slug="https://www.linkedin.com/in/Ada-Lovelace/"),
        ]
    )
    assert len(result.clusters) == 1
    assert result.clusters[0].record_ids == ("m1", "m2")
    assert result.clusters[0].slugs == ("ada-lovelace",)


def test_same_name_and_company_merges_when_no_slug_exists() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", company="jane_street"),
            rec("m2", "Smith", "Robert", company="jane_street"),
        ]
    )
    assert len(result.clusters) == 1


def test_same_name_different_company_does_not_merge() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", company="jane_street"),
            rec("m2", "Robert", "Smith", company="citadel"),
        ]
    )
    assert len(result.clusters) == 2


def test_li_ming_and_ming_li_reach_review_but_never_merge() -> None:
    """The sorted key's own limitation, handled honestly: it blocks them
    together for a human, it does not decide they are one person."""
    result = dedupe(
        [
            rec("m1", "Li", "Ming", slug="li-ming-1"),
            rec("m2", "Ming", "Li", slug="ming-li-2"),
        ]
    )
    assert len(result.clusters) == 2
    assert len(result.review_pairs) == 1
    assert result.review_pairs[0].score >= WRATIO_REVIEW_MIN


def test_differing_urns_never_merge_and_never_even_reach_review() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", urn="urn:li:member:1"),
            rec("m2", "Robert", "Smith", urn="urn:li:member:2"),
        ]
    )
    assert len(result.clusters) == 2
    assert result.review_pairs == ()


def test_matching_urns_merge_regardless_of_name() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", urn="urn:li:member:1"),
            rec("m2", "Bob", "Smith", urn="urn:li:member:1"),
        ]
    )
    assert len(result.clusters) == 1


# ---------------------------------------------------------------------------
# WRatio: within a block, review only, never an auto-accept
# ---------------------------------------------------------------------------


def test_wratio_never_merges_anything() -> None:
    """Two records scoring 100 on WRatio, in different identity keys."""
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", slug="robert-smith-a"),
            rec("m2", "Robert", "Smith", slug="robert-smith-b"),
        ]
    )
    assert len(result.clusters) == 2, "a score must never merge two identities"
    assert len(result.review_pairs) == 1
    assert result.review_pairs[0].score == 100.0


def test_scoring_never_runs_across_blocks() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", slug="a"),
            rec("m2", "Rupert", "Smythe", slug="b"),
            rec("m3", "Kenji", "Watanabe", slug="c"),
        ]
    )
    assert result.review_pairs == ()


def test_only_pairs_at_or_above_the_threshold_are_offered() -> None:
    result = dedupe(
        [
            rec("m1", "Robert", "Smith", company="x"),
            rec("m2", "Roberta", "Smith", company="y"),
            rec("m3", "Zzz", "Qqq", company="z"),
        ]
    )
    for pair in result.review_pairs:
        assert pair.score >= WRATIO_REVIEW_MIN
    assert all("m3" not in (pair.left, pair.right) for pair in result.review_pairs)


def test_review_pairs_are_deterministically_ordered() -> None:
    records = [rec(f"m{i}", "Robert", "Smith", slug=f"rs-{i}") for i in range(4)]
    first = dedupe(records).review_pairs
    second = dedupe(list(reversed(records))).review_pairs
    assert first == second
    assert [p.score for p in first] == sorted((p.score for p in first), reverse=True)


def test_review_pairs_explain_themselves() -> None:
    result = dedupe(
        [rec("m1", "Robert", "Smith", slug="a"), rec("m2", "Robert", "Smith", slug="b")]
    )
    reason = result.review_pairs[0].reason
    assert "WRatio" in reason
    assert "within block" in reason


# ---------------------------------------------------------------------------
# Adapting the real record types
# ---------------------------------------------------------------------------


def test_records_resolve_across_sources() -> None:
    provenance = Provenance(
        source="first_party_export",
        collected_at=datetime(2026, 8, 17, tzinfo=UTC),
        posture=CompliancePosture.FIRST_PARTY_EXPORT,
    )
    member = Member(
        member_id="m_1",
        first_name="Ada",
        last_name="Lovelace",
        linkedin_slug="ada-lovelace",
        company_raw="Jane Street",
        provenance=provenance,
    )
    target = Target(
        target_id="t_1",
        firm=Firm.JANE_STREET,
        first_name="Ada",
        last_name="Lovelace",
        linkedin_slug="ada-lovelace",
        provenance=provenance,
    )
    result = dedupe([from_member(member, company_key="jane_street"), from_target(target)])
    assert len(result.clusters) == 1
    cluster = result.clusters[0]
    assert cluster.record_ids == ("m_1", "t_1")
    assert cluster.firm is Firm.JANE_STREET
    assert cluster.sources == ("first_party_export",)


def test_the_same_human_at_two_firms_stays_two_records() -> None:
    """Citadel and Citadel Securities are distinct entities; merging them
    would corrupt per-firm results."""
    a = Target(target_id="t_a", firm=Firm.CITADEL, first_name="Sam", last_name="Reed")
    b = Target(target_id="t_b", firm=Firm.CITADEL_SECURITIES, first_name="Sam", last_name="Reed")
    assert len(dedupe([from_target(a), from_target(b)]).clusters) == 2


def test_resolver_maps_record_ids_to_person_ids() -> None:
    resolver = PersonResolver()
    resolver.add(rec("m1", "Ada", "Lovelace", slug="ada"))
    resolver.add(rec("m2", "Ada", "Lovelace", slug="ada"))
    assert resolver.person_id_for("m1") == resolver.person_id_for("m2")
    with pytest.raises(KeyError):
        resolver.person_id_for("nope")


def test_display_name_is_the_unmodified_original() -> None:
    """Blocking keys are barred from output (PRIV-09); this is what reports show."""
    record = rec("m1", "Nguyễn", "Văn An")
    assert record.display_name == "Nguyễn Văn An"
    assert record.display_name != record.name_key


def test_empty_input_is_safe() -> None:
    result = dedupe([])
    assert result.clusters == ()
    assert result.review_pairs == ()
    assert result.unadjudicated_count == 0
