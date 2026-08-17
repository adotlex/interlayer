"""A missing component is a sentence, never a traceback.

The other work packages are built in parallel and any of them may be absent or
broken when a user runs this. Every command that reaches into one must say
which piece is unavailable and what still works, and exit non-zero without
raising through to the terminal.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.report.json_out import dump_result
from test_report_fixtures import make_result

runner = CliRunner()

#: Commands that need another work package, with a minimal valid invocation.
DEPENDENT = {
    "init": ["init"],
    "targets": ["targets"],
    "review": ["review"],
    "analyse": ["analyse"],
    "purge": ["purge", "--all", "--yes"],
}

#: Commands that must keep working with nothing else built.
SELF_SUFFICIENT = ("report", "next", "privacy")


@pytest.fixture
def nothing_importable(monkeypatch):
    def _raise(name: str):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(cli, "_import", _raise)


@pytest.mark.parametrize("name,argv", DEPENDENT.items(), ids=list(DEPENDENT))
def test_missing_component_produces_a_message_not_a_traceback(
    name: str, argv: list[str], nothing_importable, tmp_path
) -> None:
    result = runner.invoke(cli.app, [*argv, "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert result.output.startswith("error: ")
    assert "Traceback" not in result.output
    assert "component" in result.output
    # The user is told what still works.
    assert "report" in result.output


def test_ingest_reports_a_missing_component_too(nothing_importable, tmp_path) -> None:
    csv_path = tmp_path / "Connections.csv"
    csv_path.write_text("First Name,Last Name,URL\nA,B,\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["ingest", str(csv_path), "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "interlayer.ingest.connections_csv" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("command", SELF_SUFFICIENT)
def test_rendering_commands_still_work_with_nothing_else_built(
    command: str, nothing_importable, tmp_path
) -> None:
    snapshot = tmp_path / "analysis.json"
    snapshot.write_text(json.dumps(dump_result(make_result())), encoding="utf-8")
    privacy = tmp_path / "PRIVACY.md"
    privacy.write_text("# Privacy\n", encoding="utf-8")

    argv = {
        "report": ["report", "--state-root", str(tmp_path)],
        "next": ["next", "--state-root", str(tmp_path)],
        "privacy": ["privacy", "--path", str(privacy)],
    }[command]

    result = runner.invoke(cli.app, argv)
    assert result.exit_code == 0, result.output


def test_a_component_that_exists_but_lacks_the_entry_point_is_explained(
    monkeypatch, tmp_path
) -> None:
    """Present-but-wrong is a different failure from absent, and says so."""
    import types

    empty = types.ModuleType("interlayer.graph")
    monkeypatch.setattr(cli, "_import", lambda name: empty)

    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "no entry point" in result.output
    assert "Expected one of: analyse" in result.output
    assert "docs/wave2-notes/B5.md" in result.output
    assert "Traceback" not in result.output
