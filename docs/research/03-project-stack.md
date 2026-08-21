# 03 — Project Scaffold, Toolchain & Pinned Dependencies

**Wave 1 / Agent 3.** Every recommendation below was executed in this sandbox
(Python 3.11.15, uv 0.8.17). Version numbers are the ones actually resolved and
installed, not guesses. Sections marked **VERIFIED** include the observed output.

---

## 0. Decisions at a glance

| Area | Decision | Rejected | Why |
|---|---|---|---|
| Layout | `src/` layout | flat | Stops accidental imports of the un-installed source tree in tests. |
| Build backend | **hatchling** | setuptools, uv_build | Built sdist+wheel in 0.73 s here; 3 lines of config; stable. |
| Dep manager | **uv** + `pyproject.toml` + `uv.lock` | poetry, pip-tools | `uv sync` cold = 5.9 s; concurrency-safe (tested with 6 racing syncs). |
| Models | **pydantic v2** (frozen) | dataclasses, attrs | Free CSV coercion + JSONL round-trip + cross-field validators. |
| CLI | **typer** | click, argparse | Type hints become the parser; rich `--help`; click's exit codes. |
| Persistence | **JSONL + JSON manifest** | Parquet, DuckDB, SQLite | ~3 k rows. Greppable, diffable, no binary dep. Personal data stays inspectable. |
| Clustering | **leidenalg** on igraph | sklearn, nx-only | Leiden ≫ Louvain for quality; seedable; no numpy. |
| Layout | **igraph `fr`** | nx.spring_layout | 0.28 s vs 15.5 s at 2 800 nodes — **55× faster**. |
| Viz | **hand-rolled canvas HTML** | pyvis, plotly, bokeh | 279 KB vs plotly's 5.65 MB; pyvis is CSP-disqualified (below). |
| Lint/format | **ruff** (both) | black+flake8+isort | One tool, one config, ~ms runtime. |
| Types | **mypy** + pydantic plugin | pyright, ty | Pydantic's first-party plugin understands the models. |

Total dependency closure: **46 packages** including all dev tooling. No numpy,
no pandas, no scipy, no pyarrow, no ipython.

---

## 1. Directory tree

```
interlayer/
├── pyproject.toml              # scaffold owner only
├── uv.lock                     # scaffold owner only — see §3 concurrency rules
├── README.md
├── .gitignore
├── .python-version             # "3.11"
├── docs/
│   └── research/               # wave-1 output (this file)
├── src/
│   └── interlayer/
│       ├── __init__.py         # __version__ only
│       ├── models.py           # ★ FROZEN CONTRACT — nobody edits (§5)
│       ├── config.py           # Settings, paths, seed, target-firm table
│       ├── errors.py           # exception + exit-code taxonomy
│       ├── io.py               # read_jsonl / write_jsonl / manifest helpers
│       ├── cli.py              # typer app; imports each stage's entrypoint
│       ├── ingest/
│       │   ├── __init__.py
│       │   ├── linkedin_csv.py # Connections.csv + full-archive CSVs
│       │   └── sniff.py        # preamble/header/encoding detection
│       ├── normalize/
│       │   ├── __init__.py
│       │   ├── orgs.py         # company/school canonicalisation, rapidfuzz
│       │   ├── titles.py       # title -> Seniority
│       │   └── dates.py        # "Jan 2019" -> ApproxDate
│       ├── enrich/
│       │   ├── __init__.py
│       │   ├── archive.py      # join Positions/Education/Profile.csv
│       │   └── aliases.py      # user-supplied alias YAML
│       ├── graph/
│       │   ├── __init__.py
│       │   ├── affiliation.py  # people+orgs -> Affiliation[]
│       │   ├── projection.py   # bipartite -> person-person GraphEdge[]
│       │   └── metrics.py      # degree, components, GraphStats
│       ├── cluster/
│       │   ├── __init__.py
│       │   ├── leiden.py       # seeded partitioning
│       │   └── labeling.py     # Cluster.label from top orgs/titles
│       ├── score/
│       │   ├── __init__.py
│       │   ├── targets.py      # Jane Street / Citadel matchers
│       │   ├── features.py     # personalised PageRank, hops, alumni overlap
│       │   └── rank.py         # ScoreComponents -> ScoredPerson/ScoredCluster
│       └── report/
│           ├── __init__.py
│           ├── html.py         # single-file report renderer
│           ├── viz.py          # canvas network HTML
│           ├── console.py      # rich table for the terminal
│           └── templates/
│               ├── report.html.j2
│               └── network.html.j2
└── tests/
    ├── conftest.py             # root fixtures ONLY (§8)
    ├── fixtures/
    │   ├── connections_minimal.csv
    │   ├── connections_messy.csv
    │   └── aliases.yml
    ├── unit/                   # one test_<module>.py per owned module
    ├── integration/
    ├── property/
    └── __snapshots__/          # syrupy
```

**Anti-collision rule:** every stage is a *package directory*, not a module.
An agent owns a whole directory, so two agents never open the same file.

---

## 2. `pyproject.toml` — ready to paste

**VERIFIED**: `uv lock` → *Resolved 50 packages in 123 ms*; `uv sync
--all-groups --all-extras` → 46 packages installed; `uv build` → sdist + wheel
in 0.73 s; `uv run ruff check` / `uv run mypy` → clean.

```toml
[project]
name = "interlayer"
version = "0.1.0"
description = "Map a LinkedIn network's inferred adjacency to target firms, locally."
readme = "README.md"
requires-python = ">=3.11,<3.14"
license = { text = "MIT" }

dependencies = [
  "pydantic>=2.13.4,<3",          # observed 2.13.4 (pydantic-core 2.46.4)
  "typer>=0.27.1,<0.28",          # observed 0.27.1
  "rich>=15.0.0,<16",             # observed 15.0.0
  "networkx>=3.6.1,<4",           # observed 3.6.1
  "python-igraph>=1.0.0,<2",      # observed 1.0.0  (imports as `igraph`)
  "leidenalg>=0.12.0,<0.13",      # observed 0.12.0
  "rapidfuzz>=3.14.5,<4",         # observed 3.14.5
  "jinja2>=3.1.6,<4",             # observed 3.1.6
  "platformdirs>=4.11.3,<5",      # observed 4.11.3
  "orjson>=3.12.0,<4",            # observed 3.12.0
  "python-dateutil>=2.9.0,<3",    # observed 2.9.0.post0
  "pyyaml>=6.0.3,<7",             # observed 6.0.3
]

[project.optional-dependencies]
# Escape hatch for the viz agent only; NOT installed at runtime by default.
plotly = ["plotly>=6.9.0,<7"]     # observed 6.9.0

[project.scripts]
interlayer = "interlayer.cli:app"

[dependency-groups]
dev = [
  "pytest>=9.1.1,<10",                  # observed 9.1.1
  "pytest-cov>=7.1.0,<8",               # observed 7.1.0 (coverage 7.15.4)
  "pytest-xdist>=3.8.0,<4",             # observed 3.8.0
  "hypothesis>=6.165.10,<7",            # observed 6.165.10
  "syrupy>=5.5.3,<6",                   # observed 5.5.3
  "freezegun>=1.5.5,<2",                # observed 1.5.5
  "ruff>=0.16.4,<0.17",                 # observed 0.16.4
  "mypy>=2.3.1,<3",                     # observed 2.3.1
  "types-python-dateutil>=2.9.0",       # observed 2.9.0.20260807
  "types-pyyaml>=6.0.12",               # observed 6.0.12.20260815
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/interlayer"]

# ---------------------------------------------------------------- pytest
[tool.pytest.ini_options]
minversion = "9.0"
testpaths = ["tests"]
cache_dir = ".pytest_cache"
addopts = "-ra --strict-markers --strict-config --import-mode=importlib"
markers = [
  "slow: excluded from the default fast loop",
  "integration: touches the filesystem / runs a full stage",
  "property: hypothesis-driven",
]
filterwarnings = ["error", "default::DeprecationWarning"]

# ---------------------------------------------------------------- coverage
[tool.coverage.run]
source = ["src/interlayer"]
branch = true
parallel = true

[tool.coverage.report]
skip_covered = true
exclude_lines = ["pragma: no cover", "if TYPE_CHECKING:", "raise NotImplementedError"]

# ---------------------------------------------------------------- ruff
[tool.ruff]
line-length = 100
target-version = "py311"
src = ["src", "tests"]

[tool.ruff.lint]
select = [
  "E", "F", "W",    # pycodestyle + pyflakes
  "I",              # isort
  "N",              # pep8-naming
  "UP",             # pyupgrade
  "B",              # bugbear
  "C4", "SIM", "PIE", "RET",
  "TCH",            # typing-only imports
  "PTH",            # use pathlib
  "DTZ",            # naive datetimes
  "T20",            # no stray print()
  "ARG",            # unused arguments
  "RUF",
]
ignore = [
  "E501",    # ruff-format owns line length
  "B008",    # typer puts Depends()-style calls in defaults
  "RUF012",  # pydantic ClassVar noise
  "ISC001",  # conflicts with the formatter
]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["ARG", "T20", "B011"]
"src/interlayer/cli.py" = ["T20"]          # typer.echo is fine; print is not
"src/interlayer/report/console.py" = ["T20"]

[tool.ruff.lint.isort]
known-first-party = ["interlayer"]

[tool.ruff.format]
quote-style = "double"
docstring-code-format = true

# ---------------------------------------------------------------- mypy
[tool.mypy]
python_version = "3.11"
files = ["src", "tests"]
plugins = ["pydantic.mypy"]
strict = false
warn_unused_configs = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_return_any = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
no_implicit_optional = true
show_error_codes = true
pretty = true

[[tool.mypy.overrides]]
module = ["igraph.*", "leidenalg.*", "plotly.*", "networkx.*"]
ignore_missing_imports = true

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false

[tool.pydantic-mypy]
init_typed = true
warn_required_dynamic_aliases = true
```

Strictness note: `disallow_untyped_defs` on, full `strict` off. Every function
needs annotations; nobody has to fight `Any` propagation from igraph.

---

## 3. Dependency manager: uv — and the concurrency contract

### VERIFIED command sequence (cold, no pre-existing venv)

```bash
cd /home/user/interlayer
uv sync --all-groups          # creates .venv/ AND uv.lock if absent
uv run interlayer --help      # no `activate` needed, ever
```

Observed: `Using CPython 3.11.15` → `Resolved 87 packages in 1.11s` → sync in
**5.86 s**. No `--system` needed; `.venv` creation works fine. `uv run`
auto-syncs before executing, so it is safe as the only entrypoint.

### Concurrency: the real hazard (TESTED)

Six `uv sync --all-groups` launched simultaneously against one project:

```
agent1: Audited 82 packages in 0.89ms      agent4: Audited 82 packages in 0.82ms
agent2: Audited 82 packages in 0.67ms      agent5: Audited 82 packages in 0.64ms
agent3: Audited 82 packages in 0.59ms      agent6:  + wcwidth==0.8.2
```

All six exited **0**. uv takes a venv-level file lock; five waited, one
installed, nothing corrupted. **Concurrent `uv sync` is safe.**

The hazard is elsewhere — **plain `uv sync` silently re-locks when
`pyproject.toml` drifts.** Demonstrated: adding one dependency and running a
bare `uv sync` rewrote `uv.lock` and mutated the shared venv underneath the
other five agents. Behaviour of the three modes, all measured:

| Command | Lock stale | Behaviour | Exit |
|---|---|---|---|
| `uv sync` | yes | **silently re-locks + mutates shared venv** | 0 |
| `uv sync --frozen` | yes | uses lock as-is; **uninstalls** anything not in it | 0 |
| `uv sync --locked` | yes | refuses: *"lockfile needs to be updated"* | **1** |
| `uv sync --locked` | no | no-op audit | 0 |

**Mitigation — mandatory for Wave 2:**

1. Wave 2 agents run **`uv sync --all-groups --locked`** and **`uv run
   --frozen ...`**. Never a bare `uv sync`.
2. **Only the scaffold owner (Agent A) edits `[project.dependencies]` or
   `uv.lock`.** Any other agent needing a dep files a request instead. A
   `--locked` failure is the tripwire that someone broke this rule.
3. `.pytest_cache` is *also* shared state. Each agent passes
   `-o cache_dir=.pytest_cache/<agent-id>` (**VERIFIED**: creates an isolated
   cache with its own `lastfailed`), otherwise six agents clobber each other's
   last-failed list.

Poetry and pip-tools were rejected: both are 5–20× slower here and neither has
uv's `--locked` drift guard, which is the property that makes six-way
parallelism safe.

---

## 4. LinkedIn CSV: the preamble is real, and it is *variable*

**VERIFIED** against a realistic fixture, and corroborated by LinkedIn docs and
third-party guides: `Connections.csv` opens with a `Notes:` block, then a
quoted paragraph about missing email addresses, then a blank line, then the
header. Guides say to delete "the first 3–4 rows" — the count has **changed
over time**, so `skiprows=3` is a latent bug.

**Contract: sniff, never hard-code.** `ingest/sniff.py` scans lines, CSV-parses
each candidate, and accepts the first row where ≥2 cells normalise into the
known header set. Verified to land on index 3 for the current format and to
survive a different preamble length.

Canonical header (confirmed):
`First Name,Last Name,URL,Email Address,Company,Position,Connected On`

Also handle: a UTF-8 BOM on the first header cell, `Email Address` empty for
most rows (privacy setting), and `Connected On` as `05 Jan 2024`.

The full "Get a copy of your data" archive additionally ships `Profile.csv`,
`Positions.csv`, `Education.csv`, `Skills.csv` — these are what `enrich` joins.
**`enrich` is local-only**: it reads other files from the same archive plus a
user-supplied alias YAML. No scraping, no network. That is why `httpx` and
`tenacity` are absent from the dependency list.

---

## 5. `src/interlayer/models.py` — the shared contract

**VERIFIED**: all 15 models JSON round-trip byte-exactly; all 7 validation
guards reject bad input; frozen + hashable confirmed; `ruff check` clean;
`ruff format --check` clean; `mypy` → *Success: no issues found*; doctests pass.

Two bugs were found and fixed during verification — both would have bitten
Wave 2 on day one:

1. **`@computed_field` + `extra="forbid"` breaks round-trips.** A computed field
   is *serialised* but then *rejected* on re-validation (`Extra inputs are not
   permitted [type=extra_forbidden]`). Fixed by using plain `@property`
   everywhere: derived values stay derived, strictness is preserved.
2. **`GraphEdge` orientation swap clobbered itself** — writing `source` before
   reading it produced `p_aaaa -> p_aaaa`. Fixed with a temp-pair swap.
3. **`init_typed = true` + `Annotated[T, Field(default=...)]` makes optional
   fields look *required* to mypy** (`Missing named argument "day"`). The
   plugin does not see a default buried inside `Annotated`. Fixed by writing
   `day: Annotated[int | None, Field(ge=1, le=31)] = None` — constraints in
   `Annotated`, **default after the `=`**. Wave 2 must follow this style in
   any new model or mypy will reject correct calls.

```python
"""Shared data contract for the interlayer pipeline.

This module is the ROOT of the dependency graph: it imports nothing from
``interlayer`` and every other module imports from it. It must stay free of
I/O, network access and heavy dependencies.

Stability rules for Wave 2 build agents:
  * Do NOT edit this file. If a field is missing, raise it with the scaffold
    owner rather than editing in place -- six agents share these definitions.
  * Every model is frozen (hashable, immutable). Build new instances with
    ``model_copy(update=...)`` instead of mutating.
  * Every artifact model carries ``schema_version``; bump ``SCHEMA_VERSION``
    only via the scaffold owner.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0.0"

__all__ = [
    "SCHEMA_VERSION",
    "Affiliation",
    "AffiliationKind",
    "ApproxDate",
    "Base",
    "Cluster",
    "Connection",
    "Education",
    "EvidenceItem",
    "GraphEdge",
    "GraphNode",
    "GraphStats",
    "Org",
    "OrgKind",
    "Person",
    "Position",
    "RunManifest",
    "ScoreComponents",
    "ScoredCluster",
    "ScoredPerson",
    "Seniority",
    "TargetFirm",
    "normalize_key",
    "stable_id",
]


# --------------------------------------------------------------------------
# id + normalisation helpers (deterministic; no randomness, no clock)
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s&+]", flags=re.UNICODE)


def normalize_key(value: str) -> str:
    """Fold a raw string to a comparison key.

    Deterministic and locale-independent: NFKD-fold, strip accents, lowercase,
    drop punctuation (keeping ``&`` and ``+``), collapse whitespace.

    >>> normalize_key("  Jane Street Capital, L.L.C. ")
    'jane street capital l l c'
    """
    folded = unicodedata.normalize("NFKD", value)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    folded = _PUNCT_RE.sub(" ", folded.casefold())
    return _WS_RE.sub(" ", folded).strip()


def stable_id(kind: str, *parts: str | None) -> str:
    """Content-addressed id: ``<kind>_<16 hex>``.

    Stable across processes and runs (BLAKE2b, not ``hash()``), so every stage
    derives the same id for the same entity without a shared counter.
    """
    payload = "\x1f".join([kind, *(normalize_key(p) if p else "" for p in parts)])
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()
    return f"{kind}_{digest}"


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------


class OrgKind(StrEnum):
    COMPANY = "company"
    SCHOOL = "school"
    UNKNOWN = "unknown"


class AffiliationKind(StrEnum):
    EMPLOYMENT = "employment"
    EDUCATION = "education"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    EXECUTIVE = "executive"
    FOUNDER = "founder"
    UNKNOWN = "unknown"


class TargetFirm(StrEnum):
    JANE_STREET = "jane_street"
    CITADEL = "citadel"


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


class Base(BaseModel):
    """Frozen, strict-ish base for every contract model."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        use_enum_values=False,
        ser_json_timedelta="iso8601",
    )


class ApproxDate(Base):
    """LinkedIn dates are coarse: ``2019``, ``Jan 2019`` or a full date."""

    year: Annotated[int, Field(ge=1900, le=2100)]
    month: Annotated[int | None, Field(ge=1, le=12)] = None
    day: Annotated[int | None, Field(ge=1, le=31)] = None

    @model_validator(mode="after")
    def _day_requires_month(self) -> Self:
        if self.day is not None and self.month is None:
            raise ValueError("day given without month")
        return self

    @property
    def sort_key(self) -> int:
        """Monotone integer for ordering; missing parts sort to the start."""
        return self.year * 10_000 + (self.month or 0) * 100 + (self.day or 0)

    def to_date(self) -> date:
        """Concrete date, defaulting missing month/day to January 1st."""
        return date(self.year, self.month or 1, self.day or 1)

    def __str__(self) -> str:
        if self.month is None:
            return f"{self.year:04d}"
        if self.day is None:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


# --------------------------------------------------------------------------
# entities
# --------------------------------------------------------------------------


class Org(Base):
    """A canonical company or school, after normalisation/alias resolution."""

    org_id: str
    name: str
    kind: OrgKind = OrgKind.UNKNOWN
    aliases: tuple[str, ...] = ()
    linkedin_url: str | None = None
    domain: str | None = None
    is_target: bool = False
    target_firm: TargetFirm | None = None

    @classmethod
    def make(cls, name: str, kind: OrgKind = OrgKind.UNKNOWN, **kw: Any) -> Org:
        return cls(org_id=stable_id("org", name), name=name, kind=kind, **kw)

    @model_validator(mode="after")
    def _target_consistency(self) -> Self:
        if self.is_target and self.target_firm is None:
            raise ValueError("is_target=True requires target_firm")
        return self


class Position(Base):
    """One employment stint. ``company_raw`` is verbatim from the export."""

    company_raw: str
    company_org_id: str | None = None
    title_raw: str = ""
    seniority: Seniority = Seniority.UNKNOWN
    start: ApproxDate | None = None
    end: ApproxDate | None = None
    is_current: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start and self.end and self.end.sort_key < self.start.sort_key:
            raise ValueError("position end precedes start")
        if self.is_current and self.end is not None:
            raise ValueError("is_current=True conflicts with an end date")
        return self


class Education(Base):
    school_raw: str
    school_org_id: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start: ApproxDate | None = None
    end: ApproxDate | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start and self.end and self.end.sort_key < self.start.sort_key:
            raise ValueError("education end precedes start")
        return self


class Person(Base):
    """A node in the network. ``person_id`` is content-addressed."""

    person_id: str
    first_name: str = ""
    last_name: str = ""
    full_name: str
    headline: str | None = None
    linkedin_url: str | None = None
    linkedin_slug: str | None = None
    email: str | None = None
    location: str | None = None
    positions: tuple[Position, ...] = ()
    educations: tuple[Education, ...] = ()
    is_ego: bool = False
    source: Literal["connections_csv", "enrichment", "manual", "synthetic"] = "connections_csv"

    @field_validator("linkedin_url")
    @classmethod
    def _strip_query(cls, v: str | None) -> str | None:
        return v.split("?", 1)[0].rstrip("/") if v else v

    @classmethod
    def make(cls, first_name: str, last_name: str, **kw: Any) -> Person:
        """Build with a deterministic id derived from name + slug/email."""
        slug = kw.get("linkedin_slug")
        url = kw.get("linkedin_url")
        if slug is None and url:
            slug = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or None
            kw["linkedin_slug"] = slug
        full = " ".join(p for p in (first_name, last_name) if p).strip()
        ident = slug or kw.get("email") or full
        return cls(
            person_id=stable_id("p", ident),
            first_name=first_name,
            last_name=last_name,
            full_name=kw.pop("full_name", full),
            **kw,
        )

    @property
    def current_company(self) -> str | None:
        for p in self.positions:
            if p.is_current:
                return p.company_raw
        return self.positions[0].company_raw if self.positions else None


class Connection(Base):
    """An ego-network edge: the user is connected to ``person_id``."""

    person_id: str
    connected_on: date | None = None
    degree: Literal[1, 2] = 1
    note: str | None = None


class Affiliation(Base):
    """Bipartite person->org edge; the substrate the graph is built from."""

    person_id: str
    org_id: str
    kind: AffiliationKind
    title: str | None = None
    start: ApproxDate | None = None
    end: ApproxDate | None = None
    weight: NonNegativeFloat = 1.0

    @property
    def affiliation_id(self) -> str:
        return stable_id("aff", self.person_id, self.org_id, str(self.kind), str(self.start or ""))

    def overlaps(self, other: Affiliation) -> bool:
        """True when two affiliations share an org and their spans intersect."""
        if self.org_id != other.org_id:
            return False
        a0 = self.start.sort_key if self.start else 0
        a1 = self.end.sort_key if self.end else 99_999_999
        b0 = other.start.sort_key if other.start else 0
        b1 = other.end.sort_key if other.end else 99_999_999
        return a0 <= b1 and b0 <= a1


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------


class GraphNode(Base):
    person_id: str
    label: str
    degree: NonNegativeInt = 0
    cluster_id: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(Base):
    """Undirected person-person edge; ``source < target`` is enforced."""

    source: str
    target: str
    weight: NonNegativeFloat = 1.0
    shared_org_ids: tuple[str, ...] = ()
    kinds: tuple[AffiliationKind, ...] = ()

    @model_validator(mode="after")
    def _canonical_orientation(self) -> Self:
        if self.source == self.target:
            raise ValueError("self-loops are not allowed")
        if self.source > self.target:
            lo, hi = self.target, self.source
            object.__setattr__(self, "source", lo)
            object.__setattr__(self, "target", hi)
        return self

    @property
    def edge_id(self) -> str:
        return stable_id("e", self.source, self.target)


class GraphStats(Base):
    n_nodes: NonNegativeInt = 0
    n_edges: NonNegativeInt = 0
    n_components: NonNegativeInt = 0
    density: NonNegativeFloat = 0.0
    modularity: float | None = None


# --------------------------------------------------------------------------
# clustering + scoring
# --------------------------------------------------------------------------


class Cluster(Base):
    cluster_id: str
    label: str = ""
    member_ids: tuple[str, ...] = ()
    top_orgs: tuple[tuple[str, int], ...] = ()
    top_titles: tuple[tuple[str, int], ...] = ()
    cohesion: NonNegativeFloat = 0.0
    algorithm: str = "leiden"
    resolution: float = 1.0

    @property
    def size(self) -> int:
        return len(self.member_ids)


class EvidenceItem(Base):
    """Why a score moved. Rendered verbatim in the report -- keep it human."""

    kind: Literal[
        "direct_employment",
        "past_employment",
        "shared_employer",
        "shared_school",
        "cluster_membership",
        "title_signal",
        "path_proximity",
    ]
    detail: str
    contribution: float = 0.0
    org_id: str | None = None
    via_person_id: str | None = None
    hops: NonNegativeInt | None = None


class ScoreComponents(Base):
    """Additive components; ``total`` is their sum, weights live in config."""

    direct: float = 0.0
    alumni: float = 0.0
    proximity: float = 0.0
    cluster: float = 0.0
    title: float = 0.0

    @property
    def total(self) -> float:
        return round(self.direct + self.alumni + self.proximity + self.cluster + self.title, 6)


class ScoredPerson(Base):
    person_id: str
    full_name: str
    score: float
    rank: NonNegativeInt = 0
    components: ScoreComponents = Field(default_factory=ScoreComponents)
    per_firm: dict[TargetFirm, float] = Field(default_factory=dict)
    cluster_id: str | None = None
    evidence: tuple[EvidenceItem, ...] = ()
    hops_to_target: NonNegativeInt | None = None


class ScoredCluster(Base):
    cluster_id: str
    label: str
    score: float
    rank: NonNegativeInt = 0
    size: NonNegativeInt = 0
    target_density: NonNegativeFloat = 0.0
    per_firm: dict[TargetFirm, float] = Field(default_factory=dict)
    top_person_ids: tuple[str, ...] = ()
    rationale: str = ""


# --------------------------------------------------------------------------
# run metadata (determinism / provenance)
# --------------------------------------------------------------------------


class RunManifest(Base):
    """Written next to every artifact set so a run can be reproduced."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    created_at: datetime
    seed: int = 20240101
    tool_version: str = "0.1.0"
    python_version: str = ""
    input_sha256: str | None = None
    input_filename: str | None = None
    stage_versions: dict[str, str] = Field(default_factory=dict)
    config_sha256: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
```

---

## 6. CLI surface (typer)

**VERIFIED** exit codes from a working prototype:

| Invocation | Exit |
|---|---|
| `interlayer ingest .` (success) | 0 |
| `interlayer` (no args → help) | 2 |
| `interlayer bogus` (unknown command) | 2 |
| `interlayer ingest /nonexistent` (typer `exists=True`) | 2 |
| `interlayer build` with no prior artifact | 5 |

```
interlayer ingest   <export>  [--data-dir] [--keep-raw]
interlayer normalize          [--data-dir] [--aliases FILE]
interlayer enrich             [--data-dir] [--archive DIR]
interlayer build              [--data-dir] [--seed] [--min-org-size] [--max-org-size]
interlayer cluster            [--data-dir] [--seed] [--resolution]
interlayer score              [--data-dir] [--firm jane_street|citadel] [--top N]
interlayer report             [--data-dir] [--out report.html] [--open]
interlayer run      <export>  [--data-dir] [--seed] [--out]   # chains all seven
```

Exit-code taxonomy — put this in `errors.py`, every stage uses it:

```python
class Exit(IntEnum):
    OK = 0
    USAGE = 2               # click/typer default; do not reuse
    INPUT_ERROR = 3         # unreadable / unparseable export
    VALIDATION_ERROR = 4    # pydantic rejected the data
    MISSING_STAGE = 5       # a prior stage's artifact is absent
    INTERRUPTED = 130
```

`app = typer.Typer(no_args_is_help=True, add_completion=False)` so a bare
invocation prints help. Every command gets a one-line docstring — typer renders
it as the command summary in `--help`.

---

## 7. Persistence & the personal-data posture

**JSONL for record streams, one JSON manifest, nothing else.** At ~3 000
connections a Parquet/DuckDB/SQLite layer buys nothing and costs a binary
dependency (`pyarrow` alone is larger than our entire tree). JSONL is
`grep`-able and `git diff`-able, which matters a great deal when the payload is
someone's real address book and they may want to audit it.

**VERIFIED determinism**: `orjson.dumps(rec, option=OPT_SORT_KEYS |
OPT_APPEND_NEWLINE)` produced byte-identical files across three runs.

```
$INTERLAYER_DATA_DIR/          # default ./.interlayer/ ; MUST be gitignored
├── manifest.json              # RunManifest: seed, sha256 of input, counts, versions
├── 01_people.jsonl            # Person
├── 01_connections.jsonl       # Connection
├── 02_orgs.jsonl              # Org (post-normalisation)
├── 03_affiliations.jsonl      # Affiliation
├── 04_nodes.jsonl             # GraphNode
├── 04_edges.jsonl             # GraphEdge
├── 04_graph_stats.json        # GraphStats
├── 05_clusters.jsonl          # Cluster
├── 06_scored_people.jsonl     # ScoredPerson
├── 06_scored_clusters.jsonl   # ScoredCluster
└── report/
    ├── report.html            # single self-contained file
    └── network.html
```

Numeric prefixes make stage ordering obvious and make `MISSING_STAGE` checks a
one-line `Path.exists()`.

**Privacy rules — non-negotiable:**

* Data dir defaults to **`./.interlayer/` in the cwd**, overridable via
  `--data-dir` / `$INTERLAYER_DATA_DIR`. `platformdirs` is used for the *config
  file only* (`user_config_dir("interlayer")`), never for the data.
* **Do not copy the raw export in.** `manifest.json` stores only the filename
  and a SHA-256. Copying is opt-in behind `--keep-raw`, and only then does
  `00_raw/` appear.
* Ship a `.gitignore` containing `.interlayer/`, `*.csv`, `Complete_LinkedIn*`
  from the first commit.
* Email addresses are ingested but **never rendered** in the HTML report — the
  report is the artifact most likely to be shared.
* Zero network calls at runtime. The dependency list has no HTTP client, which
  makes that property structural rather than aspirational.

---

## 8. Testing

Layout, with a strict conftest strategy that keeps agents apart:

```
tests/
├── conftest.py              # ROOT — scaffold owner only. tmp data dir, seed,
│                            #   frozen clock, fixture-file loader.
├── fixtures/                # shared CSVs/YAML — additive only, never edited
├── unit/
│   ├── test_ingest_*.py     # ← ingest agent
│   ├── test_normalize_*.py  # ← normalize agent
│   └── ...                  # one file per owned module
├── integration/
│   └── test_pipeline_*.py
├── property/
│   └── test_*_props.py      # hypothesis
└── __snapshots__/           # syrupy, auto-managed
```

**Conftest rule:** exactly one `tests/conftest.py`, owned by the scaffold owner
and frozen after Wave 2 starts. An agent needing extra fixtures adds
`tests/unit/conftest_<stage>.py` and imports it, or defines fixtures in its own
test module. Six agents editing one conftest is the classic merge disaster.

Plugin picks: **pytest-cov** (coverage), **pytest-xdist** (`-n auto`),
**hypothesis** (property tests over the messy-CSV parser and `normalize_key`),
**syrupy** (snapshots — chosen over pytest-regressions: cleaner API, no
`pytest-datadir` transitive dep), **freezegun** (pins `RunManifest.created_at`).

### Re-running only previously-failed tests — REQUIRED, fully measured

pytest writes failures to `.pytest_cache/v/cache/lastfailed` as a JSON object
keyed by node id. Measured on a 4-test suite with 2 failures:

```
full run : 2 failed, 2 passed in 0.63s
--lf run : 2 failed            in 0.14s      <- only the failures collected
cache    : {"tests/test_lf.py::test_fail_a": true,
            "tests/test_lf.py::test_fail_b": true}
```

**The command the orchestrator must use:**

```bash
uv run --frozen pytest --lf --lfnf=none -q
```

**`--lfnf=none` is the load-bearing flag.** Measured behaviour on an empty
cache (i.e. everything now passes):

| Command | Empty cache | Result | Exit |
|---|---|---|---|
| `pytest --lf` | yes | **re-runs the ENTIRE suite** (default `--lfnf=all`) | 1 |
| `pytest --lf --lfnf=none` | yes | `4 deselected in 0.16s` | **5** |

So without `--lfnf=none`, "re-run only the failures" silently becomes "re-run
everything" the moment the fixes land — the exact opposite of the intent.

**Orchestrator loop:**

```bash
uv run --frozen pytest -q                                    # 1. full run, seeds the cache
uv run --frozen pytest --lf --lfnf=none -q                   # 2. after each fix
# exit 0 = the re-run passed        exit 5 = nothing left to fix (SUCCESS)
# exit 1 = still failing            exit 2 = usage error in the invocation
```

**Treat exit 5 as success.** It is pytest's `NO_TESTS_COLLECTED`, and here it is
the completion signal.

Also verified:
* `--lf` works under xdist: `pytest -n 2 --lf` correctly re-ran only the 2
  failures and the controller still wrote `lastfailed`.
* `--ff` (failed-first) runs failures first *then the rest* — right for a final
  confidence pass, wrong for the fix loop.
* Per-agent isolation: `-o cache_dir=.pytest_cache/<agent-id>` gives each agent
  its own `lastfailed`.

---

## 9. Visualisation — measured, and pyvis is disqualified

Constraint: one self-contained HTML file, **zero external hosts** (strict CSP).
Every candidate was rendered and its `<script>`/`<link>` tags inspected.

| Option | Bytes @ 2 800 nodes | External tags | Verdict |
|---|---|---|---|
| **custom canvas + Jinja2** | **279 KB** (63 KB gz) | **0** | **CHOSEN** |
| plotly `include_plotlyjs="inline"` | 5.65 MB | **0** | fallback — works, but 20× larger |
| plotly `include_plotlyjs="cdn"` | 7.6 KB | 1 (`cdn.plot.ly`) | ✗ CSP |
| pyvis `cdn_resources="in_line"` | 698 KB | **2 (jsdelivr bootstrap)** | ✗ **CSP — see below** |
| pyvis `remote` / `local` | 4–11 KB | 4 | ✗ CSP |

**The pyvis finding matters.** `cdn_resources="in_line"` inlines vis-network
but *still* emits two `jsdelivr` tags for Bootstrap CSS/JS:

```
https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/css/bootstrap.min.css
https://cdn.jsdelivr.net/npm/bootstrap@5.0.0-beta3/dist/js/bootstrap.bundle.min.js
```

Under a strict CSP those fail silently and the page renders unstyled. The name
of the option promises self-containment it does not deliver. **Do not use
pyvis**; it also drags in ipython, jedi, parso and prompt-toolkit.

**Plotly is genuinely clean**: at tag level, `include_plotlyjs="inline"` emitted
**0** external `src`/`href` references. (A naive regex finds 16 `https://`
strings, but they are attribution URLs *inside* the JS bundle text, not
resource loads. `include_plotlyjs=True` is a synonym for `"inline"`.)

**Why the custom renderer wins:** 4.86 MB of the plotly bundle is fixed
overhead for a chart library we barely use, and Scattergl gives poor *graph*
UX — no node dragging, no neighbour highlighting, no edge hover. A prototype
canvas renderer (~60 lines of vanilla JS, built and measured here) delivers
hover tooltips, pan, zoom and cluster colouring at 279 KB. `jinja2` is already
a dependency for the HTML report, so the marginal cost is zero.

The working prototype is at
`/tmp/claude-0/-home-user-interlayer/2b0ea179-fd47-5128-a577-4097a5befa7d/scratchpad/probe_a/canvas_probe.py`
— the viz agent should start from it. `plotly` stays available as
`pip install interlayer[plotly]` with a lazy import, if that agent runs short.

**Layout is computed in Python, not JS** — igraph `fr`, **0.28 s** vs
networkx `spring_layout`'s **15.5 s** at 2 800 nodes (55× faster), and
deterministic under a seeded RNG. Coordinates are normalised to `[0,1]` and
embedded as JSON. Other igraph layouts measured: `drl` 8.1 s, `kk` 11.1 s,
`graphopt` 24.1 s — **use `fr`**.

---

## 10. Determinism

**PYTHONHASHSEED is the real threat.** Measured: iterating a set of 8 strings
under 4 different `PYTHONHASHSEED` values gave **4 distinct orders**. Any stage
that iterates a `set` of ids and feeds that order into a tie-break, a cluster
label or a layout will produce different output run-to-run.

Rules, in priority order:

1. **Export `PYTHONHASHSEED=0`** in the test harness and document it for the
   CLI. Put it in `[tool.pytest.ini_options] env` or the CI env.
2. **Never iterate a bare `set`.** `sorted(...)` at every set→sequence boundary.
   This is the belt to `PYTHONHASHSEED`'s braces, and the only one that
   survives a user running the CLI without the env var.
3. **One seed, threaded through `Settings.seed`** (default `20240101`), exposed
   as `--seed`, recorded in `RunManifest.seed`.
4. Seed each consumer explicitly at stage entry:
   ```python
   random.seed(cfg.seed)
   igraph.set_random_number_generator(random.Random(cfg.seed))   # verified deterministic
   leidenalg.find_partition(..., seed=cfg.seed, n_iterations=-1)
   ```
5. **Content-addressed ids, never counters.** `stable_id()` uses BLAKE2b, not
   `hash()` — verified identical across processes. Two agents' stages derive the
   same id for the same entity with no shared state.
6. **Byte-stable serialisation**: `orjson.OPT_SORT_KEYS | OPT_APPEND_NEWLINE`,
   verified byte-identical across runs. Sort every JSONL file by its primary id
   before writing.
7. **No naive `datetime.now()`** outside `RunManifest` — ruff's `DTZ` rules are
   enabled to enforce it, and freezegun pins the clock in tests.

Note on clustering: on a well-separated synthetic graph, Leiden and Louvain both
converged regardless of seed. On real, messy data the seed *will* matter — seed
anyway, and record it.

---

## 11. Module ownership — 6 agents, ZERO file overlap

Agent A (scaffold) runs first and alone; agents 1–6 then run fully in parallel.

| Agent | Owns (exclusive write access) | Tests owned | Depends on |
|---|---|---|---|
| **A — scaffold** *(pre-wave, solo)* | `pyproject.toml`, `uv.lock`, `.gitignore`, `.python-version`, `src/interlayer/__init__.py`, **`models.py`**, `config.py`, `errors.py`, `io.py`, `cli.py`, `tests/conftest.py`, `tests/fixtures/*` | `tests/unit/test_models.py`, `test_io.py`, `test_config.py` | — |
| **1 — ingest** | `src/interlayer/ingest/**` | `tests/unit/test_ingest_*.py`, `tests/property/test_ingest_props.py` | models, io |
| **2 — normalize** | `src/interlayer/normalize/**` | `tests/unit/test_normalize_*.py`, `tests/property/test_normalize_props.py` | models |
| **3 — enrich** | `src/interlayer/enrich/**` | `tests/unit/test_enrich_*.py` | models, normalize (interface only) |
| **4 — graph** | `src/interlayer/graph/**` | `tests/unit/test_graph_*.py`, `tests/integration/test_pipeline_graph.py` | models |
| **5 — cluster + score** | `src/interlayer/cluster/**`, `src/interlayer/score/**` | `tests/unit/test_cluster_*.py`, `test_score_*.py` | models, graph (interface only) |
| **6 — report + viz** | `src/interlayer/report/**` (incl. `templates/`) | `tests/unit/test_report_*.py`, `tests/integration/test_report_html.py` | models |

**Rules that make this safe:**

* Agent A finishes and commits **before** 1–6 start. `models.py` is frozen at
  that moment. Nobody in Wave 2 edits any file in Agent A's column.
* Agents 1–6 write **only inside their own package directory**. Never each
  other's, never Agent A's.
* Cross-stage communication is **only** through `models.py` types and JSONL
  artifacts on disk — never by importing another Wave-2 agent's module. Agents
  3 and 5 are marked "interface only": they code against the *artifact schema*,
  not the upstream implementation, so they need not wait for it.
* `cli.py` is pre-wired by Agent A to import
  `interlayer.<stage>.run(cfg) -> None` from each package. **Every stage
  package must expose exactly `run(cfg: Settings) -> None` in its
  `__init__.py`.** That is the whole seam.
* New dependency needed? Request it from Agent A. Do not edit `pyproject.toml`
  — `uv sync --locked` will fail loudly for everyone if you do.

---

## 12. Command cheat-sheet (all VERIFIED)

```bash
# ---- setup (cold, no venv) -------------------------------------------------
cd /home/user/interlayer
uv sync --all-groups                       # Agent A only (may write uv.lock)
uv sync --all-groups --locked              # Wave 2 agents: fails if lock drifted

# ---- run -------------------------------------------------------------------
uv run --frozen interlayer --help
uv run --frozen interlayer run ~/Downloads/Connections.csv --out report.html

# ---- test ------------------------------------------------------------------
uv run --frozen pytest -q                                     # full suite
uv run --frozen pytest -q -n auto                             # parallel
uv run --frozen pytest -q -o cache_dir=.pytest_cache/agent4   # per-agent cache
uv run --frozen pytest --cov --cov-report=term-missing        # coverage

# ---- RE-RUN ONLY PREVIOUSLY-FAILED TESTS (orchestrator loop) ---------------
uv run --frozen pytest --lf --lfnf=none -q
#   exit 0 -> the re-run passed
#   exit 1 -> still failing
#   exit 5 -> NO_TESTS_COLLECTED == nothing left to fix -> TREAT AS SUCCESS
uv run --frozen pytest --ff -q             # failed-first, then everything else

# ---- lint / format / types -------------------------------------------------
uv run --frozen ruff check .               # -> "All checks passed!"
uv run --frozen ruff check --fix .
uv run --frozen ruff format .              # -> "1 file already formatted"
uv run --frozen mypy                       # -> "Success: no issues found"

# ---- build -----------------------------------------------------------------
uv run --frozen pytest -q && uv build      # sdist + wheel in ~0.7s
```

One-liner gate for CI or an agent's self-check:

```bash
uv sync --all-groups --locked && uv run --frozen ruff check . \
  && uv run --frozen ruff format --check . && uv run --frozen mypy \
  && PYTHONHASHSEED=0 uv run --frozen pytest -q
```

---

## Appendix — full verified version set (46 packages)

```
annotated-doc==0.0.5            markupsafe==3.0.3          pytest-xdist==3.8.0
annotated-types==0.8.0          mdurl==0.1.2               python-dateutil==2.9.0.post0
ast-serialize==0.8.0            mypy==2.3.1                python-igraph==1.0.0
coverage==7.15.4                mypy-extensions==1.1.0     pyyaml==6.0.3
execnet==2.1.2                  networkx==3.6.1            rapidfuzz==3.14.5
freezegun==1.5.5                orjson==3.12.0             rich==15.0.0
hypothesis==6.165.10            packaging==26.3            ruff==0.16.4
igraph==1.0.0                   pathspec==1.1.1            shellingham==1.5.4
iniconfig==2.3.0                platformdirs==4.11.3       six==1.17.0
jinja2==3.1.6                   pluggy==1.6.0              sortedcontainers==2.4.0
leidenalg==0.12.0               pydantic==2.13.4           syrupy==5.5.3
librt==0.15.0                   pydantic-core==2.46.4      texttable==1.7.0
markdown-it-py==4.2.0           pygments==2.21.0           typer==0.27.1
                                pytest==9.1.1              types-python-dateutil==2.9.0.20260807
                                pytest-cov==7.1.0          types-pyyaml==6.0.12.20260815
                                                           typing-extensions==4.16.0
                                                           typing-inspection==0.4.4
```

Optional extra: `plotly==6.9.0` (viz fallback only).
Interpreter: `Python 3.11.15 (main, Mar  3 2026, 09:26:23) [GCC 13.3.0]`.

---

## Appendix B — clean-room gotchas (found by actually running this)

The documented `pyproject.toml` was extracted back out of this file, dropped
into an empty directory with `models.py`, and driven through the whole command
sequence. Two things broke. Both would have blocked all six agents at once.

**1. `readme = "README.md"` hard-fails the build if the file is missing.**

```
OSError: Readme file does not exist: README.md
× Failed to build `interlayer` ... hatchling.build.build_editable failed
```

Because `uv sync` builds the project as an editable install, this is not a
packaging-time error — it breaks `uv sync`, and therefore `ruff`, `mypy`,
`pytest` and everything else, with an error message that names hatchling rather
than the missing file. This repo already has a `README.md`, so it is fine, but
Agent A must confirm it exists *before* Wave 2 starts. The same applies to
`license = { text = "MIT" }` if it is ever changed to a `file =` form.

**2. The pydantic-mypy `init_typed` interaction described in §5, bug 3.**

After both fixes, the clean room is fully green:

```
uv sync --all-groups --locked   -> Audited 46 packages in 0.53ms          exit 0
uv run --frozen ruff check .    -> All checks passed!                     exit 0
uv run --frozen ruff format --check . -> 7 files already formatted        exit 0
uv run --frozen mypy            -> Success: no issues found in 6 files    exit 0
PYTHONHASHSEED=0 pytest -q      -> 4 passed in 0.22s                      exit 0
pytest --lf --lfnf=none -q      -> 4 deselected in 0.22s                  exit 5
uv build                        -> sdist + wheel                          exit 0
uv run --frozen interlayer --help -> Usage: interlayer [OPTIONS]          exit 0
```

**3. Minor typer behaviour, not a bug.** A `typer.Typer()` app with *exactly
one* registered command collapses into a single-command CLI — `--help` shows
that command's options rather than a command list. It disappears as soon as the
second command is registered, so it will not affect the real eight-command app.
Agent A should register all eight command stubs up front to avoid confusion.

**4. `igraph` vertex lookup by a networkx-imported attribute.**
`g.vs.find(_nx_name=1)` raises `AttributeError: 'Graph' object has no attribute
'nx_name'` — igraph strips the leading underscore when building the accessor.
Use `g.vs["_nx_name"].index(value)` instead. Relevant to Agent 4 (graph) and
Agent 5 (score), both of which round-trip through `ig.Graph.from_networkx`.

---

## Appendix C — staged reference files

Byte-identical copies of everything verified above, ready for Agent A to copy in:

| Path | Purpose |
|---|---|
| `docs/research/artifacts/models.py` | The contract. Copy to `src/interlayer/models.py` **verbatim**. Passes ruff, mypy, doctests, and a 15-model round-trip. |
| `docs/research/artifacts/pyproject.reference.toml` | Minimal dependency-only variant used to produce the verified lock. |
| `docs/research/artifacts/canvas_renderer_prototype.py` | Working 279 KB self-contained network HTML generator. Starting point for Agent 6. |
