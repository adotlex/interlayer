"""T6 — inferred edges.

An inferred edge is a guess. It must never be silently mixed with an observation
(PRIV-19), must carry a reason, and must not cross the default confidence gate on
a single weak cue. The coefficients are a documented prior, not a fit, and the
five values in T6.3 are the calibration points that pin them down.
"""

from __future__ import annotations

import pytest

from interlayer.core.models import Edge, EdgeOrigin
from interlayer.graph import analyse
from interlayer.graph.build import build_bipartite, select_edges
from interlayer.graph.inference import Cues, infer_edge, infer_edge_confidence, infer_edges
from test_graph_fixtures import (
    golden_edge_records,
    golden_members,
    golden_targets,
    make_target,
)

# --- T6.1 / T6.2 gating ------------------------------------------------------


def _with_inferred(confidence_cue: Cues, member="m0", target="t3"):
    members = golden_members()
    targets = golden_targets()
    edges = [*golden_edge_records(), infer_edge(member, target, confidence_cue)]
    return members, targets, edges


def test_t6_1_inferred_edges_are_excluded_by_default():
    strong = Cues(
        same_employer=True, tenure_overlap_years=4, employer_size=80,
        same_school=True, school_overlap_years=2, cohort_size=300, same_city=True,
    )
    members, targets, edges = _with_inferred(strong)
    bg = build_bipartite(members, targets, edges)  # include_inferred defaults to False
    assert not bg.graph.has_edge("m0", "t3")
    assert bg.n_edges_dropped_inferred == 1
    assert bg.used_inferred_edges is False


def test_t6_1_default_analyse_reports_no_inferred_use():
    strong = Cues(same_employer=True, tenure_overlap_years=4, employer_size=80,
                  same_school=True, school_overlap_years=2, cohort_size=300, same_city=True)
    members, targets, edges = _with_inferred(strong)
    result = analyse(members, targets, edges)
    assert result.used_inferred_edges is False
    assert result.params["include_inferred"] is False


@pytest.mark.parametrize(
    ("confidence", "should_appear"), [(0.62, True), (0.40, False), (0.50, True), (0.4999, False)]
)
def test_t6_2_min_confidence_gate(confidence, should_appear):
    members = golden_members()
    targets = golden_targets()
    edge = Edge(
        member_id="m0", target_id="t3", origin=EdgeOrigin.INFERRED,
        confidence=confidence, evidence="synthetic",
    )
    bg = build_bipartite(
        members, targets, [*golden_edge_records(), edge],
        include_inferred=True, min_confidence=0.5,
    )
    assert bg.graph.has_edge("m0", "t3") is should_appear
    assert bg.used_inferred_edges is should_appear


def test_t6_2_observed_edges_are_not_subject_to_the_inferred_gate():
    """An observation is an observation; the gate exists for guesses."""
    members = golden_members()
    targets = golden_targets()
    odd = Edge(member_id="m1", target_id="t3", origin=EdgeOrigin.OBSERVED, confidence=0.1)
    bg = build_bipartite(
        members, targets, [*golden_edge_records(), odd],
        include_inferred=True, min_confidence=0.5,
    )
    assert bg.graph.has_edge("m1", "t3")


# --- T6.3 calibration --------------------------------------------------------


T63_CASES = [
    ("no cues", Cues(), 0.047),
    ("same city only", Cues(same_city=True), 0.076),
    (
        "same employer 3y @200",
        Cues(same_employer=True, tenure_overlap_years=3, employer_size=200),
        0.188,
    ),
    (
        "same employer 3y @50,000",
        Cues(same_employer=True, tenure_overlap_years=3, employer_size=50_000),
        0.127,
    ),
    (
        "employer 4y @80 + school 2y @300 + city",
        Cues(
            same_employer=True, tenure_overlap_years=4, employer_size=80,
            same_school=True, school_overlap_years=2, cohort_size=300, same_city=True,
        ),
        0.619,
    ),
]


@pytest.mark.parametrize(
    ("name", "cues", "expected"), T63_CASES, ids=[c[0] for c in T63_CASES]
)
def test_t6_3_calibration_points(name, cues, expected):
    assert infer_edge_confidence(cues) == pytest.approx(expected, abs=0.005)


def test_t6_3_no_single_weak_cue_crosses_the_default_gate():
    """The design constraint the coefficients were chosen to satisfy."""
    singles = [
        Cues(same_city=True),
        Cues(shared_groups=3),
        Cues(same_school=True, school_overlap_years=4, cohort_size=500),
        Cues(same_employer=True, tenure_overlap_years=4, employer_size=1000),
    ]
    for cues in singles:
        assert infer_edge_confidence(cues) < 0.5


# --- T6.4 monotonicity -------------------------------------------------------


def test_t6_4_smaller_employer_gives_strictly_higher_confidence():
    sizes = [3, 10, 50, 80, 200, 1_000, 5_000, 50_000, 500_000]
    values = [
        infer_edge_confidence(
            Cues(same_employer=True, tenure_overlap_years=3, employer_size=s)
        )
        for s in sizes
    ]
    assert values == sorted(values, reverse=True)
    assert all(a > b for a, b in zip(values, values[1:], strict=False))


def test_t6_4_smaller_cohort_gives_strictly_higher_confidence():
    sizes = [10, 100, 300, 1_000, 10_000]
    values = [
        infer_edge_confidence(
            Cues(same_school=True, school_overlap_years=3, cohort_size=s)
        )
        for s in sizes
    ]
    assert all(a > b for a, b in zip(values, values[1:], strict=False))


def test_t6_4_longer_tenure_raises_confidence_and_saturates_at_four_years():
    def at(years: float) -> float:
        return infer_edge_confidence(
            Cues(same_employer=True, tenure_overlap_years=years, employer_size=200)
        )

    rising = [at(y) for y in (1, 2, 3, 4)]
    assert all(a < b for a, b in zip(rising, rising[1:], strict=False))
    # marginal returns beyond "we overlapped for a few years" are negligible
    assert at(10) == pytest.approx(at(4))
    assert at(40) == pytest.approx(at(4))


def test_t6_4_confidence_stays_a_probability():
    everything = Cues(
        same_employer=True, tenure_overlap_years=40, employer_size=2,
        same_school=True, school_overlap_years=40, cohort_size=2,
        same_city=True, city_pop_bucket=3.0, shared_groups=99,
    )
    value = infer_edge_confidence(everything)
    assert 0.0 < value < 1.0


# --- evidence is mandatory ---------------------------------------------------


def test_every_inferred_edge_carries_evidence():
    cases = [c[1] for c in T63_CASES]
    edges = infer_edges((f"m{i}", "t9", c) for i, c in enumerate(cases))
    assert len(edges) == len(cases)
    for edge in edges:
        assert edge.origin is EdgeOrigin.INFERRED
        assert edge.evidence
        assert edge.confidence < 1.0


def test_evidence_names_the_cues_that_fired():
    edge = infer_edge(
        "m0", "t0",
        Cues(same_employer=True, tenure_overlap_years=3, employer_size=200, same_city=True),
    )
    assert "shared employer" in edge.evidence
    assert "same city" in edge.evidence
    assert "200" in edge.evidence
    assert "shared school" not in edge.evidence


def test_infer_edges_is_sorted_and_filterable():
    cues = Cues(same_employer=True, tenure_overlap_years=4, employer_size=50)
    edges = infer_edges([("m9", "t1", cues), ("m0", "t2", cues), ("m0", "t1", cues)])
    assert [(e.member_id, e.target_id) for e in edges] == [
        ("m0", "t1"), ("m0", "t2"), ("m9", "t1")
    ]
    assert infer_edges([("m0", "t0", Cues())], min_confidence=0.5) == ()


# --- T6.5 --------------------------------------------------------------------


def test_t6_5_used_inferred_edges_is_true_only_when_one_entered():
    strong = Cues(
        same_employer=True, tenure_overlap_years=4, employer_size=80,
        same_school=True, school_overlap_years=2, cohort_size=300, same_city=True,
    )
    members, targets, edges = _with_inferred(strong)

    assert analyse(members, targets, edges).used_inferred_edges is False
    assert (
        analyse(members, targets, edges, include_inferred=True).used_inferred_edges is True
    )
    # enabled, but the edge is below the gate: nothing entered, so the flag stays False
    weak = infer_edge("m0", "t3", Cues(same_city=True))
    assert (
        analyse(
            members, targets, [*golden_edge_records(), weak], include_inferred=True
        ).used_inferred_edges
        is False
    )


def test_t6_5_inferred_edges_can_change_the_result():
    """Run both ways and the delta is visible — that is the point of the flag."""
    strong = Cues(
        same_employer=True, tenure_overlap_years=4, employer_size=80,
        same_school=True, school_overlap_years=2, cohort_size=300, same_city=True,
    )
    members, targets, edges = _with_inferred(strong, member="m6", target="t0")
    without = analyse(members, targets, edges)
    with_inferred = analyse(members, targets, edges, include_inferred=True)
    assert without.fingerprint != with_inferred.fingerprint
    reach_without = {b.member_id: b.reach for b in without.brokerage}
    reach_with = {b.member_id: b.reach for b in with_inferred.brokerage}
    assert reach_with["m6"] == reach_without["m6"] + 1


def test_select_edges_counts_every_drop_reason():
    members = golden_members()
    targets = [*golden_targets(), make_target("t_cold", harvested=False)]
    edges = [
        *golden_edge_records(),
        *golden_edge_records()[:2],  # duplicates
        Edge(member_id="m0", target_id="t_cold"),  # unharvested
        Edge(member_id="m0", target_id="t_unknown"),  # unknown target
        Edge(member_id="m_unknown", target_id="t0"),  # unknown member
        infer_edge("m1", "t2", Cues(same_city=True)),  # inferred, below gate
    ]
    _, counts = select_edges(edges, targets, members)
    assert counts["duplicate"] == 2
    assert counts["unharvested"] == 1
    assert counts["unknown"] == 2
    assert counts["inferred"] == 1

    _, counts_on = select_edges(edges, targets, members, include_inferred=True)
    assert counts_on["inferred"] == 0
    assert counts_on["confidence"] == 1
