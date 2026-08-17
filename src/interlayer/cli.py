"""Command-line interface.

Every sibling package is imported **lazily, inside the command that needs it**.
That is not a performance choice: ``interlayer.cli`` must import successfully
even when a component is missing or broken, so that ``--help`` still works and
the user gets a sentence explaining what is unavailable instead of a traceback
from a package they have never heard of.

The commands that only render — ``report`` and ``next`` — depend on nothing but
``core.models`` and this repository's own ``report`` package. They read the
analysis snapshot that ``analyse`` writes, so a stored result stays readable
even if the graph engine cannot be loaded.

Where a command must call into another work package, the entry point it looks
for is declared in :data:`INTEGRATION` rather than hard-coded at the call site.
That table is the integration contract; ``docs/wave2-notes/B5.md`` records it.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, NoReturn

import typer

APP_HELP = """Find the people in your own LinkedIn network who bridge into a target firm.

interlayer is an offline analysis tool. It never contacts LinkedIn: the only
data it sees is what you exported or captured yourself. Start with 'init',
then 'ingest' your Connections.csv, then 'analyse' and 'report'.
"""

app = typer.Typer(
    name="interlayer",
    help=APP_HELP,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)

DEFAULT_STATE_ROOT = Path.home() / ".interlayer"
SNAPSHOT_NAME = "analysis.json"
PRIVACY_FILENAME = "PRIVACY.md"

#: What each command needs from another work package, and the entry points it
#: will accept. Aliases exist because these packages are built in parallel;
#: the first name in each tuple is the contract, the rest are tolerated.
INTEGRATION: dict[str, tuple[str, tuple[str, ...]]] = {
    "config": ("interlayer.core.config", ("load_config", "load", "Config")),
    "store": ("interlayer.core.store", ("open_store", "Store", "connect", "open")),
    "connections": (
        "interlayer.ingest.connections_csv",
        ("parse_connections", "read_connections", "parse_file", "parse", "load"),
    ),
    "targets": (
        "interlayer.ingest.targets",
        ("load_targets", "load_registry", "load", "all_targets", "registry"),
    ),
    "review": (
        "interlayer.ingest.review",
        ("pending", "list_pending", "load_queue", "queue"),
    ),
    "graph": (
        "interlayer.graph",
        ("analyse", "analyze", "run", "run_pipeline", "build_analysis"),
    ),
    "retention": (
        "interlayer.core.retention",
        ("purge", "purge_all", "sweep"),
    ),
}


# ---------------------------------------------------------------------------
# Failure handling — a missing component is a message, never a traceback
# ---------------------------------------------------------------------------


def _abort(message: str, hint: str | None = None, code: int = 2) -> NoReturn:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    if hint:
        typer.secho(f"hint:  {hint}", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(code)


def _import(name: str) -> ModuleType:
    """Single indirection for every lazy import.

    Kept as a named function so a test can simulate a half-built source tree
    without depending on which packages happen to exist yet.
    """
    return importlib.import_module(name)


def _require(key: str, feature: str) -> ModuleType:
    """Import the component registered under *key*, or exit cleanly."""
    module_name, _ = INTEGRATION[key]
    try:
        return _import(module_name)
    except ImportError as exc:
        _abort(
            f"'{feature}' needs the {module_name} component, which this install does "
            f"not provide ({exc}).",
            hint=(
                "That part of interlayer is not built or failed to import. "
                "Commands that only read a stored result -- 'report', 'next', "
                "'privacy' -- still work."
            ),
        )


def _entry(module: ModuleType, key: str, feature: str) -> Any:
    """Find the callable this CLI was written against, or exit cleanly."""
    _, candidates = INTEGRATION[key]
    for name in candidates:
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    _abort(
        f"'{feature}' found {module.__name__} but no entry point in it.",
        hint=(
            "Expected one of: " + ", ".join(candidates) + ". "
            "See docs/wave2-notes/B5.md for the contract the CLI calls."
        ),
    )


def _call(fn: Any, feature: str, **available: Any) -> Any:
    """Call *fn* with whichever of *available* its signature actually accepts.

    The components this CLI drives are written by other work packages, so their
    exact signatures are not known here. Passing only the parameters a callable
    declares is more robust than guessing, and a parameter that cannot be
    satisfied produces an explanation rather than a TypeError.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return fn()

    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    kwargs = {k: v for k, v in available.items() if k in sig.parameters or accepts_kwargs}

    missing = [
        name
        for name, p in sig.parameters.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in kwargs
    ]
    if missing:
        _abort(
            f"'{feature}' cannot call {getattr(fn, '__name__', fn)}: it requires "
            f"{', '.join(missing)}, which the CLI does not know how to supply.",
            hint="See docs/wave2-notes/B5.md for the contract the CLI calls.",
        )
    return fn(**kwargs)


# ---------------------------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------------------------


def _state_root(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.expanduser()
    env = os.environ.get("INTERLAYER_STATE_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_STATE_ROOT


def _snapshot_path(state_root: Path, explicit: Path | None = None) -> Path:
    return explicit if explicit is not None else state_root / SNAPSHOT_NAME


def _config_values(state_root: Path) -> dict[str, Any]:
    """Config values the report header is obliged to state.

    Falls back to the documented defaults when ``core.config`` is unavailable,
    because a report that cannot state its retention period is worse than one
    that states the default.
    """
    defaults: dict[str, Any] = {
        "retention_days": 90,
        "retain_emails": False,
        "include_inferred": False,
    }
    module_name, candidates = INTEGRATION["config"]
    try:
        module = _import(module_name)
    except ImportError:
        return defaults
    for name in candidates:
        fn = getattr(module, name, None)
        if fn is None:
            continue
        try:
            cfg = fn(state_root=state_root) if _accepts(fn, "state_root") else fn()
        except Exception:
            # A broken config must not stop a report rendering. The header will
            # state the documented defaults, which is honest and still useful.
            continue
        for key in defaults:
            value = getattr(cfg, key, None)
            if value is not None:
                defaults[key] = value
        return defaults
    return defaults


def _accepts(fn: Any, param: str) -> bool:
    try:
        return param in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False


def _describe(obj: Any) -> str:
    """One-line rendering of a record from a package whose shape is not fixed."""
    for attrs in (
        ("target_id", "firm", "first_name", "last_name", "title"),
        ("member_id", "first_name", "last_name", "company_raw"),
        ("raw", "status", "firm", "rule", "score"),
    ):
        if hasattr(obj, attrs[0]):
            parts = []
            for a in attrs:
                v = getattr(obj, a, None)
                if v not in (None, ""):
                    parts.append(f"{a}={getattr(v, 'value', v)}")
            return "  ".join(parts)
    return str(obj)


def _emit_records(records: Any, empty: str) -> None:
    if isinstance(records, dict):
        records = list(records.values())
    try:
        items = list(records)
    except TypeError:
        typer.echo(str(records))
        return
    if not items:
        typer.echo(empty)
        return
    for item in items:
        typer.echo(_describe(item))
    typer.echo("")
    typer.echo(f"{len(items)} record(s).")


def _load_snapshot(path: Path) -> tuple[Any, dict[str, str]]:
    from interlayer.report.json_out import load_result

    if not path.exists():
        _abort(
            f"no analysis snapshot at {path}.",
            hint="Run 'interlayer analyse' first, or pass --input with a snapshot file.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _abort(f"cannot read the analysis snapshot at {path}: {exc}")
    try:
        return load_result(payload)
    except (KeyError, TypeError, ValueError) as exc:
        _abort(f"{path} is not a usable interlayer analysis snapshot: {exc}")


# ---------------------------------------------------------------------------
# Shared option types
# ---------------------------------------------------------------------------

StateRootOpt = Annotated[
    Path | None,
    typer.Option(
        "--state-root",
        help="Where interlayer keeps its database, logs and snapshots. "
        "Defaults to $INTERLAYER_STATE_ROOT, else ~/.interlayer.",
    ),
]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def init(
    state_root: StateRootOpt = None,
    force: Annotated[
        bool, typer.Option("--force", help="Rewrite the config even if one already exists.")
    ] = False,
) -> None:
    """Create the local state directory and write the default configuration.

    Safe to run twice. The defaults are the private ones: retention 90 days,
    emails discarded at parse time, inferred edges excluded from analysis, and
    the first-party export as the only enabled acquisition adapter.
    """
    root = _state_root(state_root)
    module = _require("config", "init")
    fn = _entry(module, "config", "init")
    root.mkdir(parents=True, exist_ok=True)
    result = _call(fn, "init", state_root=root, force=force, create=True)
    typer.echo(f"state root: {root}")
    if result is not None:
        for key in ("retention_days", "retain_emails", "include_inferred", "enabled_adapters"):
            value = getattr(result, key, None)
            if value is not None:
                typer.echo(f"  {key}: {value}")
    typer.echo("")
    typer.echo("Next: interlayer ingest <path-to-Connections.csv>")


@app.command()
def ingest(
    connections: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Connections.csv from your official LinkedIn data export "
            "(Settings > Data privacy > Get a copy of your data).",
        ),
    ],
    state_root: StateRootOpt = None,
    retain_emails: Annotated[
        bool,
        typer.Option(
            "--retain-emails/--no-retain-emails",
            help="Keep the Email Address column. Off by default; addresses are "
            "discarded at parse time. Reports redact them either way.",
        ),
    ] = False,
) -> None:
    """Load your connection list — the set M — from the official data export.

    This file gives you every person you are connected to and no edges at all.
    Edges come later, from the mutual-connection surfaces you capture yourself.
    """
    root = _state_root(state_root)
    module = _require("connections", "ingest")
    fn = _entry(module, "connections", "ingest")
    records = _call(
        fn,
        "ingest",
        path=connections,
        source=connections,
        state_root=root,
        retain_emails=retain_emails,
    )
    count = len(records) if hasattr(records, "__len__") else "an unknown number of"
    typer.echo(f"parsed {count} connections from {connections}")
    if not retain_emails:
        typer.echo("email addresses discarded at parse time (PRIV-06)")


@app.command()
def targets(
    state_root: StateRootOpt = None,
    firm: Annotated[
        str | None,
        typer.Option("--firm", help="Show only one firm, e.g. jane_street or citadel."),
    ] = None,
) -> None:
    """Show the target registry — the people and firms you are trying to reach.

    Citadel and Citadel Securities are separate entries on purpose: they are
    legally distinct firms with separate LinkedIn pages, and merging them
    silently corrupts per-firm results.
    """
    root = _state_root(state_root)
    module = _require("targets", "targets")
    fn = _entry(module, "targets", "targets")
    records = _call(fn, "targets", state_root=root, firm=firm)
    if firm and not _accepts(fn, "firm"):
        records = [r for r in records if str(getattr(getattr(r, "firm", ""), "value", "")) == firm]
    _emit_records(records, "No targets registered.")


@app.command()
def review(
    state_root: StateRootOpt = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Show at most this many.")] = 50,
) -> None:
    """List the ambiguous company matches waiting for a human decision.

    These records are held out of the graph until someone adjudicates them. No
    fuzzy threshold separates 'Jane Street Capital LLC' from 'Jane Street
    Entertainment', so the tool refuses to guess and asks instead.
    """
    root = _state_root(state_root)
    module = _require("review", "review")
    fn = _entry(module, "review", "review")
    records = _call(fn, "review", state_root=root, limit=limit)
    _emit_records(records, "Review queue is empty. Nothing is being held out of the graph.")


@app.command()
def analyse(
    state_root: StateRootOpt = None,
    include_inferred: Annotated[
        bool,
        typer.Option(
            "--include-inferred/--no-include-inferred",
            help="Let inferred (guessed) edges into the graph. Off by default. "
            "Inferred values stay marked as such wherever they are rendered.",
        ),
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Where to write the analysis snapshot."),
    ] = None,
) -> None:
    """Build the bipartite graph, project it, cluster it and rank the bridges.

    Pure and offline: no network access, and the same inputs always produce the
    same fingerprint. Writes a snapshot that 'report' and 'next' then read.
    """
    from interlayer.report.json_out import dump_result

    root = _state_root(state_root)
    module = _require("graph", "analyse")
    fn = _entry(module, "graph", "analyse")
    result = _call(fn, "analyse", state_root=root, include_inferred=include_inferred)
    if not hasattr(result, "coverage") or not hasattr(result, "brokerage"):
        _abort(
            f"the graph component returned {type(result).__name__}, not an AnalysisResult.",
            hint="See docs/wave2-notes/B5.md for the contract the CLI calls.",
        )

    destination = _snapshot_path(root, out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(dump_result(result), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    cov = result.coverage
    typer.echo(f"bridges: {len(result.brokerage)}   clusters: {len(result.clusters)}")
    typer.echo(
        f"coverage: {cov.n_targets_harvested}/{cov.n_targets_total} targets harvested "
        f"({cov.fraction * 100:.1f}%)"
    )
    if cov.n_targets_harvested < cov.n_targets_total:
        typer.secho(
            "coverage is incomplete, so every reach figure is a lower bound",
            fg=typer.colors.YELLOW,
        )
    if result.unadjudicated_count:
        typer.secho(
            f"{result.unadjudicated_count} needs_review item(s) held out of the graph "
            "-- run 'interlayer review'",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"snapshot: {destination}")


@app.command()
def report(
    fmt: Annotated[
        str, typer.Option("--format", "-f", help="markdown, html or json.")
    ] = "markdown",
    out: Annotated[
        Path | None,
        typer.Option("--out", "-o", help="Write here instead of standard output."),
    ] = None,
    input_path: Annotated[
        Path | None,
        typer.Option("--input", "-i", help="Analysis snapshot to render."),
    ] = None,
    names: Annotated[
        Path | None,
        typer.Option(
            "--names",
            help="Optional JSON object mapping member/target ids to display names.",
        ),
    ] = None,
    title: Annotated[
        str | None, typer.Option("--title", help="Override the report title.")
    ] = None,
    state_root: StateRootOpt = None,
) -> None:
    """Render a stored analysis as Markdown, HTML or JSON.

    Every format carries the same handling notice, marks inferred values so
    they cannot be mistaken for observed ones, states how many review items are
    still unadjudicated, and reports coverage before it reports any finding.
    Email addresses and phone numbers are stripped unconditionally.
    """
    from interlayer.report import ReportContext, render

    root = _state_root(state_root)
    result, snapshot_names = _load_snapshot(_snapshot_path(root, input_path))

    name_map = dict(snapshot_names)
    if names is not None:
        try:
            name_map.update(json.loads(names.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            _abort(f"cannot read the name map at {names}: {exc}")

    cfg = _config_values(root)
    ctx = ReportContext(
        retention_days=int(cfg["retention_days"]),
        names=name_map,
        title=title or "interlayer bridge report",
    )

    try:
        rendered = render(fmt, result, ctx)
    except KeyError as exc:
        _abort(str(exc.args[0]))

    if out is None:
        typer.echo(rendered)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered, encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command("next")
def next_targets(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 10,
    input_path: Annotated[
        Path | None, typer.Option("--input", "-i", help="Analysis snapshot to read.")
    ] = None,
    state_root: StateRootOpt = None,
) -> None:
    """Show which targets to harvest next, and why each one ranked.

    Ranked by how much a target would change the picture, not by seniority.
    Harvest these before concluding anything about who has reach: an
    unharvested target cannot produce an edge, so thin coverage and a thin
    network look identical from the numbers alone.
    """
    from interlayer.report import sorted_next_targets

    root = _state_root(state_root)
    result, name_map = _load_snapshot(_snapshot_path(root, input_path))

    cov = result.coverage
    typer.echo(
        f"coverage {cov.n_targets_harvested}/{cov.n_targets_total} "
        f"({cov.fraction * 100:.1f}%) -- reach figures are "
        f"{'exact' if cov.n_targets_harvested >= cov.n_targets_total > 0 else 'lower bounds'}"
    )
    typer.echo("")

    ranked = sorted_next_targets(result)[:limit]
    if not ranked:
        typer.echo(
            "Nothing to suggest. Every known target is harvested, or there is no "
            "structure yet."
        )
        return
    for rank, n in enumerate(ranked, start=1):
        label = name_map.get(n.target_id, n.target_id)
        typer.echo(
            f"{rank:>3}. {label}   priority {n.priority:.3f}   "
            f"{n.n_known_bridges} known bridge(s)   "
            f"{n.n_clusters_touched} cluster(s)   novelty {n.novelty:.3f}"
        )


def _privacy_candidates(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit]
    candidates = []
    env = os.environ.get("INTERLAYER_PRIVACY_FILE")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    # src/interlayer/cli.py -> src/interlayer -> src -> repository root
    candidates.extend(parent / PRIVACY_FILENAME for parent in here.parents[:4])
    candidates.append(Path.cwd() / PRIVACY_FILENAME)
    return candidates


@app.command()
def privacy(
    path: Annotated[
        Path | None, typer.Option("--path", help="Read the notice from here instead.")
    ] = None,
) -> None:
    """Print PRIVACY.md — what this tool stores about other people, and why.

    The people in your target list are third-party data subjects who have not
    been asked. That is a real obligation, not a formality, and the notice
    states the lawful basis, the retention period and how to erase a person.
    """
    for candidate in _privacy_candidates(path):
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        typer.echo(text, nl=False)
        return
    _abort(
        f"{PRIVACY_FILENAME} was not found.",
        hint=(
            "It ships at the repository root. Pass --path, or set "
            "INTERLAYER_PRIVACY_FILE, to point at your copy."
        ),
        code=1,
    )


@app.command()
def purge(
    purge_all: Annotated[
        bool,
        typer.Option("--all", help="Delete every record, the database and the audit log."),
    ] = False,
    person: Annotated[
        str | None,
        typer.Option("--person", help="Erase one connection by member id, leaving a tombstone."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Erase one target by target id, leaving a tombstone."),
    ] = None,
    state_root: StateRootOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
    ] = False,
) -> None:
    """Erase stored data. Tombstones survive re-ingest, so erasure sticks.

    Exactly one of --all, --person or --target. A person you erase stays erased:
    the tombstone means a later re-ingest of the same Connections.csv will not
    quietly bring them back.
    """
    chosen = [flag for flag in (purge_all, person is not None, target is not None) if flag]
    if len(chosen) != 1:
        _abort(
            "give exactly one of --all, --person <member-id> or --target <target-id>.",
            hint="Purging everything and purging one person are different acts.",
        )

    root = _state_root(state_root)
    scope = "all" if purge_all else ("person" if person else "target")
    identifier = person or target

    if not yes:
        what = (
            f"every record under {root}"
            if purge_all
            else f"{scope} {identifier} under {root}"
        )
        typer.confirm(f"Permanently erase {what}?", abort=True)

    module = _require("retention", "purge")
    fn = _entry(module, "retention", "purge")
    _call(
        fn,
        "purge",
        state_root=root,
        scope=scope,
        identifier=identifier,
        member_id=person,
        target_id=target,
        all=purge_all,
    )
    typer.echo(f"purged: {scope}" + (f" {identifier}" if identifier else ""))


if __name__ == "__main__":  # pragma: no cover
    app()
