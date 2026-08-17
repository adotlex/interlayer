"""`interlayer purge` — erasure, and refusing to guess what to erase.

Purging everything and purging one person are different acts with different
consequences, so the command will not infer which one was meant.
"""

from __future__ import annotations

import types

import pytest
from typer.testing import CliRunner

from interlayer import cli

runner = CliRunner()


@pytest.fixture
def retention(monkeypatch):
    """A stub for the retention component, recording how it was called."""
    calls: list[dict] = []
    module = types.ModuleType("interlayer.core.retention")

    def purge(*, state_root=None, scope=None, identifier=None):
        calls.append({"state_root": state_root, "scope": scope, "identifier": identifier})
        return None

    module.purge = purge  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "_import", lambda name: module)
    return calls


def test_no_scope_is_refused(tmp_path) -> None:
    result = runner.invoke(cli.app, ["purge", "--state-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "exactly one of --all, --person" in result.output


def test_two_scopes_are_refused(tmp_path) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--all", "--person", "m_alpha", "--state-root", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "exactly one of --all, --person" in result.output


def test_purge_all_asks_first(retention, tmp_path) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--all", "--state-root", str(tmp_path)], input="n\n"
    )
    assert result.exit_code != 0
    assert "Permanently erase" in result.output
    assert retention == []


def test_purge_all_proceeds_when_confirmed(retention, tmp_path) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--all", "--state-root", str(tmp_path)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert retention == [{"state_root": tmp_path, "scope": "all", "identifier": None}]


def test_yes_skips_the_prompt(retention, tmp_path) -> None:
    result = runner.invoke(cli.app, ["purge", "--all", "--yes", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Permanently erase" not in result.output
    assert retention[0]["scope"] == "all"


def test_purge_person(retention, tmp_path) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--person", "m_alpha", "-y", "--state-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert retention == [{"state_root": tmp_path, "scope": "person", "identifier": "m_alpha"}]
    assert "purged: person m_alpha" in result.output


def test_purge_target(retention, tmp_path) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--target", "t_one", "-y", "--state-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert retention == [{"state_root": tmp_path, "scope": "target", "identifier": "t_one"}]


def test_a_component_taking_member_id_style_arguments_also_works(monkeypatch, tmp_path) -> None:
    """The CLI passes whichever parameter names the component declares."""
    calls: list[dict] = []
    module = types.ModuleType("interlayer.core.retention")

    def purge(*, state_root, member_id=None, target_id=None, all=False):
        calls.append({"member_id": member_id, "target_id": target_id, "all": all})

    module.purge = purge  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "_import", lambda name: module)

    runner.invoke(cli.app, ["purge", "--person", "m_beta", "-y", "--state-root", str(tmp_path)])

    assert calls == [{"member_id": "m_beta", "target_id": None, "all": False}]
