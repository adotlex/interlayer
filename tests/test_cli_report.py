"""`interlayer report` — rendering a stored analysis in three formats."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.report import (
    PURPOSE_NOTICE,
    REDISTRIBUTION_NOTICE,
    RETENTION_TEMPLATE,
    THIRD_PARTY_NOTICE,
)
from interlayer.report.json_out import dump_result
from interlayer.report.redact import has_contact_details
from test_report_fixtures import LEAKY_NAMES, make_result

runner = CliRunner()


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A state root holding a snapshot, with no other component available."""
    def _raise(name: str):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(cli, "_import", _raise)
    snapshot = tmp_path / "analysis.json"
    snapshot.write_text(
        json.dumps(dump_result(make_result(used_inferred_edges=True, unadjudicated_count=4),
                               dict(LEAKY_NAMES))),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize("fmt", ["markdown", "md", "html", "json"])
def test_every_format_renders(state, fmt: str) -> None:
    result = runner.invoke(cli.app, ["report", "-f", fmt, "--state-root", str(state)])
    assert result.exit_code == 0, result.output
    assert THIRD_PARTY_NOTICE in result.output
    assert REDISTRIBUTION_NOTICE in result.output
    assert PURPOSE_NOTICE in result.output


@pytest.mark.parametrize("fmt", ["markdown", "html", "json"])
def test_no_contact_details_reach_the_terminal(state, fmt: str) -> None:
    """PRIV-17 end to end: the snapshot carries names, the output does not."""
    result = runner.invoke(cli.app, ["report", "-f", fmt, "--state-root", str(state)])
    assert result.exit_code == 0
    assert not has_contact_details(result.output)
    assert "Dana Wu" in result.output  # the person survives; the address does not


def test_writes_to_a_file_when_asked(state, tmp_path) -> None:
    out = tmp_path / "out" / "report.html"
    result = runner.invoke(
        cli.app, ["report", "-f", "html", "-o", str(out), "--state-root", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert THIRD_PARTY_NOTICE in text
    assert str(out) in result.output


def test_reads_an_explicit_snapshot(state, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(dump_result(make_result())), encoding="utf-8")
    result = runner.invoke(cli.app, ["report", "-i", str(elsewhere), "--state-root", str(state)])
    assert result.exit_code == 0, result.output


def test_a_name_map_can_be_supplied_separately(state, tmp_path) -> None:
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"m_alpha": "Wendy Okafor"}), encoding="utf-8")
    result = runner.invoke(
        cli.app, ["report", "--names", str(names), "--state-root", str(state)]
    )
    assert result.exit_code == 0, result.output
    assert "Wendy Okafor" in result.output


def test_missing_snapshot_explains_what_to_run(tmp_path) -> None:
    result = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "no analysis snapshot" in result.output
    assert "interlayer analyse" in result.output
    assert "Traceback" not in result.output


def test_a_foreign_json_file_is_rejected_clearly(tmp_path) -> None:
    snapshot = tmp_path / "analysis.json"
    snapshot.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    result = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "not a usable interlayer analysis snapshot" in result.output
    assert "Traceback" not in result.output


def test_a_corrupt_file_is_rejected_clearly(tmp_path) -> None:
    (tmp_path / "analysis.json").write_text("{not json", encoding="utf-8")
    result = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "cannot read the analysis snapshot" in result.output


def test_unknown_format_is_a_usage_error(state) -> None:
    result = runner.invoke(cli.app, ["report", "-f", "pdf", "--state-root", str(state)])
    assert result.exit_code == 2
    assert "unknown format" in result.output
    assert "markdown, html, json" in result.output


def test_retention_falls_back_to_the_documented_default(state) -> None:
    """With core.config unavailable the header still states a real number."""
    result = runner.invoke(cli.app, ["report", "--state-root", str(state)])
    assert RETENTION_TEMPLATE.format(days=90) in result.output


def test_retention_comes_from_config_when_it_is_available(tmp_path, monkeypatch) -> None:
    import types

    snapshot = tmp_path / "analysis.json"
    snapshot.write_text(json.dumps(dump_result(make_result())), encoding="utf-8")

    module = types.ModuleType("interlayer.core.config")

    class _Config:
        retention_days = 14
        retain_emails = True
        include_inferred = False

    module.load_config = lambda state_root=None: _Config()  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "_import", lambda name: module)

    result = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert RETENTION_TEMPLATE.format(days=14) in result.output


def test_state_root_can_come_from_the_environment(state, monkeypatch) -> None:
    monkeypatch.setenv("INTERLAYER_STATE_ROOT", str(state))
    result = runner.invoke(cli.app, ["report"])
    assert result.exit_code == 0, result.output
