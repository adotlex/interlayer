"""Command-line interface.

Every sibling package is imported **lazily, inside the command that needs it**.
That is not a performance choice: ``interlayer.cli`` must import successfully
even when a component is missing or broken, so that ``--help`` still works and
the user gets a sentence explaining what is unavailable instead of a traceback
from a package they have never heard of.

Three guarantees hold for every command here.

* A component that is **absent** produces a message naming it. See
  :func:`_require`.
* A component that is **present but does not expose what the CLI calls**
  produces a message naming the symbol. See :func:`_symbol`.
* A component that **raises** produces a message naming the component and the
  fault. See :func:`_guard`. Nothing reaches the terminal as a traceback.

The commands that only render — ``report`` and ``next`` — depend on nothing but
``core.models`` and this repository's own ``report`` package. They read the
analysis snapshot that ``analyse`` writes, so a stored result stays readable
even when the graph engine cannot be loaded at all.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
from collections.abc import Iterator
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

#: The components this CLI drives, and what each one is for. Recorded in one
#: place because they are built by other work packages; ``docs/wave2-notes/
#: B5.md`` carries the same table as the integration contract.
INTEGRATION: dict[str, tuple[str, str]] = {
    "config": ("interlayer.core.config", "configuration"),
    "store": ("interlayer.core.store", "local database"),
    "retention": ("interlayer.core.retention", "retention sweep and erasure"),
    "connections": ("interlayer.ingest.connections_csv", "Connections.csv parser"),
    "targets": ("interlayer.ingest.targets", "target registry"),
    "graph": ("interlayer.graph", "graph engine"),
    "collect": ("interlayer.collect.registry", "acquisition layer"),
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
    without depending on which packages happen to exist at the time.
    """
    return importlib.import_module(name)


def _require(key: str, feature: str) -> ModuleType:
    """Import the component registered under *key*, or exit cleanly."""
    module_name, what = INTEGRATION[key]
    try:
        return _import(module_name)
    except ImportError as exc:
        _abort(
            f"'{feature}' needs the {module_name} component -- the {what} -- which this "
            f"install does not provide ({exc}).",
            hint=(
                "That part of interlayer is not built or failed to import. Commands that "
                "only read a stored result -- 'report', 'next', 'privacy' -- still work."
            ),
        )


def _symbol(module: ModuleType, feature: str, *names: str) -> Any:
    """Fetch the first of *names* the component actually exposes."""
    for name in names:
        candidate = getattr(module, name, None)
        if candidate is not None:
            return candidate
    _abort(
        f"'{feature}' found {module.__name__} but no entry point in it.",
        hint=(
            "Expected one of: " + ", ".join(names) + ". "
            "See docs/wave2-notes/B5.md for the contract the CLI calls."
        ),
    )


@contextlib.contextmanager
def _guard(feature: str, component: str) -> Iterator[None]:
    """Turn any fault inside a component into a sentence.

    A user running ``interlayer analyse`` should never be shown a stack trace
    through three packages. They should be told which component failed and
    what it said, and given a non-zero exit code.
    """
    try:
        yield
    except (typer.Exit, typer.Abort, KeyboardInterrupt):
        raise
    except Exception as exc:
        _abort(
            f"'{feature}' failed inside {component}: {type(exc).__name__}: {exc}",
            hint="See docs/wave2-notes/B5.md for the contract the CLI calls.",
        )


# ---------------------------------------------------------------------------
# Paths, configuration and the store
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


def _load_config(root: Path, feature: str) -> Any:
    module = _require("config", feature)
    loader = _symbol(module, feature, "load_config", "default_config")
    with _guard(feature, "interlayer.core.config"):
        try:
            return loader(overrides={"state_root": root})
        except TypeError:
            return loader()


@contextlib.contextmanager
def _open_store(root: Path, feature: str) -> Iterator[tuple[Any, Any, Any]]:
    """Open the store with the retention sweep run first, and always close it.

    Going through ``retention.startup`` rather than opening the store directly
    is what makes "old records are swept before anything reads them" a property
    of every command instead of a rule each one has to remember.
    """
    config = _load_config(root, feature)
    module = _require("retention", feature)
    startup = _symbol(module, feature, "startup")
    with _guard(feature, "interlayer.core.retention"):
        store, sweep = startup(config)
    try:
        yield store, config, sweep
    finally:
        with contextlib.suppress(Exception):
            store.close()


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
    module_name, _ = INTEGRATION["config"]
    try:
        module = _import(module_name)
    except ImportError:
        return defaults
    loader = getattr(module, "load_config", None) or getattr(module, "default_config", None)
    if loader is None:
        return defaults
    try:
        try:
            cfg = loader(overrides={"state_root": state_root})
        except TypeError:
            cfg = loader()
    except Exception:
        # A broken config must not stop a report rendering. The header will
        # state the documented defaults, which is honest and still useful.
        return defaults
    for key in defaults:
        value = getattr(cfg, key, None)
        if value is not None:
            defaults[key] = value
    return defaults


def _person_names(members: Any, targets: Any) -> dict[str, str]:
    """``id -> display name`` for the report layer.

    Built here rather than in the graph engine because it is a presentation
    concern: the analysis works in ids and never needs to know a name.
    """
    names: dict[str, str] = {}
    for m in members:
        label = " ".join(p for p in (m.first_name, m.last_name) if p).strip()
        if label:
            names[m.member_id] = label
    for t in targets:
        label = " ".join(p for p in (t.first_name, t.last_name) if p).strip()
        firm = getattr(getattr(t, "firm", None), "value", None)
        if label and firm:
            label = f"{label} ({firm})"
        if label:
            names[t.target_id] = label
    return names


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
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
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
def init(state_root: StateRootOpt = None) -> None:
    """Create the local state directory and open the database.

    Safe to run twice. The defaults are the private ones: retention 90 days,
    emails discarded at parse time, inferred edges excluded from analysis, and
    the first-party export as the only enabled acquisition adapter.
    """
    root = _state_root(state_root)
    with _open_store(root, "init") as (store, config, sweep):
        typer.echo(f"state root: {getattr(store, 'state_root', root)}")
        for key in (
            "retention_days",
            "retain_emails",
            "include_inferred",
            "allow_contact_fields",
            "enabled_adapters",
        ):
            value = getattr(config, key, None)
            if value is not None:
                typer.echo(f"  {key}: {value}")
        deleted = getattr(sweep, "deleted", None)
        if deleted:
            typer.echo(f"retention sweep removed: {deleted}")
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
            "(Settings > Data privacy > Get a copy of your data). A .zip archive "
            "of the whole export works too.",
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
    parse = _symbol(module, "ingest", "parse_connections_csv", "parse_connections", "parse")

    with _guard("ingest", "interlayer.ingest.connections_csv"):
        parsed = parse(connections, retain_emails=retain_emails)

    members = getattr(parsed, "members", parsed)
    with _open_store(root, "ingest") as (store, _config, _sweep):
        with _guard("ingest", "interlayer.core.store"):
            written = store.add_members(members)
        counts = _counts(store)

    typer.echo(f"parsed {len(members)} connections from {connections}")
    if written is not None:
        stored = getattr(written, "written", None)
        suppressed = getattr(written, "suppressed", None)
        if stored is not None:
            typer.echo(f"stored {stored}")
        if suppressed:
            typer.echo(f"{suppressed} suppressed by an existing tombstone")
    if counts:
        typer.echo("store: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for warning in getattr(parsed, "warnings", ()) or ():
        typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)
    if not retain_emails:
        typer.echo("email addresses discarded at parse time (PRIV-06)")


@app.command()
def collect(
    paths: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            readable=True,
            help="HAR files, hand-filled mutual-connection CSVs, or directories "
            "containing them. These carry the edges — who in your network is "
            "connected to whom at the target firms.",
        ),
    ],
    state_root: StateRootOpt = None,
    allow_manual_capture: Annotated[
        bool,
        typer.Option(
            "--allow-manual-capture",
            help="Enable the manual-capture collectors for this run. Off by "
            "default: the shipped configuration permits first-party exports "
            "only, so turning this on is a deliberate, logged act.",
        ),
    ] = False,
) -> None:
    """Load captured mutual connections — the edge set E.

    'ingest' gives you who you know. This gives you who they know inside the
    target firms, which is the part LinkedIn will not export and nobody sells.
    You produce these files yourself, by the procedure in
    docs/02-operator-runbook.md.
    """
    root = _state_root(state_root)
    module = _require("collect", "collect")
    load_all = _symbol(module, "collect", "load_all")

    # The registry takes files. Expand directories here so the documented
    # "point it at a folder of captures" workflow actually works.
    inputs: list[Path] = []
    for path in paths:
        if path.is_dir():
            inputs.extend(sorted(p for p in path.rglob("*") if p.is_file()))
        else:
            inputs.append(path)
    if not inputs:
        _abort(
            "no files found in " + ", ".join(str(p) for p in paths),
            hint="Point 'collect' at a .har file, a hand-filled CSV, or a "
            "directory containing them.",
        )

    with _open_store(root, "collect") as (store, config, _sweep):
        with _guard("collect", "interlayer.core.config"):
            # A property, not a method — calling it yields 'frozenset' object is
            # not callable, which is a traceback the user should never see.
            postures = frozenset(config.enabled_postures)
            if allow_manual_capture:
                posture_enum = _import("interlayer.core.models").CompliancePosture
                postures = postures | {posture_enum.MANUAL_CAPTURE}

        with _guard("collect", "interlayer.collect.registry"):
            result = load_all(inputs, allowed_postures=postures)

        with _guard("collect", "interlayer.core.store"):
            store.add_members(result.members)
            store.add_targets(result.targets)
            store.add_edges(result.edges)
            counts = _counts(store)

    typer.echo(
        f"read {len(result.members)} people, {len(result.targets)} targets, "
        f"{len(result.edges)} edges from {len(inputs)} file(s)"
    )
    if counts:
        typer.echo("store: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))

    truncated = getattr(result, "truncated_target_ids", ())
    if truncated:
        typer.secho(
            f"{len(truncated)} target(s) had a capped capture — for those, a missing "
            "edge is not evidence of no edge",
            fg=typer.colors.YELLOW,
        )

    # A configuration that permits no collector produces exactly the output of a
    # user with no network. Those two must never look the same, so this is the
    # loudest thing the command can say.
    blocked = False
    errored = False
    for diagnostic in getattr(result, "diagnostics", ()) or ():
        code = getattr(getattr(diagnostic, "code", None), "value", "")
        severity = getattr(getattr(diagnostic, "severity", None), "value", "info")
        colour = typer.colors.RED if severity in {"error", "fatal"} else typer.colors.YELLOW
        typer.secho(f"{severity}: {diagnostic.message}", fg=colour, err=True)
        if getattr(diagnostic, "remedy", None):
            typer.secho(f"remedy: {diagnostic.remedy}", fg=typer.colors.CYAN, err=True)
        if severity in {"error", "fatal"}:
            errored = True
        if "posture" in str(code):
            blocked = True

    if blocked:
        _abort(
            "no edges were read because no collector is enabled.",
            hint="Re-run with --allow-manual-capture, or add 'manual_capture' to "
            "enabled_adapters in your config. Without edges, 'analyse' will "
            "report zero bridges — which would mean the tool is switched off, "
            "not that your network is empty.",
        )

    # Reading nothing while reporting errors is a failure, and exiting 0 would
    # let a scripted pipeline carry on into an empty analysis.
    if errored and not result.edges:
        _abort(
            "no edges were read from any input.",
            hint="Check the diagnostics above. A capture file needs the exact "
            "header row from docs/02-operator-runbook.md.",
        )


def _counts(store: Any) -> dict[str, int]:
    try:
        return dict(store.counts())
    except Exception:
        return {}


@app.command()
def targets(
    firm: Annotated[
        str | None,
        typer.Option("--firm", help="Show only one firm, e.g. jane_street or citadel."),
    ] = None,
    registry: Annotated[
        Path | None,
        typer.Option("--registry", help="Read a targets.yaml from here instead."),
    ] = None,
    state_root: StateRootOpt = None,
) -> None:
    """Show the target registry — the firms you are trying to reach.

    Citadel and Citadel Securities are separate entries on purpose: they are
    legally distinct firms with separate LinkedIn pages, and merging them
    silently corrupts per-firm results.
    """
    root = _state_root(state_root)
    module = _require("targets", "targets")
    load = _symbol(module, "targets", "load_registry", "load")

    with _guard("targets", "interlayer.ingest.targets"):
        loaded = load(registry) if registry is not None else load()
        entities = list(getattr(loaded, "entities", loaded))

    if firm:
        entities = [e for e in entities if _firm_name(e) == firm]
    if not entities:
        typer.echo("No target entities registered." if not firm else f"No entities for {firm}.")
        return

    for entity in entities:
        name = getattr(entity, "canonical", getattr(entity, "id", entity))
        typer.echo(f"{getattr(entity, 'id', '?'):<24} {_firm_name(entity):<20} {name}")
    typer.echo("")
    typer.echo(f"{len(entities)} entity(ies).")

    with contextlib.suppress(typer.Exit), _open_store(root, "targets") as (store, _c, _s):
        counts = _counts(store)
        if counts.get("targets"):
            typer.echo(f"{counts['targets']} target person(s) harvested into the store.")


def _firm_name(entity: Any) -> str:
    firm = getattr(entity, "firm", None)
    return str(getattr(firm, "value", firm) or "")


@app.command()
def review(
    state_root: StateRootOpt = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Show at most this many.")] = 50,
    show_all: Annotated[
        bool, typer.Option("--all", help="Include items that have already been decided.")
    ] = False,
) -> None:
    """List the ambiguous company matches waiting for a human decision.

    These records are held out of the graph until someone adjudicates them. No
    fuzzy threshold separates 'Jane Street Capital LLC' from 'Jane Street
    Entertainment', so the tool refuses to guess and asks instead.
    """
    root = _state_root(state_root)
    with _open_store(root, "review") as (store, _config, _sweep):
        with _guard("review", "interlayer.core.store"):
            items = list(store.reviews(pending_only=not show_all))
        pending = len(items) if not show_all else _pending_count(store)

    if not items:
        typer.echo("Review queue is empty. Nothing is being held out of the graph.")
        return

    for item in items[:limit]:
        raw = getattr(item, "raw", "?")
        suggested = getattr(item, "firm", None) or getattr(item, "suggested_firm", None)
        typer.echo(
            f"{raw:<42} rule={getattr(item, 'rule', '')} "
            f"score={getattr(item, 'score', '')} "
            f"suggests={getattr(suggested, 'value', suggested) or '-'} "
            f"members={getattr(item, 'member_count', '')}"
        )
    if len(items) > limit:
        typer.echo(f"... and {len(items) - limit} more")
    typer.echo("")
    typer.echo(f"{pending} item(s) held OUT of the graph until adjudicated (PRIV-20).")


def _pending_count(store: Any) -> int:
    try:
        return int(store.pending_review_count())
    except Exception:
        return 0


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
    run = _symbol(module, "analyse", "analyse", "analyze", "run")

    with _open_store(root, "analyse") as (store, config, _sweep):
        with _guard("analyse", "interlayer.core.store"):
            members = list(store.members())
            target_records = list(store.targets())
            edges = list(store.edges())
            unadjudicated = _pending_count(store)
        names = _person_names(members, target_records)

    with _guard("analyse", "interlayer.graph"):
        result = _run_analysis(
            run,
            members=members,
            targets=target_records,
            edges=edges,
            include_inferred=include_inferred or bool(getattr(config, "include_inferred", False)),
            unadjudicated_count=unadjudicated,
            config=config,
        )

    if not hasattr(result, "coverage") or not hasattr(result, "brokerage"):
        _abort(
            f"the graph component returned {type(result).__name__}, not an AnalysisResult.",
            hint="See docs/wave2-notes/B5.md for the contract the CLI calls.",
        )

    destination = _snapshot_path(root, out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(dump_result(result, names), indent=2, ensure_ascii=False), encoding="utf-8"
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


def _run_analysis(
    run: Any, *, members: Any, targets: Any, edges: Any, config: Any, **kw: Any
) -> Any:
    """Call the graph entry point, passing tuning parameters only if it takes them."""
    optional = {
        "min_confidence": getattr(config, "min_inferred_confidence", None),
        "gamma": getattr(config, "resolution", None),
        "base_seed": getattr(config, "base_seed", None),
        "n_runs": getattr(config, "consensus_runs", None),
    }
    kwargs = {**kw, **{k: v for k, v in optional.items() if v is not None}}
    try:
        return run(members, targets, edges, **kwargs)
    except TypeError:
        # An engine with a narrower signature still gets the essentials.
        return run(members, targets, edges, **kw)


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
    exact = cov.n_targets_total > 0 and cov.n_targets_harvested >= cov.n_targets_total
    typer.echo(
        f"coverage {cov.n_targets_harvested}/{cov.n_targets_total} "
        f"({cov.fraction * 100:.1f}%) -- reach figures are "
        f"{'exact' if exact else 'lower bounds'}"
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
        what = f"every record under {root}" if purge_all else f"{scope} {identifier} under {root}"
        typer.confirm(f"Permanently erase {what}?", abort=True)

    with (
        _open_store(root, "purge") as (store, _config, _sweep),
        _guard("purge", "interlayer.core.store"),
    ):
        if purge_all:
            removed = store.purge_all()
            typer.echo(f"purged: all ({len(list(removed))} path(s) removed)")
            return
        result = store.purge_person(identifier) if person else store.purge_target(identifier)
    typer.echo(f"purged: {scope} {identifier}")
    for field in ("members_deleted", "targets_deleted", "edges_deleted", "tombstones_written"):
        value = getattr(result, field, None)
        if value is not None:
            typer.echo(f"  {field}: {value}")


if __name__ == "__main__":  # pragma: no cover
    app()
