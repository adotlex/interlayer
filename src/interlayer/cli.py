"""Command-line surface.

Each stage is a package exposing ``run(cfg: Settings) -> None``. The CLI imports
them lazily, one command at a time, so that a stage still under construction
cannot break unrelated commands -- which matters while several agents build
different stages concurrently.

Exit codes: 0 success, 1 unexpected error, 2 usage error (typer's default),
3 a deliberate interlayer error, 4 a stage run before its input existed.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from interlayer import __version__
from interlayer.config import Settings, load_settings
from interlayer.errors import InterlayerError, StageInputMissingError

__all__ = ["app", "main"]

app = typer.Typer(
    name="interlayer",
    help="Map your LinkedIn network against target firms.",
    # Deliberately NOT no_args_is_help: that intercepts before the root callback
    # and exits 2. Asking for a program with no arguments is not a usage error,
    # so the callback prints help and exits 0.
    add_completion=False,
)
console = Console(stderr=True)

STAGES: tuple[str, ...] = (
    "ingest",
    "normalize",
    "enrich",
    "graph",
    "cluster",
    "score",
    "report",
)

FIRST_RUN_NOTICE = """\
interlayer works on data you already have: your own LinkedIn data export, plus
mutual-connection observations you collect by hand.

It ships no scraper. Automating a logged-in LinkedIn session breaches the User
Agreement and risks losing the account this workflow depends on.

Results below a confidence of 1.0 are INFERRED, not observed. Treat them as
leads to verify, never as established fact about a person.
"""


def _run_stage(name: str, cfg: Settings) -> None:
    """Import and execute one stage, translating its failures to exit codes."""
    try:
        module = importlib.import_module(f"interlayer.{name}")
    except ModuleNotFoundError as exc:  # pragma: no cover - scaffold safety net
        console.print(f"[red]stage {name!r} is not available:[/] {exc}")
        raise typer.Exit(1) from exc
    run = getattr(module, "run", None)
    if run is None:
        console.print(f"[red]stage {name!r} does not expose run(cfg)[/]")
        raise typer.Exit(1)
    try:
        run(cfg)
    except StageInputMissingError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(4) from exc
    except InterlayerError as exc:
        console.print(f"[red]{name} failed:[/] {exc}")
        raise typer.Exit(3) from exc


def _settings(
    config: Path | None,
    artifact_dir: Path | None = None,
    input_csv: Path | None = None,
    seed: int | None = None,
    redact: bool | None = None,
    include_speculative: bool | None = None,
) -> Settings:
    try:
        return load_settings(
            config,
            artifact_dir=artifact_dir,
            input_csv=input_csv,
            seed=seed,
            redact=redact,
            include_speculative=include_speculative,
        )
    except InterlayerError as exc:
        console.print(f"[red]configuration error:[/] {exc}")
        raise typer.Exit(3) from exc


ConfigOpt = Annotated[Path | None, typer.Option("--config", "-c", help="YAML settings file.")]
ArtifactOpt = Annotated[
    Path | None, typer.Option("--artifact-dir", "-a", help="Where artifacts are written.")
]
SpeculativeOpt = Annotated[
    bool | None,
    typer.Option(
        "--include-speculative/--no-include-speculative",
        help="Render rows with almost no evidence behind them. Off by default.",
    ),
]
SeedOpt = Annotated[int | None, typer.Option("--seed", help="Global determinism seed.")]
RedactOpt = Annotated[
    bool | None, typer.Option("--redact/--no-redact", help="Emit pseudonymous ids, not names.")
]


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Print the version and exit.")] = False,
) -> None:
    """Handle --version before typer demands a subcommand."""
    if version:
        console.print(__version__)
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(0)


@app.command()
def ingest(
    input_csv: Annotated[
        Path, typer.Argument(help="Connections.csv from your LinkedIn data export.")
    ],
    config: ConfigOpt = None,
    artifact_dir: ArtifactOpt = None,
    seed: SeedOpt = None,
) -> None:
    """Parse a LinkedIn export into people and connection records."""
    console.print(FIRST_RUN_NOTICE)
    _run_stage("ingest", _settings(config, artifact_dir, input_csv, seed))


@app.command()
def normalize(config: ConfigOpt = None, artifact_dir: ArtifactOpt = None) -> None:
    """Resolve raw employer and school strings to canonical organisations."""
    _run_stage("normalize", _settings(config, artifact_dir))


@app.command()
def enrich(config: ConfigOpt = None, artifact_dir: ArtifactOpt = None) -> None:
    """Fold in hand-collected mutual-connection observations."""
    _run_stage("enrich", _settings(config, artifact_dir))


@app.command()
def build(config: ConfigOpt = None, artifact_dir: ArtifactOpt = None) -> None:
    """Build the weighted person-person graph from affiliations and observations."""
    _run_stage("graph", _settings(config, artifact_dir))


@app.command()
def cluster(
    config: ConfigOpt = None, artifact_dir: ArtifactOpt = None, seed: SeedOpt = None
) -> None:
    """Detect communities using consensus Leiden."""
    _run_stage("cluster", _settings(config, artifact_dir, seed=seed))


@app.command()
def score(config: ConfigOpt = None, artifact_dir: ArtifactOpt = None) -> None:
    """Rank people and clusters by adjacency to the target firms."""
    _run_stage("score", _settings(config, artifact_dir))


@app.command()
def report(
    config: ConfigOpt = None,
    artifact_dir: ArtifactOpt = None,
    redact: RedactOpt = None,
    include_speculative: SpeculativeOpt = None,
) -> None:
    """Render the self-contained HTML report."""
    _run_stage(
        "report",
        _settings(config, artifact_dir, redact=redact, include_speculative=include_speculative),
    )


@app.command()
def run(
    input_csv: Annotated[
        Path, typer.Argument(help="Connections.csv from your LinkedIn data export.")
    ],
    config: ConfigOpt = None,
    artifact_dir: ArtifactOpt = None,
    seed: SeedOpt = None,
    redact: RedactOpt = None,
    include_speculative: SpeculativeOpt = None,
) -> None:
    """Run every stage end to end."""
    console.print(FIRST_RUN_NOTICE)
    cfg = _settings(config, artifact_dir, input_csv, seed, redact, include_speculative)
    for name in STAGES:
        console.print(f"[dim]-> {name}[/]")
        _run_stage(name, cfg)
    console.print(f"[green]done[/] report written to {cfg.report_path}")


@app.command()
def purge(
    config: ConfigOpt = None,
    artifact_dir: ArtifactOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not prompt.")] = False,
) -> None:
    """Delete every artifact produced by this tool."""
    cfg = _settings(config, artifact_dir)
    target = cfg.artifact_dir
    if not target.exists():
        console.print(f"nothing to purge at {target}")
        raise typer.Exit(0)
    if not yes and not typer.confirm(f"Permanently delete {target} and all contents?"):
        raise typer.Exit(0)
    import shutil

    shutil.rmtree(target)
    console.print(f"[green]purged[/] {target}")


def main() -> None:  # pragma: no cover - console-script entry point
    app()
