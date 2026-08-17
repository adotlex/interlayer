"""T7 — cluster labelling, and the sentinel bug it exists to prevent.

R3's prototype emitted ``"engineer wharton"`` as a top term: a bigram that opened
in one member's job title and closed in another member's school. T7.3 is the
regression test. Note that the ``" | "`` join alone does *not* fix it — sklearn's
default token pattern discards ``|``, so the bigram window closes straight over
the join. The field-aware analyzer is the actual fix.
"""

from __future__ import annotations

import pytest

from interlayer.core.models import Member
from interlayer.graph import analyse
from interlayer.graph.label import (
    SENTINEL,
    cluster_documents,
    default_attributes,
    label_clusters,
    top_shared_targets,
)
from test_graph_fixtures import (
    golden_edge_records,
    golden_members,
    golden_targets,
    make_member,
)

# --- helpers -----------------------------------------------------------------

#: (member_id, company, position, school, city)
RICH_ATTRIBUTES: dict[str, tuple[str, str, str, str]] = {}


def rich(member_id: str, company: str, position: str, school: str = "", city: str = "") -> Member:
    RICH_ATTRIBUTES[member_id] = (company, position, school, city)
    return make_member(member_id, company_raw=company, position=position)


def rich_attributes(member: Member) -> tuple[str, ...]:
    """Attribute extractor exposing school and city, which the frozen model omits."""
    values = RICH_ATTRIBUTES.get(member.member_id)
    if values is None:
        return default_attributes(member)
    return tuple(v for v in values if v)


@pytest.fixture
def three_clusters():
    RICH_ATTRIBUTES.clear()
    members = [
        rich("a0", "Google", "SRE", "", "infra"),
        rich("a1", "Google", "SRE Engineer", "", "infra"),
        rich("a2", "Google", "Engineer", "", "infra"),
        rich("b0", "Acme", "Analyst", "Wharton", "2016"),
        rich("b1", "Beta Capital", "Engineer", "Wharton", "2016"),
        rich("b2", "Gamma", "Manager", "Wharton MBA", "2016"),
        rich("c0", "Jane Street", "Quant", "", "NYC"),
        rich("c1", "Citadel", "Quant", "", "NYC"),
        rich("c2", "Jane Street", "Trader", "", "NYC"),
    ]
    by_id = {m.member_id: m for m in members}
    clusters = {0: ["a0", "a1", "a2"], 1: ["b0", "b1", "b2"], 2: ["c0", "c1", "c2"]}
    return by_id, clusters


# --- T7.1 --------------------------------------------------------------------


def test_t7_1_labels_pick_the_distinguishing_term(three_clusters):
    by_id, clusters = three_clusters
    labels = label_clusters(by_id, clusters, attributes=rich_attributes)
    assert "google" in labels[0]
    assert "wharton" in labels[1]
    assert "nyc" in labels[2]


def test_t7_1_idf_suppresses_the_term_everyone_shares(three_clusters):
    """"engineer" appears in two of three clusters, so it must not be a top term."""
    by_id, clusters = three_clusters
    labels = label_clusters(by_id, clusters, attributes=rich_attributes)
    assert "engineer" not in labels[0].split(", ")
    assert "engineer" not in labels[1].split(", ")


def test_t7_1_top_n_is_respected(three_clusters):
    by_id, clusters = three_clusters
    labels = label_clusters(by_id, clusters, top_n=3, attributes=rich_attributes)
    for label in labels.values():
        assert len(label.split(", ")) <= 3
    one = label_clusters(by_id, clusters, top_n=1, attributes=rich_attributes)
    assert all(", " not in label for label in one.values())


# --- T7.2 --------------------------------------------------------------------


def test_t7_2_a_themeless_cluster_is_labelled_mixed():
    """No repeated attribute term means no coherent theme.

    TF-IDF weights are L2-normalised, so a document of ``n`` equally-weighted
    distinct terms tops out near ``1/sqrt(n)``. Twelve members with four distinct
    attributes each puts the top weight below the 0.15 floor, which is exactly the
    signal the floor is there to catch.
    """
    RICH_ATTRIBUTES.clear()
    words = [
        ("Alpha", "Butcher", "Reed", "Lisbon"), ("Zeta", "Geologist", "Otago", "Perth"),
        ("Orion", "Editor", "Bard", "Dublin"), ("Nimbus", "Pilot", "Embry", "Denver"),
        ("Vertex", "Weaver", "Kyoto", "Osaka"), ("Harbor", "Blower", "Murano", "Venice"),
        ("Quartz", "Cooper", "Leiden", "Utrecht"), ("Basalt", "Farrier", "Uppsala", "Malmo"),
        ("Cinder", "Thatcher", "Aarhus", "Odense"), ("Drifter", "Fletcher", "Tartu", "Riga"),
        ("Ember", "Chandler", "Vilnius", "Kaunas"), ("Fathom", "Cordwainer", "Porto", "Braga"),
    ]
    members = [rich(f"x{i}", *w) for i, w in enumerate(words)]
    by_id = {m.member_id: m for m in members}
    # a second cluster is needed for IDF to exist at all
    other = [rich("y0", "Google", "SRE", "MIT", "NYC"), rich("y1", "Google", "SRE", "MIT", "NYC")]
    by_id.update({m.member_id: m for m in other})
    clusters = {0: [m.member_id for m in members], 1: ["y0", "y1"]}

    labels = label_clusters(by_id, clusters, attributes=rich_attributes)
    assert labels[0].startswith("mixed")
    assert labels[0] == f"mixed (n={len(members)})"
    assert not labels[1].startswith("mixed")


def test_t7_2_a_cluster_with_no_attributes_at_all_is_mixed():
    members = [make_member(f"z{i}") for i in range(3)]
    by_id = {m.member_id: m for m in members}
    labels = label_clusters(by_id, {0: [m.member_id for m in members]})
    assert labels[0] == "mixed (n=3)"


# --- T7.3 the regression test ------------------------------------------------


def test_t7_3_no_label_contains_the_sentinel(three_clusters):
    by_id, clusters = three_clusters
    labels = label_clusters(by_id, clusters, attributes=rich_attributes)
    for label in labels.values():
        assert SENTINEL not in label
        assert "|" not in label


def test_t7_3_no_bigram_straddles_two_attribute_fields(three_clusters):
    """The ``"engineer wharton"`` bug, made into an assertion.

    Every emitted bigram must be reproducible from a single attribute field of a
    single member. Anything else is a hybrid of two unrelated facts.
    """
    by_id, clusters = three_clusters
    labels = label_clusters(by_id, clusters, top_n=10, attributes=rich_attributes)

    legal_bigrams: set[str] = set()
    for member in by_id.values():
        for field in rich_attributes(member):
            tokens = field.lower().split()
            legal_bigrams.update(f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False))

    for label in labels.values():
        for term in label.split(", "):
            if " " in term:
                assert term in legal_bigrams, f"bigram {term!r} straddles a field boundary"


def test_t7_3_the_specific_engineer_wharton_hybrid_never_appears():
    """b1 is an Engineer who went to Wharton — a plain-space join emits the hybrid."""
    RICH_ATTRIBUTES.clear()
    members = [
        rich("b0", "Acme", "Analyst", "Wharton", "2016"),
        rich("b1", "Beta", "Engineer", "Wharton", "2016"),
        rich("c0", "Jane Street", "Quant", "MIT", "NYC"),
    ]
    by_id = {m.member_id: m for m in members}
    labels = label_clusters(by_id, {0: ["b0", "b1"], 1: ["c0"]}, top_n=25,
                            attributes=rich_attributes)
    for label in labels.values():
        assert "engineer wharton" not in label
        assert "analyst wharton" not in label
        assert "2016 beta" not in label


def test_t7_3_documents_are_sentinel_joined(three_clusters):
    by_id, clusters = three_clusters
    docs = cluster_documents(by_id, clusters, attributes=rich_attributes)
    assert SENTINEL in docs[0]
    assert docs[0].split(SENTINEL)[0] == "Google"


# --- T7.4 --------------------------------------------------------------------


def test_t7_4_labelling_is_deterministic(three_clusters):
    by_id, clusters = three_clusters
    runs = {
        tuple(sorted(label_clusters(by_id, clusters, attributes=rich_attributes).items()))
        for _ in range(5)
    }
    assert len(runs) == 1


def test_t7_4_labelling_ignores_member_order(three_clusters):
    by_id, clusters = three_clusters
    forward = label_clusters(by_id, clusters, attributes=rich_attributes)
    reversed_clusters = {k: list(reversed(v)) for k, v in clusters.items()}
    assert label_clusters(by_id, reversed_clusters, attributes=rich_attributes) == forward


# --- top shared targets ------------------------------------------------------


def test_top_shared_targets_ranks_by_member_count_then_id():
    from interlayer.graph.build import build_bipartite

    bg = build_bipartite(golden_members(), golden_targets(), golden_edge_records())
    shared = top_shared_targets(bg, {0: ["m0", "m1", "m2"], 1: ["m3", "m4", "m5"]})
    # pocket A all reach t0 and t1; only m0 additionally reaches t2 and t4
    assert shared[0][:2] == ("t0", "t1")
    assert set(shared[0]) == {"t0", "t1", "t2", "t4"}
    assert shared[1] == ("t2", "t3")


def test_top_shared_targets_caps_at_five():
    from interlayer.core.models import Edge
    from interlayer.graph.build import build_bipartite
    from test_graph_fixtures import make_target

    targets = [make_target(f"t{i}") for i in range(9)]
    edges = [Edge(member_id="m0", target_id=f"t{i}") for i in range(9)]
    bg = build_bipartite([make_member("m0")], targets, edges)
    assert len(top_shared_targets(bg, {0: ["m0"]})[0]) == 5


def test_clusters_in_a_full_result_carry_labels_and_targets():
    result = analyse(golden_members(), golden_targets(), golden_edge_records())
    for cluster in result.clusters:
        assert cluster.label
        assert SENTINEL not in cluster.label
        assert cluster.top_targets
        assert 0.0 <= cluster.stability <= 1.0
