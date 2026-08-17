"""Shared fixtures for the graph-engine tests.

The T1 golden fixture is canonical and is used everywhere. Its numbers come from
R3 §T1 and are reproduced exactly by the implementation; do not adjust one to make
a test pass.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from interlayer.core.models import Edge, EdgeOrigin, Firm, Member, Target

# --- T1 golden fixture -------------------------------------------------------

GOLDEN_EDGES: tuple[tuple[str, str], ...] = (
    ("m0", "t0"), ("m0", "t1"), ("m1", "t0"), ("m1", "t1"), ("m2", "t0"), ("m2", "t1"),
    ("m3", "t2"), ("m3", "t3"), ("m4", "t2"), ("m4", "t3"), ("m5", "t2"), ("m5", "t3"),
    ("m6", "t4"), ("m6", "t5"), ("m7", "t4"), ("m7", "t5"), ("m8", "t4"), ("m8", "t5"),
    ("m0", "t2"), ("m0", "t4"),  # m0 is the planted cross-pocket bridge
)

GOLDEN_MEMBER_IDS: tuple[str, ...] = tuple(f"m{i}" for i in range(9))
GOLDEN_TARGET_IDS: tuple[str, ...] = tuple(f"t{i}" for i in range(6))

#: R3 T1.5 — exact, including the integer labels leidenalg assigns.
GOLDEN_MEMBERSHIP: dict[str, int] = {
    "m0": 1, "m1": 1, "m2": 1,
    "m3": 2, "m4": 2, "m5": 2,
    "m6": 0, "m7": 0, "m8": 0,
}

GOLDEN_MODULARITY = 0.260000

#: R3 T1.1
GOLDEN_TARGET_DEGREES: dict[str, int] = {"t0": 3, "t1": 3, "t2": 4, "t3": 3, "t4": 4, "t5": 3}

#: R3 T1.7
GOLDEN_RARITY: dict[str, float] = {
    "m0": 1.166667,
    "m1": 0.666667, "m2": 0.666667,
    "m3": 0.583333, "m4": 0.583333, "m5": 0.583333,
    "m6": 0.583333, "m7": 0.583333, "m8": 0.583333,
}
GOLDEN_CONSTRAINT: dict[str, float] = {
    "m0": 0.330729,
    "m1": 0.878906, "m2": 0.878906,
    "m3": 0.781250, "m4": 0.781250, "m5": 0.781250,
    "m6": 0.781250, "m7": 0.781250, "m8": 0.781250,
}


def make_member(member_id: str, **kwargs: object) -> Member:
    return Member(
        member_id=member_id,
        first_name=kwargs.pop("first_name", member_id.upper()),  # type: ignore[arg-type]
        last_name=kwargs.pop("last_name", "Test"),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def make_target(target_id: str, *, harvested: bool = True, firm: Firm = Firm.JANE_STREET) -> Target:
    return Target(target_id=target_id, firm=firm, harvested=harvested)


def golden_members() -> list[Member]:
    return [make_member(m) for m in GOLDEN_MEMBER_IDS]


def golden_targets(harvested: bool = True) -> list[Target]:
    return [make_target(t, harvested=harvested) for t in GOLDEN_TARGET_IDS]


def golden_edge_records() -> list[Edge]:
    return [
        Edge(member_id=m, target_id=t, origin=EdgeOrigin.OBSERVED, confidence=1.0)
        for m, t in GOLDEN_EDGES
    ]


# --- T4 hub-pathology fixture ------------------------------------------------


def hub_fixture(
    *,
    n_pockets: int = 6,
    per_pocket: int = 25,
    targets_per_pocket: int = 2,
    noise: float = 0.12,
    hub_degree: int = 100,
    seed: int = 3,
    with_hub: bool = True,
) -> tuple[list[Member], list[Target], list[Edge], list[int], list[str]]:
    """R3 §T4: 6 planted pockets (12 targets, 25 members each), 12% cross-pocket
    noise, ``default_rng(3)``, plus one hub target joined to 100 members spanning
    all pockets.

    Returns ``(members, targets, edges, ground_truth, hub_member_ids)``.
    """
    rng = np.random.default_rng(seed)
    n_members = n_pockets * per_pocket
    n_targets = n_pockets * targets_per_pocket

    member_ids = [f"m{i:04d}" for i in range(n_members)]
    target_ids = [f"t{i:03d}" for i in range(n_targets)]
    truth = [i // per_pocket for i in range(n_members)]

    pairs: set[tuple[str, str]] = set()
    for i, member_id in enumerate(member_ids):
        pocket = truth[i]
        for j in range(targets_per_pocket):
            pairs.add((member_id, target_ids[pocket * targets_per_pocket + j]))
        if rng.random() < noise:
            pairs.add((member_id, target_ids[int(rng.integers(0, n_targets))]))

    hub_members: list[str] = []
    if with_hub:
        target_ids = [*target_ids, "t_hub"]
        chosen = sorted(rng.choice(n_members, size=hub_degree, replace=False).tolist())
        hub_members = [member_ids[i] for i in chosen]
        for member_id in hub_members:
            pairs.add((member_id, "t_hub"))

    members = [make_member(m) for m in member_ids]
    targets = [make_target(t) for t in target_ids]
    edges = [Edge(member_id=m, target_id=t) for m, t in sorted(pairs)]
    return members, targets, edges, truth, hub_members


def sbm_igraph(
    sizes: Sequence[int], p_in: float, p_out: float, seed: int = 42, planted: bool = False
):
    """A sorted-order igraph built from a networkx SBM / planted-partition graph.

    Ground truth is block order, per R3: ``sum([[i]*s for i,s in enumerate(sizes)], [])``.
    """
    import igraph as ig
    import networkx as nx

    if planted:
        graph = nx.planted_partition_graph(len(sizes), sizes[0], p_in, p_out, seed=seed)
    else:
        probs = [[p_in if i == j else p_out for j in range(len(sizes))] for i in range(len(sizes))]
        graph = nx.stochastic_block_model(sizes, probs, seed=seed)

    truth_by_node = {}
    cursor = 0
    for block, size in enumerate(sizes):
        for _ in range(size):
            truth_by_node[cursor] = block
            cursor += 1

    names = sorted(graph.nodes())
    index = {n: i for i, n in enumerate(names)}
    g = ig.Graph(n=len(names))
    g.vs["name"] = [str(n) for n in names]
    g.add_edges(
        sorted({(min(index[u], index[v]), max(index[u], index[v])) for u, v in graph.edges()})
    )
    return g, [truth_by_node[n] for n in names]
