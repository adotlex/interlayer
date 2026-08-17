"""`interlayer purge` — erasure, and refusing to guess what to erase.

Purging everything and purging one person are different acts with different
consequences, so the command will not infer which one was meant. Run against
the real store, because the property that matters — a purged person stays
purged across a re-ingest — lives in the tombstone, not in the CLI.
"""

from __future__ import annotations

import types
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.core.models import Firm, Member, Provenance, Target

runner = CliRunner()

PROV = Provenance(source="test", collected_at=datetime(2026, 8, 17, tzinfo=UTC))


def _open(state_root):
    from interlayer.core.config import load_config
    from interlayer.core.store import open_store

    return open_store(load_config(overrides={"state_root": state_root}))


@pytest.fixture
def seeded(tmp_path):
    store = _open(tmp_path)
    try:
        store.add_members(
            [
                Member(member_id="m_alpha", first_name="Dana", last_name="Wu", provenance=PROV),
                Member(member_id="m_beta", first_name="Alex", last_name="Rivera", provenance=PROV),
            ]
        )
        store.add_targets(
            [Target(target_id="t_one", firm=Firm.CITADEL, first_name="J", last_name="Doe",
                    provenance=PROV)]
        )
    finally:
        store.close()
    return tmp_path


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


def test_purge_all_asks_first_and_a_no_changes_nothing(seeded) -> None:
    result = runner.invoke(cli.app, ["purge", "--all", "--state-root", str(seeded)], input="n\n")

    assert result.exit_code != 0
    assert "Permanently erase" in result.output

    store = _open(seeded)
    try:
        assert store.member_count() == 2
    finally:
        store.close()


def test_purge_all_removes_everything_when_confirmed(seeded) -> None:
    result = runner.invoke(cli.app, ["purge", "--all", "--state-root", str(seeded)], input="y\n")
    assert result.exit_code == 0, result.output
    assert "purged: all" in result.output

    store = _open(seeded)
    try:
        assert store.member_count() == 0
        assert store.target_count() == 0
    finally:
        store.close()


def test_yes_skips_the_prompt(seeded) -> None:
    result = runner.invoke(cli.app, ["purge", "--all", "--yes", "--state-root", str(seeded)])
    assert result.exit_code == 0, result.output
    assert "Permanently erase" not in result.output


def test_purge_person_erases_only_that_person(seeded) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--person", "m_alpha", "-y", "--state-root", str(seeded)]
    )
    assert result.exit_code == 0, result.output
    assert "purged: person m_alpha" in result.output
    assert "tombstones_written" in result.output

    store = _open(seeded)
    try:
        remaining = {m.member_id for m in store.members()}
        assert remaining == {"m_beta"}
        assert store.is_tombstoned("m_alpha")
    finally:
        store.close()


def test_a_purged_person_stays_purged_across_a_re_ingest(seeded) -> None:
    runner.invoke(cli.app, ["purge", "--person", "m_alpha", "-y", "--state-root", str(seeded)])

    store = _open(seeded)
    try:
        store.add_members(
            [Member(member_id="m_alpha", first_name="Dana", last_name="Wu", provenance=PROV)]
        )
        assert {m.member_id for m in store.members()} == {"m_beta"}
    finally:
        store.close()


def test_purge_target(seeded) -> None:
    result = runner.invoke(
        cli.app, ["purge", "--target", "t_one", "-y", "--state-root", str(seeded)]
    )
    assert result.exit_code == 0, result.output
    assert "purged: target t_one" in result.output

    store = _open(seeded)
    try:
        assert store.target_count() == 0
    finally:
        store.close()


def test_a_store_that_raises_is_explained_not_traced(monkeypatch, tmp_path) -> None:
    """Any fault inside a component becomes a sentence, never a stack trace."""
    module = types.ModuleType("interlayer.core.retention")

    def startup(config):
        raise RuntimeError("database is locked")

    module.startup = startup  # type: ignore[attr-defined]
    real_import = cli._import
    monkeypatch.setattr(
        cli,
        "_import",
        lambda name: module if name == "interlayer.core.retention" else real_import(name),
    )

    result = runner.invoke(cli.app, ["purge", "--all", "-y", "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "failed inside interlayer.core.retention" in result.output
    assert "RuntimeError: database is locked" in result.output
    assert "Traceback" not in result.output
