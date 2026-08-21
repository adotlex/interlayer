"""String folding: ``norm`` vs ``norm_raw``, and the article that separates two firms.

The whole stage rests on one asymmetry. ``norm_raw("The Citadel") == "the citadel"``
while ``norm("The Citadel") == "citadel"``, which is also ``norm("Citadel")``. Fold
only once and a 40,000-alumni military college and a 3,150-person hedge fund become
the same string, after which every similarity scorer in existence returns 100 and no
threshold can separate them (research 04, section 1.2, rule C1).

These tests pin the two foldings apart, pin the legal-suffix list (``trust`` and
``partners`` are deliberately absent -- see ``test_normalize_match`` for the
regression they caused), and pin the degenerate inputs that must fold to nothing so
the stage skips them instead of inventing a shared employer out of ``"---"``.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from interlayer.normalize.normalize import (
    LEGAL_SUFFIXES,
    MULTI_TOKEN_SUFFIXES,
    NOISE_TOKENS,
    initials,
    norm,
    norm_raw,
    primary_segment,
    tokens,
)

# ---------------------------------------------------------------------------
# the verified table from research 04, section 3.1
# ---------------------------------------------------------------------------

# (input, expected norm_raw, expected norm) -- copied verbatim from the doc.
VERIFIED_FOLDINGS: tuple[tuple[str, str, str], ...] = (
    ("Jane Street Group, LLC", "jane street group llc", "jane street"),
    ("The Citadel", "the citadel", "citadel"),
    ("Citadel | Chicago", "citadel", "citadel"),
    ("D. E. Shaw & Co., L.P.", "d e shaw and co l p", "d e shaw"),
    ("ＪＡＮＥ　ＳＴＲＥＥＴ", "jane street", "jane street"),  # noqa: RUF001
    ("Jane Street​ Capital", "jane street capital", "jane street capital"),
    ("Société Générale", "societe generale", "societe generale"),
    ("Jane Street (JS)", "jane street", "jane street"),
)


@pytest.mark.parametrize(("text", "expected_raw", "expected_norm"), VERIFIED_FOLDINGS)
def test_verified_folding_table(text: str, expected_raw: str, expected_norm: str) -> None:
    """The research doc's own worked examples, asserted against the implementation.

    The doc records ``norm_raw("D. E. Shaw & Co., L.P.") == "d e shaw"``; that is a
    typo in the doc (``norm_raw`` by definition keeps legal suffixes, and the same
    doc's algorithm has ``strip_suffix=False`` for ``norm_raw``). The implemented
    value is asserted here.
    """
    assert norm_raw(text) == expected_raw
    assert norm(text) == expected_norm


# ---------------------------------------------------------------------------
# unicode folding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ＪＡＮＥ　ＳＴＲＥＥＴ", "jane street"),  # noqa: RUF001 -- NFKC: fullwidth + ideographic space
        ("ﬁrst capital", "first capital"),  # NFKC: ligature
        ("Ｃｉｔａｄｅｌ", "citadel"),  # noqa: RUF001
        ("Société Générale", "societe generale"),  # NFKD de-accent
        ("Zürich Insurance", "zurich insurance"),
        ("Jörg Müller GmbH", "jorg muller"),
        ("Straße", "strasse"),  # casefold, not lower(): ß -> ss
        ("İstanbul", "istanbul"),
        ("JANE STREET", "jane street"),
    ],
)
def test_unicode_folding(text: str, expected: str) -> None:
    assert norm(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Jane​Street Capital",  # zero-width space
        "Jane‌Street Capital",  # zero-width non-joiner
        "Jane‍Street Capital",  # zero-width joiner
        "Jane⁠Street Capital",  # word joiner
        "Jane﻿Street Capital",  # BOM / zero-width nbsp
    ],
)
def test_zero_width_characters_are_deleted_not_spaced(text: str) -> None:
    """Zero-width characters survive ``strip()`` and silently defeat exact matching.

    They are *deleted*, not turned into a space, so ``Jane<ZWSP>Street`` becomes one
    token. A LinkedIn paste carrying an invisible character must not become a
    different employer.
    """
    assert norm(text) == "janestreet capital"


def test_nbsp_becomes_a_real_space() -> None:
    assert norm("Jane Street Capital") == "jane street capital"  # noqa: RUF001


@pytest.mark.parametrize(
    "dash",
    ["‐", "‑", "‒", "–", "—", "―", "−", "-"],  # noqa: RUF001
)
def test_unicode_dashes_all_fold_to_whitespace(dash: str) -> None:
    """Every dash variant becomes a separator, so ``Jane-Street`` is ``jane street``."""
    assert norm(f"Jane{dash}Street") == "jane street"


def test_ampersand_becomes_the_word_and() -> None:
    assert norm_raw("Smith & Wesson") == "smith and wesson"
    assert norm_raw("D. E. Shaw & Co.") == "d e shaw and co"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Citadel, L.L.C.", "citadel"),
        ("Citadel (Europe)", "citadel europe"),
        ("Point72 Asset Management, L.P.", "point72 asset management"),
        ("Jane Street!!!", "jane street"),
        ("Jane   Street\t\tCapital\n", "jane street capital"),
    ],
)
def test_punctuation_and_whitespace(text: str, expected: str) -> None:
    assert norm(text) == expected


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Citadel | Chicago", "citadel"),
        ("Citadel • Chicago", "citadel"),
        ("Citadel · Chicago", "citadel"),
        ("Citadel › Chicago", "citadel"),  # noqa: RUF001
        ("Citadel » Chicago", "citadel"),
        ("Citadel Servicing Corp / Acra Lending", "citadel servicing"),
        ("Jane Street - Trading Desk", "jane street"),
        ("Jane Street – Trading Desk", "jane street"),  # noqa: RUF001
        ("Jane Street — Trading Desk", "jane street"),
    ],
)
def test_primary_segment_is_the_firm(text: str, expected: str) -> None:
    """LinkedIn employer strings are routinely ``Company | Location``."""
    assert norm(text) == expected


def test_hyphenated_name_is_not_segmented() -> None:
    """Only a *spaced* dash separates; ``Jane-Street`` is one name."""
    assert primary_segment("Jane-Street") == "Jane-Street"
    assert norm("Jane-Street") == "jane street"


def test_segment_false_keeps_the_whole_string() -> None:
    """``resolve`` uses the unsegmented form as a third probe for veto patterns."""
    assert norm_raw("Citadel Servicing Corp / Acra Lending", segment=False) == (
        "citadel servicing corp acra lending"
    )
    assert norm_raw("Citadel Servicing Corp / Acra Lending") == "citadel servicing corp"


def test_leading_separator_does_not_empty_the_string() -> None:
    assert norm("| Citadel") == "citadel"


# ---------------------------------------------------------------------------
# norm vs norm_raw: the article and the legal suffix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_raw", "expected_norm"),
    [
        ("The Citadel", "the citadel", "citadel"),
        ("Citadel", "citadel", "citadel"),
        ("The Citadel Group", "the citadel group", "citadel"),
        ("The D. E. Shaw Group", "the d e shaw group", "d e shaw"),
        ("Jane Street Group LLC", "jane street group llc", "jane street"),
        ("Citadel LLC", "citadel llc", "citadel"),
        ("Optiver Holding B.V.", "optiver holding b v", "optiver"),
    ],
)
def test_norm_raw_keeps_what_norm_drops(text: str, expected_raw: str, expected_norm: str) -> None:
    assert norm_raw(text) == expected_raw
    assert norm(text) == expected_norm


def test_the_article_is_the_only_thing_separating_college_from_fund() -> None:
    """The load-bearing asymmetry, stated as one assertion.

    Under ``norm`` the two collapse -- which is *why* negative aliases are indexed
    under ``norm_raw`` and nothing else.
    """
    assert norm("The Citadel") == norm("Citadel") == "citadel"
    assert norm_raw("The Citadel") != norm_raw("Citadel")
    assert norm_raw("The Citadel") == "the citadel"


def test_noise_tokens_are_exactly_the_definite_article() -> None:
    assert set(NOISE_TOKENS) == {"the"}


def test_noise_drop_never_empties_the_string() -> None:
    """``The`` alone must stay ``the``; an empty fold would be indistinguishable
    from a blank employer field and would be silently skipped."""
    assert norm("The") == "the"
    assert norm("the") == "the"


# ---------------------------------------------------------------------------
# legal-suffix stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jane Street Group, LLC", "jane street"),  # repeated: llc then group
        ("D. E. Shaw & Co., L.P.", "d e shaw"),  # l p (exploded), co, then "and"
        ("Citadel Securities L.P.", "citadel securities"),
        ("Jane Street Singapore Pte. Ltd.", "jane street singapore"),
        ("Jane Street Netherlands B.V.", "jane street netherlands"),
        ("Optiver Holding B.V.", "optiver"),
        ("Millennium Management LLC", "millennium management"),
        ("Virtu Financial, Inc.", "virtu financial"),
        ("Susquehanna International Group, LLP", "susquehanna international"),
    ],
)
def test_suffixes_strip_from_the_end_repeatedly(text: str, expected: str) -> None:
    assert norm(text) == expected


def test_suffix_stripping_is_end_only() -> None:
    """A global filter would eat the ``co`` inside a name that genuinely has one."""
    assert norm("Co Operative Bank") == "co operative bank"
    assert norm("Group Health Partners") == "group health partners"


def test_stripping_never_empties_the_token_list() -> None:
    """A firm literally named after a legal form keeps its only token."""
    assert norm("Group") == "group"
    assert norm("Limited") == "limited"


@pytest.mark.parametrize("term", ["trust", "partners"])
def test_trust_and_partners_are_not_legal_suffixes(term: str) -> None:
    """The regression guard, at the data level.

    While ``trust``/``partners`` were stripped, ``Citadel Trust`` folded to
    ``citadel`` and ``Jane Street Partners`` to ``jane street`` -- exact index hits,
    and an exact hit bypasses the containment guard, so both were accepted at 100.0
    as the target firms. See ``test_normalize_match`` for the end-to-end assertions.
    """
    assert term not in LEGAL_SUFFIXES
    assert term not in MULTI_TOKEN_SUFFIXES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Citadel Trust", "citadel trust"),
        ("Citadel Trust Company", "citadel trust"),
        ("Jane Street Partners", "jane street partners"),
        ("Millennium Partners", "millennium partners"),
        ("Millennium Capital Partners", "millennium capital partners"),
    ],
)
def test_trust_and_partners_survive_folding(text: str, expected: str) -> None:
    assert norm(text) == expected


@pytest.mark.parametrize(
    "term", ["llc", "ltd", "limited", "gmbh", "inc", "corp", "group", "holdings", "bv", "pte"]
)
def test_known_legal_forms_are_in_the_table(term: str) -> None:
    assert term in LEGAL_SUFFIXES


def test_multi_token_suffixes_are_recognised() -> None:
    assert "private limited" in MULTI_TOKEN_SUFFIXES
    assert norm("Acme Trading Private Limited") == "acme trading"


# ---------------------------------------------------------------------------
# acronym echo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jane Street (JS)", "jane street"),  # trailing echo
        ("HRT (Hudson River Trading)", "hudson river trading"),  # leading echo
        ("Susquehanna (SIG)", "susquehanna sig"),  # not an echo: two tokens only
    ],
)
def test_acronym_echo_is_dropped_only_when_it_is_an_echo(text: str, expected: str) -> None:
    """The one parenthetical worth deleting is a bare restatement of the initials.

    Parentheticals are otherwise kept, because ``Surveyor Capital (A Citadel
    Company)`` is the most informative string a Citadel employee can write.
    """
    assert norm(text) == expected


def test_informative_parenthetical_is_kept() -> None:
    assert norm("Surveyor Capital (A Citadel Company)") == "surveyor capital a citadel"
    assert "citadel" in norm("Surveyor Capital (A Citadel Company)").split()


@pytest.mark.parametrize(
    ("toks", "expected"),
    [
        (["jane", "street"], "js"),
        (["hudson", "river", "trading"], "hrt"),
        ([], ""),
        (["", "citadel"], "c"),
    ],
)
def test_initials(toks: list[str], expected: str) -> None:
    assert initials(toks) == expected


# ---------------------------------------------------------------------------
# degenerate inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "   \t\n ",
        " ",  # noqa: RUF001
        "---",
        "...",
        "!!!",
        "-",
        "--",
        "|",
        "()",
        "[]",
        "***",
        ",",
        ".",
        "/",
        "??",
        "~",
        "—",
    ],
)
def test_empty_whitespace_and_punctuation_only_fold_to_nothing(text: str) -> None:
    """These name no employer.

    Building an Org from one gives everybody who typed the same placeholder a
    shared-employer edge -- a fabricated relationship. ``stage.run`` skips a string
    whose fold is empty, so the emptiness assertion here is what makes that work.
    """
    assert norm(text) == ""
    assert norm_raw(text) == ""


# ---------------------------------------------------------------------------
# idempotence
# ---------------------------------------------------------------------------


IDEMPOTENCE_SAMPLES: tuple[str, ...] = (
    "Jane Street Group, LLC",
    "The Citadel",
    "Citadel | Chicago",
    "D. E. Shaw & Co., L.P.",
    "ＪＡＮＥ　ＳＴＲＥＥＴ",  # noqa: RUF001
    "Société Générale",
    "Jane Street (JS)",
    "Surveyor Capital (A Citadel Company)",
    "Citadel Securities (Europe) Limited",
    "Straße GmbH",
    "",
    "---",
    "The",
)


@pytest.mark.parametrize("text", IDEMPOTENCE_SAMPLES)
def test_norm_is_idempotent(text: str) -> None:
    once = norm(text)
    assert norm(once) == once


@pytest.mark.parametrize("text", IDEMPOTENCE_SAMPLES)
def test_norm_raw_is_idempotent(text: str) -> None:
    once = norm_raw(text)
    assert norm_raw(once) == once


@given(st.text(max_size=60))
@hyp_settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_norm_is_idempotent_property(text: str) -> None:
    """Folding an already-folded string must be a no-op for arbitrary input.

    The index is built from folded aliases and queried with folded strings; if the
    fold were not a fixed point, a string could match on the first pass and miss on
    the second depending on where it entered the pipeline.
    """
    once = norm(text)
    assert norm(once) == once


@given(st.text(max_size=60))
@hyp_settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_norm_raw_is_idempotent_property(text: str) -> None:
    once = norm_raw(text)
    assert norm_raw(once) == once


@given(st.text(max_size=60))
@hyp_settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_folded_output_has_no_leading_trailing_or_doubled_whitespace(text: str) -> None:
    folded = norm(text)
    assert folded == folded.strip()
    assert "  " not in folded
    assert tokens(folded) == folded.split()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_tokens_names_the_contract() -> None:
    assert tokens("jane street capital") == ["jane", "street", "capital"]
    assert tokens("") == []
