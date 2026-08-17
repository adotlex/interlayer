"""The shipped target registry, and the loader's refusal to accept a broken one."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from interlayer.core.models import Firm
from interlayer.ingest import RegistryError
from interlayer.ingest.targets import (
    SUPPORTED_SCHEMA_VERSIONS,
    default_registry_path,
    load_registry,
    parse_registry,
)

REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "targets.yaml"


@pytest.fixture(scope="module")
def registry():
    return load_registry(REGISTRY_PATH)


@pytest.fixture(scope="module")
def registry_text() -> str:
    return REGISTRY_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# What R5 said must ship
# ---------------------------------------------------------------------------


def test_registry_ships_at_the_documented_path() -> None:
    assert REGISTRY_PATH.is_file()
    assert default_registry_path() == REGISTRY_PATH


def test_three_entities_matching_the_frozen_firm_enum(registry) -> None:
    assert registry.ids == ("jane_street", "citadel_securities", "citadel")
    assert {e.firm for e in registry} == set(Firm)
    assert registry.schema_version in SUPPORTED_SCHEMA_VERSIONS


def test_canonical_names_and_page_display_names(registry) -> None:
    jane = registry.by_id("jane_street")
    assert jane.canonical == "Jane Street"
    assert jane.linkedin.page_display_name == "Jane Street"
    # The hedge fund's page displays as "Citadel", not "Citadel LLC".
    assert registry.by_id("citadel").linkedin.page_display_name == "Citadel"
    assert registry.by_id("citadel_securities").canonical == "Citadel Securities"


def test_jane_street_live_slug_is_jane_street_global(registry) -> None:
    jane = registry.by_id("jane_street")
    assert jane.linkedin.slug == "jane-street-global"
    # The second, near-dormant page is real — enumerate it, but it is not the
    # live one, and enumerating it alone under-collects badly.
    assert jane.linkedin.secondary_slugs == ("jane-street-capital-llc",)
    assert jane.linkedin.all_real_slugs == ("jane-street-global", "jane-street-capital-llc")


def test_janestreetgroup_is_a_negative_not_a_target(registry) -> None:
    """~45 followers, corporate events. A slug-pattern crawler would eat it."""
    jane = registry.by_id("jane_street")
    assert jane.linkedin.is_confusable("janestreetgroup")
    assert jane.linkedin.is_confusable("jane-street-entertainment")
    assert "janestreetgroup" not in jane.linkedin.all_real_slugs
    note = next(c.note for c in jane.linkedin.confusable_slugs if c.slug == "janestreetgroup")
    assert "Not Jane Street" in note


def test_citadel_confusable_slugs(registry) -> None:
    citadel = registry.by_id("citadel")
    confusable = {c.slug for c in citadel.linkedin.confusable_slugs}
    assert {"citadel-credit-union", "the-citadel", "citadel-defense-company"} <= confusable
    assert citadel.linkedin.slug == "citadel-llc"
    assert citadel.linkedin.secondary_slugs == ("citadel-global-equities",)


def test_numeric_company_ids_ship_null_with_a_documented_procedure(
    registry, registry_text: str
) -> None:
    assert all(e.linkedin.company_id is None for e in registry)
    assert "urn:li:organization:" in registry_text
    assert "f_C=" in registry_text


def test_the_securities_discriminator_pair_is_present(registry) -> None:
    securities = registry.by_id("citadel_securities")
    hedge_fund = registry.by_id("citadel")
    assert securities.requires is not None
    assert securities.excludes is None
    assert hedge_fund.excludes is not None
    assert hedge_fund.requires is None
    # No leading \b: it must also fire on the concatenated "citadelsecurities".
    assert securities.requires.search("citadelsecurities")
    assert hedge_fund.excludes.search("citadelsecurities")


def test_anchor_required_demands_both_jane_and_street(registry) -> None:
    assert registry.by_id("jane_street").anchor_required == ("jane", "street")
    assert registry.by_id("citadel_securities").anchor_required == ("citadel", "securities")
    assert registry.by_id("citadel").anchor_required == ("citadel",)


def test_citadel_carries_a_higher_threshold_because_its_anchor_is_a_common_noun(
    registry,
) -> None:
    assert registry.by_id("citadel").threshold == 92
    assert registry.by_id("citadel").review_floor == 72
    assert registry.by_id("jane_street").threshold == 88
    assert registry.by_id("citadel_securities").threshold == 88


def test_js_is_review_only_and_never_an_alias(registry) -> None:
    jane = registry.by_id("jane_street")
    assert "js" in jane.review_only_aliases
    assert "jane" in jane.review_only_aliases
    assert "js" not in jane.aliases


def test_both_citadels_share_a_rollup_group(registry) -> None:
    assert registry.group_of(Firm.CITADEL) == "citadel"
    assert registry.group_of(Firm.CITADEL_SECURITIES) == "citadel"
    assert registry.group_of(Firm.JANE_STREET) == "jane_street"


def test_the_scorer_is_token_sort_ratio_never_token_set_ratio(registry) -> None:
    assert registry.match_policy.scorer == "rapidfuzz.fuzz.token_sort_ratio"
    assert registry.match_policy.ambiguity_margin == 5
    assert registry.match_policy.unexpected_token_forgiveness == 85
    assert registry.match_policy.fuzzy_anchor_min == 88


def test_normalisation_vocabulary_shipped_intact(registry) -> None:
    """The measured 100/100 depends on this vocabulary. Do not hand-edit it."""
    legal = registry.normalisation.legal_suffixes
    modifiers = registry.normalisation.modifier_tokens
    assert {"llc", "ltd", "b.v", "group", "the"} <= legal
    assert {"europe", "netherlands", "quant", "trading", "equities", "gcs"} <= modifiers
    assert len(legal) == 43
    assert len(modifiers) == 101


def test_alias_extension_vocabulary_is_scoped_to_the_matched_alias(registry) -> None:
    """The bug R5 found by measurement: a shared vocabulary made
    ``Citadel Capital`` confident, because ``capital`` is in ``Surveyor Capital``."""
    citadel = registry.by_id("citadel")
    vocab = citadel.alias_extension_vocab("citadel")
    assert "enterprise" in vocab
    assert "capital" not in vocab
    assert "capital" in citadel.alias_extension_vocab("surveyor capital")


# ---------------------------------------------------------------------------
# The loader must refuse a broken registry
# ---------------------------------------------------------------------------


def _mutate(text: str, mutate) -> str:
    data = yaml.safe_load(text)
    mutate(data)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        (
            "bad schema version",
            lambda d: d.__setitem__("schema_version", 99),
            "unsupported registry schema_version",
        ),
        (
            "unknown entity id",
            lambda d: d["entities"][0].__setitem__("id", "citadel_llc"),
            "not a member of core.models.Firm",
        ),
        (
            "duplicate entity id",
            lambda d: d["entities"][1].__setitem__("id", d["entities"][0]["id"]),
            "duplicate entity id",
        ),
        (
            "uncompilable anchor",
            lambda d: d["entities"][0].__setitem__("anchors", ["(unclosed"]),
            "not a valid regex",
        ),
        (
            "uncompilable negative",
            lambda d: d["entities"][0].__setitem__("negative_patterns", ["*bad"]),
            "not a valid regex",
        ),
        (
            "empty aliases",
            lambda d: d["entities"][0].__setitem__("aliases", []),
            "aliases must not be empty",
        ),
        (
            "uppercase alias",
            lambda d: d["entities"][0]["aliases"].append("Jane Street Capital"),
            "must be lowercase",
        ),
        (
            "empty anchor_required",
            lambda d: d["entities"][0].__setitem__("anchor_required", []),
            "anchor_required must not be empty",
        ),
        (
            "floor above threshold",
            lambda d: d["entities"][0].__setitem__("review_floor", 99),
            "review_floor",
        ),
        (
            "token_set_ratio smuggled back in",
            lambda d: d["match_policy"].__setitem__("scorer", "rapidfuzz.fuzz.token_set_ratio"),
            "must not be token_set_ratio",
        ),
        (
            "missing normalisation",
            lambda d: d.pop("normalisation"),
            "'normalisation' must be a mapping",
        ),
        (
            "no entities",
            lambda d: d.__setitem__("entities", []),
            "declares no entities",
        ),
        (
            "non-integer company_id",
            lambda d: d["entities"][0]["linkedin"].__setitem__("company_id", "1234"),
            "must be an integer or null",
        ),
        (
            "slug listed as both real and confusable",
            lambda d: d["entities"][0]["linkedin"]["confusable_slugs"].append(
                {"slug": "jane-street-global"}
            ),
            "as both a real and a confusable slug",
        ),
    ],
)
def test_malformed_registry_is_rejected(registry_text: str, label, mutate, message) -> None:
    with pytest.raises(RegistryError, match=message):
        parse_registry(_mutate(registry_text, mutate))


def test_not_yaml_at_all() -> None:
    with pytest.raises(RegistryError, match="not valid YAML"):
        parse_registry("entities: [\n  - id: 'unclosed\n")


def test_registry_that_is_not_a_mapping() -> None:
    with pytest.raises(RegistryError, match="must be a mapping"):
        parse_registry("- just\n- a\n- list\n")


def test_missing_file_is_a_registry_error(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="cannot read target registry"):
        load_registry(tmp_path / "nope.yaml")


def test_env_override_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = tmp_path / "custom.yaml"
    copy.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("INTERLAYER_TARGETS", str(copy))
    assert default_registry_path() == copy
    assert load_registry().ids == ("jane_street", "citadel_securities", "citadel")


def test_unknown_entity_lookup_raises(registry) -> None:
    with pytest.raises(RegistryError, match="unknown target entity id"):
        registry.by_id("two_sigma")
