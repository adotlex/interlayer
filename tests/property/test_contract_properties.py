"""Property tests for the shared contract.

Example-based tests check the cases someone thought of. These check the laws the
rest of the pipeline silently relies on: that normalisation settles after one
pass, that an id is a function of its inputs alone, that co-tenure is a genuine
symmetric relation, and that anything a stage writes can be read back by the
next one.
"""

from __future__ import annotations

import string
from typing import Any

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Base,
    EvidenceItem,
    GraphEdge,
    MutualObservation,
    Org,
    OrgKind,
    Person,
    Provenance,
    ScoreComponents,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
    normalize_key,
    stable_id,
    stable_id_exact,
)

# Hypothesis runs alongside six other agents' suites; a wall-clock deadline
# would make these flaky rather than informative.
props = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])

ID_CHARS = string.ascii_letters + string.digits + "_-"
# Characters whose upper/lower really are pure case mappings.
CASED_CHARS = (
    string.ascii_letters + string.digits + " .,-&+" + "\u00e9\u00c9\u00fc\u00dc\u00f1\u00d1"
)
ids = st.text(alphabet=ID_CHARS, min_size=1, max_size=24)
names = st.text(min_size=0, max_size=40)


@st.composite
def approx_dates(draw: st.DrawFn) -> ApproxDate:
    """Calendar-valid coarse dates.

    ``day`` stops at 28 on purpose: the model accepts 2019-02-30 and only fails
    later in ``to_date()``, which ``test_models`` covers directly. Generating it
    here would just re-report that one bug in every property.
    """
    year = draw(st.integers(min_value=1900, max_value=2100))
    month = draw(st.none() | st.integers(min_value=1, max_value=12))
    day = None if month is None else draw(st.none() | st.integers(min_value=1, max_value=28))
    return ApproxDate(year=year, month=month, day=day)


@st.composite
def affiliations(draw: st.DrawFn, *, org: str | None = None, ordered: bool = True) -> Affiliation:
    start = draw(st.none() | approx_dates())
    end = draw(st.none() | approx_dates())
    if ordered and start is not None and end is not None:
        start, end = sorted((start, end), key=lambda d: d.span_start)
    return Affiliation(
        person_id=draw(ids),
        org_id=org if org is not None else draw(ids),
        kind=draw(st.sampled_from(list(AffiliationKind))),
        start=start,
        end=end,
        weight=draw(st.floats(min_value=0.0, max_value=1e6, allow_nan=False)),
    )


@st.composite
def evidence_items(draw: st.DrawFn) -> EvidenceItem:
    kind = draw(
        st.sampled_from(
            [
                "observed_mutual",
                "direct_employment",
                "past_employment",
                "shared_employer",
                "shared_school",
                "cluster_membership",
                "title_signal",
                "path_proximity",
            ]
        )
    )
    observed = kind == "observed_mutual" and draw(st.booleans())
    return EvidenceItem(
        kind=kind,
        detail=draw(names),
        contribution=draw(st.floats(min_value=-100, max_value=100, allow_nan=False)),
        org_id=draw(st.none() | ids),
        via_person_id=draw(st.none() | ids),
        hops=draw(st.none() | st.integers(min_value=0, max_value=6)),
        provenance=Provenance.OBSERVED if observed else Provenance.INFERRED,
    )


@st.composite
def scored_people(draw: st.DrawFn) -> ScoredPerson:
    evidence = tuple(draw(st.lists(evidence_items(), max_size=4)))
    score = draw(st.floats(min_value=-50, max_value=50, allow_nan=False)) if evidence else 0.0
    return ScoredPerson(
        person_id=draw(ids),
        full_name=draw(names),
        score=score,
        rank=draw(st.integers(min_value=0, max_value=1000)),
        components=ScoreComponents(
            direct=draw(st.floats(min_value=0, max_value=10, allow_nan=False))
        ),
        per_firm=draw(
            st.dictionaries(
                st.sampled_from(list(TargetFirm)),
                st.floats(min_value=0, max_value=10, allow_nan=False),
            )
        ),
        evidence=evidence,
    )


@st.composite
def graph_edges(draw: st.DrawFn) -> GraphEdge:
    source = draw(ids)
    target = draw(ids)
    assume(source != target)
    return GraphEdge(
        source=source,
        target=target,
        weight=draw(st.floats(min_value=0, max_value=1e6, allow_nan=False)),
        shared_org_ids=tuple(draw(st.lists(ids, max_size=3))),
        provenance=draw(st.sampled_from(list(Provenance))),
    )


@st.composite
def people(draw: st.DrawFn) -> Person:
    return Person.make(
        draw(names),
        draw(names),
        linkedin_url=draw(st.none() | st.builds(lambda s: f"https://www.linkedin.com/in/{s}", ids)),
        headline=draw(st.none() | names),
    )


@st.composite
def orgs(draw: st.DrawFn) -> Org:
    firm = draw(st.none() | st.sampled_from(list(TargetFirm)))
    return Org.make(
        draw(names),
        draw(st.sampled_from(list(OrgKind))),
        aliases=tuple(draw(st.lists(names, max_size=3))),
        is_target=firm is not None and draw(st.booleans()),
        target_firm=firm,
    )


@st.composite
def observations(draw: st.DrawFn) -> MutualObservation:
    return MutualObservation(
        target_person_id=draw(ids),
        bridge_person_ids=tuple(draw(st.lists(ids, max_size=8))),
        stated_count=draw(st.none() | st.integers(min_value=0, max_value=20)),
        complete=draw(st.booleans()),
    )


@st.composite
def target_people(draw: st.DrawFn) -> TargetPerson:
    return TargetPerson.make(
        draw(names),
        draw(st.sampled_from(list(TargetFirm))),
        linkedin_url=draw(st.none() | st.builds(lambda s: f"https://www.linkedin.com/in/{s}", ids)),
    )


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------


@props
@given(
    model=st.one_of(
        approx_dates(),
        affiliations(),
        evidence_items(),
        graph_edges(),
        observations(),
        orgs(),
        people(),
        scored_people(),
        target_people(),
    )
)
def test_any_valid_model_survives_a_json_round_trip(model: Base) -> None:
    """JSONL between stages is the only channel; anything unwritable is a break."""
    back = type(model).model_validate_json(model.model_dump_json())
    assert back == model
    assert back.model_dump_json() == model.model_dump_json()


@props
@given(model=st.one_of(approx_dates(), affiliations(), graph_edges(), people(), scored_people()))
def test_serialisation_is_stable_under_repetition(model: Base) -> None:
    """Byte-identical artifacts for the same input: dumping twice must agree."""
    first = model.model_dump_json()
    assert first == model.model_dump_json()
    assert type(model).model_validate_json(first).model_dump_json() == first


# ---------------------------------------------------------------------------
# normalize_key
# ---------------------------------------------------------------------------


@props
@given(raw=st.text())
def test_normalize_key_is_idempotent(raw: str) -> None:
    """Two stages normalising a different number of times must still agree."""
    once = normalize_key(raw)
    assert normalize_key(once) == once


@props
@given(raw=st.text())
def test_normalize_key_output_is_a_tidy_comparison_key(raw: str) -> None:
    key = normalize_key(raw)
    assert key == key.strip()
    assert "  " not in key
    assert key == key.casefold()
    assert "\n" not in key and "\t" not in key


@props
@given(raw=st.text(alphabet=CASED_CHARS))
def test_normalize_key_is_case_insensitive(raw: str) -> None:
    """Over the alphabet real names and employer strings are written in.

    Not over all of Unicode: ``str.upper()`` on a lone combining mark such as
    U+0345 yields a base letter, so upper/lower are not pure case mappings
    everywhere and the property would be testing Python, not this fold.
    """
    assert normalize_key(raw.upper()) == normalize_key(raw.lower()) == normalize_key(raw)


@props
@given(raw=st.text(), pad=st.text(alphabet=" \t\n", max_size=4))
def test_normalize_key_ignores_surrounding_whitespace(raw: str, pad: str) -> None:
    assert normalize_key(f"{pad}{raw}{pad}") == normalize_key(raw)


# ---------------------------------------------------------------------------
# stable_id
# ---------------------------------------------------------------------------


@props
@given(kind=st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=5), part=st.text())
def test_stable_id_is_a_pure_function_of_its_inputs(kind: str, part: str) -> None:
    assert stable_id(kind, part) == stable_id(kind, part)
    assert stable_id_exact(kind, part) == stable_id_exact(kind, part)


@props
@given(a=st.text(), b=st.text())
def test_stable_id_separates_anything_normalisation_keeps_apart(a: str, b: str) -> None:
    assume(normalize_key(a) != normalize_key(b))
    assert stable_id("p", a) != stable_id("p", b)


@props
@given(a=st.text())
def test_stable_id_depends_only_on_the_normalised_key(a: str) -> None:
    """Hashing a string and hashing its key must agree, or an id changes
    depending on how many times a stage normalised on the way in."""
    assert stable_id("p", a) == stable_id("p", normalize_key(a))


@props
@given(raw=st.text(alphabet=CASED_CHARS), pad=st.text(alphabet=" \t\n", max_size=3))
def test_case_and_padding_never_change_a_folded_id(raw: str, pad: str) -> None:
    assert stable_id("p", f"{pad}{raw.upper()}{pad}") == stable_id("p", raw.lower())


@props
@given(a=st.text(), b=st.text())
def test_exact_ids_separate_anything_that_differs_verbatim(a: str, b: str) -> None:
    """Opaque identifiers: case, and everything else, is meaning."""
    assume(a != b)
    assert stable_id_exact("p", a) != stable_id_exact("p", b)


@props
@given(urn=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=20))
def test_case_variants_of_a_urn_never_collapse(urn: str) -> None:
    assume(urn.upper() != urn.lower())
    assert stable_id_exact("p", urn.upper()) != stable_id_exact("p", urn.lower())
    assert stable_id("p", urn.upper()) == stable_id("p", urn.lower())


@props
@given(kind=st.sampled_from(["p", "org", "aff", "t", "e"]), part=st.text())
def test_stable_id_is_prefixed_by_its_kind(kind: str, part: str) -> None:
    assert stable_id(kind, part).startswith(f"{kind}_")
    assert stable_id_exact(kind, part).startswith(f"{kind}_exact_")
    assert stable_id(kind, part) != stable_id_exact(kind, part)


@props
@given(first=names, last=names, slug=st.none() | ids)
def test_person_ids_are_deterministic(first: str, last: str, slug: str | None) -> None:
    url = None if slug is None else f"https://www.linkedin.com/in/{slug}"
    a = Person.make(first, last, linkedin_url=url)
    b = Person.make(first, last, linkedin_url=url)
    assert a.person_id == b.person_id
    assert a == b


@props
@given(first=names, last=names, slug=ids)
def test_person_ids_follow_the_slug_not_the_name(first: str, last: str, slug: str) -> None:
    url = f"https://www.linkedin.com/in/{slug}"
    assert (
        Person.make(first, last, linkedin_url=url).person_id
        == Person.make("Someone", "Else", linkedin_url=url).person_id
        == stable_id_exact("p", slug)
    )


@props
@given(slug=ids)
def test_trailing_slashes_and_query_strings_never_change_a_person_id(slug: str) -> None:
    base = f"https://www.linkedin.com/in/{slug}"
    variants = [base, f"{base}/", f"{base}?trk=x", f"{base}/?trk=x&y=2"]
    assert len({Person.make("A", "B", linkedin_url=v).person_id for v in variants}) == 1


# ---------------------------------------------------------------------------
# Affiliation.overlaps
# ---------------------------------------------------------------------------


@props
@given(a=affiliations(ordered=False), b=affiliations(ordered=False))
def test_overlaps_is_symmetric(a: Affiliation, b: Affiliation) -> None:
    assert a.overlaps(b) == b.overlaps(a)


@props
@given(a=affiliations(org="org_shared"), b=affiliations(org="org_shared"))
def test_overlaps_is_symmetric_within_one_org(a: Affiliation, b: Affiliation) -> None:
    assert a.overlaps(b) == b.overlaps(a)


@props
@given(a=affiliations())
def test_overlaps_is_reflexive_for_chronological_affiliations(a: Affiliation) -> None:
    """Restricted to start<=end because nothing stops the model holding the
    reverse; ``test_models.test_affiliation_end_may_not_precede_start`` covers
    that gap and the loss of reflexivity it causes."""
    assert a.overlaps(a)


@props
@given(a=affiliations(org="org_a"), b=affiliations(org="org_b"))
def test_different_orgs_never_overlap(a: Affiliation, b: Affiliation) -> None:
    assert not a.overlaps(b)
    assert not b.overlaps(a)


@props
@given(a=affiliations(org="shared"), b=affiliations(org="shared"))
def test_overlap_agrees_with_interval_intersection(a: Affiliation, b: Affiliation) -> None:
    a0 = a.start.span_start if a.start else 0
    a1 = a.end.span_end if a.end else 99_999_999
    b0 = b.start.span_start if b.start else 0
    b1 = b.end.span_end if b.end else 99_999_999
    assert a.overlaps(b) == (max(a0, b0) <= min(a1, b1))


@props
@given(a=affiliations(org="shared"), b=affiliations(org="shared"))
def test_an_open_ended_stint_absorbs_everything_after_its_start(
    a: Affiliation, b: Affiliation
) -> None:
    ongoing = a.model_copy(update={"end": None})
    assume(b.end is None or a.start is None or a.start.span_start <= b.end.span_end)
    assert ongoing.overlaps(b)


@props
@given(d=approx_dates())
def test_a_coarse_date_span_is_never_inverted(d: ApproxDate) -> None:
    assert d.span_start <= d.span_end
    assert d.span_start <= d.sort_key + 101
    assert str(d).startswith(f"{d.year:04d}")


@props
@given(d=approx_dates())
def test_a_date_always_overlaps_a_stint_that_spans_it(d: ApproxDate) -> None:
    """The regression, generalised: a coarse date is a period, not a moment."""
    point = Affiliation(person_id="p", org_id="o", kind=AffiliationKind.EMPLOYMENT, start=d, end=d)
    covering = Affiliation(
        person_id="q",
        org_id="o",
        kind=AffiliationKind.EMPLOYMENT,
        start=ApproxDate(year=d.year),
        end=ApproxDate(year=d.year),
    )
    assert point.overlaps(covering)
    assert covering.overlaps(point)


# ---------------------------------------------------------------------------
# GraphEdge
# ---------------------------------------------------------------------------


@props
@given(a=ids, b=ids)
def test_edge_orientation_is_idempotent_and_order_free(a: str, b: str) -> None:
    assume(a != b)
    forward = GraphEdge(source=a, target=b)
    backward = GraphEdge(source=b, target=a)
    again = GraphEdge(source=forward.source, target=forward.target)
    assert forward.source < forward.target
    assert (forward.source, forward.target) == (again.source, again.target)
    assert forward == backward == again
    assert forward.edge_id == backward.edge_id
    assert hash(forward) == hash(backward)


@props
@given(edge=graph_edges())
def test_reparsing_an_edge_never_flips_it(edge: GraphEdge) -> None:
    back = GraphEdge.model_validate_json(edge.model_dump_json())
    assert (back.source, back.target) == (edge.source, edge.target)
    assert back.provenance is edge.provenance
    assert back.edge_id == edge.edge_id


@props
@given(a=ids, b=ids, c=ids)
def test_edge_ids_distinguish_distinct_pairs(a: str, b: str, c: str) -> None:
    assume(len({a, b, c}) == 3)
    assert GraphEdge(source=a, target=b).edge_id != GraphEdge(source=a, target=c).edge_id


# ---------------------------------------------------------------------------
# other invariants
# ---------------------------------------------------------------------------


@props
@given(obs=observations())
def test_bridge_ids_are_always_sorted_and_unique(obs: MutualObservation) -> None:
    assert list(obs.bridge_person_ids) == sorted(set(obs.bridge_person_ids))


@props
@given(obs=observations())
def test_truncation_is_pure_arithmetic_on_the_stated_count(obs: MutualObservation) -> None:
    if obs.stated_count is None:
        assert obs.truncated is False
    else:
        assert obs.truncated == (len(obs.bridge_person_ids) < obs.stated_count)


@props
@given(person=scored_people())
def test_a_scored_person_always_carries_its_evidence(person: ScoredPerson) -> None:
    assert person.score == 0.0 or person.evidence
    assert person.has_observed_evidence == any(
        e.provenance is Provenance.OBSERVED for e in person.evidence
    )


@props
@given(
    values=st.lists(
        st.floats(min_value=-1e3, max_value=1e3, allow_nan=False), min_size=5, max_size=5
    )
)
def test_score_components_total_is_the_sum(values: list[float]) -> None:
    direct, alumni, proximity, cluster, title = values
    components = ScoreComponents(
        direct=direct, alumni=alumni, proximity=proximity, cluster=cluster, title=title
    )
    assert components.total == round(sum(values), 6)


@props
@given(item=evidence_items())
def test_observed_evidence_is_always_a_mutual_reading(item: EvidenceItem) -> None:
    """Inferred may never be presented as observed, whatever built the item."""
    assert item.provenance is not Provenance.OBSERVED or item.kind == "observed_mutual"


@props
@given(org=orgs())
def test_a_target_org_always_names_its_firm(org: Org) -> None:
    assert not org.is_target or org.target_firm is not None


@props
@given(model=st.one_of(approx_dates(), affiliations(), graph_edges(), people(), orgs()))
def test_frozen_models_are_hashable_and_equal_by_value(model: Base) -> None:
    twin: Any = type(model).model_validate(model.model_dump())
    assert twin == model
    assert hash(twin) == hash(model)
    assert len({model, twin}) == 1
