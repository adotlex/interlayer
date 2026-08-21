"""The CLI as a program an operator types, not as an import surface.

Everything here spawns the real console script. ``uv run interlayer`` has been
observed failing to spawn inside this sandbox, so ``.venv/bin/interlayer`` is
invoked directly -- it is the same entry point ``uv run`` would resolve to.

Exit codes are the contract (``cli.py``): 0 success, 1 unexpected, 2 usage,
3 a deliberate interlayer error, 4 a stage run before its input existed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from interlayer import __version__
from interlayer.cli import STAGES
from interlayer.models import Person, ScoredPerson
from tests.integration import (
    CLI,
    MANIFEST,
    STAGE_COMMANDS,
    PipelineRun,
    build_rows,
    cli,
    read_json,
    read_models,
    run_all_stages,
    shared_run,
    write_connections_csv,
    write_observations,
)

COMMANDS: tuple[str, ...] = (*STAGE_COMMANDS, "run", "purge")


@pytest.fixture(scope="module")
def observed() -> PipelineRun:
    return shared_run("observed")


@pytest.fixture(scope="module")
def redacted() -> PipelineRun:
    return shared_run("redacted")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A throwaway directory holding an export, an observations file and artifacts."""
    rows = build_rows()
    write_connections_csv(tmp_path / "Connections.csv", rows)
    write_observations(tmp_path / "observations.yaml", rows)
    (tmp_path / "artifacts").mkdir()
    return tmp_path


def test_the_console_script_is_installed() -> None:
    assert CLI.is_file(), f"{CLI} does not exist; run `uv sync --locked`"


# ---------------------------------------------------------------------------
# help and version
# ---------------------------------------------------------------------------


def test_version_prints_the_version_and_exits_zero() -> None:
    result = cli("--version")
    assert result.returncode == 0, result
    assert result.output.strip() == __version__


def test_help_exits_zero_and_lists_every_command() -> None:
    result = cli("--help")
    assert result.returncode == 0, result
    for command in COMMANDS:
        assert command in result.output, f"--help never mentions {command!r}"


@pytest.mark.parametrize("command", COMMANDS)
def test_each_subcommand_help_exits_zero_and_names_itself(command: str) -> None:
    result = cli(command, "--help")
    assert result.returncode == 0, result
    assert command in result.output


def test_the_command_list_matches_the_stage_list() -> None:
    """``run`` iterates ``STAGES``; the CLI names the graph stage ``build``."""
    assert set(STAGES) - {"graph"} <= set(STAGE_COMMANDS)
    assert "build" in STAGE_COMMANDS


def test_no_arguments_prints_help_and_exits_zero() -> None:
    """A bare invocation is a request for help, not a usage error.

    ``cli._root`` handles ``ctx.invoked_subcommand is None`` by printing the help
    and raising ``typer.Exit(0)``. The trap this pins down is ``Typer(...,
    no_args_is_help=True)``: it intercepts before the root callback and exits 2,
    printing the same help text, which makes the regression invisible to any
    assertion that only looks at output.
    """
    result = cli()
    assert "Usage: interlayer" in result.output
    assert "Commands" in result.output
    assert result.returncode == 0, result


def test_unknown_command_is_a_usage_error() -> None:
    result = cli("frobnicate")
    assert result.returncode == 2, result
    assert "frobnicate" in result.output


def test_unknown_option_is_a_usage_error() -> None:
    result = cli("ingest", "--not-a-flag")
    assert result.returncode == 2, result


# ---------------------------------------------------------------------------
# errors an operator will actually hit
# ---------------------------------------------------------------------------


def test_a_config_error_exits_three(tmp_path: Path) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("keep_emails: true\n", encoding="utf-8")
    result = cli("normalize", "--config", str(config), "--artifact-dir", str(tmp_path))
    assert result.returncode == 3, result
    assert "configuration error" in result.output
    assert "email_hmac_key" in result.output


def test_a_missing_config_file_exits_three(tmp_path: Path) -> None:
    result = cli("normalize", "--config", str(tmp_path / "nope.yaml"))
    assert result.returncode == 3, result
    assert "config file not found" in result.output


def test_malformed_config_yaml_exits_three(tmp_path: Path) -> None:
    config = tmp_path / "settings.yaml"
    config.write_text("targets: [unclosed\n", encoding="utf-8")
    result = cli("normalize", "--config", str(config))
    assert result.returncode == 3, result
    assert "not valid YAML" in result.output


def test_a_missing_input_file_is_a_readable_error_not_a_traceback(tmp_path: Path) -> None:
    result = cli("ingest", str(tmp_path / "absent.csv"), "--artifact-dir", str(tmp_path))
    assert result.returncode == 3, result
    assert "Traceback" not in result.output, result
    assert "absent.csv" in result.output
    assert "not found" in result.output.casefold()


def test_a_directory_given_where_a_csv_was_expected_is_readable(tmp_path: Path) -> None:
    result = cli("ingest", str(tmp_path), "--artifact-dir", str(tmp_path / "artifacts"))
    assert result.returncode in {2, 3}, result
    assert "Traceback" not in result.output, result


def test_a_file_that_is_not_a_connections_export_says_so(tmp_path: Path) -> None:
    junk = tmp_path / "notes.csv"
    junk.write_text("alpha,beta\n1,2\n", encoding="utf-8")
    result = cli("ingest", str(junk), "--artifact-dir", str(tmp_path / "artifacts"))
    assert "Traceback" not in result.output, result
    assert result.returncode in {0, 3}, result
    assert "Connections.csv" in result.output or "header" in result.output.casefold()


# ---------------------------------------------------------------------------
# the first-run notice
# ---------------------------------------------------------------------------


def test_the_first_run_notice_appears_and_says_results_are_inferred(
    workspace: Path,
) -> None:
    result = cli(
        "ingest",
        str(workspace / "Connections.csv"),
        "--artifact-dir",
        str(workspace / "artifacts"),
        cwd=workspace,
    )
    assert result.returncode == 0, result
    assert "INFERRED" in result.output
    assert "no scraper" in result.output.casefold()
    assert "never as established fact" in result.output


def test_run_shows_the_notice_too(observed: PipelineRun) -> None:
    assert "INFERRED" in observed.result.output
    assert "leads to verify" in observed.result.output


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_executes_every_stage_and_produces_a_report(observed: PipelineRun) -> None:
    assert observed.result.returncode == 0, observed.result
    for stage in STAGES:
        assert f"-> {stage}" in observed.result.output, f"{stage} never announced itself"
    assert "done" in observed.result.output
    assert observed.path("report.html").is_file()
    assert observed.path(MANIFEST).is_file()


def test_run_reports_where_the_report_went(observed: PipelineRun) -> None:
    assert str(observed.path("report.html")) in observed.result.output.replace("\n", "")


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_with_yes_deletes_without_prompting(workspace: Path) -> None:
    artifacts = workspace / "artifacts"
    run = run_all_stages(workspace / "Connections.csv", artifacts, cwd=workspace)
    assert run.returncode == 0, run
    assert list(artifacts.iterdir())

    result = cli("purge", "--artifact-dir", str(artifacts), "--yes", cwd=workspace, stdin="")
    assert result.returncode == 0, result
    assert "Permanently delete" not in result.output, "--yes still prompted"
    assert not artifacts.exists()


def test_purge_without_yes_prompts_and_keeps_everything(workspace: Path) -> None:
    artifacts = workspace / "artifacts"
    (artifacts / "people.jsonl").write_text("{}\n", encoding="utf-8")
    result = cli("purge", "--artifact-dir", str(artifacts), cwd=workspace, stdin="\n")
    assert result.returncode == 0, result
    assert "Permanently delete" in result.output
    assert (artifacts / "people.jsonl").is_file(), "declining the prompt still deleted"


def test_purge_of_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    result = cli("purge", "--artifact-dir", str(tmp_path / "gone"), "--yes")
    assert result.returncode == 0, result
    assert "nothing to purge" in result.output


def test_purge_does_not_touch_the_hand_collected_observations(workspace: Path) -> None:
    """Hours of manual collection must not live where a routine cleanup destroys it."""
    artifacts = workspace / "artifacts"
    observations = workspace / "observations.yaml"
    run_all_stages(
        workspace / "Connections.csv", artifacts, observations=observations, cwd=workspace
    )
    cli("purge", "--artifact-dir", str(artifacts), "--yes", cwd=workspace)
    assert observations.is_file(), "purge deleted the operator's observation file"


# ---------------------------------------------------------------------------
# --redact
# ---------------------------------------------------------------------------


def test_redact_propagates_into_the_report(redacted: PipelineRun) -> None:
    assert redacted.result.returncode == 0, redacted.result
    manifest = read_json(redacted.path(MANIFEST))
    assert manifest["stage_versions"]["report.mode"] == "redacted"

    html = redacted.path("report.html").read_text(encoding="utf-8")
    people = read_models(redacted.path("people.jsonl"), Person)
    leaked = [p.full_name for p in people if p.full_name and p.full_name in html]
    assert not leaked, f"redacted report leaks {leaked[:5]}"


def test_without_redact_the_report_is_identified(observed: PipelineRun) -> None:
    manifest = read_json(observed.path(MANIFEST))
    assert manifest["stage_versions"]["report.mode"] == "identified"
    html = observed.path("report.html").read_text(encoding="utf-8")
    scored = read_models(observed.path("scored_people.jsonl"), ScoredPerson)
    top = max(scored, key=lambda p: p.score)
    assert top.full_name in html


def test_redact_flag_on_the_report_subcommand_alone(workspace: Path) -> None:
    artifacts = workspace / "artifacts"
    assert run_all_stages(workspace / "Connections.csv", artifacts, cwd=workspace).returncode == 0
    result = cli("report", "--artifact-dir", str(artifacts), "--redact", cwd=workspace)
    assert result.returncode == 0, result
    assert read_json(artifacts / MANIFEST)["stage_versions"]["report.mode"] == "redacted"


# ---------------------------------------------------------------------------
# --seed
# ---------------------------------------------------------------------------


def test_seed_reaches_the_manifest(observed: PipelineRun) -> None:
    assert read_json(observed.path(MANIFEST))["seed"] == 4242


def test_the_same_seed_changes_nothing(workspace: Path) -> None:
    """``--seed`` equal to the value already in effect must be a no-op."""
    first = workspace / "first"
    second = workspace / "second"
    for artifacts in (first, second):
        artifacts.mkdir()
    csv_path = workspace / "Connections.csv"
    assert run_all_stages(csv_path, first, seed=99, cwd=workspace).returncode == 0
    assert run_all_stages(csv_path, second, seed=99, cwd=workspace).returncode == 0
    for name in ("people.jsonl", "orgs.jsonl", "edges.jsonl", "clusters.jsonl", "report.html"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_a_different_seed_is_still_a_valid_run(workspace: Path) -> None:
    artifacts = workspace / "artifacts"
    result = run_all_stages(workspace / "Connections.csv", artifacts, seed=1, cwd=workspace)
    assert result.returncode == 0, result
    assert read_json(artifacts / MANIFEST)["seed"] == 1
    assert read_models(artifacts / "scored_people.jsonl", ScoredPerson)
