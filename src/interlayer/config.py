"""Runtime configuration.

Defaults come from Wave 1 research and are documented at each field so a build
agent can see why a number is what it is. Everything is overridable via
``interlayer --config path.yaml`` or environment variables prefixed ``INTERLAYER_``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from interlayer.errors import ConfigError

__all__ = ["ScoreWeights", "Settings", "load_settings"]

DEFAULT_SEED = 20240101


class ScoreWeights(BaseModel):
    """Additive score weights. Observed evidence must outrank every inference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_mutual: float = 10.0
    """A human read this off LinkedIn. Ground truth; dominates all inference."""

    direct_employment: float = 6.0
    """Currently at a target firm -- certain adjacency, but not via the ego."""

    past_employment: float = 3.0
    alumni_overlap: float = 1.5
    shared_employer: float = 1.0
    proximity: float = 2.0
    """Personalized-PageRank mass from target seeds."""

    cluster: float = 1.0
    title_signal: float = 0.5


class Settings(BaseModel):
    """Everything a stage needs. Stages receive this and nothing else."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- paths -------------------------------------------------------------
    input_csv: Path | None = None
    artifact_dir: Path = Path("artifacts")
    gazetteer: Path = Path("data/gazetteer/firms.yaml")

    # --- determinism -------------------------------------------------------
    seed: int = DEFAULT_SEED
    """Every stochastic call must be seeded from this. See consensus_runs."""

    # --- privacy -----------------------------------------------------------
    keep_emails: bool = False
    """Off by default: emails are dropped at ingest unless explicitly retained."""

    email_hmac_key: str | None = None
    """When keep_emails is on, emails are HMAC'd with this rather than stored bare."""

    redact: bool = False
    """Emit pseudonymous stable ids instead of names, for sharing results."""

    # --- entity resolution (Wave 1 / Agent 4, empirically calibrated) -------
    match_accept: float = 90.0
    """rapidfuzz WRatio at/above which a firm match is accepted. Top of a flat plateau."""

    match_review: float = 84.0
    """Between review and accept, a match is surfaced for human review, never auto-applied."""

    # --- graph (Wave 1 / Agent 2) ------------------------------------------
    require_cotenure: bool = True
    """No date overlap => no shared-employer edge. Removes ~62% of candidate edges."""

    missing_date_factor: float = 0.3
    """Damping when tenure dates are absent. Judgement call; needs real-data tuning."""

    max_overlap_years: float = 5.0
    disparity_alpha: float = 0.05
    """Serrano disparity-filter significance for backbone extraction."""

    rescue_top_k: int = 3
    """Edges per node rescued from the filter, so sparse nodes are not orphaned."""

    # --- clustering --------------------------------------------------------
    resolution: float = 2.0
    """Leiden RBConfiguration gamma. 1.0 gives clusters ~4x too coarse to act on."""

    resolution_sweep: tuple[float, ...] = (1.0, 2.0, 4.0)
    consensus_runs: int = 50
    """Single-run Leiden flaps (mean pairwise ARI 0.877). Consensus lifts it to ~1.0."""

    consensus_threshold: float = 0.5
    min_cluster_size: int = 3

    # --- scoring -----------------------------------------------------------
    pagerank_alpha: float = 0.85
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    top_n: int = 50

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.match_review > self.match_accept:
            raise ConfigError(
                f"match_review ({self.match_review}) must not exceed "
                f"match_accept ({self.match_accept})"
            )
        if self.keep_emails and not self.email_hmac_key:
            raise ConfigError("keep_emails=True requires email_hmac_key to be set")
        if not 0.0 < self.pagerank_alpha < 1.0:
            raise ConfigError(f"pagerank_alpha must be in (0,1), got {self.pagerank_alpha}")
        if self.consensus_runs < 1:
            raise ConfigError("consensus_runs must be >= 1")
        return self

    # --- artifact paths ----------------------------------------------------
    def artifact(self, name: str) -> Path:
        """Resolve an artifact filename inside the configured artifact directory."""
        return self.artifact_dir / name

    @property
    def people_path(self) -> Path:
        return self.artifact("people.jsonl")

    @property
    def orgs_path(self) -> Path:
        return self.artifact("orgs.jsonl")

    @property
    def affiliations_path(self) -> Path:
        return self.artifact("affiliations.jsonl")

    @property
    def observations_path(self) -> Path:
        return self.artifact("observations.jsonl")

    @property
    def targets_path(self) -> Path:
        return self.artifact("targets.jsonl")

    @property
    def edges_path(self) -> Path:
        return self.artifact("edges.jsonl")

    @property
    def clusters_path(self) -> Path:
        return self.artifact("clusters.jsonl")

    @property
    def scored_people_path(self) -> Path:
        return self.artifact("scored_people.jsonl")

    @property
    def scored_clusters_path(self) -> Path:
        return self.artifact("scored_clusters.jsonl")

    @property
    def manifest_path(self) -> Path:
        return self.artifact("manifest.json")

    @property
    def report_path(self) -> Path:
        return self.artifact("report.html")


def _env_overrides() -> dict[str, Any]:
    """Read INTERLAYER_* environment variables into raw setting values."""
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith("INTERLAYER_"):
            continue
        field = key.removeprefix("INTERLAYER_").lower()
        if field in Settings.model_fields:
            out[field] = value
    return out


def load_settings(path: Path | None = None, **overrides: Any) -> Settings:
    """Build Settings from an optional YAML file, environment, then keyword overrides.

    Precedence, lowest to highest: file, environment, explicit keyword arguments.
    """
    data: dict[str, Any] = {}
    if path is not None:
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"config file is not valid YAML: {path}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError(f"config file must contain a mapping, got {type(loaded).__name__}")
        data.update(loaded)
    data.update(_env_overrides())
    data.update({k: v for k, v in overrides.items() if v is not None})
    try:
        return Settings(**data)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
