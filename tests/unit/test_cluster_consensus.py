"""Consensus Leiden against planted ground truth, and the determinism it exists for.

Real affiliation data has no ground truth: nobody can say which of two partitions
of a LinkedIn export is *right*. Planted-community benchmarks are the only place
correctness is checkable at all, so the accuracy assertions here are the
load-bearing tests of the whole clustering stage.

``nx.LFR_benchmark_graph`` retries internally and the degree-sequence loop is not
bounded by ``max_iters``; the parameter sets in ``SMALL``/``LARGE`` were probed to
converge in ~0.15s at every mixing level used here, and generation is wrapped in a
wall-clock limit so a future parameter edit fails loudly instead of hanging the
suite.

``numpy``/``scikit-learn`` are test-only dependencies. They are used for ARI/NMI
and nothing else -- the runtime is deliberately numpy-free.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx
import pytest
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score

from interlayer import cluster as cluster_stage
from interlayer.cluster.consensus import (
    ConsensusResult,
    build_igraph,
    consensus_membership,
    leiden_membership,
)
from interlayer.config import Settings
from interlayer.io import secure_dir, write_jsonl
from interlayer.models import GraphEdge

# --- LFR shapes -----------------------------------------------------------
# min_degree/max_degree are given explicitly: `average_degree` sends the
# degree-sequence loop spinning for minutes on these community sizes.
SMALL: dict[str, Any] = {
    "n": 300,
    "tau1": 3,
    "tau2": 1.5,
    "min_degree": 5,
    "max_degree": 30,
    "min_community": 20,
    "max_community": 60,
}
LARGE: dict[str, Any] = {
    "n": 1000,
    "tau1": 3,
    "tau2": 1.5,
    "min_degree": 8,
    "max_degree": 50,
    "min_community": 40,
    "max_community": 120,
}

LFR_SEED = 7
GENERATION_LIMIT_S = 25.0


@contextmanager
def time_limit(seconds: float) -> Iterator[None]:
    """Fail rather than hang if graph generation stops converging.

    ``LFR_benchmark_graph`` is pure Python, so a SIGALRM handler fires between
    bytecodes and unwinds the retry loop. Silently skipped where SIGALRM is
    unavailable or we are not on the main thread; the assertion value is the
    bound, not the mechanism.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _expire(signum: int, frame: object) -> None:
        raise TimeoutError(f"graph generation exceeded {seconds}s")

    try:
        previous = signal.signal(signal.SIGALRM, _expire)
    except ValueError:  # not the main thread
        yield
        return
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class Planted:
    """An LFR graph plus the communities it was generated from."""

    node_ids: tuple[str, ...]
    graph: Any
    truth: tuple[int, ...]
    edges: tuple[tuple[str, str], ...]

    @property
    def n_planted(self) -> int:
        return len(set(self.truth))


def build_planted(shape: dict[str, Any], mu: float, seed: int = LFR_SEED) -> Planted:
    with time_limit(GENERATION_LIMIT_S):
        raw = nx.LFR_benchmark_graph(**shape, mu=mu, seed=seed, max_iters=500)
    graph = nx.Graph(raw)  # LFR returns a MultiGraph
    graph.remove_edges_from(nx.selfloop_edges(graph))  # ... containing self-loops
    truth = {v: min(graph.nodes[v]["community"]) for v in graph}

    order = sorted(graph.nodes())
    position = {node: i for i, node in enumerate(order)}
    node_ids = tuple(f"p{node:05d}" for node in order)
    triples = sorted(
        (min(position[u], position[v]), max(position[u], position[v]), 1.0)
        for u, v in graph.edges()
    )
    return Planted(
        node_ids=node_ids,
        graph=build_igraph(len(order), triples),
        truth=tuple(truth[node] for node in order),
        edges=tuple((node_ids[u], node_ids[v]) for u, v, _ in triples),
    )


PlantedFactory = Callable[..., Planted]


@pytest.fixture(scope="module")
def planted() -> PlantedFactory:
    """Memoised LFR builder; generation is cheap but not free, and mu repeats."""
    cache: dict[tuple[int, float, int], Planted] = {}

    def build(mu: float, *, shape: dict[str, Any] | None = None, seed: int = LFR_SEED) -> Planted:
        chosen = SMALL if shape is None else shape
        key = (chosen["n"], mu, seed)
        if key not in cache:
            cache[key] = build_planted(chosen, mu, seed)
        return cache[key]

    return build


ConsensusFactory = Callable[..., ConsensusResult]


@pytest.fixture(scope="module")
def consensus() -> ConsensusFactory:
    """Memoised consensus runs -- the same (mu, gamma, seed) recurs across tests."""
    cache: dict[tuple[int, int, float, int, int, float], ConsensusResult] = {}

    def run(
        case: Planted,
        *,
        resolution: float = 1.0,
        seed: int = 20240101,
        runs: int = 50,
        threshold: float = 0.5,
    ) -> ConsensusResult:
        key = (case.graph.vcount(), case.graph.ecount(), resolution, seed, runs, threshold)
        if key not in cache:
            cache[key] = consensus_membership(
                case.graph, runs=runs, threshold=threshold, resolution=resolution, seed=seed
            )
        return cache[key]

    return run


def nmi(case: Planted, membership: Sequence[int]) -> float:
    return float(normalized_mutual_info_score(list(case.truth), list(membership)))


def ari(a: Sequence[int], b: Sequence[int]) -> float:
    return float(adjusted_rand_score(list(a), list(b)))


# ===========================================================================
# correctness against planted ground truth
# ===========================================================================


def test_lfr_fixture_is_a_simple_graph_with_the_planted_communities(
    planted: PlantedFactory,
) -> None:
    """Guards the fixture itself: a MultiGraph or self-loops would skew every metric."""
    case = planted(0.1)
    assert case.graph.vcount() == SMALL["n"]
    assert not case.graph.is_directed()
    assert case.graph.ecount() == len(set(case.edges))
    assert case.n_planted == 9  # stable for this shape + seed
    assert all(pair[0] < pair[1] for pair in case.edges)


def test_nmi_against_planted_truth_at_mu_010(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """Easy regime. Anything below 0.90 here means the stage is broken, not noisy."""
    case = planted(0.1)
    score = nmi(case, consensus(case).membership)
    assert score > 0.90, f"NMI {score:.4f} at mu=0.1"


def test_nmi_against_planted_truth_at_mu_030(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """Mid regime -- roughly where an inferred affiliation graph is expected to sit."""
    case = planted(0.3)
    score = nmi(case, consensus(case).membership)
    assert score > 0.80, f"NMI {score:.4f} at mu=0.3"


def test_ari_against_planted_truth_tracks_nmi(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """ARI is the chance-corrected headline metric; NMI is biased up by many small
    clusters, so a high NMI with a collapsed ARI would be a false pass."""
    for mu, floor in ((0.1, 0.90), (0.3, 0.70)):
        case = planted(mu)
        score = ari(case.truth, consensus(case).membership)
        assert score > floor, f"ARI {score:.4f} at mu={mu}"


@pytest.mark.parametrize(("mu", "low", "high"), [(0.1, 7, 14), (0.3, 6, 18)])
def test_recovered_cluster_count_is_near_the_planted_count(
    planted: PlantedFactory, consensus: ConsensusFactory, mu: float, low: int, high: int
) -> None:
    """Under-splitting hides groups; over-splitting shatters them into dust."""
    case = planted(mu)
    found = consensus(case).n_clusters
    assert low <= found <= high, f"{found} clusters vs {case.n_planted} planted at mu={mu}"


def test_degrades_gracefully_at_mu_050(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """At mu=0.5 accuracy is expected to be poor. What is NOT acceptable is the
    Infomap/label-propagation failure mode: silently returning one community,
    which reads to an operator exactly like a real answer."""
    case = planted(0.5)
    result = consensus(case)
    assert result.n_clusters > 1, "collapsed to a single community at mu=0.5"
    sizes = [result.membership.count(c) for c in set(result.membership)]
    assert max(sizes) < 0.5 * case.graph.vcount(), f"one cluster holds {max(sizes)} of 300"
    assert len(result.membership) == case.graph.vcount()


@pytest.mark.slow
def test_nmi_holds_on_a_thousand_node_graph(planted: PlantedFactory) -> None:
    """The SMALL shape has 9 planted communities; 17 is a different regime and the
    resolution limit bites harder there."""
    for mu, floor in ((0.1, 0.90), (0.3, 0.80)):
        case = planted(mu, shape=LARGE)
        result = consensus_membership(
            case.graph, runs=50, threshold=0.5, resolution=1.0, seed=20240101
        )
        score = nmi(case, result.membership)
        assert score > floor, f"NMI {score:.4f} at mu={mu}, n=1000"
        assert 10 <= result.n_clusters <= 30, result.n_clusters


# ===========================================================================
# determinism
# ===========================================================================


def _edges_for(case: Planted) -> list[GraphEdge]:
    return [GraphEdge(source=u, target=v, weight=1.0) for u, v in case.edges]


def _prepared(directory: Path, case: Planted) -> Settings:
    """An artifact dir holding just edges.jsonl, plus settings pointed at it."""
    secure_dir(directory)
    write_jsonl(directory / "edges.jsonl", _edges_for(case))
    return Settings(
        artifact_dir=directory,
        consensus_runs=6,
        resolution=2.0,
        resolution_sweep=(2.0,),
    )


def test_consensus_is_reproducible_for_a_fixed_seed(planted: PlantedFactory) -> None:
    case = planted(0.3)
    first = consensus_membership(case.graph, runs=8, threshold=0.5, resolution=2.0, seed=1)
    second = consensus_membership(case.graph, runs=8, threshold=0.5, resolution=2.0, seed=1)
    assert first.membership == second.membership
    assert (first.iterations, first.converged) == (second.iterations, second.converged)


def test_run_writes_byte_identical_clusters_for_the_same_seed(
    planted: PlantedFactory, tmp_path: Path
) -> None:
    """The whole point of seeding. Two complete stage runs, byte for byte."""
    case = planted(0.3)
    first, second = tmp_path / "first", tmp_path / "second"
    cluster_stage.run(_prepared(first, case))
    cluster_stage.run(_prepared(second, case))

    left = (first / "clusters.jsonl").read_bytes()
    right = (second / "clusters.jsonl").read_bytes()
    assert left == right
    assert left.strip(), "clusters.jsonl is empty; the comparison would be vacuous"
    assert (first / cluster_stage.SWEEP_FILENAME).read_bytes() == (
        second / cluster_stage.SWEEP_FILENAME
    ).read_bytes()


CHILD_SCRIPT = """
import hashlib
import sys
from pathlib import Path

from interlayer import cluster as cluster_stage
from interlayer.config import Settings

target = Path(sys.argv[1])
cluster_stage.run(
    Settings(artifact_dir=target, consensus_runs=6, resolution=2.0, resolution_sweep=(2.0,))
)
print(hashlib.sha256((target / "clusters.jsonl").read_bytes()).hexdigest())
"""


def test_partition_is_identical_across_pythonhashseed_values(
    planted: PlantedFactory, tmp_path: Path
) -> None:
    """Wave 1 saw four distinct set-iteration orders across four PYTHONHASHSEED
    values, so anything derived from a set must be sorted before it decides a
    partition. Only a fresh interpreter can vary the hash seed, hence subprocesses."""
    case = planted(0.3)
    script = tmp_path / "child.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")

    digests: dict[str, str] = {}
    for hash_seed in ("0", "1", "12345"):
        directory = tmp_path / f"hs{hash_seed}"
        _prepared(directory, case)
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        completed = subprocess.run(
            [sys.executable, str(script), str(directory)],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        digests[hash_seed] = completed.stdout.strip().splitlines()[-1]

    assert len(set(digests.values())) == 1, digests
    local = hashlib.sha256((tmp_path / "hs0" / "clusters.jsonl").read_bytes()).hexdigest()
    assert digests["0"] == local


# ===========================================================================
# stability -- why consensus exists at all
# ===========================================================================


def test_consensus_beats_single_run_leiden_stability_at_mu_040(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """The test that justifies the feature's cost.

    Seeding gives *reproducibility*; it does not give *stability*. Two differently
    seeded single Leiden runs at mu=0.4 land on genuinely different partitions, so
    an operator re-running the tool would be shown different groups. Two entirely
    independent consensus ensembles agree far more closely. If this inverts,
    consensus is buying nothing and 50x the runtime.
    """
    case = planted(0.4)
    ensemble_seeds = (1, 9001, 555)

    ensembles = [consensus(case, seed=s).membership for s in ensemble_seeds]
    singles = [leiden_membership(case.graph, resolution=1.0, seed=s) for s in ensemble_seeds]

    pairs = ((0, 1), (0, 2), (1, 2))
    consensus_ari = sum(ari(ensembles[i], ensembles[j]) for i, j in pairs) / len(pairs)
    single_ari = sum(ari(singles[i], singles[j]) for i, j in pairs) / len(pairs)

    assert consensus_ari > single_ari, (
        f"consensus mean pairwise ARI {consensus_ari:.4f} did not beat "
        f"single-run {single_ari:.4f} at mu=0.4"
    )
    assert consensus_ari > 0.80, f"consensus mean pairwise ARI only {consensus_ari:.4f}"
    assert consensus_ari - single_ari > 0.20, (
        f"consensus {consensus_ari:.4f} vs single {single_ari:.4f}: too small a margin "
        "to justify 50 runs"
    )


def test_cross_ensemble_consensus_ari_at_mu_030(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """Three independent ensembles, different base seeds, must agree almost exactly."""
    case = planted(0.3)
    memberships = [consensus(case, seed=s).membership for s in (11, 2027, 88888)]
    scores = [ari(memberships[i], memberships[j]) for i, j in ((0, 1), (0, 2), (1, 2))]
    assert min(scores) > 0.95, f"cross-ensemble ARI {scores} at mu=0.3"


def test_consensus_also_denoises_not_only_stabilises(
    planted: PlantedFactory, consensus: ConsensusFactory
) -> None:
    """Consensus is claimed to improve accuracy, not merely repeatability. Averaged
    over seeds it must not be worse than a single run against planted truth."""
    case = planted(0.4)
    seeds = (1, 9001, 555)
    ensemble = sum(nmi(case, consensus(case, seed=s).membership) for s in seeds) / len(seeds)
    single = sum(
        nmi(case, leiden_membership(case.graph, resolution=1.0, seed=s)) for s in seeds
    ) / len(seeds)
    assert ensemble >= single, f"consensus NMI {ensemble:.4f} < single-run NMI {single:.4f}"


# ===========================================================================
# consensus mechanics and degenerate inputs
# ===========================================================================


def test_consensus_reaches_block_structure_on_a_clean_graph(planted: PlantedFactory) -> None:
    result = consensus_membership(
        planted(0.1).graph, runs=20, threshold=0.5, resolution=1.0, seed=3
    )
    assert result.converged
    assert 1 <= result.iterations <= 10


def test_consensus_rejects_a_zero_run_ensemble() -> None:
    """runs=0 would make every co-association vote vacuous rather than error."""
    graph = build_igraph(3, [(0, 1, 1.0), (1, 2, 1.0)])
    with pytest.raises(ValueError, match="runs must be"):
        consensus_membership(graph, runs=0, threshold=0.5, resolution=1.0, seed=1)


def test_consensus_on_an_empty_graph_is_an_empty_converged_partition() -> None:
    result = consensus_membership(
        build_igraph(0, []), runs=5, threshold=0.5, resolution=1.0, seed=1
    )
    assert result.membership == []
    assert result.converged
    assert result.n_clusters == 0


def test_consensus_on_an_edgeless_graph_leaves_every_node_alone() -> None:
    result = consensus_membership(
        build_igraph(4, []), runs=5, threshold=0.5, resolution=1.0, seed=1
    )
    assert result.n_clusters == 4
    assert result.converged


def test_leiden_on_an_edgeless_graph_skips_the_c_call() -> None:
    assert leiden_membership(build_igraph(4, []), resolution=1.0, seed=1) == [0, 1, 2, 3]
    assert leiden_membership(build_igraph(0, []), resolution=1.0, seed=1) == []


def test_leiden_finds_two_planted_cliques() -> None:
    """A sanity floor that does not depend on LFR at all."""
    left = [(u, v, 1.0) for u in range(5) for v in range(u + 1, 5)]
    right = [(u, v, 1.0) for u in range(5, 10) for v in range(u + 1, 10)]
    graph = build_igraph(10, sorted([*left, *right, (0, 5, 0.01)]))
    membership = leiden_membership(graph, resolution=1.0, seed=42)
    assert len(set(membership)) == 2
    assert len(set(membership[:5])) == 1
    assert len(set(membership[5:])) == 1


def test_unanimity_threshold_only_keeps_pairs_every_run_agreed_on(
    planted: PlantedFactory,
) -> None:
    """threshold=1.0 is the strictest reading of the co-association matrix; it must
    never produce a *coarser* partition than a majority vote."""
    case = planted(0.4)
    strict = consensus_membership(case.graph, runs=20, threshold=1.0, resolution=1.0, seed=5)
    majority = consensus_membership(case.graph, runs=20, threshold=0.5, resolution=1.0, seed=5)
    assert strict.n_clusters >= majority.n_clusters


def test_build_igraph_preserves_weights_and_insertion_order() -> None:
    """leidenalg tie-breaking depends on edge order, so the builder must not sort."""
    triples = [(0, 1, 0.25), (1, 2, 0.5), (0, 2, 0.75)]
    graph = build_igraph(3, triples)
    assert graph.vcount() == 3
    assert [tuple(sorted(e.tuple)) for e in graph.es] == [(0, 1), (1, 2), (0, 2)]
    assert graph.es["weight"] == [0.25, 0.5, 0.75]


def test_membership_is_positional_over_the_original_node_set(planted: PlantedFactory) -> None:
    """Consensus iterates on rebuilt graphs; index i must still mean the same person."""
    case = planted(0.3)
    result = consensus_membership(case.graph, runs=6, threshold=0.5, resolution=1.0, seed=2)
    assert len(result.membership) == len(case.node_ids)
