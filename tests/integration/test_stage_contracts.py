"""Stage ordering, idempotence, hand-off contracts and byte-level determinism.

Six agents built six stages against a written contract and never imported each
other. These tests are the only place the contract is checked as a contract:
what stage N writes must be exactly what stage N+1 can read, running a stage out
of order must fail loudly with exit 4 rather than half-succeeding, and the same
input plus the same seed must produce the same bytes no matter what
``PYTHONHASHSEED`` does to set iteration order.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from interlayer.models import (
    Affiliation,
    Cluster,
    GraphEdge,
    MutualObservation,
    Org,
    Person,
    ScoredCluster,
    ScoredPerson,
    TargetPerson,
)
from tests.integration import (
    MANIFEST,
    CliResult,
    PipelineRun,
    build_rows,
    cli,
    manifest_without_clock,
    read_models,
    run_all_stages,
    shared_run,
    stage_args,
    write_connections_csv,
    write_observations,
)

#: What each stage command leaves behind. Used for idempotence and hand-off.
STAGE_OUTPUTS: dict[str, tuple[str, ...]] = {
    "ingest": ("people.jsonl", "connections.jsonl", "ingest_stats.json"),
    "normalize": ("orgs.jsonl", "affiliations.jsonl", "review_queue.jsonl"),
    "enrich": ("targets.jsonl", "observations.jsonl", "unresolved_mutuals.jsonl"),
    "build": ("edges.jsonl", "graph_stats.json"),
    "cluster": ("clusters.jsonl", "clusters_sweep.jsonl"),
    "score": ("scored_people.jsonl", "scored_clusters.jsonl"),
    "report": ("report.html", MANIFEST),
}

ORDER: tuple[str, ...] = tuple(STAGE_OUTPUTS)

#: ``(stage, prerequisites actually run, artifact it should miss, its producer)``.
#: Only *real* dependencies are listed. ``enrich`` reads ``people.jsonl`` only
#: when an observations file exists, which is why every case here supplies one.
ORDERING_CASES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("normalize", (), "people.jsonl", "ingest"),
    ("enrich", (), "people.jsonl", "ingest"),
    ("build", ("ingest",), "affiliations.jsonl", "normalize"),
    ("cluster", ("ingest", "normalize", "enrich"), "edges.jsonl", "build"),
    ("score", ("ingest", "normalize", "enrich", "build"), "clusters.jsonl", "cluster"),
    ("score", ("ingest", "normalize", "enrich"), "edges.jsonl", "build"),
    (
        "report",
        ("ingest", "normalize", "enrich", "build", "cluster"),
        "scored_people.jsonl",
        "score",
    ),
    ("report", ("ingest",), "orgs.jsonl", "normalize"),
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    rows = build_rows()
    write_connections_csv(tmp_path / "Connections.csv", rows)
    write_observations(tmp_path / "observations.yaml", rows)
    (tmp_path / "artifacts").mkdir()
    return tmp_path


def run_stage(workspace: Path, command: str, **extra: str) -> CliResult:
    """Run one stage command against the workspace's artifact directory."""
    env = {"INTERLAYER_OBSERVATIONS": str(workspace / "observations.yaml")}
    env.update(extra)
    return cli(
        *stage_args(command, workspace / "Connections.csv", workspace / "artifacts"),
        env=env,
        cwd=workspace,
    )


def run_stages(workspace: Path, commands: Sequence[str]) -> None:
    for command in commands:
        result = run_stage(workspace, command)
        assert result.returncode == 0, result


@pytest.fixture(scope="module")
def observed() -> PipelineRun:
    return shared_run("observed")


@pytest.fixture(scope="module")
def staged(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workspace with every stage already run, once, stage by stage."""
    root = tmp_path_factory.mktemp("staged")
    rows = build_rows()
    write_connections_csv(root / "Connections.csv", rows)
    write_observations(root / "observations.yaml", rows)
    (root / "artifacts").mkdir()
    run_stages(root, ORDER)
    return root


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "prerequisites", "missing", "producer"),
    ORDERING_CASES,
    ids=[f"{c[0]}-without-{c[3]}" for c in ORDERING_CASES],
)
def test_a_stage_run_before_its_input_exists_exits_four(
    workspace: Path,
    stage: str,
    prerequisites: tuple[str, ...],
    missing: str,
    producer: str,
) -> None:
    run_stages(workspace, prerequisites)
    result = run_stage(workspace, stage)
    assert result.returncode == 4, result
    output = result.output
    assert missing in output, f"the error does not name {missing}"
    assert f"interlayer {producer}" in output, f"the error does not point at {producer}"
    assert "Traceback" not in output


def test_a_stage_that_failed_wrote_nothing(workspace: Path) -> None:
    """Exit 4 must not leave a half-written artifact for the next stage to trust."""
    result = run_stage(workspace, "cluster")
    assert result.returncode == 4
    for name in STAGE_OUTPUTS["cluster"]:
        assert not (workspace / "artifacts" / name).exists()


def test_enrich_without_an_observations_file_is_not_an_error(workspace: Path) -> None:
    """Tier 2 is optional by design: no file means empty artifacts, not a failure."""
    run_stages(workspace, ("ingest",))
    # The stage searches the cwd and the artifact dir's parent, so the file has
    # to be gone from disk, not merely unnamed in the environment.
    (workspace / "observations.yaml").unlink()
    result = cli(
        *stage_args("enrich", workspace / "Connections.csv", workspace / "artifacts"),
        cwd=workspace,
    )
    assert result.returncode == 0, result
    assert "Tier 2 absent" in result.output or "INFERRED" in result.output
    assert (workspace / "artifacts" / "observations.jsonl").read_text(encoding="utf-8") == ""


def test_build_without_enrich_succeeds_because_tier_two_is_optional(workspace: Path) -> None:
    run_stages(workspace, ("ingest", "normalize"))
    result = run_stage(workspace, "build")
    assert result.returncode == 0, result
    assert read_models(workspace / "artifacts" / "edges.jsonl", GraphEdge)


def test_a_named_observations_file_that_does_not_exist_is_reported(workspace: Path) -> None:
    """A typo'd path is not an absent Tier 2, and must not be silently treated as one."""
    run_stages(workspace, ("ingest",))
    result = run_stage(workspace, "enrich", INTERLAYER_OBSERVATIONS=str(workspace / "typo.yaml"))
    assert result.returncode == 0, result
    assert "does not exist" in result.output


# ---------------------------------------------------------------------------
# idempotence and re-running upstream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ORDER)
def test_every_stage_is_idempotent(staged: Path, stage: str) -> None:
    """Running a stage twice must produce the same artifacts.

    ``manifest.json`` is compared with its timestamp removed: it is the one
    artifact deliberately carrying a clock, so that a report diff is a data diff.
    """
    artifacts = staged / "artifacts"
    before = {name: (artifacts / name).read_bytes() for name in STAGE_OUTPUTS[stage]}
    result = run_stage(staged, stage)
    assert result.returncode == 0, result
    for name, previous in before.items():
        current = (artifacts / name).read_bytes()
        if name == MANIFEST:
            assert manifest_without_clock(artifacts / name) == _payload(previous)
            continue
        assert current == previous, f"{name} changed when {stage} was re-run"


def _payload(raw: bytes) -> dict[str, object]:
    import json

    data = dict(json.loads(raw.decode("utf-8")))
    data.pop("created_at", None)
    return data


def test_rerunning_an_upstream_stage_then_a_downstream_one_works(workspace: Path) -> None:
    """No stale-state coupling: stages own their inputs, not each other's memory."""
    run_stages(workspace, ORDER)
    artifacts = workspace / "artifacts"
    before = (artifacts / "report.html").read_bytes()

    run_stages(workspace, ("ingest",))
    result = run_stage(workspace, "report")
    assert result.returncode == 0, result
    assert (artifacts / "report.html").read_bytes() == before


@pytest.mark.slow
def test_rerunning_the_whole_pipeline_in_place_is_stable(workspace: Path) -> None:
    run_stages(workspace, ORDER)
    artifacts = workspace / "artifacts"
    before = {p.name: p.read_bytes() for p in sorted(artifacts.iterdir()) if p.is_file()}
    run_stages(workspace, ORDER)
    for name, previous in before.items():
        if name == MANIFEST:
            continue
        assert (artifacts / name).read_bytes() == previous, f"{name} drifted on a second pass"


def test_a_stage_leaves_no_temporary_files_behind(staged: Path) -> None:
    """``secure_write`` writes via ``mkstemp`` and renames; nothing may survive."""
    leftovers = [p.name for p in (staged / "artifacts").iterdir() if p.name.startswith(".tmp-")]
    assert not leftovers, leftovers


# ---------------------------------------------------------------------------
# hand-off contracts: stage N's output is stage N+1's input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("producer", "artifact", "model", "consumers"),
    [
        ("ingest", "people.jsonl", Person, ("normalize", "enrich", "cluster", "score", "report")),
        ("normalize", "orgs.jsonl", Org, ("build", "cluster", "score", "report")),
        ("normalize", "affiliations.jsonl", Affiliation, ("build", "cluster", "score")),
        ("enrich", "targets.jsonl", TargetPerson, ("build", "score")),
        ("enrich", "observations.jsonl", MutualObservation, ("build", "score")),
        ("build", "edges.jsonl", GraphEdge, ("cluster", "score", "report")),
        ("cluster", "clusters.jsonl", Cluster, ("score", "report")),
        ("score", "scored_people.jsonl", ScoredPerson, ("report",)),
        ("score", "scored_clusters.jsonl", ScoredCluster, ("report",)),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_artifacts_validate_as_the_model_the_next_stage_reads(
    observed: PipelineRun,
    producer: str,
    artifact: str,
    model: type,
    consumers: tuple[str, ...],
) -> None:
    records: list[Any] = read_models(observed.path(artifact), model)
    assert consumers, "every artifact must have a consumer"
    if artifact not in {"targets.jsonl", "observations.jsonl"}:
        assert records, f"{producer} wrote an empty {artifact}"


def test_ids_referenced_downstream_all_exist_upstream(observed: PipelineRun) -> None:
    """The hand-off that actually breaks: a foreign key nobody validates."""
    people = {p.person_id for p in read_models(observed.path("people.jsonl"), Person)}
    orgs = {o.org_id for o in read_models(observed.path("orgs.jsonl"), Org)}
    targets = {
        t.target_person_id for t in read_models(observed.path("targets.jsonl"), TargetPerson)
    }

    for aff in read_models(observed.path("affiliations.jsonl"), Affiliation):
        assert aff.person_id in people, "affiliation references an unknown person"
        assert aff.org_id in orgs, "affiliation references an unknown org"

    for obs in read_models(observed.path("observations.jsonl"), MutualObservation):
        assert obs.target_person_id in targets
        assert set(obs.bridge_person_ids) <= people, "a bridge is not one of the ego's connections"

    for edge in read_models(observed.path("edges.jsonl"), GraphEdge):
        assert edge.source in people and edge.target in people
        assert set(edge.shared_org_ids) <= orgs

    clusters = read_models(observed.path("clusters.jsonl"), Cluster)
    for cluster in clusters:
        assert set(cluster.member_ids) <= people

    cluster_ids = {c.cluster_id for c in clusters}
    for scored in read_models(observed.path("scored_people.jsonl"), ScoredPerson):
        assert scored.person_id in people
        if scored.cluster_id is not None:
            assert scored.cluster_id in cluster_ids
        for item in scored.evidence:
            if item.org_id is not None:
                assert item.org_id in orgs
            if item.via_person_id is not None:
                assert item.via_person_id in people | targets

    for scored_cluster in read_models(observed.path("scored_clusters.jsonl"), ScoredCluster):
        assert scored_cluster.cluster_id in cluster_ids
        assert set(scored_cluster.top_person_ids) <= people


def test_running_the_stages_one_by_one_matches_running_them_together(
    staged: Path, observed: PipelineRun
) -> None:
    """``interlayer run`` must be exactly the seven commands, not a second path."""
    result = run_all_stages(
        staged / "Connections.csv",
        staged / "combined",
        observations=staged / "observations.yaml",
        cwd=staged,
    )
    assert result.returncode == 0, result
    for name in ("people.jsonl", "orgs.jsonl", "affiliations.jsonl", "edges.jsonl"):
        assert (staged / "combined" / name).read_bytes() == (
            staged / "artifacts" / name
        ).read_bytes(), f"{name} differs between `run` and the individual commands"


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------


def _snapshot(artifacts: Path, into: Path) -> dict[str, bytes]:
    shutil.copytree(artifacts, into)
    return {p.name: p.read_bytes() for p in sorted(into.iterdir()) if p.is_file()}


def _diff(first: dict[str, bytes], second: dict[str, bytes]) -> list[str]:
    """Names of artifacts that are not byte-identical. ``manifest.json`` is
    compared with its timestamp stripped -- it is the only artifact that carries
    a clock, by design."""
    differing: list[str] = []
    assert set(first) == set(second), "the two runs produced different file sets"
    for name in sorted(first):
        if name == MANIFEST:
            if _payload(first[name]) != _payload(second[name]):
                differing.append(name)
            continue
        if first[name] != second[name]:
            differing.append(name)
    return differing


def test_two_runs_with_the_same_seed_are_byte_identical(workspace: Path) -> None:
    """Same input, same seed, same bytes -- for every artifact.

    Both runs write to the *same* artifact directory path, because
    ``Settings.artifact_dir`` is folded into ``manifest.json``'s
    ``config_sha256``; two different directories would differ for a reason that
    has nothing to do with determinism.
    """
    artifacts = workspace / "artifacts"
    csv_path = workspace / "Connections.csv"
    observations = workspace / "observations.yaml"

    assert (
        run_all_stages(
            csv_path, artifacts, observations=observations, seed=4242, cwd=workspace
        ).returncode
        == 0
    )
    first = _snapshot(artifacts, workspace / "snap-a")

    shutil.rmtree(artifacts)
    artifacts.mkdir()
    assert (
        run_all_stages(
            csv_path, artifacts, observations=observations, seed=4242, cwd=workspace
        ).returncode
        == 0
    )
    second = _snapshot(artifacts, workspace / "snap-b")

    assert _diff(first, second) == []


@pytest.mark.slow
@pytest.mark.parametrize("hashseed", ["0", "1", "12345"])
def test_artifacts_are_identical_across_python_hash_seeds(workspace: Path, hashseed: str) -> None:
    """``PYTHONHASHSEED`` produced four distinct set-iteration orders in Wave 1.

    Anything derived from a set therefore has to be sorted before it is written,
    and this is the only test that can prove it -- ``PYTHONHASHSEED`` is read at
    interpreter start, so it means nothing without a fresh process.
    """
    artifacts = workspace / "artifacts"
    csv_path = workspace / "Connections.csv"
    observations = workspace / "observations.yaml"

    def once(tag: str, seed_env: str) -> dict[str, bytes]:
        if artifacts.exists():
            shutil.rmtree(artifacts)
        artifacts.mkdir()
        result = run_all_stages(
            csv_path,
            artifacts,
            observations=observations,
            seed=4242,
            cwd=workspace,
            extra_env={"PYTHONHASHSEED": seed_env},
        )
        assert result.returncode == 0, result
        return _snapshot(artifacts, workspace / f"snap-{tag}")

    baseline = once("base", "0")
    other = once(hashseed, hashseed)
    assert _diff(baseline, other) == [], f"PYTHONHASHSEED={hashseed} changed these artifacts"
