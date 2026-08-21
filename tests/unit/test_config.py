"""Tests for :mod:`interlayer.config`.

``Settings`` is the only argument a stage receives, so every guard in it is the
last line of defence before a stage does something the operator did not ask for
(keeping emails without a key, auto-accepting a firm match below the review
threshold, running an unseeded ensemble). The guards raise ``ConfigError``
rather than ``ValidationError`` on purpose: the CLI maps error type to exit
code without inspecting messages.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from interlayer.config import DEFAULT_SEED, ScoreWeights, Settings, load_settings
from interlayer.errors import ConfigError, InterlayerError

PATH_PROPERTIES: list[str] = sorted(
    name
    for name, attr in vars(Settings).items()
    if isinstance(attr, property) and name.endswith("_path")
)

REQUIRED_ARTIFACTS = {
    "people_path": "people.jsonl",
    "connections_path": "connections.jsonl",
    "orgs_path": "orgs.jsonl",
    "affiliations_path": "affiliations.jsonl",
    "observations_path": "observations.jsonl",
    "targets_path": "targets.jsonl",
    "edges_path": "edges.jsonl",
    "clusters_path": "clusters.jsonl",
    "scored_people_path": "scored_people.jsonl",
    "scored_clusters_path": "scored_clusters.jsonl",
    "manifest_path": "manifest.json",
    "report_path": "report.html",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambient ``INTERLAYER_*`` variables would silently change every result."""
    for key in list(os.environ):
        if key.startswith("INTERLAYER_"):
            monkeypatch.delenv(key, raising=False)


def write_yaml(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_keep_emails_without_a_key_is_refused() -> None:
    """Retaining emails unhashed is exactly what the privacy design forbids."""
    with pytest.raises(ConfigError, match="email_hmac_key"):
        Settings(keep_emails=True)


def test_keep_emails_with_a_key_is_allowed() -> None:
    cfg = Settings(keep_emails=True, email_hmac_key="secret")
    assert cfg.keep_emails and cfg.email_hmac_key == "secret"


def test_a_key_without_keep_emails_is_harmless() -> None:
    assert Settings(email_hmac_key="secret").keep_emails is False


def test_review_threshold_above_accept_is_refused() -> None:
    with pytest.raises(ConfigError, match="match_review"):
        Settings(match_review=95.0)


def test_review_threshold_equal_to_accept_is_allowed() -> None:
    assert Settings(match_review=90.0, match_accept=90.0).match_review == 90.0


def test_lowering_accept_below_the_default_review_is_refused() -> None:
    with pytest.raises(ConfigError, match="match_review"):
        Settings(match_accept=80.0)


@pytest.mark.parametrize("alpha", [0.0, 1.0, -0.1, 1.5, 2.0])
def test_pagerank_alpha_outside_the_open_unit_interval_is_refused(alpha: float) -> None:
    with pytest.raises(ConfigError, match="pagerank_alpha"):
        Settings(pagerank_alpha=alpha)


@pytest.mark.parametrize("alpha", [0.001, 0.5, 0.85, 0.999])
def test_pagerank_alpha_inside_the_open_unit_interval_is_accepted(alpha: float) -> None:
    assert Settings(pagerank_alpha=alpha).pagerank_alpha == alpha


@pytest.mark.parametrize("runs", [0, -1, -50])
def test_consensus_runs_below_one_is_refused(runs: int) -> None:
    with pytest.raises(ConfigError, match="consensus_runs"):
        Settings(consensus_runs=runs)


def test_consensus_runs_of_one_is_allowed() -> None:
    assert Settings(consensus_runs=1).consensus_runs == 1


def test_guards_raise_config_error_not_validation_error() -> None:
    """The CLI maps error type to exit code, so the type is part of the contract."""
    with pytest.raises(ConfigError) as exc:
        Settings(consensus_runs=0)
    assert isinstance(exc.value, InterlayerError)
    assert not isinstance(exc.value, ValidationError)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        Settings(nonexistent_option=1)


# ---------------------------------------------------------------------------
# immutability and defaults
# ---------------------------------------------------------------------------


def test_settings_is_frozen() -> None:
    cfg = Settings()
    with pytest.raises(ValidationError, match="frozen"):
        cfg.seed = 1


def test_score_weights_are_frozen() -> None:
    with pytest.raises(ValidationError, match="frozen"):
        Settings().weights.observed_mutual = 1.0


def test_defaults_match_the_frozen_wave_one_decisions() -> None:
    cfg = Settings()
    assert cfg.seed == DEFAULT_SEED
    assert (cfg.match_accept, cfg.match_review) == (90.0, 84.0)
    assert cfg.resolution == 2.0
    assert cfg.resolution_sweep == (1.0, 2.0, 4.0)
    assert cfg.consensus_runs == 50
    assert cfg.consensus_threshold == 0.5
    assert cfg.pagerank_alpha == 0.85
    assert cfg.disparity_alpha == 0.05
    assert cfg.rescue_top_k == 3
    assert cfg.require_cotenure is True
    assert cfg.keep_emails is False
    assert cfg.redact is False


def test_observed_evidence_outranks_every_inferred_signal() -> None:
    """An inferred signal may never outweigh something a human actually read."""
    weights = ScoreWeights()
    inferred = {
        name: getattr(weights, name)
        for name in ScoreWeights.model_fields
        if name != "observed_mutual"
    }
    assert weights.observed_mutual > max(inferred.values())
    assert weights.direct_employment == max(inferred.values())


# ---------------------------------------------------------------------------
# artifact paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prop", PATH_PROPERTIES)
def test_every_artifact_path_resolves_under_artifact_dir(prop: str, tmp_path: Path) -> None:
    cfg = Settings(artifact_dir=tmp_path / "arts")
    path = getattr(cfg, prop)
    assert isinstance(path, Path)
    assert path.parent == cfg.artifact_dir
    assert path.name


@pytest.mark.parametrize(("prop", "filename"), sorted(REQUIRED_ARTIFACTS.items()))
def test_well_known_artifact_filenames_are_stable(prop: str, filename: str) -> None:
    """Stages agree on these names on disk; renaming one breaks the next stage."""
    assert getattr(Settings(artifact_dir=Path("arts")), prop) == Path("arts") / filename


def test_artifact_helper_joins_onto_the_artifact_dir(tmp_path: Path) -> None:
    cfg = Settings(artifact_dir=tmp_path)
    assert cfg.artifact("x.jsonl") == tmp_path / "x.jsonl"


def test_artifact_paths_are_unique() -> None:
    cfg = Settings()
    names = [getattr(cfg, prop).name for prop in PATH_PROPERTIES]
    assert len(names) == len(set(names))


def test_hand_collected_observations_input_is_not_under_artifact_dir(tmp_path: Path) -> None:
    """``artifact_dir`` is purged wholesale; hours of manual collection may not
    live somewhere routine cleanup destroys."""
    cfg = Settings(artifact_dir=tmp_path, observations_input=tmp_path.parent / "observed.yaml")
    assert cfg.observations_input is not None
    assert cfg.artifact_dir not in cfg.observations_input.parents
    assert Settings().observations_input is None


def test_default_paths_are_relative() -> None:
    cfg = Settings()
    assert cfg.artifact_dir == Path("artifacts")
    assert cfg.gazetteer == Path("data/gazetteer/firms.yaml")
    assert cfg.input_csv is None


# ---------------------------------------------------------------------------
# load_settings
# ---------------------------------------------------------------------------


def test_load_settings_with_no_arguments_is_all_defaults() -> None:
    assert load_settings() == Settings()


def test_load_settings_reads_a_yaml_file(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "seed: 111\ntop_n: 7\nredact: true\n")
    cfg = load_settings(path)
    assert (cfg.seed, cfg.top_n, cfg.redact) == (111, 7, True)


def test_environment_beats_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_yaml(tmp_path, "seed: 111\n")
    monkeypatch.setenv("INTERLAYER_SEED", "222")
    assert load_settings(path).seed == 222


def test_keyword_overrides_beat_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_yaml(tmp_path, "seed: 111\n")
    monkeypatch.setenv("INTERLAYER_SEED", "222")
    assert load_settings(path, seed=333).seed == 333


def test_full_precedence_chain_file_then_env_then_keyword(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_yaml(tmp_path, "seed: 111\ntop_n: 11\nmin_cluster_size: 11\n")
    monkeypatch.setenv("INTERLAYER_SEED", "222")
    monkeypatch.setenv("INTERLAYER_TOP_N", "22")
    cfg = load_settings(path, seed=333)
    assert (cfg.seed, cfg.top_n, cfg.min_cluster_size) == (333, 22, 11)


def test_a_none_keyword_does_not_override(tmp_path: Path) -> None:
    """The CLI passes every flag, unset ones as ``None``; they must not win."""
    path = write_yaml(tmp_path, "seed: 111\n")
    assert load_settings(path, seed=None, input_csv=None).seed == 111


def test_environment_values_are_coerced_to_field_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERLAYER_KEEP_EMAILS", "true")
    monkeypatch.setenv("INTERLAYER_EMAIL_HMAC_KEY", "k")
    monkeypatch.setenv("INTERLAYER_ARTIFACT_DIR", "/tmp/arts")
    monkeypatch.setenv("INTERLAYER_PAGERANK_ALPHA", "0.5")
    cfg = load_settings()
    assert cfg.keep_emails is True
    assert cfg.artifact_dir == Path("/tmp/arts")
    assert cfg.pagerank_alpha == 0.5


def test_guards_still_fire_on_environment_supplied_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERLAYER_KEEP_EMAILS", "1")
    with pytest.raises(ConfigError, match="email_hmac_key"):
        load_settings()


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERLAYER_NOT_A_FIELD", "x")
    monkeypatch.setenv("INTERLAYERISH", "x")
    assert load_settings().seed == DEFAULT_SEED


def test_missing_config_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml")


def test_a_directory_is_not_a_config_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path)


def test_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "seed: [unclosed\n  bad: :\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_settings(path)


@pytest.mark.parametrize("body", ["- a\n- b\n", "just a string\n", "42\n"])
def test_non_mapping_yaml_raises_config_error(tmp_path: Path, body: str) -> None:
    path = write_yaml(tmp_path, body)
    with pytest.raises(ConfigError, match="must contain a mapping"):
        load_settings(path)


def test_empty_yaml_file_is_treated_as_no_overrides(tmp_path: Path) -> None:
    assert load_settings(write_yaml(tmp_path, "")) == Settings()


def test_unknown_key_in_yaml_is_reported_as_config_error(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "not_a_setting: 1\n")
    with pytest.raises(ConfigError, match="not_a_setting"):
        load_settings(path)


def test_wrong_type_in_yaml_is_reported_as_config_error(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "seed: not-an-int\n")
    with pytest.raises(ConfigError, match="seed"):
        load_settings(path)


def test_out_of_range_value_in_yaml_is_reported_as_config_error(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "pagerank_alpha: 2.0\n")
    with pytest.raises(ConfigError, match="pagerank_alpha"):
        load_settings(path)


def test_paths_from_yaml_become_path_objects(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "artifact_dir: out\ninput_csv: Connections.csv\n")
    cfg = load_settings(path)
    assert cfg.artifact_dir == Path("out")
    assert cfg.input_csv == Path("Connections.csv")
    assert cfg.people_path == Path("out/people.jsonl")


def test_nested_weights_can_be_overridden_from_yaml(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "weights:\n  observed_mutual: 20.0\n  proximity: 1.0\n")
    weights = load_settings(path).weights
    assert weights.observed_mutual == 20.0
    assert weights.proximity == 1.0
    assert weights.direct_employment == ScoreWeights().direct_employment


def test_unknown_weight_key_is_rejected(tmp_path: Path) -> None:
    path = write_yaml(tmp_path, "weights:\n  vibes: 1.0\n")
    with pytest.raises(ConfigError, match="vibes"):
        load_settings(path)


def test_settings_from_the_same_inputs_compare_equal(tmp_path: Path) -> None:
    """Determinism starts here: the same file must give the same configuration."""
    path = write_yaml(tmp_path, "seed: 7\nresolution: 1.5\n")
    assert load_settings(path) == load_settings(path)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"seed": 5}, "seed"),
        ({"redact": True}, "redact"),
        ({"top_n": 3}, "top_n"),
    ],
)
def test_keyword_overrides_apply_without_a_file(kwargs: dict[str, Any], expected: str) -> None:
    assert getattr(load_settings(**kwargs), expected) == kwargs[expected]
