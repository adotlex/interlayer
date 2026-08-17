"""Optional profile enrichment. Offline by default, and never a source of edges.

Wave 1's central finding is encoded here as a type, not a comment: no provider
sells LinkedIn connection-graph or mutual-connection data, so
:attr:`ProviderCapabilities.provides_connection_edges` is ``False`` on every
shipped adapter and a test asserts it across the registry. Enrichment contributes
*optional attributes* to people the collectors already found. Remove it entirely
and the graph is unchanged.

Importing this package imports no provider and no HTTP client.
"""

from interlayer.enrichment.interface import (
    EnrichedPerson,
    EnrichmentProvenance,
    EnrichmentProvider,
    EnrichmentResult,
    LawfulBasis,
    LookupKey,
    LookupKeyType,
    ProviderCapabilities,
    ProviderCompliance,
    ProviderConfigError,
    ProviderCost,
    ProviderDisabledError,
    ProviderError,
    Seniority,
    ToSRisk,
)

__all__ = [
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
]
