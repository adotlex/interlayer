"""`interlayer privacy` — print PRIVACY.md, and cope when it is not there.

PRIVACY.md is written by another work package, so this file may not exist yet.
The command must print it verbatim when it does and explain itself when it does
not; either way it must not raise.
"""

from __future__ import annotations

from typer.testing import CliRunner

from interlayer import cli

runner = CliRunner()

SAMPLE = """# Privacy notice

Controller: you. Lawful basis: legitimate interest.
Retention: 90 days.
"""


def test_prints_the_notice_verbatim(tmp_path) -> None:
    notice = tmp_path / "PRIVACY.md"
    notice.write_text(SAMPLE, encoding="utf-8")

    result = runner.invoke(cli.app, ["privacy", "--path", str(notice)])

    assert result.exit_code == 0
    assert result.output == SAMPLE


def test_finds_the_notice_via_the_environment(tmp_path, monkeypatch) -> None:
    notice = tmp_path / "PRIVACY.md"
    notice.write_text(SAMPLE, encoding="utf-8")
    monkeypatch.setenv("INTERLAYER_PRIVACY_FILE", str(notice))

    result = runner.invoke(cli.app, ["privacy"])

    assert result.exit_code == 0
    assert result.output == SAMPLE


def test_absence_is_explained_rather_than_raised(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INTERLAYER_PRIVACY_FILE", str(tmp_path / "nope.md"))
    monkeypatch.chdir(tmp_path)
    # Point the search away from the real repository copy.
    monkeypatch.setattr(cli, "_privacy_candidates", lambda explicit: [tmp_path / "nope.md"])

    result = runner.invoke(cli.app, ["privacy"])

    assert result.exit_code == 1
    assert "PRIVACY.md was not found" in result.output
    assert "--path" in result.output
    assert "Traceback" not in result.output


def test_an_explicit_missing_path_is_reported(tmp_path) -> None:
    result = runner.invoke(cli.app, ["privacy", "--path", str(tmp_path / "absent.md")])
    assert result.exit_code == 1
    assert "was not found" in result.output


def test_the_search_covers_the_repository_root() -> None:
    """PRIVACY.md ships at the repository root; the lookup must reach it.

    Asserted against the candidate list rather than the file, because the
    notice belongs to another work package and may not exist yet. This file
    owns the lookup, not the notice.
    """
    from pathlib import Path

    repo_root = Path(cli.__file__).resolve().parents[2]
    candidates = cli._privacy_candidates(None)

    assert repo_root / "PRIVACY.md" in candidates
    assert Path.cwd() / "PRIVACY.md" in candidates


def test_an_explicit_path_overrides_the_search(tmp_path) -> None:
    notice = tmp_path / "elsewhere.md"
    notice.write_text(SAMPLE, encoding="utf-8")
    assert cli._privacy_candidates(notice) == [notice]

    result = runner.invoke(cli.app, ["privacy", "--path", str(notice)])
    assert result.output == SAMPLE
