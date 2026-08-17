"""Bright Data adapter — present, but disabled by default (ruling C5).

Shipping it disabled is the whole point. PRIV-04 says the default configuration
enables first-party sources only; R2 recommends this vendor as the least-bad
network option if a user chooses one. Both hold: the adapter exists, it is
reviewable, and turning it on is an explicit, logged user act rather than
something that happens because a config file was copied from somewhere.

What it can and cannot do:

* It **cannot** provide connection edges. ``provides_connection_edges`` is
  ``False`` and no price unlocks it — a member's connection list is never
  rendered logged-out and mutuals are computed per-viewer server-side, so there
  is no artefact to collect.
* It sells profile attributes, and one integer named ``connections``.

The Web Scraper API is an **async job model**: submit a request, then poll for the
snapshot. A timeout surfaces as a miss, never an exception, because a slow vendor
is not a reason to lose the rest of the run.

No HTTP client is imported at module scope. This module imports cleanly with no
credentials, no ``httpx`` installed and no network available; that is what lets
the registry introspect every adapter's capabilities in an offline test.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
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
    canonical_profile_url,
    normalize_person,
)
from interlayer.enrichment.ratelimit import RateLimiter, for_provider

NAME = "brightdata"

DEFAULT_BASE_URL = "https://api.brightdata.com"
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# Bright Data: the field literally named "connections" is an INTEGER count, not a
# list of connections. Verified against the vendor's published sample:
#   {"name": ..., "profile_info": {...}, "connections": 2, ...}
# See docs/research/R2-third-party-providers.md. Nothing in this payload is an
# edge, and normalize.py additionally discards the integer once it saturates at
# 500 because the source censors it there.
FIELD_MAP = FieldMap(
    provider=NAME,
    person={
        "linkedin_public_id": ("url", "input_url"),
        "profile_url": ("url", "input_url"),
        "full_name": ("name",),
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "headline": ("position", "profile_info.position"),
        "location_country": ("country_code",),
        "location_region": ("region", "state"),
        "location_city": ("city",),
        "current_employer_name": ("current_company.name", "current_company_name"),
        "current_title": ("current_company.title", "position"),
        "connections_count": ("connections",),
        "followers_count": ("followers", "profile_info.followers"),
    },
    employment={
        "employer_name": ("company", "company_name"),
        "employer_domain": ("company_url", "url"),
        "title": ("title", "positions.title"),
        "start_date": ("start_date", "starts_at"),
        "end_date": ("end_date", "ends_at"),
        "location": ("location",),
    },
    education={
        "school_name": ("title", "school"),
        "degree": ("degree",),
        "field_of_study": ("field",),
        "start_year": ("start_year",),
        "end_year": ("end_year",),
    },
    employment_list=("experience",),
    education_list=("education",),
    skills_list=("skills",),
)


class BrightDataEnrichmentProvider:
    """Profile enrichment via Bright Data's dataset API. Disabled by default."""

    name = NAME
    capabilities = ProviderCapabilities(
        provides_connection_edges=False,
        provides_connections_count=True,
        provides_employment_history=True,
        provides_education_history=True,
        provides_skills=True,
        provides_location=True,
        supports_batch=True,
        max_batch_size=100,
        requires_network=True,
    )
    compliance = ProviderCompliance(
        tos_risk=ToSRisk.PUBLIC_SCRAPE,
        lawful_basis=LawfulBasis.LEGITIMATE_INTEREST,
        requires_user_credentials=False,
        stores_data_offshore=True,
        vendor_opt_out_url="https://brightdata.com/privacy",
        posture=CompliancePosture.THIRD_PARTY_API,
        notes=(
            "Logged-out public pages, collected by the vendor. Ships disabled; "
            "enabling it is an explicit, logged user act (ruling C5)."
        ),
    )
    cost = ProviderCost(
        usd_per_lookup=0.001,
        notes="Indicative only — confirm against the current rate card before a run.",
    )

    def __init__(
        self,
        *,
        api_key: str | None = None,
        dataset_id: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        limiter: RateLimiter | None = None,
        budget: BudgetGuard | None = None,
        allow_contact_fields: bool = False,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        transport: Any | None = None,
    ) -> None:
        # Credentials arrive as an explicit argument. Nothing here reaches into
        # the environment on its own, so a stray variable cannot silently switch
        # a network adapter on.
        self.api_key = api_key
        self.dataset_id = dataset_id
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or for_provider()
        self.budget = budget or BudgetGuard()
        self.allow_contact_fields = allow_contact_fields
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._transport = transport

    # -- protocol ----------------------------------------------------------

    def estimate_cost(self, n_keys: int) -> float:
        return round(max(0, n_keys) * self.cost.usd_per_lookup, 6)

    def enrich(self, keys: Sequence[LookupKey]) -> Iterator[EnrichmentResult]:
        if not keys:
            return
        self._require_config()
        for key in keys:
            url = _target_url(key)
            if url is None:
                yield EnrichmentResult(
                    key=key,
                    person=None,
                    error="brightdata resolves profile URLs; this key has none",
                )
                continue
            self.budget.reserve(calls=1, cost_usd=self.cost.usd_per_lookup)
            if self.budget.dry_run:
                yield EnrichmentResult(key=key, person=None, error="dry-run")
                continue
            self.limiter.acquire()
            try:
                payload = self._fetch(url)
            except ProviderConfigError:
                raise
            except Exception as exc:
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
                    retrieved_at=datetime.now(UTC),
                    field_map=FIELD_MAP,
                    allow_contact_fields=self.allow_contact_fields,
                    source_url=url,
                    license_note="Bright Data dataset record",
                ),
                cost_usd=self.cost.usd_per_lookup,
            )

    # -- internals ---------------------------------------------------------

    def _require_config(self) -> None:
        if not self.api_key or not self.dataset_id:
            raise ProviderConfigError(
                f"{self.name}: an API key and dataset id are required. This adapter "
                "ships disabled; enable it explicitly in config and supply "
                "credentials there."
            )

    def _fetch(self, profile_url: str) -> Mapping[str, Any] | None:
        """Submit one profile and poll for the snapshot. Timeout is a miss."""
        trigger = self._request(
            "POST",
            f"{self.base_url}/datasets/v3/trigger?dataset_id={self.dataset_id}",
            body=[{"url": profile_url}],
        )
        snapshot_id = trigger.get("snapshot_id") if isinstance(trigger, Mapping) else None
        if not snapshot_id:
            return None

        waited = 0.0
        while waited < self.poll_timeout_seconds:
            result = self._request(
                "GET", f"{self.base_url}/datasets/v3/snapshot/{snapshot_id}?format=json"
            )
            if isinstance(result, list) and result:
                first = result[0]
                return first if isinstance(first, Mapping) else None
            if isinstance(result, Mapping) and result.get("status") not in {
                "running",
                "building",
                "collecting",
            }:
                records = result.get("data")
                if isinstance(records, list) and records:
                    first = records[0]
                    return first if isinstance(first, Mapping) else None
                return None
            self.limiter.sleeper(self.poll_interval_seconds)
            waited += self.poll_interval_seconds
        return None

    def _request(self, method: str, url: str, body: Any | None = None) -> Any:
        if self._transport is not None:
            return self._transport(method, url, body, self._headers())
        from interlayer.enrichment.providers import http_json

        return http_json(method, url, body, self._headers())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


def _target_url(key: LookupKey) -> str | None:
    if key.key_type is LookupKeyType.NAME_AND_EMPLOYER:
        return None
    return canonical_profile_url(key.value)


def factory(**kwargs: Any) -> BrightDataEnrichmentProvider:
    return BrightDataEnrichmentProvider(**kwargs)


__all__ = ["NAME", "BrightDataEnrichmentProvider", "factory"]
