"""Firm resolution: the Citadel disambiguation, the guard, and the research case table.

This is where a false positive puts a stranger in the operator's results and a false
negative hides a real contact. Research 04 measured precision 1.000 with a wrong-firm
rate of 0.000 over the 94 curated cases of sections 8.1-8.7; those cases are
parametrised here verbatim, and the aggregate is asserted as a single measurement.

Three things are load-bearing and each gets its own block below:

* **Citadel LLC and Citadel Securities never collapse into one another.** Separate
  companies, separate pages, separate people. A Citadel Securities C++ engineer is
  not a warm introduction to a Citadel LLC portfolio manager.
* **The guard, not the score, is what supplies precision.** ``partial_ratio``,
  ``token_set_ratio`` and ``token_ratio`` all return 100 for ``Citadel`` against
  ``Citadel Broadcasting``; that trap is asserted directly against ``rapidfuzz`` so
  it is documented in the suite, and then the pipeline is asserted to reject the
  string anyway.
* **``trust`` and ``partners`` are not legal suffixes.** They were, briefly, and
  ``Citadel Trust`` / ``Jane Street Partners`` folded onto a target's exact key.
  Exact hits bypass the containment guard, so both were accepted at 100.0.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rapidfuzz import fuzz

from interlayer.models import AffiliationKind, MatchVerdict, TargetFirm
from interlayer.normalize.gazetteer import Gazetteer, load_gazetteer
from interlayer.normalize.match import (
    ALLOW_TOKENS,
    GUARD_TOKEN_THRESHOLD,
    containment_guard,
    resolve,
)
from interlayer.normalize.normalize import norm, norm_raw

REPO_ROOT = Path(__file__).resolve().parents[2]
GAZETTEER_PATH = REPO_ROOT / "data" / "gazetteer" / "firms.yaml"

POSITION = AffiliationKind.EMPLOYMENT
EDUCATION = AffiliationKind.EDUCATION

ACCEPT = 90.0
REVIEW = 84.0


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    """The real gazetteer. Module-scoped: loading it runs the full index audit."""
    return load_gazetteer(GAZETTEER_PATH)


# ===========================================================================
# The research case table -- docs/research/04-entity-resolution.md sections 8.1-8.7
# ===========================================================================

# (case number, input string, expected entity key or None). Position field
# throughout, per the doc's "Field context is `position` unless stated".
CASE_TABLE: tuple[tuple[int, str, str | None], ...] = (
    # 8.1 Jane Street -- true positives
    (1, "Jane Street", "jane_street"),
    (2, "Jane Street Capital", "jane_street"),
    (3, "Jane Street Capital, LLC", "jane_street"),
    (4, "Jane Street Group", "jane_street"),
    (5, "Jane Street Group, LLC", "jane_street"),
    (6, "JANE STREET GROUP LLC", "jane_street"),
    (7, "jane street", "jane_street"),
    (8, "Jane  Street   Capital", "jane_street"),
    (9, "Jane Street Europe", "jane_street"),
    (10, "Jane Street Financial Limited", "jane_street"),
    (11, "Jane Street Financial Ltd.", "jane_street"),
    (12, "Jane Street Netherlands B.V.", "jane_street"),
    (13, "Jane Street Asia Trading Limited", "jane_street"),
    (14, "Jane Street Hong Kong Limited", "jane_street"),
    (15, "Jane Street Singapore Pte. Ltd.", "jane_street"),
    (16, "Jane Street Execution Services, LLC", "jane_street"),
    (17, "Jane St.", "jane_street"),
    (18, "JaneStreet", "jane_street"),
    (19, "Jane-Street", "jane_street"),
    (20, "Jane Street (JS)", "jane_street"),
    (21, "Jane Street​ Capital", "jane_street"),
    (22, "ＪＡＮＥ　ＳＴＲＥＥＴ", "jane_street"),  # noqa: RUF001
    # 8.2 Jane Street -- near-miss true negatives
    (23, "Jane Street Coffee", None),
    (24, "Jane Street Entertainment", None),
    (25, "Jane Street Dental Practice", None),
    (26, "123 Jane Street Bakery", None),
    (27, "Mary Jane Street Foods", None),
    (28, "Jane Street Studios Toronto", None),
    (29, "Janestreet Realty of Toronto", None),
    # 8.3 Citadel LLC (the fund) -- true positives
    (30, "Citadel", "citadel_llc"),
    (31, "Citadel LLC", "citadel_llc"),
    (32, "Citadel | Chicago", "citadel_llc"),
    (33, "Citadel Advisors LLC", "citadel_llc"),
    (34, "Citadel Enterprise Americas LLC", "citadel_llc"),
    (35, "Citadel Investment Group", "citadel_llc"),
    (36, "Surveyor Capital (A Citadel Company)", "citadel_llc"),
    (37, "Ashler Capital (A Citadel Company)", "citadel_llc"),
    (38, "Citadel Global Equities", "citadel_llc"),
    (39, "CITADEL  llc", "citadel_llc"),
    # 8.4 Citadel Securities -- must NOT resolve to citadel_llc
    (40, "Citadel Securities", "citadel_securities"),
    (41, "Citadel Securities LLC", "citadel_securities"),
    (42, "Citadel Securities Europe Limited", "citadel_securities"),
    (43, "CITADEL SECURITIES (EUROPE) LIMITED", "citadel_securities"),
    (44, "Citadel Securities LP", "citadel_securities"),
    (45, "Citadel Securities GCS (Ireland) Limited", "citadel_securities"),
    # 8.5 Citadel -- the money negatives
    (46, "The Citadel", None),
    (47, "The Citadel, The Military College of South Carolina", None),
    (48, "Citadel Military College of South Carolina", None),
    (49, "The Citadel Alumni Association", None),
    (50, "The Citadel School of Engineering", None),
    (51, "Citadel Broadcasting", None),
    (52, "Citadel Broadcasting Corporation", None),
    (53, "Citadel Communications", None),
    (54, "Citadel Credit Union", None),
    (55, "Citadel Federal Credit Union", None),
    (56, "Citadel Defense Company", None),
    (57, "Citadel Servicing Corporation", None),
    (58, "Citadel Servicing Corp / Acra Lending", None),
    (59, "The Citadel Group", None),
    (60, "Citadel Group Australia", None),
    (61, "Citadel Security Software", None),
    (62, "Citadel Insurance Services", None),
    (63, "Citadel Salisbury", None),
    (64, "Citadel Bank", None),
    (65, "Citadel Mall", None),
    (66, "Citadelle Distillery", None),
    (67, "La Citadelle", None),
    # 8.6 Adjacent quant firms
    (68, "Two Sigma Investments", "two_sigma"),
    (69, "Hudson River Trading", "hrt"),
    (70, "HRT", "hrt"),
    (71, "Jump Trading", "jump"),
    (72, "D. E. Shaw & Co.", "de_shaw"),
    (73, "The D. E. Shaw Group", "de_shaw"),
    (74, "Optiver", "optiver"),
    (75, "IMC Trading", "imc"),
    (76, "Susquehanna International Group", "sig"),
    (77, "SIG Susquehanna", "sig"),
    (78, "Virtu Financial", "virtu"),
    (79, "Millennium Management", "millennium"),
    (80, "Point72 Asset Management", "point72"),
    (81, "Point 72", "point72"),
    (82, "Two Sigma Ventures", "two_sigma"),
    (83, "IMC Financial Markets", "imc"),
    (84, "Optiver Australia", "optiver"),
    # 8.7 Cross-firm confusable negatives
    (85, "Sigma Software", None),
    (86, "SIG Sauer", None),
    (87, "SIG Group AG", None),
    (88, "Virtu Health", None),
    (89, "Jump Capital", None),
    (90, "Millennium Physician Group", None),
    (91, "Millennium Trust Company", None),
    (92, "Hudson River Community Credit Union", None),
    (93, "Hudson Bay Capital", None),
    (94, "Shaw Communications", None),
)


def test_case_table_is_the_documented_size() -> None:
    """94 executable company cases, numbered 1..94 with no gaps."""
    assert len(CASE_TABLE) == 94
    assert [n for n, _, _ in CASE_TABLE] == list(range(1, 95))


@pytest.mark.parametrize(
    ("case", "text", "expected"),
    CASE_TABLE,
    ids=[f"{n:03d}-{t}" for n, t, _ in CASE_TABLE],
)
def test_research_case_table(case: int, text: str, expected: str | None, gaz: Gazetteer) -> None:
    result = resolve(text, field=POSITION, gaz=gaz, accept=ACCEPT, review=REVIEW)
    assert result.firm_key == expected, (
        f"case {case} {text!r}: expected {expected!r}, got {result.firm_key!r} "
        f"(verdict={result.verdict}, score={result.score}, reason={result.reason})"
    )


def test_case_table_precision_recall_and_wrong_firm_rate(gaz: Gazetteer) -> None:
    """The headline measurement, reported as one number set.

    Research 04 section 4.3 records precision 1.000 / recall 1.000 / 0 false
    positives with negatives + guard, and section 4.5 records a wrong-firm rate of
    0.000 at every threshold. A *wrong firm* is the failure that poisons the
    product: a miss costs recall, a misroute costs trust.
    """
    tp = fp = fn = wrong_firm = 0
    misses: list[str] = []
    for case, text, expected in CASE_TABLE:
        got = resolve(text, field=POSITION, gaz=gaz, accept=ACCEPT, review=REVIEW).firm_key
        if expected is None:
            if got is not None:
                fp += 1
                misses.append(f"#{case} {text!r}: false positive -> {got}")
        elif got == expected:
            tp += 1
        elif got is None:
            fn += 1
            misses.append(f"#{case} {text!r}: missed {expected}")
        else:
            fp += 1
            wrong_firm += 1
            misses.append(f"#{case} {text!r}: WRONG FIRM {expected} -> {got}")

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    assert wrong_firm == 0, f"wrong-firm rate must be 0.000; {misses}"
    assert precision == 1.000, f"precision {precision:.3f}; {misses}"
    assert recall == 1.000, f"recall {recall:.3f}; {misses}"


# --- section 8.8: field context ------------------------------------------------

# (case, input, field, expected entity key or None). The doc's "expect" column
# names an *entity key*, which for a negative-tier entity coexists with a REJECT
# verdict -- MatchVerdict is about target-firm resolution, not about identification.
FIELD_CONTEXT_CASES: tuple[tuple[int, str, AffiliationKind, str | None], ...] = (
    (95, "The Citadel", EDUCATION, "the_citadel_college"),
    (96, "Citadel", EDUCATION, "the_citadel_college"),
    (97, "The Citadel, The Military College of South Carolina", EDUCATION, "the_citadel_college"),
    (98, "Citadel Securities", EDUCATION, None),
    (99, "Jane Street", EDUCATION, None),
)


@pytest.mark.parametrize(("case", "text", "field", "expected"), FIELD_CONTEXT_CASES)
def test_field_context_cases(
    case: int, text: str, field: AffiliationKind, expected: str | None, gaz: Gazetteer
) -> None:
    """Section 8.8, cases 95-99.

    Case 99 is the one the doc leaves open ("NONE (or ``jane_street`` as an
    *event/program* edge, not employment)"). The implementation takes the strict
    reading: a market maker is not a school, so nothing is identified.
    """
    result = resolve(text, field=field, gaz=gaz, accept=ACCEPT, review=REVIEW)
    assert result.entity_key == expected
    assert result.firm_key is None, "no education-field string may resolve to a target firm"


def test_case_100_the_citadel_in_a_position_field_goes_to_review(gaz: Gazetteer) -> None:
    """Section 8.8, case 100. Never a silent accept, never a silent drop.

    The doc calls the output ``citadel_ambiguous``; there is no gazetteer entity of
    that name. The implementation expresses the same state as a REVIEW verdict with
    ``entity_key=None`` and both candidates in ``alternatives`` -- an ambiguous
    string must not be filed under either candidate while it is still ambiguous.
    """
    result = resolve("The Citadel", field=POSITION, gaz=gaz, accept=ACCEPT, review=REVIEW)
    assert result.verdict is MatchVerdict.REVIEW
    assert result.entity_key is None
    assert result.firm_key is None
    assert set(result.alternatives) == {"the_citadel_college", "citadel_llc"}
    assert result.reason.startswith("ambiguous:")


def test_bare_citadel_is_the_fund_in_a_position_field_and_the_college_in_education(
    gaz: Gazetteer,
) -> None:
    """The field is what decides. Same string, two entities, no ambiguity either way."""
    position = resolve("Citadel", field=POSITION, gaz=gaz)
    education = resolve("Citadel", field=EDUCATION, gaz=gaz)
    assert position.firm_key == "citadel_llc"
    assert position.verdict is MatchVerdict.ACCEPT
    assert education.entity_key == "the_citadel_college"
    assert education.firm_key is None


def test_the_citadel_in_education_is_never_ambiguous(gaz: Gazetteer) -> None:
    """The article plus the education field is a confident college identification."""
    result = resolve("The Citadel", field=EDUCATION, gaz=gaz)
    assert result.entity_key == "the_citadel_college"
    assert result.verdict is not MatchVerdict.REVIEW


def test_case_82_two_sigma_ventures_documents_a_doc_internal_disagreement(
    gaz: Gazetteer,
) -> None:
    """Section 8.6 case 82 says ``two_sigma``; section 4.4 says the guard rejects it.

    Both are true and they do not contradict the *implementation*: ``ventures`` is
    deliberately absent from ALLOW, so the fuzzy path would reject the string -- but
    ``Two Sigma Ventures`` is a curated alias, and step 4 (exact hit) short-circuits
    before the fuzzy sweep ever runs. The policy therefore lives in the YAML, exactly
    as the doc's own footnote says it should. Asserted both ways so a change to
    either the alias list or ALLOW shows up here.
    """
    assert resolve("Two Sigma Ventures", field=POSITION, gaz=gaz).firm_key == "two_sigma"
    assert containment_guard("two sigma ventures", "two sigma") is False
    assert "ventures" not in ALLOW_TOKENS
    # The contrasting policy from the same footnote: Jump Capital is a negative.
    assert resolve("Jump Capital", field=POSITION, gaz=gaz).firm_key is None


# ===========================================================================
# Citadel LLC vs Citadel Securities -- they never merge
# ===========================================================================

FUND_STRINGS: tuple[str, ...] = (
    "Citadel",
    "Citadel LLC",
    "Citadel L.L.C.",
    "Citadel Advisors",
    "Citadel Advisors LLC",
    "Citadel Enterprise Americas LLC",
    "Citadel Investment Group",
    "Citadel Global Equities",
    "Citadel | Chicago",
    "CITADEL  llc",
    "Surveyor Capital (A Citadel Company)",
    "Ashler Capital",
    "Aptigon Capital",
    "Pioneer Path Capital",
    "Ravelin Technology",
    "Global Quantitative Strategies",
)

MARKET_MAKER_STRINGS: tuple[str, ...] = (
    "Citadel Securities",
    "Citadel Securities LLC",
    "Citadel Securities LP",
    "Citadel Securities L.P.",
    "Citadel Securities Europe Limited",
    "CITADEL SECURITIES (EUROPE) LIMITED",
    "Citadel Securities GCS (Ireland) Limited",
    "Citadel Securities Hong Kong",
    "Citadel Securities Americas",
    "Citadel Securities | Miami",
    "CitSec",
)


@pytest.mark.parametrize("text", FUND_STRINGS)
def test_fund_strings_resolve_to_the_fund_and_never_the_market_maker(
    text: str, gaz: Gazetteer
) -> None:
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.firm_key == "citadel_llc"
    assert result.firm_key != "citadel_securities"


@pytest.mark.parametrize("text", MARKET_MAKER_STRINGS)
def test_market_maker_strings_resolve_to_the_market_maker_and_never_the_fund(
    text: str, gaz: Gazetteer
) -> None:
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.firm_key == "citadel_securities"
    assert result.firm_key != "citadel_llc"


def test_the_two_citadels_share_no_resolution_at_all(gaz: Gazetteer) -> None:
    """Load-bearing, stated as a set relation over every string in both families."""
    fund = {resolve(t, field=POSITION, gaz=gaz).firm_key for t in FUND_STRINGS}
    maker = {resolve(t, field=POSITION, gaz=gaz).firm_key for t in MARKET_MAKER_STRINGS}
    assert fund == {"citadel_llc"}
    assert maker == {"citadel_securities"}
    assert fund.isdisjoint(maker)


def test_securities_is_never_an_allowed_extra_token() -> None:
    """``securities`` is the only token separating the fund from the market maker.

    If it were ever added to ALLOW, ``Citadel`` would drift to ``Citadel Securities``
    and back through the guard, and the two firms would silently merge.
    """
    assert "securities" not in ALLOW_TOKENS
    assert "security" not in ALLOW_TOKENS
    assert containment_guard("citadel", "citadel securities") is False
    assert containment_guard("citadel securities", "citadel") is False


def test_the_two_citadels_are_distinct_target_firm_members() -> None:
    """There is deliberately no bare ``CITADEL`` member."""
    assert TargetFirm.CITADEL_LLC is not TargetFirm.CITADEL_SECURITIES
    assert TargetFirm.CITADEL_LLC.value == "citadel_llc"
    assert TargetFirm.CITADEL_SECURITIES.value == "citadel_securities"
    assert "citadel" not in {m.value for m in TargetFirm}


def test_citadel_security_singular_is_not_citadel_securities(gaz: Gazetteer) -> None:
    """One character apart: ``partial_ratio`` 90.9, ``jaro_winkler`` 91.3.

    The physical-security firms are a separate, thousands-strong population.
    """
    assert resolve("Citadel Security", field=POSITION, gaz=gaz).firm_key is None
    assert resolve("Citadel Security Software", field=POSITION, gaz=gaz).firm_key is None
    assert resolve("Citadel Security Agency", field=POSITION, gaz=gaz).firm_key is None
    assert resolve("Citadel Securities Services", field=POSITION, gaz=gaz).firm_key is None
    assert resolve("Citadel Protection", field=POSITION, gaz=gaz).firm_key is None


@pytest.mark.parametrize(
    "text",
    [
        "Citadel Broadcasting",
        "Citadel Broadcasting Corporation",
        "Citadel Communications",
        "Citadel Credit Union",
        "Citadel Federal Credit Union",
        "Citadel Defense",
        "Citadel Defense Company",
        "Citadel Servicing",
        "Citadel Servicing Corporation",
        "The Citadel Group",
        "Citadel Group",
        "Citadel Group Australia",
    ],
)
def test_the_named_citadel_impostors_are_all_rejected(text: str, gaz: Gazetteer) -> None:
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.verdict is MatchVerdict.REJECT
    assert result.firm_key is None


def test_a_rejection_names_what_it_was_rejected_as(gaz: Gazetteer) -> None:
    """A rejection carries an auditable reason, not a silent drop."""
    result = resolve("Citadel Broadcasting", field=POSITION, gaz=gaz)
    assert result.verdict is MatchVerdict.REJECT
    assert result.reason.startswith("negative_")
    assert result.entity_key == "citadel_broadcasting"
    assert result.tier == "negative"


# ===========================================================================
# Jane Street
# ===========================================================================


@pytest.mark.parametrize(
    "text",
    [
        "Jane Street",
        "Jane Street Capital",
        "Jane Street Group, LLC",
        "Jane Street Europe",
        "Jane Street Europe Limited",
        "Jane Street Netherlands B.V.",
        "Jane Street Hong Kong Limited",
        "Jane Street Singapore Pte Ltd",
        "Jane Street Asia Trading Limited",
        "Jane Street Execution Services LLC",
        "Jane Street Financial Ltd",
        "Jane Street Global",
        "JaneStreet",
        "Jane St",
        "Jane Street (JS)",
    ],
)
def test_jane_street_variants(text: str, gaz: Gazetteer) -> None:
    assert resolve(text, field=POSITION, gaz=gaz).firm_key == "jane_street"


@pytest.mark.parametrize(
    "text",
    [
        "Jane Street Coffee",
        "Jane Street Cafe",
        "Jane Street Dental",
        "Jane Street Dental Practice",
        "Jane Street Entertainment",
        "Jane Street Studios",
        "Jane Street Bakery",
        "Jane Street Realty",
        "Jane Street Animal Hospital",
        "Jane Street Public School",
        "Jane Street Medical Centre",
        "Jane Street Tattoo",
        "Jane Street Records",
        "Jane Street Coffee Roasters",
        "123 Jane Street Bakery",
        "45 Jane Street Deli",
        "Mary Jane Street Foods",
        "Janestreet Realty of Toronto",
        "Jane Street Studios Toronto",
    ],
)
def test_jane_street_false_friends(text: str, gaz: Gazetteer) -> None:
    """A road in Toronto, one in the West Village, and several in the UK and AU."""
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.verdict is MatchVerdict.REJECT
    assert result.firm_key is None


def test_street_address_pattern_fires_on_a_house_number(gaz: Gazetteer) -> None:
    result = resolve("123 Jane Street Bakery", field=POSITION, gaz=gaz)
    assert result.reason.startswith("negative_pattern:")


def test_js_alone_is_quarantined(gaz: Gazetteer) -> None:
    """``JS`` matches JavaScript and several thousand initialisms.

    Without corroboration it is not a match; with a second Jane Street signal on the
    same profile it is.
    """
    plain = resolve("JS", field=POSITION, gaz=gaz)
    assert plain.firm_key is None

    weak = resolve("JS", field=POSITION, gaz=gaz, allow_weak=True)
    assert weak.verdict is MatchVerdict.REVIEW
    assert weak.reason == "weak_alias_uncorroborated:jane_street"

    corroborated = resolve(
        "JS",
        field=POSITION,
        gaz=gaz,
        allow_weak=True,
        corroborating_keys=frozenset({"jane_street"}),
    )
    assert corroborated.verdict is MatchVerdict.ACCEPT
    assert corroborated.firm_key == "jane_street"
    assert corroborated.reason == "weak_alias_corroborated:jane_street"


@pytest.mark.parametrize(("alias", "key"), [("CS", "citadel_securities"), ("TS", "two_sigma")])
def test_other_quarantined_initialisms_need_corroboration(
    alias: str, key: str, gaz: Gazetteer
) -> None:
    assert resolve(alias, field=POSITION, gaz=gaz).firm_key is None
    weak = resolve(alias, field=POSITION, gaz=gaz, allow_weak=True)
    assert weak.verdict is MatchVerdict.REVIEW
    assert weak.entity_key == key


# ===========================================================================
# The trust / partners regression -- recently fixed, must not come back
# ===========================================================================


@pytest.mark.parametrize("text", ["Citadel Trust", "Citadel Trust Company", "Jane Street Partners"])
def test_trust_and_partners_no_longer_fold_onto_a_target_key(text: str, gaz: Gazetteer) -> None:
    """The exact-hit bypass, closed.

    While ``trust``/``partners`` were stripped as legal suffixes these folded to
    ``citadel`` and ``jane street``, hit the exact index, and skipped the containment
    guard entirely -- accepted at 100.0 as the target firms. Three assertions so a
    reintroduction fails loudly at whichever layer it happens: the fold, the score,
    and the verdict.
    """
    folded = norm(text)
    assert folded not in {"citadel", "jane street"}, "folded onto a target's exact key"
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.firm_key is None
    assert result.verdict is not MatchVerdict.ACCEPT


@pytest.mark.parametrize(
    "text",
    ["Millennium Partners", "Millennium Capital Partners", "Millennium Capital Partners LLP"],
)
def test_millennium_partners_still_resolves(text: str, gaz: Gazetteer) -> None:
    """The other half of the fix: an alias that genuinely needs the word still works.

    It is indexed under its full form, so no suffix stripping is required for it to
    match -- which is exactly why the suffix could be removed without losing recall.
    """
    assert resolve(text, field=POSITION, gaz=gaz).firm_key == "millennium"


def test_millennium_trust_company_is_still_rejected(gaz: Gazetteer) -> None:
    assert resolve("Millennium Trust Company", field=POSITION, gaz=gaz).firm_key is None


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("Citadel Holdings", "citadel_llc"),
        ("Citadel Holding Corporation", "citadel_llc"),
        ("Jane Street Holdings", "jane_street"),
    ],
)
def test_holdings_must_not_reopen_the_exact_hit_bypass(
    text: str, target: str, gaz: Gazetteer
) -> None:
    """The same failure mode as ``Citadel Trust``, still open for ``holdings``.

    ``holdings``/``holding``/``group`` remain in the trailing-suffix table, so
    ``Citadel Holdings`` folds to ``citadel``, hits the exact index, bypasses the
    containment guard and is accepted at 100.0 as the fund. Citadel Holding
    Corporation is a real and unrelated US company.

    ``Citadel Group`` and ``The Citadel Group`` are protected -- but by hand, as
    explicit negative aliases, not by the mechanism. Every unrelated ``<target>
    Holdings`` a curator has not thought of is a confident false positive at the
    target firm, which is the one failure this stage exists to prevent.
    """
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.firm_key != target, (
        f"{text!r} accepted as {target} at {result.score} via {result.reason}; "
        "an exact hit skips the containment guard"
    )


# ===========================================================================
# Scorer traps -- the guard is what saves you, not the score
# ===========================================================================

# (a, b) pairs from research 04 section 4.1, where the subset-tolerant scorers
# saturate at an unusable 100.
SUBSET_TRAPS: tuple[tuple[str, str], ...] = (
    ("citadel", "citadel broadcasting"),
    ("citadel", "citadel credit union"),
    ("citadel", "citadel securities"),
    ("jane street", "jane street coffee"),
    ("jane street", "jane street entertainment"),
    ("two sigma", "two sigma ventures"),
    ("sig", "sig sauer"),
    ("optiver", "optiver australia"),
)


@pytest.mark.parametrize(("a", "b"), SUBSET_TRAPS)
def test_subset_tolerant_scorers_all_return_100(a: str, b: str) -> None:
    """Documented directly against ``rapidfuzz`` so the trap lives in the suite.

    ``partial_ratio`` is the notorious one, but ``token_set_ratio`` and
    ``token_ratio`` are equally broken here and for the same structural reason:
    whenever one string's tokens are a subset of the other's, the score saturates
    regardless of how discriminating the extra tokens are.
    """
    assert fuzz.partial_ratio(a, b) == 100.0
    assert fuzz.token_set_ratio(a, b) == 100.0
    assert fuzz.token_ratio(a, b) == 100.0


# The subset relation is identical in every row above; only the *meaning* of the
# extra tokens differs, and only the guard can tell them apart.
DISCRIMINATING_EXTRAS: tuple[tuple[str, str], ...] = tuple(
    pair for pair in SUBSET_TRAPS if pair != ("optiver", "optiver australia")
)


@pytest.mark.parametrize(("a", "b"), DISCRIMINATING_EXTRAS)
def test_the_containment_guard_sees_what_the_scorers_cannot(a: str, b: str) -> None:
    """The scorer reports how much agrees; the guard asks what the disagreement was."""
    assert containment_guard(b, a) is False


def test_the_guard_is_a_judgement_about_the_extra_not_a_blanket_no() -> None:
    """``Optiver Australia`` is the control case in the same table.

    It saturates every subset-tolerant scorer exactly like ``Citadel Broadcasting``
    does, and it is a *true positive* -- a firm's country arm is the same firm. The
    guard keeps it and drops the other seven, which is the whole argument for having
    a closed ALLOW list rather than a threshold.
    """
    assert fuzz.token_set_ratio("optiver", "optiver australia") == 100.0
    assert containment_guard("optiver australia", "optiver") is True
    assert containment_guard("citadel broadcasting", "citadel") is False


def test_wratio_is_also_high_enough_to_accept_every_trap() -> None:
    """WRatio scores 90.0 on these pairs -- exactly at the accept threshold.

    The recommendation is never the scorer alone: unguarded WRatio was the *worst*
    matcher measured (precision 0.591). Every trap would be auto-accepted on score.
    """
    for a, b in SUBSET_TRAPS:
        assert fuzz.WRatio(a, b) >= ACCEPT


@pytest.mark.parametrize(
    "text",
    [
        "Citadel Broadcasting",
        "Citadel Credit Union",
        "Jane Street Coffee",
        "Jane Street Entertainment",
        "SIG Sauer",
    ],
)
def test_the_pipeline_rejects_every_trap_anyway(text: str, gaz: Gazetteer) -> None:
    assert resolve(text, field=POSITION, gaz=gaz).firm_key is None


def test_no_scorer_can_separate_citadel_from_the_citadel_after_folding() -> None:
    """The row from section 4.1 that proves normalisation must solve this, not tuning.

    Once ``The`` is gone, *every* scorer returns 100 -- which is why the article is
    preserved in ``norm_raw`` and the vetoes are keyed on it.
    """
    a, b = norm("Citadel"), norm("The Citadel")
    assert a == b == "citadel"
    for scorer in (
        fuzz.ratio,
        fuzz.partial_ratio,
        fuzz.token_sort_ratio,
        fuzz.token_set_ratio,
        fuzz.WRatio,
        fuzz.token_ratio,
    ):
        assert scorer(a, b) == 100.0
    assert norm_raw("Citadel") != norm_raw("The Citadel")


# ===========================================================================
# The containment guard, directly
# ===========================================================================


@pytest.mark.parametrize(
    ("query", "alias"),
    [
        ("jane street london", "jane street"),
        ("jane street europe", "jane street"),
        ("citadel chicago", "citadel"),
        ("citadel miami", "citadel"),
        ("optiver australia", "optiver"),
        ("jane street global", "jane street"),
        ("two sigma investments", "two sigma"),
        ("hudson river trading company", "hudson river trading"),
    ],
)
def test_guard_allows_only_non_discriminating_extras(query: str, alias: str) -> None:
    assert containment_guard(query, alias) is True


@pytest.mark.parametrize(
    ("query", "alias"),
    [
        ("citadel broadcasting", "citadel"),
        ("citadel credit union", "citadel"),
        ("citadel defense", "citadel"),
        ("citadel servicing", "citadel"),
        ("two sigma ventures", "two sigma"),
        ("jane street coffee", "jane street"),
        ("jane street entertainment", "jane street"),
        ("sig sauer", "sig"),
        ("citadel securities", "citadel"),
    ],
)
def test_guard_rejects_a_discriminating_extra(query: str, alias: str) -> None:
    assert containment_guard(query, alias) is False


@pytest.mark.parametrize(
    "token",
    [
        "broadcasting",
        "securities",
        "credit",
        "union",
        "defense",
        "servicing",
        "coffee",
        "entertainment",
        "sauer",
        "ventures",
        "dental",
        "bakery",
        "trust",
        "partners",
    ],
)
def test_discriminating_tokens_are_absent_from_allow(token: str) -> None:
    """What is *absent* from ALLOW is the point of the list."""
    assert token not in ALLOW_TOKENS


@pytest.mark.parametrize(
    "token",
    [
        "london",
        "europe",
        "hong",
        "kong",
        "chicago",
        "miami",
        "trading",
        "capital",
        "management",
        "group",
        "the",
        "and",
    ],
)
def test_non_discriminating_tokens_are_present_in_allow(token: str) -> None:
    assert token in ALLOW_TOKENS


def test_guard_alignment_is_fuzzy_not_exact() -> None:
    """A typo inside an otherwise-aligned token must not manufacture an extra.

    Exact token comparison collapsed recall to 8% under one-character corruptions.
    """
    assert containment_guard("jane streey capital", "jane street capital") is True
    assert fuzz.ratio("streey", "street") >= GUARD_TOKEN_THRESHOLD


def test_guard_token_threshold_is_the_documented_value() -> None:
    assert GUARD_TOKEN_THRESHOLD == 80.0


def test_guard_forgives_an_initials_echo() -> None:
    """``jane street js`` against ``jane street`` leaves only the initials behind."""
    assert containment_guard("jane street js", "jane street") is True


def test_guard_is_symmetric_about_which_side_the_extra_is_on() -> None:
    assert containment_guard("citadel", "citadel broadcasting") is False
    assert containment_guard("citadel broadcasting", "citadel") is False
    assert containment_guard("citadel", "citadel europe") is True
    assert containment_guard("citadel europe", "citadel") is True


def test_guard_on_identical_strings_is_trivially_true() -> None:
    assert containment_guard("citadel securities", "citadel securities") is True


# ===========================================================================
# Thresholds
# ===========================================================================


def test_threshold_boundaries_at_exactly_accept_and_review(gaz: Gazetteer) -> None:
    """``Citadel Chicago`` scores exactly 90.0 -- the accept boundary itself.

    Accept is ``>=``, review is ``>=``, so the same string flips verdict as the
    thresholds move by a hundredth either side of its score.
    """
    text = "Citadel Chicago"
    at_accept = resolve(text, field=POSITION, gaz=gaz, accept=90.0, review=84.0)
    assert at_accept.score == pytest.approx(90.0)
    assert at_accept.verdict is MatchVerdict.ACCEPT

    just_above = resolve(text, field=POSITION, gaz=gaz, accept=90.01, review=84.0)
    assert just_above.verdict is MatchVerdict.REVIEW
    assert just_above.firm_key is None, "REVIEW must not expose a firm_key"

    just_below = resolve(text, field=POSITION, gaz=gaz, accept=89.99, review=84.0)
    assert just_below.verdict is MatchVerdict.ACCEPT


def test_review_boundary_is_inclusive_and_below_it_is_a_reject(gaz: Gazetteer) -> None:
    text = "Citadel Chicago"
    at_review = resolve(text, field=POSITION, gaz=gaz, accept=95.0, review=90.0)
    assert at_review.verdict is MatchVerdict.REVIEW

    above_review = resolve(text, field=POSITION, gaz=gaz, accept=95.0, review=90.01)
    assert above_review.verdict is MatchVerdict.REJECT
    assert above_review.reason == "below_threshold"

    below_review = resolve(text, field=POSITION, gaz=gaz, accept=95.0, review=89.99)
    assert below_review.verdict is MatchVerdict.REVIEW


def test_a_review_band_match_lands_in_the_band(gaz: Gazetteer) -> None:
    result = resolve("Citadal", field=POSITION, gaz=gaz, accept=ACCEPT, review=REVIEW)
    assert REVIEW <= result.score < ACCEPT
    assert result.verdict is MatchVerdict.REVIEW
    assert result.entity_key == "citadel_llc"
    assert result.firm_key is None


def test_exact_hits_score_exactly_one_hundred(gaz: Gazetteer) -> None:
    for text in ("Jane Street", "Citadel", "Citadel Securities", "Optiver"):
        result = resolve(text, field=POSITION, gaz=gaz)
        assert result.score == 100.0
        assert result.reason.startswith("exact")


def test_raising_accept_above_one_hundred_still_accepts_an_exact_hit(gaz: Gazetteer) -> None:
    """Exact hits short-circuit before any threshold is consulted."""
    result = resolve("Citadel", field=POSITION, gaz=gaz, accept=101.0, review=101.0)
    assert result.verdict is MatchVerdict.ACCEPT


# ===========================================================================
# Resolution order, determinism and empty input
# ===========================================================================


def test_the_veto_beats_an_exact_positive_hit(gaz: Gazetteer) -> None:
    """Steps 2-3 run before step 4 on purpose.

    ``The Citadel, The Military College of South Carolina`` folds onto ``citadel``'s
    exact key under ``norm``. A veto that ran second would never fire.
    """
    text = "The Citadel"
    assert norm(text) == norm("Citadel") == "citadel"
    assert "citadel" in gaz.exact, "the veto has to beat a live exact positive key"
    assert gaz.exact["citadel"][0] == "citadel_llc"
    # In an education field the college wins outright; in a position field the
    # ambiguity guard catches it first. Either way the exact hit never lands.
    assert resolve(text, field=POSITION, gaz=gaz).firm_key is None

    longer = "The Citadel, The Military College of South Carolina"
    result = resolve(longer, field=POSITION, gaz=gaz)
    assert result.verdict is MatchVerdict.REJECT
    assert result.reason.startswith("negative_")


def test_a_veto_only_fires_for_an_entity_in_scope(gaz: Gazetteer) -> None:
    """``The Citadel`` vetoes the fund; the fund is not a candidate for a school column."""
    assert resolve("The Citadel", field=EDUCATION, gaz=gaz).entity_key == "the_citadel_college"


@pytest.mark.parametrize("text", ["", "   ", "---", "...", "-", "|", "()"])
def test_unusable_input_rejects_as_empty(text: str, gaz: Gazetteer) -> None:
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.verdict is MatchVerdict.REJECT
    assert result.reason == "empty"
    assert result.entity_key is None
    assert result.score == 0.0


@pytest.mark.parametrize(
    "text", ["Citadel", "Citadel Securities", "Jane Street", "The Citadel", "Citadel Chicago"]
)
def test_resolution_is_deterministic(text: str, gaz: Gazetteer) -> None:
    """Ties in the fuzzy sweep resolve by a pre-sorted index, not by set order."""
    first = resolve(text, field=POSITION, gaz=gaz)
    for _ in range(5):
        assert resolve(text, field=POSITION, gaz=gaz) == first


def test_firm_key_is_none_for_every_non_accept(gaz: Gazetteer) -> None:
    """``entity_key`` may be set on a REJECT; ``firm_key`` may not.

    Identifying a string as Citadel Broadcasting is a confident identification and
    an explicit no at the same time.
    """
    rejected = resolve("Citadel Broadcasting", field=POSITION, gaz=gaz)
    assert rejected.entity_key == "citadel_broadcasting"
    assert rejected.firm_key is None

    reviewed = resolve("Citadal", field=POSITION, gaz=gaz)
    assert reviewed.entity_key == "citadel_llc"
    assert reviewed.firm_key is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Jane Street London", "jane_street"),
        ("Jane Street Chicago", "jane_street"),
        ("Jane Street New York", "jane_street"),
        ("Jane Street Amsterdam", "jane_street"),
    ],
)
def test_jane_street_offices_should_resolve_like_every_other_firms_offices(
    text: str, expected: str, gaz: Gazetteer
) -> None:
    """A real Jane Street employee's office string is dropped -- a recall bug.

    ``Citadel Chicago``, ``Optiver Chicago``, ``Two Sigma New York`` and ``Hudson
    River Trading London`` all resolve, because ``london``/``chicago``/``new``/
    ``york``/``amsterdam`` are in ALLOW. Jane Street is the one firm with a short
    abbreviation alias, and the fuzzy sweep keeps the *first* alias at the maximum
    score rather than the first that survives the guard: ``jane st`` and ``jane
    street`` both score 90.0, ``jane st`` sorts first, and the guard then rejects
    ``street`` as an unaligned extra. The pipeline never reaches the alias that
    would have matched.
    """
    result = resolve(text, field=POSITION, gaz=gaz)
    assert result.firm_key == expected, (
        f"{text!r} -> {result.reason} at {result.score}; "
        "the guard rejected a shadowing alias instead of trying the next candidate"
    )


@pytest.mark.parametrize(("text", "expected"), [("SIG", "sig"), ("IMC", "imc")])
def test_quarantined_initialisms_must_not_resolve_without_corroboration(
    text: str, expected: str, gaz: Gazetteer
) -> None:
    """``SIG`` and ``IMC`` are declared ``weak_aliases`` but resolve anyway.

    The gazetteer sets ``weak_alias_accept: 100`` and
    ``weak_alias_requires_corroboration: true``, and research 04 section 2 lists
    ``SIG`` and ``IMC`` alongside ``JS`` as quarantined. The quarantine leaks two
    ways: ``IMC B.V.`` folds to ``imc``, putting the bare initialism in the *exact*
    positive index, and ``SIG`` reaches ``sig asia`` on the fuzzy path with ``asia``
    allowed as a geography. Both are accepted with no corroboration at all, which is
    what ``JS`` is correctly protected from.
    """
    result = resolve(text, field=POSITION, gaz=gaz, accept=ACCEPT, review=REVIEW)
    assert result.verdict is not MatchVerdict.ACCEPT, (
        f"{text!r} accepted as {result.entity_key} via {result.reason} at {result.score} "
        "without corroboration, despite being a weak_alias"
    )
    assert result.firm_key != expected
