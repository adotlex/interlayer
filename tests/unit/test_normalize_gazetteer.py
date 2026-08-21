"""Structural integrity of ``data/gazetteer/firms.yaml`` and the indexes built from it.

Precision in this problem comes from the gazetteer, not from the scorer: with the
negatives and the guard in place every scorer measured reached 1.000 at every
threshold from 84 to 100; without them the best available managed 0.591. So the
data file and *which normalisation each index is keyed on* are the load-bearing
parts, and both are asserted here rather than assumed.

The single rule that must never regress: **negative aliases are indexed under
``norm_raw`` and nothing else.** ``norm("The Citadel Group") == "citadel"``, so
indexing negatives under ``norm`` as well would put the bare token ``citadel`` into
the veto table and reject every genuine Citadel employee. That bug was hit and fixed
once already; ``audit_index`` exists so it cannot come back silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from interlayer.errors import ResolutionError
from interlayer.models import AffiliationKind, OrgKind, TargetFirm
from interlayer.normalize.gazetteer import (
    FIRM_TIERS,
    SUPPORTED_SCHEMA_VERSION,
    Entity,
    Gazetteer,
    audit_index,
    load_gazetteer,
)
from interlayer.normalize.match import resolve
from interlayer.normalize.normalize import norm, norm_raw

REPO_ROOT = Path(__file__).resolve().parents[2]
GAZETTEER_PATH = REPO_ROOT / "data" / "gazetteer" / "firms.yaml"

KNOWN_ENTITY_TYPES = frozenset(
    {"fund", "market_maker", "bank", "broker", "university", "school", "college", "unrelated"}
)
KNOWN_TIERS = frozenset({"target", "adjacent", "negative"})

REQUIRED_KEYS = ("key", "canonical_name", "entity_type", "tier")


@pytest.fixture(scope="module")
def raw_yaml() -> dict[str, Any]:
    data = yaml.safe_load(GAZETTEER_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def raw_entities(raw_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    entities = raw_yaml["entities"]
    assert isinstance(entities, list)
    return entities


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    return load_gazetteer(GAZETTEER_PATH)


# ===========================================================================
# it parses, and it says what it is
# ===========================================================================


def test_gazetteer_file_exists_where_the_default_config_points() -> None:
    assert GAZETTEER_PATH.is_file()


def test_gazetteer_parses(gaz: Gazetteer) -> None:
    assert gaz.entities


def test_schema_version_is_the_one_this_build_understands(raw_yaml: dict[str, Any]) -> None:
    """Field semantics change with the version, so the loader refuses rather than guesses."""
    assert raw_yaml["schema_version"] == SUPPORTED_SCHEMA_VERSION


def test_tier_census_matches_the_research_deliverable(gaz: Gazetteer) -> None:
    """21 entities: 3 target, 10 adjacent, 8 negative (research 04, section 5)."""
    counts = {tier: sum(1 for e in gaz.entities.values() if e.tier == tier) for tier in KNOWN_TIERS}
    assert counts == {"target": 3, "adjacent": 10, "negative": 8}
    assert len(gaz.entities) == 21


# ===========================================================================
# every entity is well-formed
# ===========================================================================


def test_every_entity_has_the_required_keys(raw_entities: list[dict[str, Any]]) -> None:
    missing = {
        raw.get("key", "<no key>"): [k for k in REQUIRED_KEYS if k not in raw]
        for raw in raw_entities
        if any(k not in raw for k in REQUIRED_KEYS)
    }
    assert missing == {}


def test_every_entity_type_is_from_the_known_set(gaz: Gazetteer) -> None:
    unknown = {
        e.key: e.entity_type
        for e in gaz.entities.values()
        if e.entity_type not in KNOWN_ENTITY_TYPES
    }
    assert unknown == {}


def test_every_tier_is_from_the_known_set(gaz: Gazetteer) -> None:
    unknown = {e.key: e.tier for e in gaz.entities.values() if e.tier not in KNOWN_TIERS}
    assert unknown == {}


def test_no_two_entities_share_a_key(raw_entities: list[dict[str, Any]]) -> None:
    keys = [str(raw["key"]) for raw in raw_entities]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert duplicates == []
    assert len(keys) == len(set(keys))


def test_duplicate_keys_are_rejected_by_the_loader(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n"
        "- {key: a, canonical_name: A, entity_type: fund, tier: target}\n"
        "- {key: a, canonical_name: B, entity_type: fund, tier: target}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="duplicate gazetteer key"):
        load_gazetteer(path)


def test_every_firm_entity_has_at_least_one_alias(gaz: Gazetteer) -> None:
    empty = [e.key for e in gaz.entities.values() if e.is_firm and not e.aliases]
    assert empty == []


def test_canonical_names_are_unique(gaz: Gazetteer) -> None:
    """Two entities with the same canonical name would build one merged Org."""
    names = [e.canonical_name for e in gaz.entities.values()]
    assert len(names) == len(set(names))


# ===========================================================================
# target firms
# ===========================================================================


def test_every_target_entity_maps_to_a_real_target_firm_member(gaz: Gazetteer) -> None:
    members = {m.value for m in TargetFirm}
    for entity in gaz.entities.values():
        if entity.is_target:
            assert entity.key in members, f"{entity.key} is tier=target but not a TargetFirm"
            assert entity.target_firm is TargetFirm(entity.key)


def test_every_target_firm_member_has_a_gazetteer_entity(gaz: Gazetteer) -> None:
    """A member with no entity can never be matched -- a silently dead target."""
    for member in TargetFirm:
        entity = gaz.entities.get(member.value)
        assert entity is not None, f"TargetFirm.{member.name} has no gazetteer entity"
        assert entity.is_target


def test_non_target_entities_have_no_target_firm(gaz: Gazetteer) -> None:
    for entity in gaz.entities.values():
        if not entity.is_target:
            assert entity.target_firm is None


def test_the_two_citadels_declare_each_other_as_separate_companies(
    raw_entities: list[dict[str, Any]],
) -> None:
    by_key = {str(raw["key"]): raw for raw in raw_entities}
    assert by_key["citadel_llc"]["sibling_entity"] == "citadel_securities"
    assert by_key["citadel_securities"]["sibling_entity"] == "citadel_llc"


def test_firm_tiers_are_target_and_adjacent(gaz: Gazetteer) -> None:
    assert set(FIRM_TIERS) == {"target", "adjacent"}
    for entity in gaz.entities.values():
        assert entity.is_firm == (entity.tier in FIRM_TIERS)


# ===========================================================================
# audit_index: no negative alias may veto a real firm
# ===========================================================================


def test_audit_index_reports_no_collision(gaz: Gazetteer) -> None:
    """The property the loader enforces on every load. Currently ``conflicts: []``."""
    assert audit_index(gaz) == []


def test_no_veto_key_is_also_a_positive_firm_key(gaz: Gazetteer) -> None:
    """Stated independently of ``audit_index``, under *both* normalisations."""
    positive: set[str] = set()
    for entity in gaz.entities.values():
        if not entity.is_firm:
            continue
        for alias in (*entity.aliases, entity.canonical_name):
            positive |= {norm(alias), norm_raw(alias)}
    positive.discard("")
    assert set(gaz.neg_exact) & positive == set()


def test_indexing_negatives_under_norm_would_veto_every_citadel_employee() -> None:
    """The exact bug the rule exists to prevent, spelled out.

    ``The Citadel Group`` is an Australian government IT firm and a negative alias of
    ``citadel_llc``. Under ``norm`` it folds to the bare token ``citadel`` -- which
    is the fund's own primary key. One line of index-building separates vetoing an
    IT firm from vetoing all 3,150 Citadel employees.
    """
    assert norm("The Citadel Group") == "citadel"
    assert norm("Citadel Group") == "citadel"
    assert norm_raw("The Citadel Group") == "the citadel group"


def test_the_veto_table_does_not_contain_the_bare_target_tokens(gaz: Gazetteer) -> None:
    for token in ("citadel", "jane street", "citadel securities"):
        assert token not in gaz.neg_exact, f"{token!r} in the veto table vetoes the firm itself"


def test_negatives_are_indexed_under_norm_raw(gaz: Gazetteer) -> None:
    """Every declared negative alias is actually reachable, and keyed on the raw fold."""
    for entity in gaz.entities.values():
        for alias in entity.negative_aliases:
            raw = norm_raw(alias)
            if not raw:
                continue
            assert raw in gaz.neg_exact, f"{alias!r} declared but not indexed"
            assert entity.key in gaz.neg_exact[raw]


def test_the_citadel_is_a_veto_and_citadel_is_not(gaz: Gazetteer) -> None:
    """The article, at index level."""
    assert "the citadel" in gaz.neg_exact
    assert "citadel_llc" in gaz.neg_exact["the citadel"]
    assert "citadel" not in gaz.neg_exact
    assert gaz.exact["citadel"][0] == "citadel_llc"


def test_education_raw_holds_the_article_but_not_the_bare_token(gaz: Gazetteer) -> None:
    """This is what makes ``The Citadel`` ambiguous in a job field and ``Citadel`` not.

    The ambiguity check keys on ``education_raw``. If a bare ``citadel`` were in
    there, every real Citadel employee would be routed to review instead of matched.
    """
    assert "the citadel" in gaz.education_raw
    assert gaz.education_raw["the citadel"] == ("the_citadel_college",)
    assert "citadel" not in gaz.education_raw


def test_loader_raises_when_a_negative_alias_collides(tmp_path: Path) -> None:
    path = tmp_path / "collide.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n"
        "- {key: a, canonical_name: Citadel, entity_type: fund, tier: target, aliases: [Citadel]}\n"
        "- {key: b, canonical_name: Other, entity_type: unrelated, tier: negative,"
        " negative_aliases: [Citadel]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="would veto real employees"):
        load_gazetteer(path)


# ===========================================================================
# category buckets veto but never name an org
# ===========================================================================


BUCKET_KEYS = ("jane_street_generic", "citadel_security_generic")


@pytest.mark.parametrize("key", BUCKET_KEYS)
def test_bucket_entities_are_not_specific_orgs(key: str, gaz: Gazetteer) -> None:
    """A bucket stands for thousands of unrelated businesses; no page exists for it.

    Building an Org out of one puts a barista and a dentist in the same company and
    hands the graph stage a fabricated co-employment edge between them.
    """
    entity = gaz.entities[key]
    assert entity.linkedin_slug is None
    assert entity.is_specific_org is False
    assert entity.is_firm is False
    assert entity.tier == "negative"


def test_every_slugless_entity_is_a_bucket(gaz: Gazetteer) -> None:
    """The invariant stated over the whole file, not just the two known buckets."""
    slugless = {e.key for e in gaz.entities.values() if e.linkedin_slug is None}
    assert slugless == set(BUCKET_KEYS)
    for key in slugless:
        assert gaz.entities[key].is_specific_org is False


def test_every_named_negative_entity_may_name_an_org(gaz: Gazetteer) -> None:
    """``Citadel Broadcasting`` is a real company, so every spelling of it may
    collapse onto one canonical Org. That is a rejection *and* an identification."""
    named = {e.key for e in gaz.entities.values() if e.tier == "negative" and e.is_specific_org}
    assert named == {
        "the_citadel_college",
        "citadel_broadcasting",
        "citadel_credit_union",
        "citadel_defense",
        "citadel_servicing",
        "citadel_group_au",
    }


@pytest.mark.parametrize(
    "text", ["Jane Street Coffee", "Jane Street Dental", "Jane Street Entertainment"]
)
def test_a_bucket_veto_never_returns_the_bucket_as_the_entity(text: str, gaz: Gazetteer) -> None:
    """The string is rejected, but nothing is named -- so the stage falls back to a
    surface-form Org and the coffee shop stays separate from the dental practice."""
    result = resolve(text, field=AffiliationKind.EMPLOYMENT, gaz=gaz)
    assert result.firm_key is None
    assert result.entity_key not in BUCKET_KEYS


def test_a_named_negative_does_get_returned(gaz: Gazetteer) -> None:
    result = resolve("Citadel Broadcasting Corporation", field=AffiliationKind.EMPLOYMENT, gaz=gaz)
    assert result.entity_key == "citadel_broadcasting"


# ===========================================================================
# field scope
# ===========================================================================


def test_university_entities_are_education_only(gaz: Gazetteer) -> None:
    college = gaz.entities["the_citadel_college"]
    assert college.entity_type == "university"
    assert college.fields == frozenset({AffiliationKind.EDUCATION})
    assert college.org_kind is OrgKind.SCHOOL


@pytest.mark.parametrize("key", ["jane_street", "citadel_llc", "citadel_securities"])
def test_target_firms_are_position_only(key: str, gaz: Gazetteer) -> None:
    entity = gaz.entities[key]
    assert entity.fields == frozenset({AffiliationKind.EMPLOYMENT})
    assert entity.org_kind is OrgKind.COMPANY


def test_scope_selects_by_field_kind(gaz: Gazetteer) -> None:
    employment = gaz.scope(AffiliationKind.EMPLOYMENT)
    education = gaz.scope(AffiliationKind.EDUCATION)
    assert "citadel_llc" in employment
    assert "citadel_llc" not in education
    assert "the_citadel_college" in education
    assert "the_citadel_college" not in employment


def test_unrelated_entities_are_matchable_from_both_fields(gaz: Gazetteer) -> None:
    """A radio network appearing in a school field is still not the fund."""
    both = frozenset({AffiliationKind.EMPLOYMENT, AffiliationKind.EDUCATION})
    assert gaz.entities["citadel_broadcasting"].fields == both


def test_declared_field_scope_wins_over_the_entity_type(tmp_path: Path) -> None:
    path = tmp_path / "scope.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n"
        "- {key: a, canonical_name: A, entity_type: fund, tier: target,"
        " field_scope: [education], aliases: [Aaa]}\n",
        encoding="utf-8",
    )
    loaded = load_gazetteer(path)
    assert loaded.entities["a"].fields == frozenset({AffiliationKind.EDUCATION})


# ===========================================================================
# indexes
# ===========================================================================


def test_every_positive_alias_is_in_the_exact_index(gaz: Gazetteer) -> None:
    for entity in gaz.entities.values():
        for alias in (*entity.aliases, entity.canonical_name):
            quarantined = {
                q for weak in entity.weak_aliases for q in (norm(weak), norm_raw(weak)) if q
            }
            for form in (norm(alias), norm_raw(alias)):
                if not form:
                    continue
                if form in quarantined:
                    # An ordinary alias whose folded form collapses onto the
                    # entity's own quarantined initialism ("IMC B.V." -> "imc")
                    # must NOT be strongly indexed: after folding it is
                    # indistinguishable from the bare initialism, so indexing it
                    # reopens the quarantine from the inside. The alias stays
                    # reachable under its raw form ("imc b v").
                    assert entity.key in gaz.weak_exact.get(form, ()), (
                        f"{alias!r} folds onto quarantined {form!r} but is not in the weak index"
                    )
                    continue
                assert entity.key in gaz.exact.get(form, ()), f"{alias!r} not indexed"


def test_every_positive_alias_is_in_the_fuzzy_index(gaz: Gazetteer) -> None:
    pairs = set(gaz.fuzzy)
    for entity in gaz.entities.values():
        quarantined = {q for weak in entity.weak_aliases for q in (norm(weak),) if q}
        for alias in (*entity.aliases, entity.canonical_name):
            folded = norm(alias)
            if not folded or folded in quarantined:
                # See test_every_positive_alias_is_in_the_exact_index: a fuzzy
                # entry would let the bare initialism match without corroboration.
                continue
            assert (folded, entity.key) in pairs


def test_weak_aliases_are_in_the_weak_index_only(gaz: Gazetteer) -> None:
    """``JS`` must not be reachable without the quarantine being opened explicitly."""
    assert "js" in gaz.weak_exact
    assert gaz.weak_exact["js"] == ("jane_street",)
    assert "js" not in gaz.exact


def test_exact_index_orders_candidates_by_tier_then_priority(gaz: Gazetteer) -> None:
    """Target beats adjacent beats negative, deterministically."""
    for keys in gaz.exact.values():
        ranks = [gaz.preference(k) for k in keys]
        assert ranks == sorted(ranks)


def test_fuzzy_index_is_sorted(gaz: Gazetteer) -> None:
    """The sweep uses a strict ``>``, so ties resolve by index order.

    ``PYTHONHASHSEED`` produced four distinct set-iteration orders during Wave 1;
    an unsorted index would make the winner of a tie depend on the interpreter.
    """
    ordered = sorted(gaz.fuzzy, key=lambda pair: (gaz.preference(pair[1]), pair[0]))
    assert list(gaz.fuzzy) == ordered


def test_the_two_citadels_share_no_index_entry(gaz: Gazetteer) -> None:
    """No folded alias may point at both the fund and the market maker."""
    for key, owners in gaz.exact.items():
        assert not {"citadel_llc", "citadel_securities"} <= set(owners), (
            f"{key!r} resolves to both Citadels"
        )


def test_no_alias_is_also_a_negative_alias_of_its_own_entity(gaz: Gazetteer) -> None:
    for entity in gaz.entities.values():
        positives = {norm_raw(a) for a in (*entity.aliases, entity.canonical_name)} - {""}
        negatives = {norm_raw(a) for a in entity.negative_aliases} - {""}
        assert positives & negatives == set(), f"{entity.key} contradicts itself"


# ===========================================================================
# slugs and provenance
# ===========================================================================


def test_unverified_slugs_never_render_as_a_profile_url(gaz: Gazetteer) -> None:
    """linkedin.com was egress-blocked from the research sandbox.

    Rendering an unverified slug as *the firm's page* hands the operator a link that
    may point at the wrong company.
    """
    for entity in gaz.entities.values():
        if entity.linkedin_slug and not entity.linkedin_slug_verified:
            assert entity.linkedin_url is None


def test_the_one_verified_slug_renders(gaz: Gazetteer) -> None:
    sig = gaz.entities["sig"]
    assert sig.linkedin_slug_verified is True
    assert sig.linkedin_url == "https://www.linkedin.com/company/susquehanna-international-group"


def test_the_college_is_a_school_url_not_a_company_url(gaz: Gazetteer) -> None:
    college = gaz.entities["the_citadel_college"]
    assert college.linkedin_url_kind == "school"


def test_domains_and_headcounts_are_carried_through(gaz: Gazetteer) -> None:
    assert gaz.entities["jane_street"].domain == "janestreet.com"
    assert gaz.entities["citadel_llc"].domain == "citadel.com"
    assert gaz.entities["citadel_securities"].domain == "citadelsecurities.com"
    # The order-of-magnitude fact the whole disambiguation rests on.
    college = gaz.entities["the_citadel_college"].approx_headcount
    fund = gaz.entities["citadel_llc"].approx_headcount
    assert college is not None and fund is not None
    assert college > 10 * fund


# ===========================================================================
# loader failure modes -- it must refuse, never half-build
# ===========================================================================


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ResolutionError, match="gazetteer not found"):
        load_gazetteer(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("schema_version: 1\nentities: [\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="not valid YAML"):
        load_gazetteer(path)


def test_non_mapping_raises(tmp_path: Path) -> None:
    path = tmp_path / "list.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="must be a mapping"):
        load_gazetteer(path)


def test_unsupported_schema_version_raises(tmp_path: Path) -> None:
    path = tmp_path / "v2.yaml"
    path.write_text(
        "schema_version: 2\nentities:\n"
        "- {key: a, canonical_name: A, entity_type: fund, tier: target}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="schema_version"):
        load_gazetteer(path)


def test_empty_entity_list_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("schema_version: 1\nentities: []\n", encoding="utf-8")
    with pytest.raises(ResolutionError, match="no entities"):
        load_gazetteer(path)


def test_entity_missing_a_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "nokey.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n- {canonical_name: A, entity_type: fund, tier: target}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="missing"):
        load_gazetteer(path)


def test_invalid_negative_pattern_raises(tmp_path: Path) -> None:
    path = tmp_path / "badre.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n"
        "- {key: a, canonical_name: A, entity_type: fund, tier: target,"
        " negative_patterns: ['(']}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="invalid negative_pattern"):
        load_gazetteer(path)


def test_a_declared_legal_suffix_the_normaliser_cannot_apply_raises(tmp_path: Path) -> None:
    """A curator adding a suffix to the YAML must see it take effect, not silently
    do nothing -- the suffix list itself lives in code."""
    path = tmp_path / "suffix.yaml"
    path.write_text(
        "schema_version: 1\nentities:\n"
        "- {key: a, canonical_name: A, entity_type: fund, tier: target}\n"
        "normalization: {legal_suffixes: [wibble]}\n",
        encoding="utf-8",
    )
    with pytest.raises(ResolutionError, match="normaliser does not implement"):
        load_gazetteer(path)


def test_the_real_gazetteers_declared_suffixes_are_all_implemented(gaz: Gazetteer) -> None:
    """Implied by the file loading at all; asserted so the reason is legible."""
    assert gaz.entities


def test_trust_and_partners_are_not_declared_as_legal_suffixes(
    raw_yaml: dict[str, Any],
) -> None:
    """The data half of the regression fix, in the file the curator edits."""
    declared = {str(s).casefold() for s in raw_yaml["normalization"]["legal_suffixes"]}
    assert "trust" not in declared
    assert "partners" not in declared


def test_the_gazetteer_records_the_banned_scorers(raw_yaml: dict[str, Any]) -> None:
    """The list is documentation, but it is documentation someone will read."""
    banned = set(raw_yaml["thresholds"]["banned_scorers"])
    assert {"partial_ratio", "partial_token_sort_ratio", "partial_token_set_ratio"} <= banned
    assert raw_yaml["thresholds"]["require_containment_guard"] is True
    assert raw_yaml["thresholds"]["accept"] == 90
    assert raw_yaml["thresholds"]["review_low"] == 84


def test_entity_is_frozen(gaz: Gazetteer) -> None:
    """Entities are shared across every resolution; a mutable one is a cross-call bug."""
    entity: Entity = gaz.entities["citadel_llc"]
    with pytest.raises((AttributeError, TypeError)):
        entity.key = "nope"  # type: ignore[misc]
