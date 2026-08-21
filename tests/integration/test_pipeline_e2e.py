"""End-to-end: do the six independently-built stages actually compose?

Every test here drives the real console script as a subprocess against a
synthetic ``Connections.csv`` of ~220 people, and asserts on the *final*
artifacts. Asserting on an intermediate would let a downstream stage quietly
undo a correct upstream decision -- which is exactly the class of bug that six
parallel build agents produce and unit tests cannot see.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from interlayer.models import (
    Affiliation,
    Cluster,
    Connection,
    GraphEdge,
    MutualObservation,
    Org,
    Person,
    Provenance,
    RunManifest,
    ScoredCluster,
    ScoredPerson,
    TargetFirm,
    TargetPerson,
)
from tests.integration import (
    CSV_FIXTURE,
    MANIFEST,
    MAY_BE_EMPTY,
    OBSERVATIONS_FIXTURE,
    PipelineRun,
    Row,
    bridge_rows,
    build_observation_plans,
    build_rows,
    control_rows,
    file_mode,
    read_json,
    read_models,
    render_csv,
    render_observations,
    run_all_stages,
    scale_rows,
    shared_run,
    write_connections_csv,
)

TARGET_ARTIFACTS: tuple[str, ...] = (
    "people.jsonl",
    "connections.jsonl",
    "ingest_stats.json",
    "orgs.jsonl",
    "affiliations.jsonl",
    "review_queue.jsonl",
    "targets.jsonl",
    "observations.jsonl",
    "unresolved_mutuals.jsonl",
    "edges.jsonl",
    "graph_stats.json",
    "clusters.jsonl",
    "clusters_sweep.jsonl",
    "scored_people.jsonl",
    "scored_clusters.jsonl",
    "report.html",
    MANIFEST,
)

#: Employer strings that share a token with a target firm and are NOT it. Every
#: one of these has to come out of the far end of the pipeline unscored.
DECOYS: tuple[str, ...] = (
    "The Citadel",
    "The Citadel, The Military College of South Carolina",
    "The Citadel Alumni Association",
    "The Citadel Graduate College",
    "Citadel Broadcasting",
    "Citadel Broadcasting Corporation",
    "Citadel Communications",
    "Citadel Credit Union",
    "Citadel Federal Credit Union",
    "Citadel Defense Company",
    "Jane Street Coffee",
    "Jane Street Bakery",
    "Jane Street Dental Practice",
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def observed() -> PipelineRun:
    """A full run with a Tier 2 observations file present."""
    return shared_run("observed")


@pytest.fixture(scope="module")
def bare() -> PipelineRun:
    """A full run with no observations file anywhere -- Tier 1 only."""
    return shared_run("bare")


@pytest.fixture(scope="module")
def rows() -> list[Row]:
    return build_rows()


def _by_name(run: PipelineRun) -> dict[str, Person]:
    return {p.full_name: p for p in read_models(run.path("people.jsonl"), Person)}


def _scored(run: PipelineRun) -> dict[str, ScoredPerson]:
    return {p.person_id: p for p in read_models(run.path("scored_people.jsonl"), ScoredPerson)}


def _scored_by_name(run: PipelineRun) -> dict[str, ScoredPerson]:
    people = _by_name(run)
    scored = _scored(run)
    return {name: scored[p.person_id] for name, p in people.items() if p.person_id in scored}


# ---------------------------------------------------------------------------
# the fixture data itself
# ---------------------------------------------------------------------------


def test_committed_fixture_matches_the_generator(rows: list[Row]) -> None:
    """The checked-in export is exactly what the generator emits.

    If these drift, every assertion below is about a file nobody can regenerate.
    """
    assert CSV_FIXTURE.read_text(encoding="utf-8") == render_csv(rows)
    assert OBSERVATIONS_FIXTURE.read_text(encoding="utf-8") == render_observations(
        build_observation_plans(rows)
    )


def test_fixture_is_a_realistic_cast(rows: list[Row]) -> None:
    """150-300 people, real target coverage, decoys, unicode, missing employers."""
    assert 150 <= len(rows) <= 300
    firms = {r.expect_firm for r in rows}
    assert {"jane_street", "citadel_llc", "citadel_securities"} <= firms
    assert {r.company for r in rows} >= set(DECOYS)
    assert any(not r.company for r in rows), "no rows with a missing employer"
    assert any(not r.full_name.isascii() for r in rows), "no unicode names"
    assert len({r.slug for r in rows}) == len(rows), "slugs must be unique"
    assert len({r.full_name for r in rows}) == len(rows), "names must be unique"


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def test_run_succeeds(observed: PipelineRun) -> None:
    assert observed.result.returncode == 0, observed.result


@pytest.mark.parametrize("name", TARGET_ARTIFACTS)
def test_every_artifact_exists_and_is_0600(observed: PipelineRun, name: str) -> None:
    path = observed.path(name)
    assert path.is_file(), f"{name} was never written"
    assert file_mode(path) == 0o600, f"{name} is {file_mode(path):o}, not 0600"


@pytest.mark.parametrize("name", TARGET_ARTIFACTS)
def test_artifacts_are_non_empty_where_expected(observed: PipelineRun, name: str) -> None:
    size = observed.path(name).stat().st_size
    if name in MAY_BE_EMPTY:
        pytest.skip(f"{name} may legitimately be empty")
    assert size > 0, f"{name} is empty"


def test_artifact_directory_is_0700(observed: PipelineRun) -> None:
    assert file_mode(observed.artifact_dir) == 0o700


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("people.jsonl", Person),
        ("connections.jsonl", Connection),
        ("orgs.jsonl", Org),
        ("affiliations.jsonl", Affiliation),
        ("targets.jsonl", TargetPerson),
        ("observations.jsonl", MutualObservation),
        ("edges.jsonl", GraphEdge),
        ("clusters.jsonl", Cluster),
        ("clusters_sweep.jsonl", Cluster),
        ("scored_people.jsonl", ScoredPerson),
        ("scored_clusters.jsonl", ScoredCluster),
    ],
)
def test_each_jsonl_artifact_parses_as_its_model(
    observed: PipelineRun, name: str, model: type
) -> None:
    read_models(observed.path(name), model)


def test_manifest_parses_as_run_manifest(observed: PipelineRun) -> None:
    manifest = RunManifest.model_validate(read_json(observed.path(MANIFEST)))
    assert manifest.seed == 4242, "--seed did not reach the manifest"
    assert manifest.input_filename == observed.csv_path.name
    assert manifest.input_sha256, "the input digest is what makes a run reproducible"
    assert manifest.counts["people"] == len(build_rows())


def test_every_person_in_the_export_survives_to_the_score(observed: PipelineRun) -> None:
    people = read_models(observed.path("people.jsonl"), Person)
    scored = read_models(observed.path("scored_people.jsonl"), ScoredPerson)
    assert len(people) == len(build_rows())
    assert {p.person_id for p in people} == {p.person_id for p in scored}


def test_unicode_names_round_trip_unmangled(observed: PipelineRun, rows: list[Row]) -> None:
    """NFKD folding happens inside id derivation; the display name must not fold."""
    people = _by_name(observed)
    unicode_rows = [r for r in rows if not r.full_name.isascii()]
    assert len(unicode_rows) >= 10
    for row in unicode_rows:
        assert row.full_name in people, f"{row.full_name!r} did not survive ingest"


def test_rows_without_an_employer_get_no_affiliation(
    observed: PipelineRun, rows: list[Row]
) -> None:
    """An empty Company must not mint an Org that everyone then shares."""
    people = _by_name(observed)
    affiliated = {
        a.person_id for a in read_models(observed.path("affiliations.jsonl"), Affiliation)
    }
    scored = _scored(observed)
    empties = [r for r in rows if not r.company]
    assert empties
    for row in empties:
        person = people[row.full_name]
        assert person.person_id not in affiliated
        assert scored[person.person_id].score == 0.0
        assert scored[person.person_id].per_firm == {}


# ---------------------------------------------------------------------------
# the Citadel problem -- asserted on the FINAL output
# ---------------------------------------------------------------------------


def _firm_members(run: PipelineRun, firm: TargetFirm) -> set[str]:
    """People the pipeline believes are employed by ``firm``, from final output."""
    orgs = read_models(run.path("orgs.jsonl"), Org)
    org_ids = {o.org_id for o in orgs if o.is_target and o.target_firm is firm}
    return {
        a.person_id
        for a in read_models(run.path("affiliations.jsonl"), Affiliation)
        if a.org_id in org_ids
    }


def test_jane_street_and_citadel_securities_never_share_a_per_firm_bucket(
    observed: PipelineRun,
) -> None:
    """The single most important correctness property of the product.

    Not merely "the orgs are distinct": no *person* may end up carrying credit
    for both firms, because that is what an operator reads off the report.
    """
    scored = read_models(observed.path("scored_people.jsonl"), ScoredPerson)
    js = {p.person_id for p in scored if p.per_firm.get(TargetFirm.JANE_STREET)}
    cs = {p.person_id for p in scored if p.per_firm.get(TargetFirm.CITADEL_SECURITIES)}
    assert js and cs, "the fixture must produce people in both buckets"
    assert not js & cs, f"{len(js & cs)} people scored into both firm buckets"


def test_citadel_llc_and_citadel_securities_stay_separate_end_to_end(
    observed: PipelineRun,
) -> None:
    """The fund and the market maker are different companies with different staff."""
    orgs = read_models(observed.path("orgs.jsonl"), Org)
    by_firm = {o.target_firm: o for o in orgs if o.is_target}
    assert TargetFirm.CITADEL_LLC in by_firm
    assert TargetFirm.CITADEL_SECURITIES in by_firm
    assert by_firm[TargetFirm.CITADEL_LLC].org_id != by_firm[TargetFirm.CITADEL_SECURITIES].org_id

    llc = _firm_members(observed, TargetFirm.CITADEL_LLC)
    sec = _firm_members(observed, TargetFirm.CITADEL_SECURITIES)
    assert llc and sec
    assert not llc & sec, "the same person is employed by both Citadels"

    scored = read_models(observed.path("scored_people.jsonl"), ScoredPerson)
    llc_scored = {p.person_id for p in scored if p.per_firm.get(TargetFirm.CITADEL_LLC)}
    sec_scored = {p.person_id for p in scored if p.per_firm.get(TargetFirm.CITADEL_SECURITIES)}
    assert not llc_scored & sec_scored


@pytest.mark.parametrize("employer", DECOYS)
def test_decoy_employers_are_never_scored_as_a_target_firm(
    observed: PipelineRun, rows: list[Row], employer: str
) -> None:
    """The military college, the broadcaster, the credit union, the coffee shop.

    ``The Citadel`` alone has ~40,000 alumni against the fund's ~3,150 staff, so
    a single leak here makes the Citadel bucket mostly college alumni.
    """
    people = _by_name(observed)
    scored = _scored(observed)
    victims = [r for r in rows if r.company == employer]
    assert victims, f"the fixture contains nobody at {employer!r}"
    for row in victims:
        person = people[row.full_name]
        result = scored[person.person_id]
        assert result.per_firm == {}, (
            f"{row.full_name!r} at {employer!r} was scored into {dict(result.per_firm)}"
        )
        kinds = {e.kind for e in result.evidence}
        assert "direct_employment" not in kinds
        assert "past_employment" not in kinds


def test_target_employees_are_attributed_to_their_own_firm_only(
    observed: PipelineRun, rows: list[Row]
) -> None:
    people = _by_name(observed)
    scored = _scored(observed)
    checked = 0
    for row in rows:
        if row.expect_firm is None:
            continue
        checked += 1
        result = scored[people[row.full_name].person_id]
        assert result.per_firm, f"{row.company!r} produced no firm attribution"
        assert set(result.per_firm) == {TargetFirm(row.expect_firm)}, (
            f"{row.full_name!r} at {row.company!r} -> {dict(result.per_firm)}"
        )
    assert checked >= 40


def test_the_citadel_college_stays_a_separate_org(observed: PipelineRun) -> None:
    """Normalise-twice: ``The Citadel`` must not collapse onto ``Citadel``."""
    orgs = {o.name: o for o in read_models(observed.path("orgs.jsonl"), Org)}
    assert "The Citadel" in orgs
    assert orgs["The Citadel"].is_target is False
    assert orgs["The Citadel"].target_firm is None
    citadel = [o for o in orgs.values() if o.target_firm is TargetFirm.CITADEL_LLC]
    assert citadel and orgs["The Citadel"].org_id != citadel[0].org_id


def test_ambiguous_citadel_in_a_position_field_goes_to_review(observed: PipelineRun) -> None:
    """Field context decides: ambiguous is a review item, never a silent match."""
    review = observed.path("review_queue.jsonl")
    raw = [json.loads(line) for line in review.read_text(encoding="utf-8").splitlines() if line]
    assert any(item["raw_text"] == "The Citadel" for item in raw), (
        "The Citadel in a position field must be surfaced for review"
    )
    assert all(item["verdict"] == "review" for item in raw)


# ---------------------------------------------------------------------------
# Tier 2: observed evidence
# ---------------------------------------------------------------------------


def test_observed_evidence_reaches_the_final_scored_people(observed: PipelineRun) -> None:
    scored = read_models(observed.path("scored_people.jsonl"), ScoredPerson)
    observed_people = [p for p in scored if p.has_observed_evidence]
    expected = {name for plan in build_observation_plans(build_rows()) for name in plan.bridges}
    names = {_by_name(observed)[n].person_id for n in expected}
    assert {p.person_id for p in observed_people} == names
    for person in observed_people:
        items = [e for e in person.evidence if e.provenance is Provenance.OBSERVED]
        assert items, "has_observed_evidence without an observed item"
        assert all(e.kind == "observed_mutual" for e in items)
        assert all(e.detail for e in items), "observed evidence with no human-readable detail"


def test_observed_people_outrank_matched_inference_only_controls(
    observed: PipelineRun, rows: list[Row]
) -> None:
    """A bridge must beat the colleague sitting next to them who was not observed.

    The control shares the bridge's employer, so every inferred signal available
    to one is available to the other. The only difference is the observation.
    """
    scored = _scored_by_name(observed)
    for firm in ("jane_street", "citadel_securities", "citadel_llc"):
        for bridge, control in zip(bridge_rows(rows, firm), control_rows(rows, firm), strict=True):
            got, want = scored[bridge.full_name], scored[control.full_name]
            assert got.score > want.score, (
                f"{bridge.full_name!r} (observed) scored {got.score} but "
                f"{control.full_name!r} (inferred only, same employer) scored {want.score}"
            )
            assert got.rank < want.rank


def test_observed_evidence_outranks_every_inferred_signal(observed: PipelineRun) -> None:
    """weights.observed_mutual is 10.0 against direct_employment's 6.0, and the
    ranking has to reflect that: a hand-read bridge beats a target employee."""
    scored = sorted(
        read_models(observed.path("scored_people.jsonl"), ScoredPerson),
        key=lambda p: p.rank,
    )
    top_observed = [p for p in scored if p.has_observed_evidence]
    best_inferred = max((p.score for p in scored if not p.has_observed_evidence), default=0.0)
    assert top_observed
    assert min(p.score for p in top_observed) > best_inferred


def test_observed_edges_appear_in_edges_jsonl(observed: PipelineRun) -> None:
    edges = read_models(observed.path("edges.jsonl"), GraphEdge)
    observed_edges = [e for e in edges if e.provenance is Provenance.OBSERVED]
    assert observed_edges, "no OBSERVED edges survived the graph stage"
    diagnostics = read_json(observed.path("graph_stats.json"))["diagnostics"]
    assert diagnostics["observed_edges"] == len(observed_edges)
    assert diagnostics["observed_edges_not_also_inferred"] == len(observed_edges), (
        "the fixture is meant to make every observed edge a genuinely new edge"
    )
    people = {p.person_id for p in read_models(observed.path("people.jsonl"), Person)}
    for edge in observed_edges:
        assert edge.source in people and edge.target in people
        assert edge.source < edge.target, "GraphEdge did not self-canonicalise"


def test_truncated_observation_is_recorded_as_incomplete(observed: PipelineRun) -> None:
    """Absence from a truncated list is not evidence of absence, and must say so."""
    obs = read_models(observed.path("observations.jsonl"), MutualObservation)
    assert len(obs) == 3
    incomplete = [o for o in obs if not o.complete]
    assert len(incomplete) == 1
    only = incomplete[0]
    assert only.truncated
    assert only.note and "NOT evidence of absence" in only.note


def test_targets_keep_their_firms_distinct(observed: PipelineRun) -> None:
    targets = read_models(observed.path("targets.jsonl"), TargetPerson)
    assert {t.firm for t in targets} == set(TargetFirm)
    assert len({t.target_person_id for t in targets}) == len(targets)


# ---------------------------------------------------------------------------
# Tier 1 only
# ---------------------------------------------------------------------------


def test_pipeline_completes_with_no_observations_file(bare: PipelineRun) -> None:
    assert bare.result.returncode == 0, bare.result
    for name in TARGET_ARTIFACTS:
        assert bare.path(name).is_file(), f"{name} missing from a Tier 1 only run"


def test_without_observations_everything_is_inferred(bare: PipelineRun) -> None:
    assert bare.path("observations.jsonl").read_text(encoding="utf-8") == ""
    assert bare.path("targets.jsonl").read_text(encoding="utf-8") == ""
    edges = read_models(bare.path("edges.jsonl"), GraphEdge)
    assert edges
    assert all(e.provenance is Provenance.INFERRED for e in edges)
    scored = read_models(bare.path("scored_people.jsonl"), ScoredPerson)
    assert any(p.score for p in scored), "a Tier 1 run still has to score somebody"
    assert not any(p.has_observed_evidence for p in scored)
    for person in scored:
        assert all(e.provenance is Provenance.INFERRED for e in person.evidence)


def test_a_tier_one_run_says_out_loud_that_it_is_inferred(bare: PipelineRun) -> None:
    """The operator must not have to infer that the results are inferred."""
    output = bare.result.output
    assert "INFERRED" in output
    assert re.search(r"no Tier 2 .*observations were available", output), output
    html = bare.path("report.html").read_text(encoding="utf-8")
    assert "INFERRED" in html.upper()


def test_tier_one_still_separates_the_citadels(bare: PipelineRun, rows: list[Row]) -> None:
    """The firm separation must not depend on having Tier 2 data."""
    people = _by_name(bare)
    scored = _scored(bare)
    for row in rows:
        result = scored[people[row.full_name].person_id]
        if row.expect_firm is None:
            assert result.per_firm == {}, f"{row.company!r} -> {dict(result.per_firm)}"
        else:
            assert set(result.per_firm) == {TargetFirm(row.expect_firm)}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_is_self_contained(observed: PipelineRun) -> None:
    html = observed.path("report.html").read_text(encoding="utf-8")
    assert "Content-Security-Policy" in html
    assert "licdn.com" not in html
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', html)
    assert not remote, f"report reaches out to {remote[:3]}"


def test_report_marks_observed_and_inferred_differently(observed: PipelineRun) -> None:
    html = observed.path("report.html").read_text(encoding="utf-8")
    assert "OBSERVED" in html.upper()
    assert "INFERRED" in html.upper()


# ---------------------------------------------------------------------------
# clusters
# ---------------------------------------------------------------------------


def test_clusters_are_ranked_densely_and_uniquely(observed: PipelineRun) -> None:
    clusters = read_models(observed.path("scored_clusters.jsonl"), ScoredCluster)
    assert clusters
    assert sorted(c.rank for c in clusters) == list(range(1, len(clusters) + 1))


def test_cluster_members_are_real_people(observed: PipelineRun) -> None:
    people = {p.person_id for p in read_models(observed.path("people.jsonl"), Person)}
    for cluster in read_models(observed.path("clusters.jsonl"), Cluster):
        assert cluster.size >= 3, "min_cluster_size was not honoured"
        assert set(cluster.member_ids) <= people


def test_clusters_full_of_target_employees_outrank_clusters_with_none(
    observed: PipelineRun,
) -> None:
    """A cluster that is 100% Jane Street must beat one with no target employees.

    This is the product's whole promise. It currently fails: cluster score is
    "mean non-seed PPR mass", which is 0 by construction for a cluster made
    entirely of seeds, so a pure target-firm cluster ties with every neutral one
    and the tie is broken by cluster id.
    """
    clusters = read_models(observed.path("scored_clusters.jsonl"), ScoredCluster)
    pure = [c for c in clusters if c.target_density == 1.0]
    none = [c for c in clusters if c.target_density == 0.0]
    assert pure and none, "the fixture must contain both kinds of cluster"
    assert max(c.rank for c in pure) < min(c.rank for c in none), (
        "a cluster with no target-firm employees outranks a cluster that is "
        "entirely target-firm employees"
    )


# ---------------------------------------------------------------------------
# scale
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_two_thousand_person_export_completes_end_to_end(tmp_path: Path) -> None:
    """The realistic upper end of a LinkedIn export. ~12s here; still marked slow."""
    big = scale_rows(2000)
    assert len(big) == 2000
    csv_path = write_connections_csv(tmp_path / "Connections.csv", big)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    result = run_all_stages(csv_path, artifacts, seed=7, cwd=tmp_path)
    assert result.returncode == 0, result
    people = read_models(artifacts / "people.jsonl", Person)
    assert len(people) == 2000
    scored = read_models(artifacts / "scored_people.jsonl", ScoredPerson)
    assert len(scored) == 2000
    assert any(p.per_firm for p in scored)
    assert (artifacts / "report.html").stat().st_size > 0
