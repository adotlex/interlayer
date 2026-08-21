"""Integration-test support: synthetic exports, CLI drivers, artifact helpers.

Everything here exists to make the *composition* of the six stages testable.
Unit tests get to stub a stage's neighbours; an integration test may not, so it
needs (a) a realistic ``Connections.csv``, (b) a Tier 2 observations file whose
names actually resolve against that CSV, and (c) a way to drive the CLI as a
real subprocess. All three are built once, here, so the three test modules agree
on the same cast of people.

The generator is deterministic by construction: no RNG decides *who* exists,
only a fixed round-robin over explicit lists. A fixture whose contents depend on
a random seed would make a determinism test tautological.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "synthetic"
GAZETTEER = REPO_ROOT / "data" / "gazetteer" / "firms.yaml"

#: ``uv run interlayer`` has been observed failing to spawn inside this sandbox,
#: so the CLI is driven through the venv console script directly. Same entry
#: point, one less process in the way.
CLI = REPO_ROOT / ".venv" / "bin" / "interlayer"

M = TypeVar("M", bound=BaseModel)


# ---------------------------------------------------------------------------
# the synthetic export
# ---------------------------------------------------------------------------

PREAMBLE = (
    '"Notes:"\n'
    '"When exporting your connection data, you may notice that some of the email '
    "addresses are missing. You will only see email addresses for connections who "
    "have allowed their connections to see or download their email address using "
    'this setting https://www.linkedin.com/psettings/privacy/email."\n'
    "\n"
)

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"

#: Employer strings that must resolve to a target firm.
JANE_STREET_EMPLOYERS: tuple[str, ...] = (
    "Jane Street",
    "Jane Street Capital",
    "Jane Street Group, LLC",
    "Jane Street",
    "Jane Street Capital LLC",
)
CITADEL_SECURITIES_EMPLOYERS: tuple[str, ...] = (
    "Citadel Securities",
    "Citadel Securities LLC",
    "Citadel Securities",
)
CITADEL_LLC_EMPLOYERS: tuple[str, ...] = (
    "Citadel",
    "Citadel LLC",
    "Citadel Advisors LLC",
    "Citadel",
)

#: Employer strings that share a token with a target and must never resolve to
#: one. ``The Citadel`` is the military college -- ~40k alumni against the
#: fund's ~3.1k employees, so it is the dominant false-positive source.
DECOY_EMPLOYERS: tuple[str, ...] = (
    "The Citadel",
    "Citadel Broadcasting",
    "The Citadel, The Military College of South Carolina",
    "Citadel Credit Union",
    "Jane Street Coffee",
    "Citadel Broadcasting Corporation",
    "The Citadel",
    "Jane Street Dental Practice",
    "Citadel Defense Company",
    "The Citadel Alumni Association",
    "Citadel Communications",
    "The Citadel",
    "Citadel Federal Credit Union",
    "Jane Street Bakery",
    "The Citadel Graduate College",
)

NEUTRAL_EMPLOYERS: tuple[str, ...] = (
    "Acme Widgets",
    "Globex Corporation",
    "Initech",
    "Umbrella Health",
    "Northwind Traders",
    "Contoso Ltd",
    "Hooli",
    "Vandelay Industries",
    "Stark Industries",
    "Wayne Enterprises",
    "Soylent Corp",
    "Massive Dynamic",
    "Cyberdyne Systems",
    "Tyrell Corporation",
)

TITLES: tuple[str, ...] = (
    "Quantitative Trader",
    "Software Engineer",
    "Senior Software Engineer",
    "Quantitative Researcher",
    "Head of Trading",
    "Data Analyst",
    "Vice President, Technology",
    "Product Manager",
    "Professor of History",
    "Account Executive",
    "Managing Director",
    "Intern",
)

#: Non-ASCII names are not decoration: NFKD folding, casefolding and the CSV
#: encoding path all have to survive them, and a slug built from a folded name
#: would collide two distinct people.
UNICODE_NAMES: tuple[tuple[str, str], ...] = (
    ("José", "Álvarez"),
    ("Zoë", "Müller"),
    ("Łukasz", "Nowak"),
    ("Ana Beatriz", "Souza"),
    ("Björn", "Åkerlund"),
    ("Нина", "Иванова"),
    ("محمد", "الأحمد"),
    ("太郎", "田中"),
    ("Sinéad", "Ó Briain"),
    ("François", "Lefèvre"),
)

ASCII_FIRST: tuple[str, ...] = (
    "Sarah",
    "Marcus",
    "Priya",
    "Daniel",
    "Elena",
    "Tom",
    "Rajiv",
    "Lena",
    "Omar",
    "Grace",
    "Hannah",
    "Victor",
    "Nadia",
    "Peter",
    "Ivy",
    "Kwame",
    "Mei",
    "Diego",
    "Ruth",
    "Samir",
)
ASCII_LAST: tuple[str, ...] = (
    "Chen",
    "Webb",
    "Raman",
    "Osei",
    "Vasquez",
    "Nakamura",
    "Patel",
    "Fischer",
    "Haddad",
    "Okonkwo",
    "Lindqvist",
    "Moreau",
    "Bianchi",
    "Novak",
    "Kaur",
    "Silva",
    "Andersen",
    "Kowalski",
    "Dubois",
    "Ferreira",
)


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the synthetic export, plus what it is *supposed* to become."""

    first: str
    last: str
    slug: str
    company: str
    title: str
    connected_on: str
    email: str = ""
    #: ``jane_street`` / ``citadel_llc`` / ``citadel_securities`` / ``None``.
    expect_firm: str | None = None

    @property
    def full_name(self) -> str:
        return f"{self.first} {self.last}".strip()

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/in/{self.slug}"

    def to_csv(self) -> str:
        cells = [
            self.first,
            self.last,
            self.url,
            self.email,
            self.company,
            self.title,
            self.connected_on,
        ]
        return ",".join(_csv_cell(c) for c in cells)


def _csv_cell(value: str) -> str:
    if any(ch in value for ch in ',"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value


FIRST_POOL: tuple[str, ...] = ASCII_FIRST + tuple(n[0] for n in UNICODE_NAMES)
LAST_POOL: tuple[str, ...] = ASCII_LAST + tuple(n[1] for n in UNICODE_NAMES)


def _name(index: int) -> tuple[str, str]:
    """A distinct full name for every index, with unicode mixed throughout.

    Names must be unique across the cast: the enrich stage resolves a typed
    mutual by name, and two people sharing one would make every observation
    ambiguous -- which would test the resolver's tie-break, not composition.
    Uniqueness is a co-prime walk over the two pools, extended with a
    hyphenated surname once the 30x30 grid is exhausted.
    """
    a = index % len(FIRST_POOL)
    b = (index // len(FIRST_POOL) + a) % len(LAST_POOL)
    grid = len(FIRST_POOL) * len(LAST_POOL)
    tier = index // grid
    last = LAST_POOL[b]
    if tier:
        last = f"{last}-{LAST_POOL[(tier * 7) % len(LAST_POOL)]}"
    return FIRST_POOL[a], last


def _connected_on(index: int) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{(index % 28) + 1:02d} {months[index % 12]} {2016 + (index % 9)}"


def _firm_of(company: str) -> str | None:
    lowered = company.casefold()
    if lowered.startswith("the citadel") or "broadcasting" in lowered:
        return None
    if "citadel securities" in lowered:
        return "citadel_securities"
    if lowered.startswith("citadel") and lowered.split()[-1] in {
        "citadel",
        "llc",
        "advisors",
        "l.l.c.",
    }:
        return "citadel_llc"
    if lowered in {"citadel", "citadel llc", "citadel advisors llc"}:
        return "citadel_llc"
    if lowered.startswith("jane street") and not any(
        w in lowered for w in ("coffee", "dental", "bakery", "cafe")
    ):
        return "jane_street"
    return None


def build_rows(*, neutral_people: int = 150) -> list[Row]:
    """The canonical synthetic cast: targets, decoys, neutrals, edge cases.

    Deterministic and explicit -- the same call always yields the same people in
    the same order, which is what lets a determinism test mean something.
    """
    rows: list[Row] = []
    index = 0

    def add(company: str, *, email: str = "", title: str | None = None) -> None:
        nonlocal index
        first, last = _name(index)
        slug = f"{_slugify(first)}-{_slugify(last)}-{index:04d}"
        rows.append(
            Row(
                first=first,
                last=last,
                slug=slug,
                company=company,
                title=TITLES[index % len(TITLES)] if title is None else title,
                connected_on=_connected_on(index),
                email=email,
                expect_firm=_firm_of(company),
            )
        )
        index += 1

    # Target-firm employees. Jane Street and Citadel Securities are deliberately
    # interleaved so that any bucket confusion between them shows up.
    for i in range(18):
        add(JANE_STREET_EMPLOYERS[i % len(JANE_STREET_EMPLOYERS)])
    for i in range(15):
        add(CITADEL_SECURITIES_EMPLOYERS[i % len(CITADEL_SECURITIES_EMPLOYERS)])
    for i in range(12):
        add(CITADEL_LLC_EMPLOYERS[i % len(CITADEL_LLC_EMPLOYERS)])

    # Decoys: same tokens, different companies.
    for i in range(len(DECOY_EMPLOYERS)):
        add(
            DECOY_EMPLOYERS[i],
            title="Professor of History" if "Citadel," in DECOY_EMPLOYERS[i] else None,
        )

    # The bulk: ordinary employers nobody is targeting.
    for i in range(neutral_people):
        add(NEUTRAL_EMPLOYERS[i % len(NEUTRAL_EMPLOYERS)])

    # Rows with no employer at all. LinkedIn emits these constantly.
    for _ in range(6):
        add("")

    # A row with an email, so keep_emails/HMAC has something to chew on.
    add("Globex Corporation", email="Sample.Person+tag@Example.COM")

    return rows


def _slugify(value: str) -> str:
    out = []
    for ch in value.casefold():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in {" ", "-", "'"}:
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "x"


def render_csv(rows: Sequence[Row], *, preamble: str = PREAMBLE) -> str:
    """Render rows as a LinkedIn export, preamble and all."""
    body = "\n".join(row.to_csv() for row in rows)
    return f"{preamble}{HEADER}\n{body}\n"


def write_connections_csv(path: Path, rows: Sequence[Row] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_csv(rows if rows is not None else build_rows()), encoding="utf-8")
    return path


def scale_rows(n: int) -> list[Row]:
    """A larger cast for the scale test; same shape, more neutrals."""
    fixed = len(build_rows(neutral_people=0))
    return build_rows(neutral_people=max(n - fixed, 0))


# ---------------------------------------------------------------------------
# the synthetic Tier 2 observations file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationPlan:
    """Who we claim to have read, and which of the ego's connections bridged."""

    target_name: str
    target_slug: str
    firm: str
    title: str
    bridges: tuple[str, ...]
    stated: int | None = None
    truncated: bool = False


def employer_cohort(rows: Sequence[Row], employer: str) -> list[Row]:
    """Everyone at ``employer``, in export order."""
    return [r for r in rows if r.company == employer]


#: Bridges are drawn one per employer from these, so that an observed edge is a
#: genuinely *new* edge rather than one the shared-employer projection already
#: produced. The second person at each employer is the matched control: same
#: employer, same inference, no observation.
BRIDGE_EMPLOYERS: dict[str, tuple[str, ...]] = {
    "jane_street": ("Globex Corporation", "Contoso Ltd", "Wayne Enterprises", "Soylent Corp"),
    "citadel_securities": ("Initech", "Umbrella Health", "Stark Industries"),
    "citadel_llc": ("Hooli", "Massive Dynamic"),
}


def bridge_rows(rows: Sequence[Row], firm: str) -> list[Row]:
    """The first person at each of ``firm``'s bridge employers."""
    return [employer_cohort(rows, e)[0] for e in BRIDGE_EMPLOYERS[firm]]


def control_rows(rows: Sequence[Row], firm: str) -> list[Row]:
    """The *second* person at each bridge employer -- matched inference-only controls."""
    return [employer_cohort(rows, e)[1] for e in BRIDGE_EMPLOYERS[firm]]


def build_observation_plans(rows: Sequence[Row]) -> list[ObservationPlan]:
    """Observations whose bridges are all neutral-employer people.

    Bridges are chosen from *neutral* employers on purpose. A bridge who already
    works at a target firm would score highly for that reason alone, and the
    "observed outranks inferred" assertion would prove nothing.
    """
    js = bridge_rows(rows, "jane_street")
    cs = bridge_rows(rows, "citadel_securities")
    cl = bridge_rows(rows, "citadel_llc")
    return [
        ObservationPlan(
            target_name="Priya Raman",
            target_slug="priya-raman-js-4821",
            firm="jane_street",
            title="Quantitative Trader",
            bridges=tuple(r.full_name for r in js),
            stated=len(js),
        ),
        ObservationPlan(
            target_name="Elena Vasquez",
            target_slug="elena-vasquez-cs",
            firm="citadel_securities",
            title="Senior Software Engineer, Low Latency",
            bridges=tuple(r.full_name for r in cs),
            stated=len(cs) + 9,
            truncated=True,
        ),
        ObservationPlan(
            target_name="Daniel Osei",
            target_slug="daniel-osei-cllc",
            firm="citadel_llc",
            title="Portfolio Manager",
            bridges=tuple(r.full_name for r in cl),
            stated=len(cl),
        ),
    ]


def render_observations(
    plans: Sequence[ObservationPlan], *, observed_on: str = "2026-08-01"
) -> str:
    """Emit the hand-authored YAML shape documented in docs/observations-template.yaml."""
    out = [f"observed_on: {observed_on}", "targets:"]
    for plan in plans:
        out.append(f"  - name: {plan.target_name}")
        out.append(f"    firm: {plan.firm}")
        out.append(f"    url: https://www.linkedin.com/in/{plan.target_slug}")
        out.append(f"    title: {plan.title}")
        if plan.stated is not None:
            out.append(f"    mutuals_stated: {plan.stated}")
        if plan.truncated:
            out.append("    truncated: yes")
        out.append("    mutuals: |")
        for bridge in plan.bridges:
            out.append(f"      {bridge}")
    return "\n".join(out) + "\n"


def write_observations(path: Path, rows: Sequence[Row]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_observations(build_observation_plans(rows)), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# artifact helpers
# ---------------------------------------------------------------------------

#: Every file the pipeline is contracted to leave behind, and whether it may be
#: empty. ``review_queue`` and ``unresolved_mutuals`` legitimately can be.
ARTIFACT_NAMES: tuple[str, ...] = (
    "people.jsonl",
    "connections.jsonl",
    "ingest_stats.json",
    "orgs.jsonl",
    "affiliations.jsonl",
    "review_queue.jsonl",
    "targets.jsonl",
    "observations.jsonl",
    "unresolved_mutuals.jsonl",
    "edges.jsonl",
    "graph_stats.json",
    "clusters.jsonl",
    "clusters_sweep.jsonl",
    "scored_people.jsonl",
    "scored_clusters.jsonl",
    "report.html",
    "manifest.json",
)

MAY_BE_EMPTY: frozenset[str] = frozenset(
    {"review_queue.jsonl", "unresolved_mutuals.jsonl", "targets.jsonl", "observations.jsonl"}
)


def file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def read_models(path: Path, model: type[M]) -> list[M]:
    """Parse a JSONL artifact as the model the next stage expects."""
    out: list[M] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(model.model_validate_json(line))
    return out


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


#: ``manifest.json`` is the one artifact that deliberately carries a clock, so a
#: byte-identity check has to compare it with the timestamp taken out.
CLOCK_FIELD = "created_at"
MANIFEST = "manifest.json"


def manifest_without_clock(path: Path) -> dict[str, Any]:
    payload = dict(read_json(path))
    payload.pop(CLOCK_FIELD, None)
    return payload


def artifact_digests(artifact_dir: Path, *, skip: Iterable[str] = ()) -> dict[str, bytes]:
    """Content of every artifact, keyed by name, for byte-identity comparisons."""
    skipped = set(skip)
    out: dict[str, bytes] = {}
    for path in sorted(artifact_dir.rglob("*")):
        if not path.is_file():
            continue
        name = str(path.relative_to(artifact_dir))
        if name in skipped:
            continue
        out[name] = hashlib.sha256(path.read_bytes()).digest()
    return out


# ---------------------------------------------------------------------------
# driving the CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CliResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def __str__(self) -> str:  # pragma: no cover - only used in failure messages
        return (
            f"interlayer {' '.join(self.args)} -> {self.returncode}\n"
            f"--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )


def cli(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 600.0,
    stdin: str | None = None,
) -> CliResult:
    """Run the real console script as a subprocess.

    A subprocess, not typer's ``CliRunner``: exit codes, the ``--version``
    short-circuit and ``PYTHONHASHSEED`` only mean anything in a separate
    process, and this is the surface an operator actually types.
    """
    environ = base_env()
    if env:
        environ.update(env)
    proc = subprocess.run(  # fixed executable, test-controlled args
        [str(CLI), *args],
        capture_output=True,
        text=True,
        env=environ,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        input=stdin,
        check=False,
    )
    return CliResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def base_env() -> dict[str, str]:
    """A clean environment: no inherited observations path, no colour codes.

    ``INTERLAYER_OBSERVATIONS`` is cleared because the enrich stage reads it, and
    a stray value in the developer's shell would silently change what a test
    proves. ``TERM=dumb``/``NO_COLOR`` keep rich from wrapping output in escape
    sequences that assertions then have to see through.
    """
    environ = dict(os.environ)
    for key in list(environ):
        if key.startswith("INTERLAYER_"):
            del environ[key]
    environ.update(
        {
            # ``Settings.gazetteer`` defaults to the *relative* path
            # ``data/gazetteer/firms.yaml``, so an invocation from anywhere but
            # the repository root fails in normalize. Pinning it absolutely makes
            # these tests independent of the working directory -- which they must
            # be, since the observation-file search walks the cwd.
            "INTERLAYER_GAZETTEER": str(GAZETTEER),
            "NO_COLOR": "1",
            "TERM": "dumb",
            "COLUMNS": "200",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return environ


def run_all_stages(
    csv_path: Path,
    artifact_dir: Path,
    *,
    observations: Path | None = None,
    seed: int | None = None,
    redact: bool = False,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> CliResult:
    """``interlayer run`` with the artifact dir and observations wired up."""
    env = dict(extra_env or {})
    if observations is not None:
        env["INTERLAYER_OBSERVATIONS"] = str(observations)
    args = ["run", str(csv_path), "--artifact-dir", str(artifact_dir)]
    if seed is not None:
        args += ["--seed", str(seed)]
    if redact:
        args.append("--redact")
    return cli(*args, env=env, cwd=cwd or artifact_dir.parent)


STAGE_COMMANDS: tuple[str, ...] = (
    "ingest",
    "normalize",
    "enrich",
    "build",
    "cluster",
    "score",
    "report",
)


def stage_args(command: str, csv_path: Path, artifact_dir: Path) -> list[str]:
    """Argument vector for one stage command; only ingest takes the CSV."""
    args = [command]
    if command == "ingest":
        args.append(str(csv_path))
    args += ["--artifact-dir", str(artifact_dir)]
    return args


def iter_stage_pairs() -> Iterator[tuple[str, str]]:
    """(predecessor, stage) pairs, so a test can run each stage too early."""
    for i in range(1, len(STAGE_COMMANDS)):
        yield STAGE_COMMANDS[i - 1], STAGE_COMMANDS[i]


@dataclass
class PipelineRun:
    """A completed end-to-end run plus everything a test wants to look at."""

    csv_path: Path
    artifact_dir: Path
    result: CliResult
    rows: list[Row] = field(default_factory=list)

    def path(self, name: str) -> Path:
        return self.artifact_dir / name


CSV_FIXTURE = FIXTURE_DIR / "connections_medium.csv"
OBSERVATIONS_FIXTURE = FIXTURE_DIR / "observations.yaml"


@cache
def shared_run(kind: str) -> PipelineRun:
    """One full ``interlayer run`` per flavour, per process, reused everywhere.

    A full run is ~2s, and three test modules want to interrogate the same
    output. Caching here rather than in a session fixture keeps a single run
    shared across modules -- an imported session-scoped fixture is re-registered
    per module and would execute once for each.
    """
    root = Path(tempfile.mkdtemp(prefix=f"interlayer-e2e-{kind}-"))
    atexit.register(shutil.rmtree, root, True)
    artifacts = root / "artifacts"
    artifacts.mkdir()
    rows = build_rows()
    csv_path = write_connections_csv(root / "Connections.csv", rows)
    observations: Path | None = None
    if kind in {"observed", "redacted"}:
        observations = write_observations(root / "observations.yaml", rows)
    result = run_all_stages(
        csv_path,
        artifacts,
        observations=observations,
        seed=4242,
        redact=kind == "redacted",
        cwd=root,
    )
    return PipelineRun(csv_path=csv_path, artifact_dir=artifacts, result=result, rows=rows)
