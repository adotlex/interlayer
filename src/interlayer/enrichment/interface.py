"""The ``EnrichmentProvider`` protocol and the types adapters are allowed to return.

**The one finding this module exists to encode.** No commercially available
provider sells LinkedIn connection-graph or mutual-connection data. A member's
connection list is never rendered on a logged-out surface, and mutuals are
computed server-side per viewer, so there is no artefact for a scraper to collect
and nothing for a broker to resell. What vendors do sell is ``connections_count``
— one integer, censored by the source at "500+", therefore saturated and
information-free for exactly the senior finance population this tool is pointed
at.

Hence :attr:`ProviderCapabilities.provides_connection_edges`. It is ``False`` on
every shipped adapter and a test asserts it across the registry. It is not a
feature flag; it is a fact about the market, written into the type system so that
a future adapter claiming otherwise has to say so explicitly and get looked at by
a human. The tools that genuinely do return mutual connections achieve it by
taking the user's session credential and acting as them from a third-party cloud.
That is credential surrender, it is what got several of those companies sued out
of existence, and it is out of scope here permanently:
:class:`ProviderCompliance` refuses to construct with
``requires_user_credentials=True``.

Two naming notes. ``core.models`` is frozen and already owns the names
``CompliancePosture`` and ``Provenance``, so this module's richer, provider-scoped
versions are :class:`ProviderCompliance` and :class:`EnrichmentProvenance`; the
latter converts to the frozen contract via :meth:`EnrichmentProvenance.to_core`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from interlayer.core.models import CompliancePosture, Provenance

#: Bumped when :class:`EnrichedPerson` changes shape. Part of every cache key, so
#: a bump invalidates cleanly instead of serving records the reader cannot parse.
ENRICHMENT_SCHEMA_VERSION = 1


class ToSRisk(str, Enum):
    """Declared compliance posture. Every adapter must declare one."""

    NONE = "none"
    """First-party or user-supplied data."""

    PUBLIC_SCRAPE = "public"
    """Logged-off public pages."""

    AGGREGATED = "aggregated"
    """Broker database of mixed provenance."""

    ACCOUNT_AUTOMATION = "account_automation"
    """Requires the user's own credentials — banned, see module docstring."""


class LawfulBasis(str, Enum):
    FIRST_PARTY = "first_party"
    USER_SUPPLIED = "user_supplied"
    LEGITIMATE_INTEREST = "legitimate_interest"
    UNKNOWN = "unknown"


class Seniority(str, Enum):
    """Fixed vocabulary. A provider's raw seniority string never passes through."""

    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    DIRECTOR = "director"
    VP = "vp"
    CXO = "cxo"
    OWNER = "owner"
    UNKNOWN = "unknown"


class DatePrecision(str, Enum):
    """How precise a normalised date actually is.

    An overlap computed from year-only dates is far weaker evidence than one
    computed from month-level dates, and the affinity scorer must be able to tell
    them apart rather than treating ``date(2019, 1, 1)`` as a fact.
    """

    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    NONE = "none"


class ProviderError(RuntimeError):
    """Base class for unrecoverable adapter errors."""


class ProviderConfigError(ProviderError):
    """Missing or invalid configuration — no API key, no file, no credit.

    Adapters raise this and nothing else. A lookup that simply does not resolve is
    a miss (``EnrichmentResult(person=None)``), never an exception.
    """


class ProviderDisabledError(ProviderError):
    """A provider that is present but not enabled was asked to run."""


@dataclass(frozen=True)
class ProviderCompliance:
    """Where an adapter's data comes from and what that costs the user."""

    tos_risk: ToSRisk
    lawful_basis: LawfulBasis
    requires_user_credentials: bool
    stores_data_offshore: bool
    vendor_opt_out_url: str | None
    posture: CompliancePosture = CompliancePosture.THIRD_PARTY_API
    notes: str = ""

    def __post_init__(self) -> None:
        if self.requires_user_credentials:
            raise ValueError(
                "Adapters requiring the user's own LinkedIn credentials are "
                "prohibited. The tools that genuinely return mutual connections do "
                "it by taking the user's session cookie and acting as them from a "
                "third-party cloud; that is credential surrender and it is out of "
                "scope permanently. See docs/research/R2-third-party-providers.md."
            )


@dataclass(frozen=True)
class ProviderCapabilities:
    """What an adapter can actually supply."""

    provides_connection_edges: bool = False
    """THE discriminating field, and ``False`` on every shipped adapter.

    No commercially available provider can set this ``True``. Setting it is a
    claim that a vendor sells the bipartite edge set itself, which Wave 1 found
    does not exist for sale at any price. A registry-wide test asserts it stays
    ``False``; changing that is a review event, not a config change."""

    provides_connections_count: bool = False
    """The censored scalar. Saturates at "500+", so it is information-free for
    anyone senior enough to be worth targeting."""

    provides_employment_history: bool = False
    provides_education_history: bool = False
    provides_skills: bool = False
    provides_location: bool = False
    supports_batch: bool = False
    max_batch_size: int = 1
    requires_network: bool = True


@dataclass(frozen=True)
class ProviderCost:
    """Pre-flight cost model. ``usd_per_lookup`` may be 0.0."""

    usd_per_lookup: float = 0.0
    free_tier_lookups_per_month: int = 0
    notes: str = ""


class LookupKeyType(str, Enum):
    LINKEDIN_URL = "linkedin_url"
    LINKEDIN_PUBLIC_ID = "linkedin_public_id"
    NAME_AND_EMPLOYER = "name_and_employer"


@dataclass(frozen=True)
class LookupKey:
    """One thing to look up."""

    key_type: LookupKeyType
    value: str
    employer_hint: str | None = None
    """Only meaningful for :attr:`LookupKeyType.NAME_AND_EMPLOYER`."""

    def cache_id(self) -> str:
        """Stable content address for caching."""
        return cache_id_for(self.key_type, self.value, self.employer_hint)


def cache_id_for(
    key_type: LookupKeyType, value: str, employer_hint: str | None = None
) -> str:
    """``sha256(key_type|value|employer_hint)``, casefolded and stripped."""
    basis = f"{key_type.value}|{value.casefold().strip()}|{(employer_hint or '').casefold().strip()}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmploymentSpan:
    employer_name: str
    employer_domain: str | None = None
    employer_id: str | None = None
    title: str | None = None
    title_normalized: str | None = None
    seniority: Seniority = Seniority.UNKNOWN
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False
    location: str | None = None
    start_precision: DatePrecision = DatePrecision.NONE
    end_precision: DatePrecision = DatePrecision.NONE


@dataclass(frozen=True)
class EducationSpan:
    school_name: str
    school_normalized: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_year: int | None = None
    end_year: int | None = None


@dataclass(frozen=True)
class EnrichmentProvenance:
    """Mandatory on every enriched record. Enables audit, purge and honest labels."""

    provider: str
    retrieved_at: datetime
    source_record_id: str | None = None
    source_url: str | None = None
    confidence: float = 1.0
    is_inferred: bool = False
    license_note: str | None = None

    def to_core(
        self,
        *,
        posture: CompliancePosture = CompliancePosture.THIRD_PARTY_API,
        truncated: bool = False,
    ) -> Provenance:
        """The frozen-contract provenance for anything crossing into analysis."""
        return Provenance(
            source=self.provider,
            collected_at=self.retrieved_at,
            posture=posture,
            truncated=truncated,
        )


@dataclass(frozen=True)
class EnrichedPerson:
    """The only person type an adapter may return.

    Provider payloads never escape ``normalize.py``. Contact fields are
    **deliberately absent**: no email, no phone, no address, no birth date. They
    are not needed for a graph problem, they are the fields that turn a research
    tool into a data-protection liability, and ``normalize.py`` drops them at the
    adapter boundary even when a provider volunteers them.
    """

    person_key: str
    linkedin_public_id: str | None = None
    profile_url: str | None = None

    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None

    location_country: str | None = None
    location_region: str | None = None
    location_city: str | None = None

    current_employer_name: str | None = None
    current_title: str | None = None
    current_seniority: Seniority = Seniority.UNKNOWN
    current_start_date: date | None = None
    current_start_precision: DatePrecision = DatePrecision.NONE

    employment_history: tuple[EmploymentSpan, ...] = ()
    education_history: tuple[EducationSpan, ...] = ()
    skills: tuple[str, ...] = ()

    connections_count: int | None = None
    """``None`` whenever the source reported 500 or more: the source censors the
    value there, so a "500" is not a measurement."""

    followers_count: int | None = None

    provenance: EnrichmentProvenance | None = None
    dropped_fields: tuple[str, ...] = ()
    """Names of fields the provider supplied and the boundary discarded. Recorded
    so the user can see that a contact field was offered and refused."""

    def with_provenance(self, provenance: EnrichmentProvenance) -> EnrichedPerson:
        return replace(self, provenance=provenance)


@dataclass(frozen=True)
class EnrichmentResult:
    """Exactly one of these per input key, in input order."""

    key: LookupKey
    person: EnrichedPerson | None = None
    error: str | None = None
    cache_hit: bool = False
    cost_usd: float = 0.0

    @property
    def is_miss(self) -> bool:
        return self.person is None


@runtime_checkable
class EnrichmentProvider(Protocol):
    """Every enrichment adapter.

    Contract for :meth:`enrich`:

    * MUST yield exactly one :class:`EnrichmentResult` per input key, in input
      order.
    * MUST yield ``person=None`` for a miss, never raise for one.
    * MUST raise only on unrecoverable configuration or auth errors.
    * MUST NOT perform network I/O when ``capabilities.requires_network`` is
      ``False``.
    * MUST set provenance on every returned person.
    * MUST respect the injected rate limiter and budget guard.
    """

    name: str
    capabilities: ProviderCapabilities
    compliance: ProviderCompliance
    cost: ProviderCost

    def enrich(self, keys: Sequence[LookupKey]) -> Iterable[EnrichmentResult]:
        ...

    def estimate_cost(self, n_keys: int) -> float:
        """Pre-flight estimate in USD for ``--dry-run``. Must not do I/O."""
        ...


__all__ = [
    "ENRICHMENT_SCHEMA_VERSION",
    "DatePrecision",
    "EducationSpan",
    "EmploymentSpan",
    "EnrichedPerson",
    "EnrichmentProvenance",
    "EnrichmentProvider",
    "EnrichmentResult",
    "LawfulBasis",
    "LookupKey",
    "LookupKeyType",
    "ProviderCapabilities",
    "ProviderCompliance",
    "ProviderConfigError",
    "ProviderCost",
    "ProviderDisabledError",
    "ProviderError",
    "Seniority",
    "ToSRisk",
    "cache_id_for",
]
