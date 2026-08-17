"""Runtime configuration.

Every default here is a privacy decision that Wave 1 argued for, not a
preference:

* ``state_root`` — a single root under which *all* persistent state lives, so
  "delete everything" is one directory removal and an audit of writes has one
  place to look (PRIV-03).
* ``retention_days = 90`` — storage limitation, GDPR Art. 5(1)(e) (PRIV-11).
* ``retain_emails = False`` — the ``Email Address`` column is not needed to find
  a bridge, so it is not kept (PRIV-06).
* ``enabled_adapters = ["first_party_export"]`` — the shipped configuration is
  the fully-sanctioned one; enabling anything else is an explicit, logged user
  act (PRIV-04, setup-guide C5).
* ``include_inferred = False`` — an inference is never silently blended with an
  observation (PRIV-19).
* ``allow_contact_fields = False`` — contact details are dropped at the
  acquisition boundary unless deliberately turned on.

``base_seed``, ``resolution`` and ``consensus_runs`` are the determinism knobs
R3 measured; they live here so ``config_hash`` covers them and every audit line
records which parameters produced a result.

Standard library only at import time. ``pyyaml`` is imported lazily inside the
YAML branch so the analysis path never pays for it and never depends on it.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

from interlayer.core.errors import ConfigError
from interlayer.core.models import CompliancePosture

#: Default location of all persistent state (PRIV-03).
DEFAULT_STATE_ROOT: Final[Path] = Path("~/.interlayer")

#: Filenames inside the state root. Defined here rather than in ``store``/
#: ``audit`` so there is exactly one definition and no import cycle.
DB_FILENAME: Final[str] = "interlayer.db"
AUDIT_FILENAME: Final[str] = "audit.log"

#: Directories holding derived artefacts. Their contents are disposable and are
#: cleared whenever a person or target is purged (PRIV-13, PRIV-14).
DERIVED_DIRNAMES: Final[tuple[str, ...]] = ("derived", "cache")

#: Environment variables are ``INTERLAYER_<FIELD_NAME_UPPERCASED>``.
ENV_PREFIX: Final[str] = "INTERLAYER_"

#: ``enabled_adapters`` names *compliance postures*, not provider ids — see the
#: ``CompliancePosture`` docstring in ``models``: "config.enabled_adapters gates
#: on these". An adapter runs iff its declared posture appears in the list.
KNOWN_ADAPTERS: Final[frozenset[str]] = frozenset(p.value for p in CompliancePosture)

_TRUE = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "f", "no", "n", "off"})


def _default_adapters() -> list[str]:
    return [CompliancePosture.FIRST_PARTY_EXPORT.value]


@dataclass(frozen=True, slots=True)
class Config:
    """Validated settings for one run.

    Frozen: a run's parameters cannot drift after ``config_hash`` has been
    written into an audit line. Use :meth:`replace` to derive a variant, which
    revalidates.
    """

    state_root: Path = DEFAULT_STATE_ROOT
    """Root of all persistent state. ``~`` is expanded; relative paths are made
    absolute against the current directory. Nothing is ever written outside it
    (PRIV-03)."""

    retention_days: int = 90
    """Records older than this are deleted by the startup sweep (PRIV-11).
    ``0`` means "expire everything on the next sweep"; negative is refused."""

    retain_emails: bool = False
    """When False the ``email`` column is written NULL even if a ``Member``
    carries one (PRIV-06). Reports redact emails regardless (PRIV-17)."""

    enabled_adapters: list[str] = field(default_factory=_default_adapters)
    """Compliance postures permitted to produce records (PRIV-04)."""

    include_inferred: bool = False
    """Whether inferred edges may enter the graph (PRIV-19)."""

    min_inferred_confidence: float = 0.5
    """Floor applied to inferred edges when ``include_inferred`` is True."""

    allow_contact_fields: bool = False
    """Whether acquisition adapters may carry contact details across the
    boundary at all. Independent of ``retain_emails``, which governs
    persistence."""

    base_seed: int = 42
    """Seed for the final Leiden pass. Not sufficient for determinism on its
    own — R3 got 12 partitions from 12 vertex orderings at a fixed seed — which
    is why the sort rules and consensus clustering exist."""

    resolution: float = 1.0
    """Leiden resolution parameter. Must be > 0."""

    consensus_runs: int = 25
    """Number of seeds in the consensus ensemble. Must be >= 1."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", _coerce_root(self.state_root))
        object.__setattr__(self, "enabled_adapters", _coerce_adapters(self.enabled_adapters))
        object.__setattr__(
            self, "retention_days", _coerce_int("retention_days", self.retention_days)
        )
        object.__setattr__(self, "base_seed", _coerce_int("base_seed", self.base_seed))
        object.__setattr__(
            self, "consensus_runs", _coerce_int("consensus_runs", self.consensus_runs)
        )
        object.__setattr__(self, "retain_emails", _coerce_bool("retain_emails", self.retain_emails))
        object.__setattr__(
            self, "include_inferred", _coerce_bool("include_inferred", self.include_inferred)
        )
        object.__setattr__(
            self,
            "allow_contact_fields",
            _coerce_bool("allow_contact_fields", self.allow_contact_fields),
        )
        object.__setattr__(self, "resolution", _coerce_float("resolution", self.resolution))
        object.__setattr__(
            self,
            "min_inferred_confidence",
            _coerce_float("min_inferred_confidence", self.min_inferred_confidence),
        )
        self.validate()

    # -- validation ------------------------------------------------------

    def validate(self) -> None:
        """Raise :class:`ConfigError` on any setting that cannot be honoured."""
        if self.retention_days < 0:
            raise ConfigError(
                f"retention_days must be >= 0, got {self.retention_days}. "
                "A negative retention period has no meaning; use 0 to expire everything."
            )
        if self.retention_days > 3650:
            raise ConfigError(
                f"retention_days must be <= 3650 (10 years), got {self.retention_days}. "
                "Storage limitation (GDPR Art. 5(1)(e)) is not satisfied by an unbounded period."
            )
        unknown = [a for a in self.enabled_adapters if a not in KNOWN_ADAPTERS]
        if unknown:
            raise ConfigError(
                f"unknown adapter posture(s): {sorted(unknown)!r}. "
                f"enabled_adapters names compliance postures, one of {sorted(KNOWN_ADAPTERS)!r}"
            )
        if self.resolution <= 0:
            raise ConfigError(f"resolution must be > 0, got {self.resolution}")
        if self.consensus_runs < 1:
            raise ConfigError(f"consensus_runs must be >= 1, got {self.consensus_runs}")
        if not 0.0 <= self.min_inferred_confidence <= 1.0:
            raise ConfigError(
                f"min_inferred_confidence must be in [0.0, 1.0], got {self.min_inferred_confidence}"
            )
        if self.base_seed < 0:
            raise ConfigError(f"base_seed must be >= 0, got {self.base_seed}")

    # -- derived paths ---------------------------------------------------

    @property
    def db_path(self) -> Path:
        """SQLite database holding members, targets, edges, review queue and
        tombstones."""
        return self.state_root / DB_FILENAME

    @property
    def audit_path(self) -> Path:
        """Append-only JSONL audit log (PRIV-15)."""
        return self.state_root / AUDIT_FILENAME

    # -- adapter gating (PRIV-04) ----------------------------------------

    @property
    def enabled_postures(self) -> frozenset[CompliancePosture]:
        return frozenset(CompliancePosture(a) for a in self.enabled_adapters)

    def adapter_enabled(self, posture: CompliancePosture | str) -> bool:
        """True iff an adapter declaring ``posture`` is permitted to run."""
        value = posture.value if isinstance(posture, CompliancePosture) else str(posture)
        return value in set(self.enabled_adapters)

    # -- identity --------------------------------------------------------

    def settings(self) -> dict[str, Any]:
        """JSON-safe view of every setting, for hashing and for reports."""
        return {
            "state_root": self.state_root.as_posix(),
            "retention_days": self.retention_days,
            "retain_emails": self.retain_emails,
            "enabled_adapters": list(self.enabled_adapters),
            "include_inferred": self.include_inferred,
            "min_inferred_confidence": self.min_inferred_confidence,
            "allow_contact_fields": self.allow_contact_fields,
            "base_seed": self.base_seed,
            "resolution": self.resolution,
            "consensus_runs": self.consensus_runs,
        }

    @property
    def config_hash(self) -> str:
        """SHA-256 over the settings, keys sorted.

        Stable across processes and ``PYTHONHASHSEED`` values because the input
        is canonical JSON, not a Python ``hash()``. Written into every audit
        line so a result can be tied to the parameters that produced it.
        """
        payload = json.dumps(self.settings(), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def replace(self, **changes: Any) -> Config:
        """Derived config with ``changes`` applied. Revalidates."""
        unknown = set(changes) - {f.name for f in fields(self)}
        if unknown:
            raise ConfigError(f"unknown setting(s): {sorted(unknown)!r}")
        return replace(self, **changes)


# ---------------------------------------------------------------------------
# Coercion helpers — TOML, YAML and the environment all supply loose types.
# ---------------------------------------------------------------------------


def _coerce_root(value: Any) -> Path:
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str):
        if not value.strip():
            raise ConfigError("state_root must not be empty")
        path = Path(value)
    else:
        raise ConfigError(f"state_root must be a path or string, got {type(value).__name__}")
    expanded = path.expanduser()
    # ``absolute()`` rather than ``resolve()``: symlinked temp roots must keep
    # the name the caller gave them, or PRIV-03's "descendant of the root"
    # assertion compares two different spellings of the same directory.
    return expanded if expanded.is_absolute() else Path(os.path.abspath(expanded))


def _coerce_adapters(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, frozenset, set)):
        items = [str(part).strip() for part in value]
    else:
        raise ConfigError(
            f"enabled_adapters must be a list of posture names, got {type(value).__name__}"
        )
    return [item for item in items if item]


def _coerce_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
        raise ConfigError(f"{name} must be a boolean, got {value!r}")
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


def _coerce_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    raise ConfigError(f"{name} must be an integer, got {type(value).__name__}")


def _coerce_float(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    raise ConfigError(f"{name} must be a number, got {type(value).__name__}")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_config() -> Config:
    """The shipped configuration: local-only, 90 days, no emails, sanctioned
    adapter only (PRIV-04)."""
    return Config()


def _read_mapping(path: Path) -> Mapping[str, Any]:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".toml":
            with path.open("rb") as handle:
                data: Any = tomllib.load(handle)
        elif suffix in (".yaml", ".yml"):
            import yaml  # imported lazily: the analysis path must not need it

            with path.open("r", encoding="utf-8") as text_handle:
                data = yaml.safe_load(text_handle)
        else:
            raise ConfigError(
                f"unsupported config format {path.suffix!r} for {path}; use .toml, .yaml or .yml"
            )
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except Exception as exc:  # pragma: no cover - yaml raises its own family
        if type(exc).__module__.startswith("yaml"):
            raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
        raise

    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"config file {path} must contain a table/mapping at the top level")
    # Tolerate a single wrapping [interlayer] table.
    inner = data.get("interlayer")
    if len(data) == 1 and isinstance(inner, Mapping):
        return inner
    return data


def config_from_mapping(data: Mapping[str, Any], *, base: Config | None = None) -> Config:
    """Build a config from a mapping, rejecting unknown keys.

    Unknown keys are an error rather than a warning: a silently ignored
    ``retain_emails: flase`` is a privacy failure that looks like a working
    configuration.
    """
    known = {f.name for f in fields(Config)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"unknown config key(s): {unknown!r}; known keys are {sorted(known)!r}")
    start = base if base is not None else Config()
    return replace(start, **{key: value for key, value in data.items()})


def config_from_env(
    env: Mapping[str, str] | None = None, *, base: Config | None = None
) -> Config:
    """Apply ``INTERLAYER_*`` overrides on top of ``base``."""
    source = os.environ if env is None else env
    overrides: dict[str, Any] = {}
    for f in fields(Config):
        key = ENV_PREFIX + f.name.upper()
        if key in source:
            overrides[f.name] = source[key]
    return config_from_mapping(overrides, base=base)


def load_config(
    path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Load configuration from a file, then the environment, then explicit
    overrides.

    Precedence, lowest first: dataclass defaults, file, ``INTERLAYER_*``
    environment variables, ``overrides`` (the CLI's flags).

    With ``path=None`` the first of ``$INTERLAYER_CONFIG``,
    ``<state_root>/config.toml`` and ``./interlayer.toml`` that exists is used;
    if none does, the defaults stand.
    """
    source = os.environ if env is None else env
    config = Config()

    candidate: Path | None
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
    else:
        candidate = _discover_config(source)

    if candidate is not None:
        config = config_from_mapping(_read_mapping(candidate), base=config)

    config = config_from_env(source, base=config)

    if overrides:
        config = config_from_mapping(
            {k: v for k, v in overrides.items() if v is not None}, base=config
        )
    return config


def _discover_config(env: Mapping[str, str]) -> Path | None:
    explicit = env.get(ENV_PREFIX + "CONFIG")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"{ENV_PREFIX}CONFIG points at a missing file: {candidate}")
        return candidate

    root_setting = env.get(ENV_PREFIX + "STATE_ROOT")
    root = _coerce_root(root_setting) if root_setting else _coerce_root(DEFAULT_STATE_ROOT)
    for candidate in (root / "config.toml", root / "config.yaml", Path("interlayer.toml")):
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "AUDIT_FILENAME",
    "DB_FILENAME",
    "DEFAULT_STATE_ROOT",
    "DERIVED_DIRNAMES",
    "ENV_PREFIX",
    "KNOWN_ADAPTERS",
    "Config",
    "config_from_env",
    "config_from_mapping",
    "default_config",
    "load_config",
]
