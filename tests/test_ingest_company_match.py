"""The deterministic company ladder, and R5's 100-case regression fixture.

The fixture is the contract. A registry edit that improves recall on one string
and silently breaks another is caught here and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from rapidfuzz import fuzz

from interlayer.core.models import CompanyMatch, Firm, MatchStatus
from interlayer.ingest import NeedsReviewError
from interlayer.ingest.company_match import (
    RULE_ANCHORED_PREFIX,
    RULE_EMPTY,
    RULE_EXACT_ALIAS,
    RULE_FUZZY,
    RULE_NEGATIVE,
    RULE_REVIEW_ONLY,
    CompanyMatcher,
    is_admissible,
    normalise_company,
    partition,
    require_admissible,
)
from interlayer.ingest.targets import load_registry

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "company_strings.yaml"


@pytest.fixture(scope="module")
def matcher() -> CompanyMatcher:
    return CompanyMatcher(load_registry())


@pytest.fixture(scope="module")
def cases() -> list[dict[str, object]]:
    loaded = yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]
    assert isinstance(loaded, list)
    return loaded


# ---------------------------------------------------------------------------
# The 100 cases
# ---------------------------------------------------------------------------


def test_fixture_has_exactly_one_hundred_cases(cases) -> None:
    assert len(cases) == 100
    buckets = {"confident": 0, "needs_review": 0, "no_match": 0}
    for case in cases:
        buckets[str(case["expect"])] += 1
    assert buckets == {"confident": 46, "needs_review": 13, "no_match": 41}


def test_r5_company_fixture_passes_one_hundred_of_one_hundred(matcher, cases) -> None:
    """The acceptance criterion: 100/100, and the count is printed."""
    failures: list[str] = []
    passed = 0
    for case in cases:
        raw = str(case["raw"])
        expect = str(case["expect"])
        entity = case["entity"]
        result = matcher.classify(raw)
        firm = result.firm.value if result.firm else None

        ok = result.status.value == expect
        if ok and expect == "confident":
            ok = firm == entity
        if ok and expect == "needs_review" and entity is not None:
            ok = firm == entity
        if ok and expect == "no_match":
            ok = firm is None

        if ok:
            passed += 1
        else:
            failures.append(
                f"{raw!r}: expected {expect}/{entity}, got "
                f"{result.status.value}/{firm} via {result.rule} ({result.score})"
            )

    print(f"\nR5 company-string fixture: {passed}/{len(cases)} passed")
    assert not failures, "\n".join(failures)
    assert passed == 100


def test_every_verdict_names_the_rung_that_fired(matcher, cases) -> None:
    """`rule` is the audit trail: an unexplained assignment is a fabrication."""
    for case in cases:
        result = matcher.classify(str(case["raw"]))
        assert result.rule, f"{case['raw']!r} produced no rule"
        rung = result.rule.split("/")[0].split(":")[0]
        assert rung in {
            RULE_EMPTY,
            RULE_NEGATIVE,
            "no_anchor_or_negated",
            "below_floor",
            RULE_REVIEW_ONLY,
            RULE_EXACT_ALIAS,
            RULE_ANCHORED_PREFIX,
            RULE_FUZZY,
            "ambiguous_entity",
        }, result.rule


@pytest.mark.parametrize(
    ("raw", "rule"),
    [
        ("Jane Street Capital", RULE_EXACT_ALIAS),
        ("JaneStreet", RULE_EXACT_ALIAS),
        ("Jane Street Captial", RULE_ANCHORED_PREFIX),
        ("Citadel Securites", RULE_FUZZY),
        ("JS", RULE_REVIEW_ONLY),
        ("The Citadel", RULE_NEGATIVE),
        ("", RULE_EMPTY),
    ],
)
def test_specific_rungs(matcher, raw: str, rule: str) -> None:
    assert matcher.classify(raw).rule.split("/")[0].split(":")[0] == rule


def test_review_reasons_are_recorded_with_the_offending_tokens(matcher) -> None:
    result = matcher.classify("Citadel Technology")
    assert result.status is MatchStatus.NEEDS_REVIEW
    assert "unexpected_tokens" in result.rule
    assert "technology" in result.rule
    assert matcher.explain("Citadel Technology").unexpected_tokens == ("technology",)


# ---------------------------------------------------------------------------
# Why there is no threshold-based general matcher (R5's measurement)
# ---------------------------------------------------------------------------


def test_token_set_ratio_cannot_separate_the_true_and_false_positives() -> None:
    """The measurement that killed fuzzy matching as a primary strategy."""
    assert fuzz.token_set_ratio("Jane Street Capital LLC", "Jane Street") == 100
    assert fuzz.token_set_ratio("Jane Street Entertainment", "Jane Street") == 100
    assert fuzz.partial_ratio("The Citadel", "Citadel") == 100


def test_the_ladder_separates_exactly_what_fuzzy_could_not(matcher) -> None:
    good = matcher.classify("Jane Street Capital LLC")
    bad = matcher.classify("Jane Street Entertainment")
    fort = matcher.classify("The Citadel")
    assert (good.status, good.firm) == (MatchStatus.CONFIDENT, Firm.JANE_STREET)
    assert bad.status is MatchStatus.NO_MATCH
    assert fort.status is MatchStatus.NO_MATCH


def test_the_typo_that_used_to_reassign_the_firm(matcher) -> None:
    """``Citadel Securites`` scored 100 against the hedge fund under
    ``token_set_ratio`` — a market maker routed into the wrong bucket."""
    result = matcher.classify("Citadel Securites")
    assert result.firm is Firm.CITADEL_SECURITIES
    assert result.status is MatchStatus.CONFIDENT


def test_fuzzy_is_a_typo_backstop_only(matcher, cases) -> None:
    """Overwhelmingly, true positives resolve at the exact/prefix rungs."""
    fuzzy_confident = [
        case["raw"]
        for case in cases
        if case["expect"] == "confident"
        and matcher.classify(str(case["raw"])).rule.startswith(RULE_FUZZY)
    ]
    assert len(fuzzy_confident) <= 2


def test_the_two_citadels_are_never_conflated(matcher) -> None:
    for raw in ("Citadel Securities", "CitadelSecurities", "Citadel-Securities"):
        assert matcher.classify(raw).firm is Firm.CITADEL_SECURITIES
    for raw in ("Citadel", "Citadel LLC", "Citadel Global Equities", "Surveyor Capital"):
        assert matcher.classify(raw).firm is Firm.CITADEL


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The ambiguous characters below are the point of these cases.
        ("Ｊａｎｅ　Ｓｔｒｅｅｔ", "jane street"),  # noqa: RUF001
        ("Jané Stréet", "jane street"),
        ("Jane-Street", "jane street"),
        ("Jane Street Capital – London", "jane street capital london"),  # noqa: RUF001
        ("Citadel Roofing & Solar", "citadel roofing and solar"),
        ("Jane Street Netherlands B.V.", "jane street netherlands b.v."),
        ("jane  street", "jane street"),
        ("Citadel Securities | Equities", "citadel securities equities"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalise_company(raw: str | None, expected: str) -> None:
    assert normalise_company(raw) == expected


def test_normalisation_does_not_transliterate_non_latin_scripts() -> None:
    """We fold combining marks; we never invent a romanisation."""
    assert normalise_company("李明 Capital") == "李明 capital"
    assert "li ming" not in normalise_company("李明 Capital")


# ---------------------------------------------------------------------------
# PRIV-20 — needs_review is a barrier, not a hint
# ---------------------------------------------------------------------------


def test_needs_review_is_not_admissible(matcher) -> None:
    result = matcher.classify("Citadel Technology")
    assert result.status is MatchStatus.NEEDS_REVIEW
    assert result.firm is not None  # a suggestion for the reviewer...
    assert is_admissible(result) is False  # ...that still cannot reach the graph
    with pytest.raises(NeedsReviewError, match="PRIV-20"):
        require_admissible(result)


def test_no_match_is_not_admissible(matcher) -> None:
    result = matcher.classify("Goldman Sachs")
    assert is_admissible(result) is False
    with pytest.raises(ValueError, match="no target firm"):
        require_admissible(result)


def test_confident_is_the_only_thing_that_gets_through(matcher) -> None:
    result = matcher.classify("Jane Street")
    assert is_admissible(result) is True
    assert require_admissible(result) is Firm.JANE_STREET


def test_every_fixture_review_item_is_barred_from_the_graph(matcher, cases) -> None:
    review = [str(c["raw"]) for c in cases if c["expect"] == "needs_review"]
    assert review
    for raw in review:
        with pytest.raises(NeedsReviewError):
            require_admissible(matcher.classify(raw))


def test_partition_counts_the_unadjudicated(matcher, cases) -> None:
    split = partition(matcher.classify(str(c["raw"])) for c in cases)
    assert len(split.confident) == 46
    assert split.unadjudicated_count == 13
    assert len(split.no_match) == 41
    assert all(is_admissible(m) for m in split.confident)


# ---------------------------------------------------------------------------
# Memoisation and the decision cache
# ---------------------------------------------------------------------------


def test_memoisation_is_transparent(matcher) -> None:
    first = matcher.classify("Jane Street Capital")
    second = matcher.classify("Jane Street Capital")
    assert first == second


def test_memoisation_can_be_disabled_without_changing_answers(cases) -> None:
    registry = load_registry()
    cached = CompanyMatcher(registry, memoise=True)
    uncached = CompanyMatcher(registry, memoise=False)
    for case in cases:
        raw = str(case["raw"])
        assert cached.classify(raw) == uncached.classify(raw)


def test_a_recorded_decision_short_circuits_the_ladder() -> None:
    matcher = CompanyMatcher(load_registry())
    assert matcher.classify("Citadel Technology").status is MatchStatus.NEEDS_REVIEW

    matcher.set_decision(normalise_company("Citadel Technology"), Firm.CITADEL)
    decided = matcher.classify("Citadel Technology")
    assert decided.status is MatchStatus.CONFIDENT
    assert decided.firm is Firm.CITADEL
    assert decided.rule == "cached_decision"

    matcher.set_decision(normalise_company("Citadel Analytics"), None)
    rejected = matcher.classify("Citadel Analytics")
    assert rejected.status is MatchStatus.NO_MATCH
    assert rejected.firm is None


def test_a_decision_covers_every_spelling_that_normalises_to_it() -> None:
    matcher = CompanyMatcher(load_registry())
    matcher.set_decision(normalise_company("Citadel Technology"), Firm.CITADEL)
    for spelling in ("CITADEL TECHNOLOGY", "Citadel  Technology", "Citadel-Technology"):
        assert matcher.classify(spelling).firm is Firm.CITADEL


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "\t\n", "!!!", "...", "'''", "12345", "🙂", "李明"]
)
def test_junk_input_never_raises(matcher, raw: str | None) -> None:
    result = matcher.classify(raw)
    assert isinstance(result, CompanyMatch)
    assert result.status in set(MatchStatus)


def test_classification_is_deterministic_across_matchers(cases) -> None:
    a = CompanyMatcher(load_registry())
    b = CompanyMatcher(load_registry())
    assert [a.classify(str(c["raw"])) for c in cases] == [b.classify(str(c["raw"])) for c in cases]


def test_explanations_record_the_eliminations(matcher) -> None:
    explanation = matcher.explain("Citadel Securities")
    assert explanation.entity_ids == ("citadel_securities",)
    eliminated = dict(explanation.eliminated)
    assert eliminated["citadel"] == "hit_excludes"
    assert eliminated["jane_street"] == "no_anchor_or_negated"
