"""Contract tests for :mod:`interlayer.models`.

The models are the only thing six independently-built stages share, so a wrong
assumption here is wrong in every stage at once. These tests are deliberately
adversarial about the invariants the contract claims in prose: frozen-ness,
coarse-date span semantics, identifier folding, edge orientation, and the
provenance rules that keep an inference from being presented as a fact.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime

import pytest
from pydantic import BaseModel, ValidationError

from interlayer import models
from interlayer.models import (
    SCHEMA_VERSION,
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Base,
    Cluster,
    Connection,
    Education,
    EvidenceItem,
    GraphEdge,
    GraphNode,
    GraphStats,
    MutualObservation,
    Org,
    OrgKind,
    Person,
    Position,
    Provenance,
    RunManifest,
    ScoreComponents,
    ScoredCluster,
    ScoredPerson,
    Seniority,
    TargetFirm,
    TargetPerson,
    UnresolvedMutual,
    normalize_key,
    stable_id,
    stable_id_exact,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def aff(
    start: ApproxDate | None = None,
    end: ApproxDate | None = None,
    *,
    org: str = "org_a",
    person: str = "p_1",
    current: bool = False,
) -> Affiliation:
    return Affiliation(
        person_id=person,
        org_id=org,
        kind=AffiliationKind.EMPLOYMENT,
        start=start,
        end=end,
        is_current=current,
    )


def y(year: int, month: int | None = None, day: int | None = None) -> ApproxDate:
    return ApproxDate(year=year, month=month, day=day)


# One instance of every contract model, used for the round-trip sweep.
INSTANCES: dict[str, Base] = {
    "Affiliation": aff(y(2019, 6), y(2021), current=False),
    "ApproxDate": y(2019, 6, 3),
    "Cluster": Cluster(
        cluster_id="c_1",
        label="Jane Street traders",
        member_ids=("p_1", "p_2"),
        top_orgs=(("org_a", 2),),
        top_titles=(("Trader", 2),),
        cohesion=0.5,
        resolution=2.0,
    ),
    "Connection": Connection(person_id="p_1", connected_on=date(2021, 3, 1), degree=1, note="n"),
    "Education": Education(
        school_raw="MIT", school_org_id="org_s", degree="BSc", start=y(2010), end=y(2014)
    ),
    "EvidenceItem": EvidenceItem(
        kind="observed_mutual",
        detail="read off the mutual-connections list",
        contribution=10.0,
        via_person_id="p_2",
        hops=1,
        provenance=Provenance.OBSERVED,
    ),
    "GraphEdge": GraphEdge(
        source="p_2",
        target="p_1",
        weight=0.25,
        shared_org_ids=("org_a",),
        kinds=(AffiliationKind.EMPLOYMENT,),
        provenance=Provenance.OBSERVED,
    ),
    "GraphNode": GraphNode(person_id="p_1", label="Ada", degree=3, attrs={"k": "v", "n": 1}),
    "GraphStats": GraphStats(n_nodes=3, n_edges=2, n_components=1, density=0.5, modularity=0.4),
    "MutualObservation": MutualObservation(
        target_person_id="t_1",
        bridge_person_ids=("p_2", "p_1"),
        observed_on=date(2024, 5, 1),
        stated_count=4,
        complete=False,
        note="paginated",
    ),
    "Org": Org.make(
        "Citadel Securities",
        OrgKind.COMPANY,
        aliases=("Citadel Sec",),
        is_target=True,
        target_firm=TargetFirm.CITADEL_SECURITIES,
    ),
    "Person": Person.make(
        "Ada",
        "Lovelace",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
        headline="Trader",
        positions=(Position(company_raw="Jane Street", start=y(2019), is_current=True),),
        educations=(Education(school_raw="MIT"),),
    ),
    "Position": Position(
        company_raw="Jane Street",
        company_org_id="org_a",
        title_raw="Trader",
        seniority=Seniority.SENIOR,
        start=y(2019, 6),
        end=y(2021, 1, 31),
    ),
    "RunManifest": RunManifest(
        run_id="r_1",
        created_at=datetime(2024, 1, 1, 12, 30, 15),
        counts={"people": 5},
        stage_versions={"ingest": "1"},
        input_sha256="ab" * 32,
    ),
    "ScoreComponents": ScoreComponents(direct=6.0, alumni=1.5, proximity=2.0),
    "ScoredCluster": ScoredCluster(
        cluster_id="c_1",
        label="Jane Street traders",
        score=3.0,
        size=2,
        per_firm={TargetFirm.JANE_STREET: 3.0},
        top_person_ids=("p_1",),
    ),
    "ScoredPerson": ScoredPerson(
        person_id="p_1",
        full_name="Ada Lovelace",
        score=10.0,
        per_firm={TargetFirm.JANE_STREET: 10.0, TargetFirm.CITADEL_LLC: 0.0},
        evidence=(
            EvidenceItem(kind="observed_mutual", detail="d", provenance=Provenance.OBSERVED),
        ),
        hops_to_target=1,
    ),
    "TargetPerson": TargetPerson.make(
        "Grace Hopper",
        TargetFirm.CITADEL_LLC,
        linkedin_url="https://www.linkedin.com/in/grace-hopper?trk=x",
        mutual_count=3,
    ),
    "UnresolvedMutual": UnresolvedMutual(
        raw_text="G. Hopper", reason="ambiguous_name", candidates=("p_1",), source_line=12
    ),
}


# ---------------------------------------------------------------------------
# frozen-ness, hashing, copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(INSTANCES))
def test_every_model_is_frozen(name: str) -> None:
    """Immutability is what removes the cross-stage mutation bug class."""
    instance = INSTANCES[name]
    field = next(iter(type(instance).model_fields))
    with pytest.raises(ValidationError) as exc:
        setattr(instance, field, getattr(instance, field))
    assert "frozen" in str(exc.value).lower()


@pytest.mark.parametrize("name", sorted(INSTANCES))
def test_every_model_forbids_extra_fields(name: str) -> None:
    instance = INSTANCES[name]
    with pytest.raises(ValidationError):
        type(instance).model_validate({**instance.model_dump(), "definitely_not_a_field": 1})


def test_model_copy_update_produces_a_changed_copy() -> None:
    original = INSTANCES["Person"]
    assert isinstance(original, Person)
    updated = original.model_copy(update={"headline": "Quant"})
    assert updated.headline == "Quant"
    assert original.headline == "Trader"
    assert updated.person_id == original.person_id
    assert updated is not original


def test_model_copy_deep_leaves_original_untouched() -> None:
    original = INSTANCES["ScoredPerson"]
    assert isinstance(original, ScoredPerson)
    copy = original.model_copy(deep=True, update={"rank": 4})
    assert copy.rank == 4
    assert original.rank == 0
    assert copy.evidence == original.evidence


@pytest.mark.parametrize(
    "name",
    [
        "Affiliation",
        "ApproxDate",
        "Cluster",
        "Connection",
        "Education",
        "EvidenceItem",
        "GraphEdge",
        "GraphStats",
        "Org",
        "Person",
        "Position",
        "ScoreComponents",
        "TargetPerson",
        "UnresolvedMutual",
    ],
)
def test_value_models_are_hashable(name: str) -> None:
    """Frozen models are used as dict keys and set members across stages."""
    instance = INSTANCES[name]
    assert hash(instance) == hash(instance)
    assert len({instance, instance.model_copy()}) == 1


@pytest.mark.parametrize("name", ["GraphNode", "RunManifest", "ScoredCluster", "ScoredPerson"])
def test_models_with_dict_fields_are_not_hashable(name: str) -> None:
    """Documents the exception: a ``dict`` field defeats pydantic's frozen hash.

    These four carry ``dict`` payloads, so they may not be put in a set. A stage
    that dedupes scored people must key on ``person_id``, not on the model.
    """
    with pytest.raises(TypeError):
        hash(INSTANCES[name])


def test_equal_models_hash_equal_and_dedupe() -> None:
    a = Person.make("Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/ada-lovelace")
    b = Person.make("Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/ada-lovelace/")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1


# ---------------------------------------------------------------------------
# ApproxDate
# ---------------------------------------------------------------------------


def test_year_only_span_covers_the_whole_year() -> None:
    d = y(2019)
    assert d.span_start == 20190101
    assert d.span_end == 20191231


def test_year_month_span_covers_the_whole_month() -> None:
    d = y(2019, 6)
    assert d.span_start == 20190601
    assert d.span_end == 20190631  # month-end is uniformly 31 in span arithmetic


def test_full_date_span_is_a_point() -> None:
    d = y(2019, 6, 15)
    assert d.span_start == d.span_end == 20190615


@pytest.mark.parametrize(
    ("d", "expected"),
    [(y(2019), 20190000), (y(2019, 6), 20190600), (y(2019, 6, 15), 20190615)],
)
def test_sort_key_sorts_missing_parts_to_the_start(d: ApproxDate, expected: int) -> None:
    assert d.sort_key == expected


def test_sort_key_orders_coarse_before_fine_within_a_year() -> None:
    assert y(2019).sort_key < y(2019, 1).sort_key < y(2019, 1, 1).sort_key


def test_day_without_month_raises() -> None:
    with pytest.raises(ValidationError, match="day given without month"):
        ApproxDate(year=2019, day=5)


@pytest.mark.parametrize("year", [1900, 2100])
def test_boundary_years_are_accepted(year: int) -> None:
    assert ApproxDate(year=year).year == year


@pytest.mark.parametrize("year", [1899, 2101, 0, -1])
def test_years_outside_the_boundary_are_rejected(year: int) -> None:
    with pytest.raises(ValidationError):
        ApproxDate(year=year)


@pytest.mark.parametrize(("month", "day"), [(0, None), (13, None), (1, 0), (1, 32)])
def test_out_of_range_month_or_day_rejected(month: int, day: int | None) -> None:
    with pytest.raises(ValidationError):
        ApproxDate(year=2019, month=month, day=day)


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (y(2019), date(2019, 1, 1)),
        (y(2019, 6), date(2019, 6, 1)),
        (y(2019, 6, 15), date(2019, 6, 15)),
    ],
)
def test_to_date_defaults_missing_parts_to_january_first(d: ApproxDate, expected: date) -> None:
    assert d.to_date() == expected


@pytest.mark.parametrize(
    ("d", "expected"),
    [
        (y(2019), "2019"),
        (y(2019, 6), "2019-06"),
        (y(2019, 6, 3), "2019-06-03"),
        (y(1900, 12, 31), "1900-12-31"),
    ],
)
def test_str_format_is_zero_padded_iso_prefix(d: ApproxDate, expected: str) -> None:
    assert str(d) == expected


@pytest.mark.parametrize("d", [y(2019), y(2019, 6), y(2019, 6, 15), y(1900), y(2100, 12, 31)])
def test_span_start_never_exceeds_span_end(d: ApproxDate) -> None:
    assert d.span_start <= d.span_end


def test_calendar_impossible_date_is_rejected_at_construction() -> None:
    """A full date the model cannot convert must not validate.

    ``ApproxDate`` bounds ``day`` at 31 without consulting the month, so
    ``2019-02-30`` is accepted and then ``to_date()`` raises ``ValueError`` deep
    inside whichever stage first tries to use it. A contract model must not hold
    a value it cannot serve.
    """
    with pytest.raises(ValidationError):
        ApproxDate(year=2019, month=2, day=30)


# ---------------------------------------------------------------------------
# Affiliation.overlaps
# ---------------------------------------------------------------------------


def test_year_only_overlaps_a_mid_year_stint() -> None:
    """THE regression: ``2019`` must overlap ``2019-06`` to ``2020``.

    Comparing points instead of spans put both ends of the year-only stint at
    2019-01-01 and silently dropped a real seven-month co-tenure.
    """
    a = aff(y(2019), y(2019))
    b = aff(y(2019, 6), y(2020))
    assert a.overlaps(b)
    assert b.overlaps(a)


def test_year_only_start_overlaps_same_year_month_start() -> None:
    assert aff(y(2019), y(2022)).overlaps(aff(y(2019, 11), y(2019, 12)))


def test_adjacent_year_spans_do_not_overlap() -> None:
    a = aff(y(2018), y(2019))
    b = aff(y(2020), y(2021))
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_adjacent_month_spans_do_not_overlap() -> None:
    a = aff(y(2019, 1), y(2019, 6))
    b = aff(y(2019, 7), y(2019, 12))
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_same_month_spans_overlap() -> None:
    assert aff(y(2019, 6), y(2019, 6)).overlaps(aff(y(2019, 6), y(2019, 6)))


def test_open_ended_stint_overlaps_everything_after_it_starts() -> None:
    ongoing = aff(y(2015), None)
    assert ongoing.overlaps(aff(y(2099), y(2100)))
    assert not ongoing.overlaps(aff(y(1900), y(2014)))


def test_current_stint_overlaps_another_current_stint() -> None:
    assert aff(y(2019), None, current=True).overlaps(aff(y(2021), None, current=True))


def test_both_fully_open_affiliations_overlap() -> None:
    assert aff().overlaps(aff())


def test_missing_start_is_open_on_the_left() -> None:
    assert aff(None, y(1901)).overlaps(aff(y(1900), y(1900)))


def test_different_orgs_never_overlap_even_with_identical_dates() -> None:
    a = aff(y(2019), y(2021), org="org_a")
    b = aff(y(2019), y(2021), org="org_b")
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_affiliation_overlaps_itself() -> None:
    a = aff(y(2019, 6), y(2021, 3))
    assert a.overlaps(a)
    assert aff().overlaps(aff())


def test_overlap_ignores_person_so_the_graph_stage_must_filter_self_pairs() -> None:
    a = aff(y(2019), y(2021), person="p_1")
    b = aff(y(2019), y(2021), person="p_1")
    assert a.overlaps(b)


def test_current_affiliation_may_not_carry_an_end_date() -> None:
    """``Position`` forbids this; ``Affiliation`` must too.

    ``overlaps`` documents that "an affiliation marked current has no end", but
    nothing enforces it, and the method reads ``end`` while ignoring
    ``is_current``. So a record that says "still there" is silently treated as
    having left, and the normalize stage can build one straight out of a
    ``Position`` without ever being told.
    """
    with pytest.raises(ValidationError):
        aff(y(2019), y(2021), current=True)


def test_affiliation_end_may_not_precede_start() -> None:
    """``Position`` and ``Education`` both guard this; ``Affiliation`` does not.

    The consequence is not cosmetic: a reversed-date affiliation does not
    overlap *itself*, so co-tenure stops being reflexive and the graph stage's
    pairwise filter silently drops the person.
    """
    reversed_dates = aff(y(2021), y(2019))
    assert reversed_dates.overlaps(reversed_dates), "reversed dates break overlap reflexivity"
    with pytest.raises(ValidationError):
        aff(y(2021), y(2019))


# ---------------------------------------------------------------------------
# stable_id / stable_id_exact / normalize_key
# ---------------------------------------------------------------------------


def test_member_urns_differing_only_in_case_get_different_ids() -> None:
    """Casefolding an opaque URN merges two distinct humans into one record."""
    assert stable_id_exact("p", "ACoAAB123") != stable_id_exact("p", "acoaab123")


def test_person_make_keeps_case_distinct_urns_apart() -> None:
    upper = Person.make("A", "B", linkedin_url="https://www.linkedin.com/in/ACoAAB123")
    lower = Person.make("A", "B", linkedin_url="https://www.linkedin.com/in/acoaab123")
    assert upper.linkedin_slug == "ACoAAB123"
    assert lower.linkedin_slug == "acoaab123"
    assert upper.person_id != lower.person_id


def test_target_person_make_keeps_case_distinct_urns_apart() -> None:
    upper = TargetPerson.make(
        "X", TargetFirm.JANE_STREET, linkedin_url="https://www.linkedin.com/in/ACoAAB123"
    )
    lower = TargetPerson.make(
        "X", TargetFirm.JANE_STREET, linkedin_url="https://www.linkedin.com/in/acoaab123"
    )
    assert upper.target_person_id != lower.target_person_id


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Ada Lovelace", "ADA LOVELACE"),
        ("Ada Lovelace", "ada lovelace"),
        ("Jane Street", "jane street"),
        ("Jane Street Capital, L.L.C.", "jane street capital l l c"),
        ("  Ada   Lovelace  ", "Ada Lovelace"),
        ("Ada\tLovelace", "Ada Lovelace"),
        ("Renaissance Technologies!", "Renaissance Technologies"),
        ("Two Sigma · Investments", "Two Sigma Investments"),
        ("Blå Kapital", "Blä Kapital"),
    ],
)
def test_presentation_strings_fold_to_the_same_id(a: str, b: str) -> None:
    assert stable_id("p", a) == stable_id("p", b)
    assert normalize_key(a) == normalize_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Citadel LLC", "Citadel Securities"),
        ("Citadel", "The Citadel"),
        ("Jane Street", "Jane Street Group"),
        ("A B", "AB"),
    ],
)
def test_genuinely_different_strings_get_different_ids(a: str, b: str) -> None:
    assert stable_id("org", a) != stable_id("org", b)


def test_normalize_key_docstring_example() -> None:
    assert normalize_key("  Jane Street Capital, L.L.C. ") == "jane street capital l l c"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A&B Partners", "a&b partners"),
        ("C++ Capital", "c++ capital"),
        ("  ", ""),
        ("...", ""),
        ("Édouard", "edouard"),
        ("café", "cafe"),
        ("\uff2a\uff41\uff4e\uff45", "jane"),  # fullwidth Jane
    ],
)
def test_normalize_key_folds_unicode_punctuation_and_whitespace(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


def test_stable_id_and_exact_live_in_different_namespaces() -> None:
    assert stable_id("p", "x").startswith("p_")
    assert stable_id_exact("p", "x").startswith("p_exact_")
    assert stable_id("p", "x") != stable_id_exact("p", "x")


def test_stable_id_shape_is_kind_plus_sixteen_hex() -> None:
    ident = stable_id("org", "Jane Street")
    kind, digest = ident.split("_", 1)
    assert kind == "org"
    assert len(digest) == 16
    assert set(digest) <= set("0123456789abcdef")


def test_stable_id_is_positional() -> None:
    assert stable_id("aff", "a", "b") != stable_id("aff", "b", "a")


def test_stable_id_treats_none_and_empty_alike() -> None:
    assert stable_id("p", None) == stable_id("p", "")


def test_stable_id_kind_is_part_of_the_payload() -> None:
    assert stable_id("p", "x").split("_", 1)[1] != stable_id("org", "x").split("_", 1)[1]


@pytest.mark.parametrize("hashseed", ["0", "1", "12345", "random"])
def test_ids_are_stable_across_processes(hashseed: str) -> None:
    """Ids must not depend on ``hash()``, which is salted per process."""
    script = (
        "from interlayer.models import Org, Person, stable_id, stable_id_exact;"
        "print(stable_id('org', 'Jane Street'));"
        "print(stable_id_exact('p', 'ACoAAB123'));"
        "print(Person.make('Ada', 'Lovelace',"
        " linkedin_url='https://www.linkedin.com/in/ada-lovelace').person_id);"
        "print(Org.make('Citadel LLC').org_id)"
    )
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env
    ).stdout.splitlines()
    assert out == [
        stable_id("org", "Jane Street"),
        stable_id_exact("p", "ACoAAB123"),
        Person.make(
            "Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/ada-lovelace"
        ).person_id,
        Org.make("Citadel LLC").org_id,
    ]


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------


def test_edge_canonicalises_orientation_regardless_of_construction_order() -> None:
    forward = GraphEdge(source="p_a", target="p_z")
    backward = GraphEdge(source="p_z", target="p_a")
    assert (forward.source, forward.target) == ("p_a", "p_z")
    assert (backward.source, backward.target) == ("p_a", "p_z")
    assert forward == backward
    assert hash(forward) == hash(backward)
    assert len({forward, backward}) == 1


def test_edge_id_is_order_independent() -> None:
    assert (
        GraphEdge(source="p_z", target="p_a").edge_id
        == GraphEdge(source="p_a", target="p_z").edge_id
    )


def test_edge_id_distinguishes_distinct_pairs() -> None:
    assert (
        GraphEdge(source="p_a", target="p_b").edge_id
        != GraphEdge(source="p_a", target="p_c").edge_id
    )


def test_self_loops_are_rejected() -> None:
    with pytest.raises(ValidationError, match="self-loops"):
        GraphEdge(source="p_a", target="p_a")


def test_edge_provenance_defaults_to_inferred() -> None:
    """An edge is a guess until a stage says otherwise; defaulting the other way
    would let inference render as fact."""
    assert GraphEdge(source="p_a", target="p_b").provenance is Provenance.INFERRED


def test_observed_edge_keeps_its_provenance_through_a_round_trip() -> None:
    edge = GraphEdge(source="p_z", target="p_a", provenance=Provenance.OBSERVED)
    back = GraphEdge.model_validate_json(edge.model_dump_json())
    assert back.provenance is Provenance.OBSERVED
    assert (back.source, back.target) == ("p_a", "p_z")


def test_orientation_is_applied_when_parsing_a_reversed_edge() -> None:
    back = GraphEdge.model_validate({"source": "p_z", "target": "p_a"})
    assert (back.source, back.target) == ("p_a", "p_z")


# ---------------------------------------------------------------------------
# EvidenceItem
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "direct_employment",
        "past_employment",
        "shared_employer",
        "shared_school",
        "cluster_membership",
        "title_signal",
        "path_proximity",
    ],
)
def test_observed_provenance_is_only_valid_for_observed_mutuals(kind: str) -> None:
    with pytest.raises(ValidationError, match="observed_mutual"):
        EvidenceItem(kind=kind, detail="d", provenance=Provenance.OBSERVED)


def test_observed_mutual_may_be_observed() -> None:
    item = EvidenceItem(kind="observed_mutual", detail="d", provenance=Provenance.OBSERVED)
    assert item.provenance is Provenance.OBSERVED


def test_evidence_defaults_to_inferred() -> None:
    assert EvidenceItem(kind="shared_school", detail="d").provenance is Provenance.INFERRED


def test_observed_provenance_rule_also_applies_when_parsing_json() -> None:
    payload = '{"kind": "shared_employer", "detail": "d", "provenance": "observed"}'
    with pytest.raises(ValidationError, match="observed_mutual"):
        EvidenceItem.model_validate_json(payload)


def test_unknown_evidence_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EvidenceItem(kind="vibes", detail="d")


# ---------------------------------------------------------------------------
# ScoredPerson
# ---------------------------------------------------------------------------


def test_nonzero_score_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        ScoredPerson(person_id="p_1", full_name="Ada", score=1.0)


def test_negative_score_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        ScoredPerson(person_id="p_1", full_name="Ada", score=-1.0)


def test_zero_score_without_evidence_is_allowed() -> None:
    person = ScoredPerson(person_id="p_1", full_name="Ada", score=0.0)
    assert person.evidence == ()
    assert person.has_observed_evidence is False


def test_evidence_requirement_also_applies_when_parsing_json() -> None:
    payload = '{"person_id": "p_1", "full_name": "Ada", "score": 4.0, "evidence": []}'
    with pytest.raises(ValidationError, match="evidence"):
        ScoredPerson.model_validate_json(payload)


def test_has_observed_evidence_is_true_only_with_an_observed_item() -> None:
    inferred = EvidenceItem(kind="shared_employer", detail="d")
    observed = EvidenceItem(kind="observed_mutual", detail="d", provenance=Provenance.OBSERVED)
    only_inferred = ScoredPerson(person_id="p_1", full_name="Ada", score=1.0, evidence=(inferred,))
    mixed = ScoredPerson(person_id="p_1", full_name="Ada", score=1.0, evidence=(inferred, observed))
    assert only_inferred.has_observed_evidence is False
    assert mixed.has_observed_evidence is True


def test_score_components_total_sums_and_rounds() -> None:
    assert ScoreComponents().total == 0.0
    assert ScoreComponents(direct=6.0, alumni=1.5, proximity=2.0).total == 9.5
    assert ScoreComponents(direct=0.1, alumni=0.2).total == 0.3


# ---------------------------------------------------------------------------
# MutualObservation
# ---------------------------------------------------------------------------


def test_bridge_ids_are_sorted_and_deduped_on_construction() -> None:
    obs = MutualObservation(target_person_id="t_1", bridge_person_ids=("c", "a", "b", "a"))
    assert obs.bridge_person_ids == ("a", "b", "c")


def test_bridge_ids_are_sorted_and_deduped_when_parsed() -> None:
    payload = '{"target_person_id": "t_1", "bridge_person_ids": ["c", "a", "a"]}'
    assert MutualObservation.model_validate_json(payload).bridge_person_ids == ("a", "c")


@pytest.mark.parametrize(
    ("bridges", "stated", "expected"),
    [
        (("a", "b", "c"), 5, True),
        (("a", "b", "c"), 3, False),
        (("a", "b", "c"), 2, False),
        ((), 1, True),
        ((), 0, False),
        (("a",), None, False),
        ((), None, False),
    ],
)
def test_truncated_compares_recorded_bridges_against_stated_count(
    bridges: tuple[str, ...], stated: int | None, expected: bool
) -> None:
    obs = MutualObservation(target_person_id="t_1", bridge_person_ids=bridges, stated_count=stated)
    assert obs.truncated is expected


def test_dedupe_happens_before_the_truncation_check() -> None:
    """Three names, two distinct: the reading really is short of the stated count."""
    obs = MutualObservation(
        target_person_id="t_1", bridge_person_ids=("a", "a", "b"), stated_count=3
    )
    assert obs.bridge_person_ids == ("a", "b")
    assert obs.truncated is True


def test_complete_defaults_to_true_and_is_independent_of_truncated() -> None:
    """``complete`` is the human's claim; ``truncated`` is arithmetic on counts.

    They are not cross-validated, so a stage may not infer either from the other:
    absence from a truncated list is not evidence of absence.
    """
    assert MutualObservation(target_person_id="t_1").complete is True
    contradictory = MutualObservation(
        target_person_id="t_1", bridge_person_ids=("a",), stated_count=9, complete=True
    )
    assert contradictory.complete is True
    assert contradictory.truncated is True


def test_observation_provenance_is_pinned_to_observed() -> None:
    assert MutualObservation(target_person_id="t_1").provenance is Provenance.OBSERVED
    with pytest.raises(ValidationError):
        MutualObservation(target_person_id="t_1", provenance=Provenance.INFERRED)


def test_negative_stated_count_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MutualObservation(target_person_id="t_1", stated_count=-1)


# ---------------------------------------------------------------------------
# Person / TargetPerson / Org construction
# ---------------------------------------------------------------------------


def test_person_make_derives_slug_and_full_name() -> None:
    person = Person.make(
        "Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/ada-lovelace/"
    )
    assert person.linkedin_slug == "ada-lovelace"
    assert person.full_name == "Ada Lovelace"
    assert person.person_id == stable_id_exact("p", "ada-lovelace")


def test_person_make_accepts_an_explicit_full_name() -> None:
    person = Person.make("Ada", "Lovelace", full_name="Augusta Ada King")
    assert person.full_name == "Augusta Ada King"


def test_person_make_falls_back_to_email_then_name() -> None:
    by_email = Person.make("Ada", "Lovelace", email="ada@example.com")
    by_name = Person.make("Ada", "Lovelace")
    assert by_email.person_id == stable_id("p", "ada@example.com")
    assert by_name.person_id == stable_id("p", "Ada Lovelace")
    assert by_email.person_id != by_name.person_id


def test_person_id_ignores_name_case_and_padding() -> None:
    assert Person.make("Ada", "Lovelace").person_id == Person.make("  ADA ", "lovelace ").person_id


def test_slug_beats_the_name_fallback() -> None:
    a = Person.make("Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/x")
    b = Person.make("Grace", "Hopper", linkedin_url="https://www.linkedin.com/in/x")
    assert a.person_id == b.person_id


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/ada-lovelace",
        "https://www.linkedin.com/in/ada-lovelace/",
        "https://www.linkedin.com/in/ada-lovelace?trk=contacts",
        "https://www.linkedin.com/in/ada-lovelace/?trk=contacts&x=1",
    ],
)
def test_url_variants_of_one_profile_collapse_to_one_id(url: str) -> None:
    person = Person.make("Ada", "Lovelace", linkedin_url=url)
    assert person.linkedin_slug == "ada-lovelace"
    assert person.linkedin_url == "https://www.linkedin.com/in/ada-lovelace"
    assert person.person_id == stable_id_exact("p", "ada-lovelace")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.linkedin.com/in/ada?trk=x", "https://www.linkedin.com/in/ada"),
        ("https://www.linkedin.com/in/ada/", "https://www.linkedin.com/in/ada"),
        ("https://www.linkedin.com/in/ada/?a=1&b=2", "https://www.linkedin.com/in/ada"),
        ("https://www.linkedin.com/in/ada", "https://www.linkedin.com/in/ada"),
        ("https://www.linkedin.com/pub/ada/1/2/3", "https://www.linkedin.com/pub/ada/1/2/3"),
        (None, None),
    ],
)
def test_strip_query_behaves_identically_on_person_and_target_person(
    raw: str | None, expected: str | None
) -> None:
    """The asymmetry this replaced was a determinism trap: the same profile
    round-tripped differently depending on which model held it."""
    person = Person(person_id="p_1", full_name="Ada", linkedin_url=raw)
    target = TargetPerson(
        target_person_id="t_1", full_name="Ada", firm=TargetFirm.JANE_STREET, linkedin_url=raw
    )
    assert person.linkedin_url == expected
    assert target.linkedin_url == expected
    assert person.linkedin_url == target.linkedin_url


def test_target_person_make_derives_slug_and_strips_query() -> None:
    target = TargetPerson.make(
        "Grace Hopper",
        TargetFirm.CITADEL_LLC,
        linkedin_url="https://www.linkedin.com/in/grace-hopper/?trk=x",
    )
    assert target.linkedin_slug == "grace-hopper"
    assert target.linkedin_url == "https://www.linkedin.com/in/grace-hopper"
    assert target.target_person_id == stable_id_exact("t", "grace-hopper", "citadel_llc")


def test_target_person_id_separates_the_two_citadels() -> None:
    llc = TargetPerson.make("Grace Hopper", TargetFirm.CITADEL_LLC)
    sec = TargetPerson.make("Grace Hopper", TargetFirm.CITADEL_SECURITIES)
    assert llc.target_person_id != sec.target_person_id


def test_target_person_name_fallback_folds_case() -> None:
    a = TargetPerson.make("Grace Hopper", TargetFirm.JANE_STREET)
    b = TargetPerson.make("GRACE HOPPER", TargetFirm.JANE_STREET)
    assert a.target_person_id == b.target_person_id == stable_id("t", "Grace Hopper", "jane_street")


def test_org_make_id_folds_case_and_punctuation() -> None:
    assert Org.make("Jane Street").org_id == Org.make("JANE STREET").org_id
    assert Org.make("Jane Street").org_id == stable_id("org", "jane street")
    assert Org.make("Citadel LLC").org_id != Org.make("Citadel Securities").org_id


def test_org_is_target_requires_a_target_firm() -> None:
    with pytest.raises(ValidationError, match="target_firm"):
        Org.make("Jane Street", OrgKind.COMPANY, is_target=True)


def test_org_target_with_firm_is_accepted() -> None:
    org = Org.make(
        "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
    )
    assert org.is_target and org.target_firm is TargetFirm.JANE_STREET


def test_org_may_name_a_firm_without_being_a_target() -> None:
    assert Org.make("Jane Street", target_firm=TargetFirm.JANE_STREET).is_target is False


def test_org_defaults_to_unknown_kind_and_no_target() -> None:
    org = Org.make("Acme Widgets")
    assert org.kind is OrgKind.UNKNOWN
    assert org.is_target is False and org.target_firm is None


def test_person_current_company_prefers_the_current_position() -> None:
    past = Position(company_raw="Old Co", start=y(2010), end=y(2015))
    now = Position(company_raw="Jane Street", start=y(2016), is_current=True)
    assert Person.make("A", "B", positions=(past, now)).current_company == "Jane Street"
    assert Person.make("A", "B", positions=(past,)).current_company == "Old Co"
    assert Person.make("A", "B").current_company is None


def test_position_rejects_an_end_before_its_start() -> None:
    with pytest.raises(ValidationError, match="end precedes start"):
        Position(company_raw="X", start=y(2021), end=y(2019))


def test_position_rejects_current_with_an_end_date() -> None:
    with pytest.raises(ValidationError, match="is_current"):
        Position(company_raw="X", start=y(2019), end=y(2021), is_current=True)


def test_education_rejects_an_end_before_its_start() -> None:
    with pytest.raises(ValidationError, match="end precedes start"):
        Education(school_raw="MIT", start=y(2021), end=y(2019))


def test_position_allows_equal_start_and_end() -> None:
    assert Position(company_raw="X", start=y(2019), end=y(2019)).company_raw == "X"


# ---------------------------------------------------------------------------
# TargetFirm
# ---------------------------------------------------------------------------


def test_there_is_no_bare_citadel_member() -> None:
    """Citadel LLC and Citadel Securities are different companies with different
    people. A bare ``CITADEL`` would let a stage merge them by accident."""
    assert not hasattr(TargetFirm, "CITADEL")
    assert "CITADEL" not in TargetFirm.__members__
    with pytest.raises(ValueError, match="citadel"):
        TargetFirm("citadel")


def test_both_citadel_members_exist_and_are_distinct() -> None:
    assert TargetFirm.CITADEL_LLC.value == "citadel_llc"
    assert TargetFirm.CITADEL_SECURITIES.value == "citadel_securities"
    assert TargetFirm.CITADEL_LLC != TargetFirm.CITADEL_SECURITIES
    assert len(set(TargetFirm)) == 3


def test_target_firm_members_are_exactly_the_three_agreed_firms() -> None:
    assert {f.value for f in TargetFirm} == {"jane_street", "citadel_llc", "citadel_securities"}


def test_target_firm_str_is_its_value_because_ids_are_built_from_it() -> None:
    assert str(TargetFirm.CITADEL_LLC) == "citadel_llc"


# ---------------------------------------------------------------------------
# JSON round-trip sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(INSTANCES))
def test_json_round_trip_is_lossless(name: str) -> None:
    instance = INSTANCES[name]
    back = type(instance).model_validate_json(instance.model_dump_json())
    assert back == instance
    assert back.model_dump_json() == instance.model_dump_json()


@pytest.mark.parametrize("name", sorted(INSTANCES))
def test_python_round_trip_is_lossless(name: str) -> None:
    instance = INSTANCES[name]
    assert type(instance).model_validate(instance.model_dump()) == instance


def test_round_trip_sweep_covers_every_exported_model() -> None:
    """Guard against a model being added to the contract untested."""
    exported = {
        name
        for name in models.__all__
        if isinstance(getattr(models, name), type)
        and issubclass(getattr(models, name), BaseModel)
        and getattr(models, name) is not Base
    }
    assert exported == set(INSTANCES)


def test_scored_person_round_trip_keeps_enum_keyed_per_firm() -> None:
    original = INSTANCES["ScoredPerson"]
    assert isinstance(original, ScoredPerson)
    back = ScoredPerson.model_validate_json(original.model_dump_json())
    assert back.per_firm == {TargetFirm.JANE_STREET: 10.0, TargetFirm.CITADEL_LLC: 0.0}
    assert all(isinstance(k, TargetFirm) for k in back.per_firm)


def test_schema_version_is_pinned() -> None:
    assert SCHEMA_VERSION == "1.0.0"
    assert RunManifest(run_id="r", created_at=datetime(2024, 1, 1)).schema_version == SCHEMA_VERSION
