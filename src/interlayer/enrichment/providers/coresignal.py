"""Coresignal adapter — present, but disabled by default (ruling C5).

Same posture as the Bright Data adapter and for the same reasons: it exists so the
option is reviewable, it ships off, and turning it on is an explicit user act.

It sells profile attributes and a ``connections_count`` integer.
``provides_connection_edges`` is ``False``: the connection graph is not for sale
here either, and no tier changes that.

Like every network adapter in this package, no HTTP client is imported at module
scope, so this module imports cleanly with no credentials and no network.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from interlayer.core.models import CompliancePosture
from interlayer.enrichment.budget import BudgetGuard
from interlayer.enrichment.interface import (
    EnrichmentResult,
    LawfulBasis,
    LookupKey,
    LookupKeyType,
    ProviderCapabilities,
    ProviderCompliance,
    ProviderConfigError,
    ProviderCost,
    ToSRisk,
)
from interlayer.enrichment.normalize import (
    FieldMap,
    normalize_person,
    normalize_public_id,
)
from interlayer.enrichment.ratelimit import RateLimiter, for_provider

NAME = "coresignal"

DEFAULT_BASE_URL = "https://api.coresignal.com/cdapi/v1"

FIELD_MAP = FieldMap(
    provider=NAME,
    person={
        "linkedin_public_id": ("shorthand_name", "url", "canonical_url"),
        "profile_url": ("url", "canonical_url"),
        "full_name": ("name", "full_name"),
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "headline": ("title", "headline"),
        "location_country": ("country",),
        "location_region": ("region",),
        "location_city": ("location", "city"),
        "current_employer_name": ("experience_company_name", "company_name"),
        "current_title": ("experience_title", "title"),
        "connections_count": ("connections_count", "connections"),
        "followers_count": ("followers_count", "follower_count"),
    },
    employment={
        "employer_name": ("company_name",),
        "employer_domain": ("company_url", "company_website"),
        "title": ("title", "position_title"),
        "start_date": ("date_from", "start_date"),
        "end_date": ("date_to", "end_date"),
        "location": ("location",),
    },
    education={
        "school_name": ("title", "school"),
        "degree": ("subtitle", "degree"),
        "field_of_study": ("field_of_study",),
        "start_year": ("date_from",),
        "end_year": ("date_to",),
    },
    employment_list=("member_experience_collection", "experience"),
    education_list=("member_education_collection", "education"),
    skills_list=("member_skills_collection", "skills"),
)


class CoresignalEnrichmentProvider:
    """Profile enrichment via Coresignal's member API. Disabled by default."""

    name = NAME
    capabilities = ProviderCapabilities(
        provides_connection_edges=False,
        provides_connections_count=True,
        provides_employment_history=True,
        provides_education_history=True,
        provides_skills=True,
        provides_location=True,
        supports_batch=False,
        max_batch_size=1,
        requires_network=True,
    )
    compliance = ProviderCompliance(
        tos_risk=ToSRisk.AGGREGATED,
        lawful_basis=LawfulBasis.LEGITIMATE_INTEREST,
        requires_user_credentials=False,
        stores_data_offshore=True,
        vendor_opt_out_url="https://coresignal.com/privacy-policy/",
        posture=CompliancePosture.THIRD_PARTY_API,
        notes=(
            "Aggregated broker database of mixed provenance. Ships disabled; "
            "enabling it is an explicit, logged user act (ruling C5)."
        ),
    )
    cost = ProviderCost(
        usd_per_lookup=0.02,
        notes="Indicative only — credit-based pricing, confirm before a run.",
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        limiter: RateLimiter | None = None,
        budget: BudgetGuard | None = None,
        allow_contact_fields: bool = False,
        transport: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or for_provider()
        self.budget = budget or BudgetGuard()
        self.allow_contact_fields = allow_contact_fields
        self._transport = transport

    def estimate_cost(self, n_keys: int) -> float:
        return round(max(0, n_keys) * self.cost.usd_per_lookup, 6)

    def enrich(self, keys: Sequence[LookupKey]) -> Iterator[EnrichmentResult]:
        if not keys:
            return
        if not self.api_key:
            raise ProviderConfigError(
                f"{self.name}: an API key is required. This adapter ships disabled; "
                "enable it explicitly in config and supply credentials there."
            )
        for key in keys:
            slug = _slug_for(key)
            if slug is None:
                yield EnrichmentResult(
                    key=key,
                    person=None,
                    error="coresignal resolves public identifiers; this key has none",
                )
                continue
            self.budget.reserve(calls=1, cost_usd=self.cost.usd_per_lookup)
            if self.budget.dry_run:
                yield EnrichmentResult(key=key, person=None, error="dry-run")
                continue
            self.limiter.acquire()
            try:
                payload = self._fetch(slug)
            except ProviderConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - a miss must never kill a run
                yield EnrichmentResult(key=key, person=None, error=str(exc))
                continue
            if payload is None:
                yield EnrichmentResult(key=key, person=None, cost_usd=self.cost.usd_per_lookup)
                continue
            yield EnrichmentResult(
                key=key,
                person=normalize_person(
                    payload,
                    provider=self.name,
                    retrieved_at=datetime.now(timezone.utc),
                    field_map=FIELD_MAP,
                    allow_contact_fields=self.allow_contact_fields,
                    source_record_id=str(payload.get("id")) if payload.get("id") else None,
                    license_note="Coresignal member record",
                ),
                cost_usd=self.cost.usd_per_lookup,
            )

    def _fetch(self, slug: str) -> Mapping[str, Any] | None:
        result = self._request("GET", f"{self.base_url}/member/collect/{slug}")
        return result if isinstance(result, Mapping) else None

    def _request(self, method: str, url: str, body: Any | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self._transport is not None:
            return self._transport(method, url, body, headers)
        from interlayer.enrichment.providers import http_json  # noqa: PLC0415

        return http_json(method, url, body, headers)


def _slug_for(key: LookupKey) -> str | None:
    if key.key_type is LookupKeyType.NAME_AND_EMPLOYER:
        return None
    return normalize_public_id(key.value)


def factory(**kwargs: Any) -> CoresignalEnrichmentProvider:
    return CoresignalEnrichmentProvider(**kwargs)


__all__ = ["NAME", "CoresignalEnrichmentProvider", "factory"]
