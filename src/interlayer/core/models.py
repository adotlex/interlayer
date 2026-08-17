"""Shared data contract for interlayer.

FROZEN FOR WAVE 2. Every work package imports from this module; no work package
edits it. If something here is wrong, write the problem to
``docs/wave2-notes/<your-id>.md`` and the orchestrator amends it centrally.

Standard library only, by design: every layer imports this, so it must never pull
in a dependency that the offline analysis path is forbidden from touching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CompliancePosture(str, Enum):
    """How a collector obtained its data.

    Ordered from least to most aggressive. ``config.enabled_adapters`` gates on
    these, and the shipped default enables ``FIRST_PARTY_EXPORT`` only (PRIV-04).
    """

    FIRST_PARTY_EXPORT = "first_party_export"
    MANUAL_CAPTURE = "manual_capture"
    THIRD_PARTY_API = "third_party_api"
    AUTOMATED = "automated"


class MatchStatus(str, Enum):
    """Outcome of resolving a free-text employer string to a target entity.

    ``NEEDS_REVIEW`` is a hard barrier, not a hint: such records must not enter
    the analysis graph until a human adjudicates them (PRIV-20). Wave 1 measured
    why — no fuzzy threshold separates "Jane Street Capital LLC" from
    "Jane Street Entertainment".
    """

    CONFIDENT = "confident"
    NEEDS_REVIEW = "needs_review"
    NO_MATCH = "no_match"


class EdgeOrigin(str, Enum):
    """Whether an edge was seen or guessed.

    OBSERVED edges come from a mutual-connections surface the user actually
    viewed. INFERRED edges are co-affiliation guesses and carry a confidence
    below 1.0. The two must never be blended in output (PRIV-19).
    """

    OBSERVED = "observed"
    INFERRED = "inferred"


class Firm(str, Enum):
    """Target entities. Citadel and Citadel Securities are legally distinct
    firms with separate LinkedIn pages; conflating them silently corrupts
    results, so they are separate members here rather than one value.
    """

    JANE_STREET = "jane_street"
    CITADEL = "citadel"
    CITADEL_SECURITIES = "citadel_securities"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Attached to every record crossing into the analysis layer."""

    source: str
    """Adapter id that produced the record."""

    collected_at: datetime
    """ISO-8601 UTC. Drives the retention sweep (PRIV-11)."""

    posture: CompliancePosture = CompliancePosture.FIRST_PARTY_EXPORT

    truncated: bool = False
    """True when the upstream surface capped results (e.g. a 1,000-row search
    ceiling), so absence of an edge cannot be read as evidence of no edge."""


@dataclass(frozen=True, slots=True)
class Member:
    """One of the user's 1st-degree connections — an element of ``M``."""

    member_id: str
    first_name: str
    last_name: str
    linkedin_slug: str | None = None
    company_raw: str | None = None
    position: str | None = None
    connected_on: date | None = None

    email: str | None = None
    """Discarded at parse time unless ``config.retain_emails`` (PRIV-06), and
    redacted from every rendered report unconditionally (PRIV-17)."""

    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class Target:
    """A Jane Street / Citadel / Citadel Securities person — an element of ``T``."""

    target_id: str
    firm: Firm
    first_name: str = ""
    last_name: str = ""
    linkedin_slug: str | None = None
    title: str | None = None
    team: str | None = None

    harvested: bool = False
    """True once this target's mutual-connections list has actually been
    collected. Unharvested targets contribute no edges and are excluded from
    degree denominators — otherwise "not yet collected" reads as "no edge"."""

    provenance: Provenance | None = None


@dataclass(frozen=True, slots=True)
class Edge:
    """``member_id`` is a 1st-degree connection of ``target_id``."""

    member_id: str
    target_id: str
    origin: EdgeOrigin = EdgeOrigin.OBSERVED
    confidence: float = 1.0
    evidence: str | None = None
    """Human-readable justification. Mandatory for INFERRED edges: an
    unexplained score is indistinguishable from a fabrication."""

    provenance: Provenance | None = None

    @property
    def observed(self) -> bool:
        return self.origin is EdgeOrigin.OBSERVED


@dataclass(frozen=True, slots=True)
class CompanyMatch:
    """Result of resolving one free-text employer string."""

    raw: str
    status: MatchStatus
    firm: Firm | None = None
    rule: str = ""
    """Which ladder rung fired. Required for auditability."""
    score: float | None = None


@dataclass(frozen=True, slots=True)
class Brokerage:
    """Per-bridge structural metrics, all computed on observed edges."""

    member_id: str
    reach: int = 0
    rarity: float = 0.0
    betweenness: float = 0.0
    effective_size: float = 0.0
    constraint: float = 1.0
    autonomy: float = 0.0
    composite: float = 0.0


@dataclass(frozen=True, slots=True)
class Cluster:
    """A pocket of bridges that reach into the same part of the target graph."""

    cluster_id: int
    label: str
    member_ids: tuple[str, ...]
    top_targets: tuple[str, ...] = ()
    stability: float = 1.0
    """Mean co-association across the consensus seeds."""
    score_sum: float = 0.0


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of the target set has actually been looked at.

    Reported alongside every result so a thin harvest is never mistaken for a
    thin network.
    """

    n_targets_total: int = 0
    n_targets_harvested: int = 0
    n_members_total: int = 0
    n_bridges: int = 0

    @property
    def fraction(self) -> float:
        if self.n_targets_total == 0:
            return 0.0
        return self.n_targets_harvested / self.n_targets_total


@dataclass(frozen=True, slots=True)
class NextTarget:
    """A suggested next target to harvest, with the reason it ranked."""

    target_id: str
    priority: float
    n_known_bridges: int = 0
    n_clusters_touched: int = 0
    novelty: float = 0.0


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """The whole output of the analysis layer."""

    clusters: tuple[Cluster, ...] = ()
    brokerage: tuple[Brokerage, ...] = ()
    coverage: Coverage = field(default_factory=Coverage)
    next_targets: tuple[NextTarget, ...] = ()
    used_inferred_edges: bool = False
    unadjudicated_count: int = 0
    """Count of NEEDS_REVIEW records held out of the graph (PRIV-20)."""
    fingerprint: str = ""
    params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Persisted-field allowlist (PRIV-08)
# ---------------------------------------------------------------------------

MEMBER_FIELD_ALLOWLIST: frozenset[str] = frozenset(
    {
        "member_id",
        "first_name",
        "last_name",
        "linkedin_slug",
        "company_raw",
        "position",
        "connected_on",
        "email",
        "source",
        "collected_at",
    }
)

TARGET_FIELD_ALLOWLIST: frozenset[str] = frozenset(
    {
        "target_id",
        "firm",
        "first_name",
        "last_name",
        "linkedin_slug",
        "title",
        "team",
        "harvested",
        "source",
        "collected_at",
    }
)

EDGE_FIELD_ALLOWLIST: frozenset[str] = frozenset(
    {
        "member_id",
        "target_id",
        "origin",
        "confidence",
        "evidence",
        "source",
        "collected_at",
        "truncated",
    }
)

#: Adding a field to the persisted schema must fail the PRIV-08 test until the
#: addition is reviewed against GDPR Art. 9. That is the point of the constant.
ALL_FIELD_ALLOWLISTS: dict[str, frozenset[str]] = {
    "member": MEMBER_FIELD_ALLOWLIST,
    "target": TARGET_FIELD_ALLOWLIST,
    "edge": EDGE_FIELD_ALLOWLIST,
}

__all__ = [
    "ALL_FIELD_ALLOWLISTS",
    "AnalysisResult",
    "Brokerage",
    "Cluster",
    "CompanyMatch",
    "CompliancePosture",
    "Coverage",
    "EDGE_FIELD_ALLOWLIST",
    "Edge",
    "EdgeOrigin",
    "Firm",
    "MEMBER_FIELD_ALLOWLIST",
    "MatchStatus",
    "Member",
    "NextTarget",
    "Provenance",
    "TARGET_FIELD_ALLOWLIST",
    "Target",
]
