"""Stage-level tests for :mod:`interlayer.graph.build`.

Covers the contract every stage owes the pipeline -- run in the wrong order and
fail loudly, survive degenerate input, never emit a self-loop, and produce
byte-identical artifacts from the same input -- plus the two numbers this stage
is actually judged on: the surviving weight concentration and the stats file
agreeing with the edge list it describes.
"""

from __future__ import annotations

import json
import os
import random
import stat
import subprocess
import sys
import time
from pathlib import Path

import networkx as nx
import pytest
import yaml

from interlayer.config import Settings
from interlayer.errors import GraphError, StageInputMissingError
from interlayer.graph.build import STATS_FILENAME, build_graph, run
from interlayer.io import write_jsonl
from interlayer.models import (
    SCHEMA_VERSION,
    Affiliation,
    AffiliationKind,
    ApproxDate,
    GraphEdge,
    GraphStats,
    MutualObservation,
    Org,
    OrgKind,
    Provenance,
    TargetFirm,
    TargetPerson,
)

SHARED_START = ApproxDate(year=2010, month=1, day=1)
SHARED_END = ApproxDate(year=2019, month=12, day=31)

MEGACORP = ("MegaCorp", 200, 200_000)
TAIL_MEMBERS = (2, 2, 2, 3, 3, 3, 4, 4, 5, 5, 6, 7, 8, 10, 12, 15, 18, 22, 28, 35)
TAIL_HEADCOUNT_MULTIPLIER = (1, 1, 2, 3, 5, 8, 12, 20, 30, 50)


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


def _org(name: str) -> Org:
    return Org.make(name, OrgKind.COMPANY)


def _aff(
    person: str,
    org: Org,
    *,
    start: ApproxDate | None = SHARED_START,
    end: ApproxDate | None = SHARED_END,
    kind: AffiliationKind = AffiliationKind.EMPLOYMENT,
    weight: float = 1.0,
) -> Affiliation:
    return Affiliation(
        person_id=person,
        org_id=org.org_id,
        kind=kind,
        start=start,
        end=end,
        weight=weight,
    )


def _write_gazetteer(path: Path, firms: dict[str, int]) -> Path:
    """A minimal gazetteer in the real schema, so ``load_firm_sizes`` is exercised."""
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entities": [
                    {
                        "key": f"key_{index}",
                        "canonical_name": name,
                        "approx_headcount": headcount,
                    }
                    for index, (name, headcount) in enumerate(sorted(firms.items()))
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _settings(tmp_path: Path, gazetteer: Path | None = None, **overrides: object) -> Settings:
    return Settings(
        artifact_dir=tmp_path / "artifacts",
        gazetteer=gazetteer if gazetteer is not None else tmp_path / "absent.yaml",
        **overrides,  # type: ignore[arg-type]
    )


def _export_firms(seed: int = 20240101, tail_people: int = 1_000) -> list[tuple[str, int, int]]:
    rng = random.Random(seed)
    firms = [MEGACORP]
    remaining, index = tail_people, 0
    while remaining > 0:
        index += 1
        members = min(remaining, rng.choice(TAIL_MEMBERS))
        remaining -= members
        firms.append((f"Firm{index:03d}", members, members * rng.choice(TAIL_HEADCOUNT_MULTIPLIER)))
    return firms


def _export_world(
    firms: list[tuple[str, int, int]], seed: int = 20240101
) -> tuple[list[Org], list[Affiliation]]:
    rng = random.Random(seed)
    orgs: list[Org] = []
    affiliations: list[Affiliation] = []
    person = 0
    for name, members, _headcount in firms:
        org = _org(name)
        orgs.append(org)
        for _ in range(members):
            person += 1
            start = 2005 + rng.randrange(0, 12)
            affiliations.append(
                _aff(
                    f"p{person:05d}",
                    org,
                    start=ApproxDate(year=start),
                    end=ApproxDate(year=start + rng.randrange(1, 6)),
                )
            )
    return orgs, affiliations


@pytest.fixture
def export(tmp_path: Path) -> tuple[Settings, list[Org], list[Affiliation]]:
    """A ~1,200-person export with one 200,000-person employer in it."""
    firms = _export_firms()
    orgs, affiliations = _export_world(firms)
    gazetteer = _write_gazetteer(
        tmp_path / "firms.yaml", {name: headcount for name, _m, headcount in firms}
    )
    return _settings(tmp_path, gazetteer), orgs, affiliations


def _small_world() -> tuple[list[Org], list[Affiliation]]:
    orgs = [_org("Alpha"), _org("Beta"), _org("Gamma")]
    affiliations = [
        _aff(f"{org.name}_p{i:02d}", org)
        for org, n in zip(orgs, (6, 5, 4), strict=True)
        for i in range(n)
    ]
    return orgs, affiliations


# ---------------------------------------------------------------------------
# stage ordering
# ---------------------------------------------------------------------------


def test_run_before_normalize_raises_stage_input_missing(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    with pytest.raises(StageInputMissingError) as excinfo:
        run(cfg)
    assert excinfo.value.produced_by == "normalize"
    assert "affiliations.jsonl" in excinfo.value.path
    assert "interlayer normalize" in str(excinfo.value)


def test_run_with_affiliations_but_no_orgs_still_raises(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    _orgs, affiliations = _small_world()
    write_jsonl(cfg.affiliations_path, affiliations)
    with pytest.raises(StageInputMissingError) as excinfo:
        run(cfg)
    assert "orgs.jsonl" in excinfo.value.path


def test_run_does_not_require_the_optional_tier_two_artifacts(tmp_path: Path) -> None:
    """Tier 1 is designed to work with zero manual collection."""
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    write_jsonl(cfg.affiliations_path, affiliations)
    write_jsonl(cfg.orgs_path, orgs)
    assert not cfg.observations_path.exists()
    assert not cfg.targets_path.exists()

    run(cfg)
    assert cfg.edges_path.is_file()
    assert cfg.artifact(STATS_FILENAME).is_file()


def test_run_writes_artifacts_at_0600(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    write_jsonl(cfg.affiliations_path, affiliations)
    write_jsonl(cfg.orgs_path, orgs)
    run(cfg)
    for path in (cfg.edges_path, cfg.artifact(STATS_FILENAME)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


# ---------------------------------------------------------------------------
# configuration validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_overlap_years", 0.0),
        ("max_overlap_years", -1.0),
        ("disparity_alpha", 0.0),
        ("disparity_alpha", 1.5),
        ("rescue_top_k", -1),
        ("missing_date_factor", -0.1),
        ("missing_date_factor", 1.1),
    ],
)
def test_nonsense_knobs_fail_loudly(tmp_path: Path, field: str, value: float) -> None:
    cfg = _settings(tmp_path).model_copy(update={field: value})
    orgs, affiliations = _small_world()
    with pytest.raises(GraphError) as excinfo:
        build_graph(cfg, affiliations=affiliations, orgs=orgs)
    assert field in str(excinfo.value)


def test_an_unknown_size_damping_strategy_is_rejected(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    with pytest.raises(GraphError):
        build_graph(cfg, affiliations=affiliations, orgs=orgs, size_damping="hyperbolic")


def test_a_missing_gazetteer_degrades_to_the_documented_fallback(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    assert not cfg.gazetteer.exists()
    orgs, affiliations = _small_world()
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    assert result.edges
    assert result.diagnostics["orgs_with_gazetteer_headcount"] == 0
    assert result.diagnostics["orgs_using_n_f_fallback"] == len(orgs)


# ---------------------------------------------------------------------------
# degenerate inputs
# ---------------------------------------------------------------------------


def test_no_affiliations_at_all(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    result = build_graph(cfg, affiliations=[], orgs=[])
    assert result.edges == ()
    assert result.stats == GraphStats(n_nodes=0, n_edges=0, n_components=0, density=0.0)

    write_jsonl(cfg.affiliations_path, [])
    write_jsonl(cfg.orgs_path, [])
    run(cfg)
    assert cfg.edges_path.read_text(encoding="utf-8") == ""


def test_a_single_person(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    org = _org("Solo")
    result = build_graph(cfg, affiliations=[_aff("only", org)], orgs=[org])
    assert result.edges == ()
    assert result.stats.n_nodes == 1
    assert result.stats.n_edges == 0
    assert result.stats.n_components == 1
    assert result.stats.density == 0.0


def test_everyone_at_one_firm(tmp_path: Path) -> None:
    """One employer projects to a clique -- which the backbone then thins.

    Every edge in that clique carries identical weight, so none of them is
    statistically surprising and the whole graph survives on the rescue alone.
    """
    cfg = _settings(tmp_path)
    org = _org("OnlyEmployer")
    people = [f"p{i:02d}" for i in range(8)]
    affiliations = [_aff(person, org) for person in people]
    clique = {(a, b) for a in people for b in people if a < b}

    unfiltered = build_graph(
        cfg.model_copy(update={"disparity_alpha": 1.0}),
        affiliations=affiliations,
        orgs=[org],
    )
    assert {(e.source, e.target) for e in unfiltered.edges} == clique

    result = build_graph(cfg, affiliations=affiliations, orgs=[org])
    assert result.stats.n_nodes == 8
    assert result.stats.n_components == 1
    assert 0 < result.stats.n_edges <= len(clique)
    assert {(e.source, e.target) for e in result.edges} <= clique
    assert all(edge.provenance is Provenance.INFERRED for edge in result.edges)
    assert result.diagnostics["isolated_nodes"] == 0


def test_everyone_at_a_different_firm(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs = [_org(f"Firm{i:02d}") for i in range(8)]
    affiliations = [_aff(f"p{i:02d}", org) for i, org in enumerate(orgs)]
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    assert result.edges == ()
    assert result.stats.n_nodes == 8
    assert result.stats.n_components == 8
    assert result.stats.density == 0.0


def test_an_affiliation_naming_an_org_that_was_never_emitted(tmp_path: Path) -> None:
    """Agent 2's two artifacts can drift; a dangling org_id must not crash the stage."""
    cfg = _settings(tmp_path)
    ghost = _org("Ghost")
    result = build_graph(cfg, affiliations=[_aff("a", ghost), _aff("b", ghost)], orgs=[])
    assert result.stats.n_edges == 1
    assert result.diagnostics["orgs_using_n_f_fallback"] == 1


# ---------------------------------------------------------------------------
# structural invariants
# ---------------------------------------------------------------------------


def test_the_edge_model_really_does_reject_a_self_loop() -> None:
    """The invariant the builder has to respect is enforced, not assumed."""
    with pytest.raises(ValueError, match="self-loops"):
        GraphEdge(source="a", target="a")


def test_the_builder_never_constructs_a_self_loop(tmp_path: Path) -> None:
    """Messy input -- repeat stints, exact duplicate rows, undated and invalid
    dates, a person who is both a colleague and an observed bridge."""
    cfg = _settings(tmp_path)
    org = _org("Messy")
    other = _org("AlsoMessy")
    duplicate = _aff("a", org)
    affiliations = [
        duplicate,
        duplicate,
        _aff("a", org, start=ApproxDate(year=2001), end=ApproxDate(year=2004)),
        _aff("a", org, start=None, end=None),
        _aff("a", org, start=ApproxDate(year=2019, month=2, day=28), end=SHARED_END),
        _aff("a", other, kind=AffiliationKind.EDUCATION),
        _aff("b", org),
        _aff("b", org, start=None, end=None),
        _aff("b", other, kind=AffiliationKind.EDUCATION),
        _aff("c", other),
    ]
    target = TargetPerson.make("Jane Target", TargetFirm.JANE_STREET)
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("a", "a", "b", "c"),
    )
    result = build_graph(
        cfg, affiliations=affiliations, orgs=[org, other], observations=[observation]
    )
    assert result.edges
    assert all(edge.source != edge.target for edge in result.edges)


def test_edge_orientation_is_canonical_and_the_file_is_sorted(tmp_path: Path) -> None:
    """``PYTHONHASHSEED`` produced four distinct set-iteration orders in the
    research, so unsorted output is not reproducible output."""
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    write_jsonl(cfg.affiliations_path, affiliations)
    write_jsonl(cfg.orgs_path, orgs)
    run(cfg)

    lines = cfg.edges_path.read_text(encoding="utf-8").splitlines()
    edges = [GraphEdge.model_validate_json(line) for line in lines]
    assert edges
    assert all(edge.source < edge.target for edge in edges)
    keys = [(edge.source, edge.target) for edge in edges]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_shared_org_ids_and_kinds_are_sorted(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    employer = _org("Zulu")
    school = _org("Alpha")
    affiliations = [_aff(p, employer) for p in ("a", "b")] + [
        _aff(p, school, kind=AffiliationKind.EDUCATION) for p in ("a", "b")
    ]
    result = build_graph(cfg, affiliations=affiliations, orgs=[employer, school])
    edge = result.edges[0]
    assert list(edge.shared_org_ids) == sorted(edge.shared_org_ids)
    assert list(edge.kinds) == sorted(edge.kinds)
    assert set(edge.kinds) == {AffiliationKind.EDUCATION, AffiliationKind.EMPLOYMENT}


# ---------------------------------------------------------------------------
# the stats artifact
# ---------------------------------------------------------------------------


def test_graph_stats_agree_with_the_edge_list(
    export: tuple[Settings, list[Org], list[Affiliation]],
) -> None:
    cfg, orgs, affiliations = export
    target = TargetPerson.make("Jane Target", TargetFirm.JANE_STREET)
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("p00001", "p00002", "an_outsider"),
    )
    result = build_graph(
        cfg,
        affiliations=affiliations,
        orgs=orgs,
        observations=[observation],
        targets=[target],
    )

    expected_nodes = {a.person_id for a in affiliations} | set(observation.bridge_person_ids)
    rebuilt = nx.Graph()
    rebuilt.add_nodes_from(sorted(expected_nodes))
    rebuilt.add_edges_from((edge.source, edge.target) for edge in result.edges)

    assert result.stats.n_nodes == rebuilt.number_of_nodes()
    assert result.stats.n_edges == rebuilt.number_of_edges()
    assert result.stats.n_edges == len(result.edges)
    assert result.stats.n_components == nx.number_connected_components(rebuilt)
    assert result.stats.density == pytest.approx(nx.density(rebuilt))
    assert result.diagnostics["isolated_nodes"] == sum(
        1 for _, degree in rebuilt.degree() if degree == 0
    )
    assert result.diagnostics["nodes_with_edges"] == sum(
        1 for _, degree in rebuilt.degree() if degree > 0
    )
    assert result.diagnostics["largest_component"] == max(
        len(component) for component in nx.connected_components(rebuilt)
    )


def test_stats_file_has_the_documented_shape(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    write_jsonl(cfg.affiliations_path, affiliations)
    write_jsonl(cfg.orgs_path, orgs)
    run(cfg)

    payload = json.loads(cfg.artifact(STATS_FILENAME).read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "stats", "diagnostics"}
    assert payload["schema_version"] == SCHEMA_VERSION
    stats = GraphStats.model_validate(payload["stats"])
    assert stats.n_edges == sum(1 for _ in cfg.edges_path.read_text(encoding="utf-8").splitlines())
    assert payload["diagnostics"]["require_cotenure"] is True
    assert payload["diagnostics"]["disparity_alpha"] == cfg.disparity_alpha
    assert payload["diagnostics"]["rescue_top_k"] == cfg.rescue_top_k


def test_the_concentration_diagnostic_names_the_dominant_employer(
    export: tuple[Settings, list[Org], list[Affiliation]],
) -> None:
    cfg, orgs, affiliations = export
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    ranked = result.diagnostics["projected_weight_share_by_org"]
    assert len(ranked) == 5
    assert ranked[0]["name"] == "MegaCorp"
    assert ranked[0]["headcount"] == 200_000
    assert ranked[0]["headcount_from_gazetteer"] is True
    shares = [entry["weight_share"] for entry in ranked]
    assert shares == sorted(shares, reverse=True)


def test_redaction_removes_org_names_from_the_diagnostic(
    export: tuple[Settings, list[Org], list[Affiliation]],
) -> None:
    cfg, orgs, affiliations = export
    result = build_graph(
        cfg.model_copy(update={"redact": True}), affiliations=affiliations, orgs=orgs
    )
    assert all(
        entry["name"] is None for entry in result.diagnostics["projected_weight_share_by_org"]
    )


def test_the_dominant_employer_is_a_minority_of_surviving_edge_weight(
    export: tuple[Settings, list[Org], list[Affiliation]],
) -> None:
    """End to end: naive projection gave this firm two thirds of the graph."""
    cfg, orgs, affiliations = export
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    megacorp = next(org for org in orgs if org.name == "MegaCorp")

    total = sum(edge.weight for edge in result.edges)
    theirs = sum(edge.weight for edge in result.edges if megacorp.org_id in edge.shared_org_ids)
    projected_share = result.diagnostics["projected_weight_share_by_org"][0]["weight_share"]
    measured = {
        "share_of_projected_weight": round(projected_share, 4),
        "share_of_surviving_edge_weight": round(theirs / total, 4),
    }
    assert projected_share < 0.15, measured
    assert theirs / total < 0.05, measured


def test_no_node_with_a_candidate_edge_is_orphaned(
    export: tuple[Settings, list[Org], list[Affiliation]],
) -> None:
    """The rescue, end to end: the raw filter once cut 500 people to 86."""
    cfg, orgs, affiliations = export
    unfiltered = build_graph(
        cfg.model_copy(update={"disparity_alpha": 1.0, "rescue_top_k": 0}),
        affiliations=affiliations,
        orgs=orgs,
    )
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)

    candidates = {node for edge in unfiltered.edges for node in (edge.source, edge.target)}
    connected = {node for edge in result.edges for node in (edge.source, edge.target)}
    assert candidates, "the fixture must produce candidate edges"
    assert candidates - connected == set()


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def test_two_runs_produce_byte_identical_artifacts(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs, affiliations = _small_world()
    target = TargetPerson.make("Jane Target", TargetFirm.JANE_STREET)
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("Alpha_p00", "Beta_p00", "Gamma_p00"),
    )
    write_jsonl(cfg.affiliations_path, affiliations)
    write_jsonl(cfg.orgs_path, orgs)
    write_jsonl(cfg.observations_path, [observation])
    write_jsonl(cfg.targets_path, [target])

    run(cfg)
    first = (cfg.edges_path.read_bytes(), cfg.artifact(STATS_FILENAME).read_bytes())
    run(cfg)
    second = (cfg.edges_path.read_bytes(), cfg.artifact(STATS_FILENAME).read_bytes())
    assert first == second
    assert first[0], "the fixture must actually produce edges"


DRIVER = """\
import sys
from pathlib import Path

from interlayer.config import Settings
from interlayer.graph import run

run(Settings(artifact_dir=Path(sys.argv[1]), gazetteer=Path(sys.argv[2])))
"""

HASH_SEEDS = ("0", "1", "12345")


def test_artifacts_are_byte_identical_across_pythonhashseed(tmp_path: Path) -> None:
    """Set iteration order varies with ``PYTHONHASHSEED``; four values gave four
    distinct orders in the research. A subprocess per seed is the only honest
    way to test it -- the interpreter fixes its hash seed at startup."""
    firms = _export_firms(tail_people=200)
    orgs, affiliations = _export_world(firms)
    target = TargetPerson.make("Jane Target", TargetFirm.JANE_STREET)
    observation = MutualObservation(
        target_person_id=target.target_person_id,
        bridge_person_ids=("p00001", "p00002", "p00003", "stranger"),
    )
    gazetteer = _write_gazetteer(
        tmp_path / "firms.yaml", {name: headcount for name, _m, headcount in firms}
    )
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")

    digests: dict[str, tuple[bytes, bytes]] = {}
    for seed in HASH_SEEDS:
        artifacts = tmp_path / f"seed_{seed}"
        cfg = Settings(artifact_dir=artifacts, gazetteer=gazetteer)
        write_jsonl(cfg.affiliations_path, affiliations)
        write_jsonl(cfg.orgs_path, orgs)
        write_jsonl(cfg.observations_path, [observation])
        write_jsonl(cfg.targets_path, [target])

        completed = subprocess.run(
            [sys.executable, str(driver), str(artifacts), str(gazetteer)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        digests[seed] = (
            cfg.edges_path.read_bytes(),
            cfg.artifact(STATS_FILENAME).read_bytes(),
        )

    reference = digests[HASH_SEEDS[0]]
    assert reference[0], "the fixture must actually produce edges"
    for seed in HASH_SEEDS[1:]:
        assert digests[seed] == reference, f"PYTHONHASHSEED={seed} changed the artifacts"


def test_build_is_independent_of_input_record_order(tmp_path: Path) -> None:
    cfg = _settings(tmp_path)
    orgs, affiliations = _export_world(_export_firms(tail_people=200))
    baseline = build_graph(cfg, affiliations=affiliations, orgs=orgs)

    shuffled_affiliations = list(affiliations)
    shuffled_orgs = list(orgs)
    rng = random.Random(99)
    rng.shuffle(shuffled_affiliations)
    rng.shuffle(shuffled_orgs)
    other = build_graph(cfg, affiliations=shuffled_affiliations, orgs=shuffled_orgs)

    assert other.edges == baseline.edges
    assert other.stats == baseline.stats


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_three_thousand_person_network_builds_quickly(tmp_path: Path) -> None:
    """The projection is quadratic inside each org, so a heavy-tailed firm-size
    distribution is the shape that would blow up if the loop regressed."""
    firms = _export_firms(tail_people=2_800)
    orgs, affiliations = _export_world(firms)
    assert len({a.person_id for a in affiliations}) == 3_000

    gazetteer = _write_gazetteer(
        tmp_path / "firms.yaml", {name: headcount for name, _m, headcount in firms}
    )
    cfg = _settings(tmp_path, gazetteer)

    started = time.perf_counter()
    result = build_graph(cfg, affiliations=affiliations, orgs=orgs)
    elapsed = time.perf_counter() - started

    assert result.stats.n_nodes == 3_000
    assert result.edges
    assert elapsed < 30.0, f"3,000-person build took {elapsed:.1f}s"
