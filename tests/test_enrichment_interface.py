"""The provider contract types."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from interlayer.core.models import CompliancePosture
from interlayer.enrichment.interface import (
    EnrichmentProvenance,
    EnrichmentResult,
    LawfulBasis,
    LookupKey,
    LookupKeyType,
    ProviderCapabilities,
    ProviderCompliance,
    ProviderCost,
    Seniority,
    ToSRisk,
    cache_id_for,
)


def test_credential_requiring_adapters_cannot_be_constructed() -> None:
    """The prohibition is structural: the dataclass refuses to exist."""
    with pytest.raises(ValueError, match="credential surrender"):
        ProviderCompliance(
            tos_risk=ToSRisk.ACCOUNT_AUTOMATION,
            lawful_basis=LawfulBasis.UNKNOWN,
            requires_user_credentials=True,
            stores_data_offshore=True,
            vendor_opt_out_url=None,
        )


def test_connection_edges_default_to_false() -> None:
    assert ProviderCapabilities().provides_connection_edges is False
    assert ProviderCapabilities().provides_connections_count is False


def test_lookup_key_cache_id_is_stable_and_case_insensitive() -> None:
    a = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "AlexRivera")
    b = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, " alexrivera ")
    assert a.cache_id() == b.cache_id()
    assert len(a.cache_id()) == 64


def test_cache_id_separates_key_types_and_employer_hints() -> None:
    url = cache_id_for(LookupKeyType.LINKEDIN_URL, "alexrivera")
    slug = cache_id_for(LookupKeyType.LINKEDIN_PUBLIC_ID, "alexrivera")
    named = cache_id_for(LookupKeyType.NAME_AND_EMPLOYER, "Alex Rivera", "Acme")
    other = cache_id_for(LookupKeyType.NAME_AND_EMPLOYER, "Alex Rivera", "Globex")
    assert len({url, slug, named, other}) == 4


def test_provenance_converts_to_the_frozen_contract() -> None:
    now = datetime(2026, 8, 17, 10, tzinfo=UTC)
    provenance = EnrichmentProvenance(provider="local_file", retrieved_at=now)
    core = provenance.to_core(posture=CompliancePosture.FIRST_PARTY_EXPORT)
    assert core.source == "local_file"
    assert core.collected_at == now
    assert core.posture is CompliancePosture.FIRST_PARTY_EXPORT
    assert core.truncated is False


def test_result_reports_a_miss() -> None:
    key = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "nobody")
    assert EnrichmentResult(key=key).is_miss is True


def test_cost_defaults_to_free() -> None:
    assert ProviderCost().usd_per_lookup == 0.0


def test_seniority_vocabulary_is_closed() -> None:
    assert {s.value for s in Seniority} == {
        "intern",
        "junior",
        "mid",
        "senior",
        "lead",
        "director",
        "vp",
        "cxo",
        "owner",
        "unknown",
    }


def test_enriched_person_has_no_contact_fields() -> None:
    from interlayer.enrichment.interface import EnrichedPerson

    fields = set(EnrichedPerson.__dataclass_fields__)
    assert not {"email", "phone", "address", "birth_date"} & fields
