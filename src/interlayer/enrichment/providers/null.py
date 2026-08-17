"""The default provider: offline, free, and returns nothing.

This is not a placeholder. It is the shipped default, and the whole pipeline must
run to completion against it: bridge detection, projection, clustering and
reporting all work with every enrichment field ``None``, because enrichment
contributes *optional* attributes and never the edges. Anything that breaks when
this provider is selected is a bug in the caller, not a missing feature here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from interlayer.enrichment.interface import (
    EnrichmentResult,
    LawfulBasis,
    LookupKey,
    ProviderCapabilities,
    ProviderCompliance,
    ProviderCost,
    ToSRisk,
)
from interlayer.core.models import CompliancePosture

NAME = "null"


class NullEnrichmentProvider:
    """Yields one miss per key. No I/O of any kind."""

    name = NAME
    capabilities = ProviderCapabilities(
        provides_connection_edges=False,
        requires_network=False,
        max_batch_size=1,
    )
    compliance = ProviderCompliance(
        tos_risk=ToSRisk.NONE,
        lawful_basis=LawfulBasis.FIRST_PARTY,
        requires_user_credentials=False,
        stores_data_offshore=False,
        vendor_opt_out_url=None,
        posture=CompliancePosture.FIRST_PARTY_EXPORT,
        notes="No external data source. The shipped default.",
    )
    cost = ProviderCost(usd_per_lookup=0.0, notes="Free, offline.")

    def enrich(self, keys: Sequence[LookupKey]) -> Iterator[EnrichmentResult]:
        for key in keys:
            yield EnrichmentResult(key=key, person=None)

    def estimate_cost(self, n_keys: int) -> float:
        return 0.0


def factory(**_: object) -> NullEnrichmentProvider:
    return NullEnrichmentProvider()


__all__ = ["NAME", "NullEnrichmentProvider", "factory"]
