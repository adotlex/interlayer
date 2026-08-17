"""`interlayer next` — what to harvest next, and the coverage caveat with it."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.report.json_out import dump_result
from test_report_fixtures import make_result

runner = CliRunner()


@pytest.fixture
def state(tmp_path, monkeypatch):
    def _raise(name: str):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(cli, "_import", _raise)
    (tmp_path / "analysis.json").write_text(
        json.dumps(dump_result(make_result(), {"t_two": "K. Chen", "t_three": "R. Bell"})),
        encoding="utf-8",
    )
    return tmp_path


def test_lists_targets_in_priority_order(state) -> None:
    result = runner.invoke(cli.app, ["next", "--state-root", str(state)])
    assert result.exit_code == 0, result.output
    assert result.output.index("K. Chen") < result.output.index("R. Bell")


def test_says_why_each_target_ranked(state) -> None:
    result = runner.invoke(cli.app, ["next", "--state-root", str(state)])
    assert "priority" in result.output
    assert "known bridge(s)" in result.output
    assert "cluster(s)" in result.output
    assert "novelty" in result.output


def test_leads_with_coverage_so_the_list_is_read_in_context(state) -> None:
    result = runner.invoke(cli.app, ["next", "--state-root", str(state)])
    assert result.output.index("coverage 141/300") < result.output.index("K. Chen")
    assert "lower bounds" in result.output


def test_limit_is_respected(state) -> None:
    result = runner.invoke(cli.app, ["next", "-n", "1", "--state-root", str(state)])
    assert "K. Chen" in result.output
    assert "R. Bell" not in result.output


def test_an_exhausted_target_set_says_so(tmp_path) -> None:
    empty = make_result(empty=True, n_targets_total=10, n_targets_harvested=10)
    (tmp_path / "analysis.json").write_text(json.dumps(dump_result(empty)), encoding="utf-8")
    result = runner.invoke(cli.app, ["next", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Nothing to suggest" in result.output
    assert "exact" in result.output


def test_missing_snapshot_explains_what_to_run(tmp_path) -> None:
    result = runner.invoke(cli.app, ["next", "--state-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "interlayer analyse" in result.output
    assert "Traceback" not in result.output
