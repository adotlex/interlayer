"""`interlayer analyse` — driving the graph engine and storing the snapshot.

The graph engine belongs to another work package, so it is stubbed here. What
is under test is the CLI's half of the contract: calling the entry point,
refusing to accept the wrong return type, writing a snapshot the rendering
commands can read, and warning about incomplete coverage and a non-empty review
queue at the point the number is produced rather than only in the report.
"""

from __future__ import annotations

import json
import types

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.report.json_out import load_result
from test_report_fixtures import make_result

runner = CliRunner()


def _graph_module(result, record: dict | None = None):
    module = types.ModuleType("interlayer.graph")

    def analyse(*, state_root=None, include_inferred=False):
        if record is not None:
            record["state_root"] = state_root
            record["include_inferred"] = include_inferred
        return result

    module.analyse = analyse  # type: ignore[attr-defined]
    return module


@pytest.fixture
def graph(monkeypatch):
    def _install(result, record=None):
        monkeypatch.setattr(cli, "_import", lambda name: _graph_module(result, record))

    return _install


def test_writes_a_snapshot_the_report_command_can_read(graph, tmp_path) -> None:
    expected = make_result(unadjudicated_count=2)
    graph(expected)

    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output

    snapshot = tmp_path / "analysis.json"
    assert snapshot.exists()
    restored, _ = load_result(json.loads(snapshot.read_text(encoding="utf-8")))
    assert restored == expected

    rendered = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert rendered.exit_code == 0, rendered.output


def test_passes_the_state_root_and_the_inference_flag_through(graph, tmp_path) -> None:
    record: dict = {}
    graph(make_result(), record)

    runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path), "--include-inferred"])

    assert record["state_root"] == tmp_path
    assert record["include_inferred"] is True


def test_inferred_edges_are_off_by_default(graph, tmp_path) -> None:
    record: dict = {}
    graph(make_result(), record)
    runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert record["include_inferred"] is False


def test_incomplete_coverage_is_warned_about_immediately(graph, tmp_path) -> None:
    graph(make_result(n_targets_total=300, n_targets_harvested=141))
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert "141/300" in result.output
    assert "lower bound" in result.output


def test_complete_coverage_is_not_warned_about(graph, tmp_path) -> None:
    graph(make_result(n_targets_total=300, n_targets_harvested=300))
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert "lower bound" not in result.output


def test_a_non_empty_review_queue_is_surfaced(graph, tmp_path) -> None:
    graph(make_result(unadjudicated_count=9))
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert "9 needs_review item(s) held out of the graph" in result.output
    assert "interlayer review" in result.output


def test_the_snapshot_location_can_be_overridden(graph, tmp_path) -> None:
    graph(make_result())
    out = tmp_path / "nested" / "run-1.json"
    result = runner.invoke(
        cli.app, ["analyse", "--state-root", str(tmp_path), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
