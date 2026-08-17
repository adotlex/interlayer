"""The CLI must import and describe itself with nothing else built.

``interlayer.cli`` is the one module a user reaches first. If it imports a
sibling package at module scope, a half-built or broken component turns
``interlayer --help`` into a traceback about a package the user has never heard
of. So the sibling imports live inside the command bodies, and this file is
what keeps them there.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from interlayer import cli

runner = CliRunner()

COMMANDS = (
    "init",
    "ingest",
    "targets",
    "review",
    "analyse",
    "report",
    "next",
    "privacy",
    "purge",
)

#: Packages built by other work packages. None may be imported at module scope.
SIBLINGS = ("interlayer.core", "interlayer.ingest", "interlayer.graph",
            "interlayer.collect", "interlayer.enrichment")


def _module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.If):
            # `if TYPE_CHECKING:` blocks do not execute at runtime.
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom) and sub.module:
                    names.add(f"typing-only:{sub.module}")
    return names


def test_no_sibling_package_is_imported_at_module_scope() -> None:
    imported = _module_level_imports(Path(cli.__file__))
    offenders = [
        name
        for name in imported
        if any(name == s or name.startswith(s + ".") for s in SIBLINGS)
    ]
    assert offenders == [], f"cli.py imports {offenders} at module scope"


def test_the_app_is_named_app_for_the_entry_point() -> None:
    """pyproject declares `interlayer = "interlayer.cli:app"`."""
    assert hasattr(cli, "app")
    assert callable(cli.app)


def test_top_level_help_lists_every_command() -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_has_real_help_text(command: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"])
    assert result.exit_code == 0, result.output

    body = result.output.split("Options:")[0]
    body = body.split("\n", 1)[1] if "\n" in body else body
    prose = " ".join(line.strip() for line in body.splitlines() if line.strip())
    prose = prose.replace("Usage:", "").strip()
    assert len(prose) > 60, f"{command} has no substantive help: {prose!r}"


def test_running_with_no_arguments_shows_help_rather_than_failing_silently() -> None:
    result = runner.invoke(cli.app, [])
    assert "Usage:" in result.output
    for name in COMMANDS:
        assert name in result.output


def test_purge_declares_all_person_and_target() -> None:
    result = runner.invoke(cli.app, ["purge", "--help"])
    assert result.exit_code == 0
    for flag in ("--all", "--person", "--target"):
        assert flag in result.output


def test_report_declares_the_three_formats() -> None:
    result = runner.invoke(cli.app, ["report", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output
    assert "markdown" in result.output
    assert "html" in result.output
    assert "json" in result.output
