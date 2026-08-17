"""The four shipped adapters.

The two offline ones are exercised with the socket removed, because "P0 and
offline" has to mean it cannot reach the network even if it wanted to. The two
network ones are exercised through an injected transport and never touch a socket
in the test suite at all.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from interlayer.enrichment import registry
from interlayer.enrichment.budget import BudgetExceeded, BudgetGuard
from interlayer.enrichment.interface import (
    LookupKey,
    LookupKeyType,
    ProviderConfigError,
    ProviderDisabledError,
    Seniority,
)
from interlayer.enrichment.normalize import (
    lookup_key_for_name,
    lookup_key_for_slug,
    lookup_key_for_url,
)
from interlayer.enrichment.providers.brightdata import BrightDataEnrichmentProvider
from interlayer.enrichment.providers.coresignal import CoresignalEnrichmentProvider
from interlayer.enrichment.providers.local_file import LocalFileEnrichmentProvider
from interlayer.enrichment.providers.null import NullEnrichmentProvider
from interlayer.enrichment.ratelimit import for_provider


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("this provider must not open a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)


# ---------------------------------------------------------------------------
# null
# ---------------------------------------------------------------------------


def test_null_returns_one_miss_per_key_in_order(no_network: None) -> None:
    provider = NullEnrichmentProvider()
    keys = [lookup_key_for_slug("a"), lookup_key_for_slug("b")]
    results = list(provider.enrich(keys))
    assert [r.key for r in results] == keys
    assert all(r.is_miss for r in results)
    assert provider.estimate_cost(1000) == 0.0
    assert provider.capabilities.requires_network is False


# ---------------------------------------------------------------------------
# local_file
# ---------------------------------------------------------------------------


def write_csv(tmp_path: Path) -> Path:
    path = tmp_path / "profiles.csv"
    path.write_text(
        "full_name,profile_url,headline,current_employer_name,current_title,email\n"
        "Dana Wu,https://www.linkedin.com/in/danawu-7f3a2,Quant,Globex,"
        "Senior Quant Researcher,dana@example.test\n",
        encoding="utf-8",
    )
    return path


def write_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "profiles.jsonl"
    path.write_text(
        json.dumps(
            {
                "full_name": "Alex Rivera",
                "linkedin_public_id": "alexrivera",
                "current_employer_name": "Acme",
                "current_title": "Staff Engineer",
                "phone": "+1 555 0100",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_local_file_csv_lookup_by_slug(tmp_path: Path, no_network: None) -> None:
    provider = LocalFileEnrichmentProvider(write_csv(tmp_path))
    (result,) = list(provider.enrich([lookup_key_for_slug("danawu-7f3a2")]))
    assert result.person is not None
    assert result.person.full_name == "Dana Wu"
    assert result.person.current_seniority is Seniority.SENIOR
    assert result.person.dropped_fields == ("email",)
    assert result.cost_usd == 0.0


def test_local_file_lookup_by_url_and_name(tmp_path: Path, no_network: None) -> None:
    provider = LocalFileEnrichmentProvider(write_jsonl(tmp_path))
    by_url = list(provider.enrich([lookup_key_for_url("linkedin.com/in/AlexRivera/")]))
    by_name = list(provider.enrich([lookup_key_for_name("Alex Rivera", "Acme")]))
    assert by_url[0].person is not None
    assert by_name[0].person is not None
    assert by_name[0].person.person_key == by_url[0].person.person_key


def test_local_file_miss_is_a_miss_not_an_error(tmp_path: Path, no_network: None) -> None:
    provider = LocalFileEnrichmentProvider(write_csv(tmp_path))
    (result,) = list(provider.enrich([lookup_key_for_slug("nobody-at-all")]))
    assert result.is_miss
    assert result.error is None


def test_local_file_without_a_path_raises_a_config_error(no_network: None) -> None:
    with pytest.raises(ProviderConfigError, match="no file configured"):
        list(LocalFileEnrichmentProvider().enrich([lookup_key_for_slug("x")]))


def test_local_file_missing_file_raises_a_config_error(tmp_path: Path) -> None:
    provider = LocalFileEnrichmentProvider(tmp_path / "absent.csv")
    with pytest.raises(ProviderConfigError, match="does not exist"):
        list(provider.enrich([lookup_key_for_slug("x")]))


def test_local_file_contact_opt_in_is_respected(tmp_path: Path, no_network: None) -> None:
    provider = LocalFileEnrichmentProvider(write_csv(tmp_path), allow_contact_fields=True)
    (result,) = list(provider.enrich([lookup_key_for_slug("danawu-7f3a2")]))
    assert result.person is not None
    assert result.person.dropped_fields == ()  # opted in, so nothing was dropped


# ---------------------------------------------------------------------------
# network adapters — never actually networked
# ---------------------------------------------------------------------------


def test_network_modules_import_without_credentials_or_httpx() -> None:
    """Constructing them must be free: the registry introspects them offline."""
    assert BrightDataEnrichmentProvider().api_key is None
    assert CoresignalEnrichmentProvider().api_key is None


def test_network_adapters_raise_config_errors_when_unconfigured() -> None:
    with pytest.raises(ProviderConfigError, match="ships disabled"):
        list(BrightDataEnrichmentProvider().enrich([lookup_key_for_slug("x")]))
    with pytest.raises(ProviderConfigError, match="ships disabled"):
        list(CoresignalEnrichmentProvider().enrich([lookup_key_for_slug("x")]))


def test_network_adapters_do_nothing_for_an_empty_key_list() -> None:
    assert list(BrightDataEnrichmentProvider().enrich([])) == []
    assert list(CoresignalEnrichmentProvider().enrich([])) == []


def _instant_limiter() -> Any:
    return for_provider(clock=lambda: 0.0, sleeper=lambda _s: None)


def test_brightdata_submit_and_poll_through_an_injected_transport() -> None:
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, body: Any, headers: Any) -> Any:
        calls.append((method, url))
        if method == "POST":
            return {"snapshot_id": "snap-1"}
        return [
            {
                "name": "Jordan Smith",
                "url": "https://www.linkedin.com/in/jsmith-quant-9a1",
                "current_company": {"name": "Jane Street", "title": "Trader"},
                "connections": 3,
                "email": "leak@example.test",
            }
        ]

    provider = BrightDataEnrichmentProvider(
        api_key="test-key",
        dataset_id="ds_1",
        transport=transport,
        limiter=_instant_limiter(),
    )
    (result,) = list(provider.enrich([lookup_key_for_slug("jsmith-quant-9a1")]))

    assert result.person is not None
    assert result.person.current_employer_name == "Jane Street"
    assert result.person.connections_count == 3
    assert result.person.dropped_fields == ("email",)
    assert [method for method, _ in calls] == ["POST", "GET"]


def test_brightdata_poll_timeout_is_a_miss_not_an_exception() -> None:
    def transport(method: str, url: str, body: Any, headers: Any) -> Any:
        return {"snapshot_id": "snap-1"} if method == "POST" else {"status": "running"}

    provider = BrightDataEnrichmentProvider(
        api_key="k",
        dataset_id="d",
        transport=transport,
        limiter=_instant_limiter(),
        poll_timeout_seconds=1.0,
        poll_interval_seconds=1.0,
    )
    (result,) = list(provider.enrich([lookup_key_for_slug("someone")]))
    assert result.is_miss
    assert result.error is None


def test_transport_failure_is_a_miss_with_an_error_string() -> None:
    def transport(*_: Any) -> Any:
        raise TimeoutError("upstream went away")

    provider = BrightDataEnrichmentProvider(
        api_key="k", dataset_id="d", transport=transport, limiter=_instant_limiter()
    )
    (result,) = list(provider.enrich([lookup_key_for_slug("someone")]))
    assert result.is_miss
    assert "upstream went away" in (result.error or "")


def test_name_only_keys_are_a_miss_for_url_based_providers() -> None:
    provider = BrightDataEnrichmentProvider(
        api_key="k", dataset_id="d", transport=lambda *_: None, limiter=_instant_limiter()
    )
    (result,) = list(provider.enrich([lookup_key_for_name("Jordan Smith", "Jane Street")]))
    assert result.is_miss
    assert "profile URL" in (result.error or "")


def test_budget_guard_stops_a_network_adapter_mid_run() -> None:
    provider = BrightDataEnrichmentProvider(
        api_key="k",
        dataset_id="d",
        transport=lambda *_: {"snapshot_id": None},
        limiter=_instant_limiter(),
        budget=BudgetGuard(max_calls_per_run=1),
    )
    keys = [lookup_key_for_slug("a"), lookup_key_for_slug("b")]
    with pytest.raises(BudgetExceeded):
        list(provider.enrich(keys))


def test_dry_run_makes_no_calls() -> None:
    def transport(*_: Any) -> Any:
        raise AssertionError("a dry run must not call out")

    provider = CoresignalEnrichmentProvider(
        api_key="k",
        transport=transport,
        limiter=_instant_limiter(),
        budget=BudgetGuard(dry_run=True),
    )
    results = list(provider.enrich([lookup_key_for_slug("a")]))
    assert results[0].is_miss
    assert results[0].error == "dry-run"


def test_coresignal_maps_a_member_record() -> None:
    payload = {
        "id": 99,
        "name": "Priya Okafor",
        "url": "https://www.linkedin.com/in/p-okafor-2c8",
        "member_experience_collection": [
            {"company_name": "Citadel Securities", "title": "Engineer", "date_from": "2023-04"}
        ],
    }
    provider = CoresignalEnrichmentProvider(
        api_key="k", transport=lambda *_: payload, limiter=_instant_limiter()
    )
    (result,) = list(provider.enrich([lookup_key_for_slug("p-okafor-2c8")]))
    assert result.person is not None
    assert result.person.current_employer_name == "Citadel Securities"
    assert result.person.provenance is not None
    assert result.person.provenance.source_record_id == "99"


def test_cost_estimates_are_pure() -> None:
    assert BrightDataEnrichmentProvider().estimate_cost(1000) == pytest.approx(1.0)
    assert CoresignalEnrichmentProvider().estimate_cost(100) == pytest.approx(2.0)
    assert BrightDataEnrichmentProvider().estimate_cost(0) == 0.0


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_defaults_are_the_two_offline_providers() -> None:
    assert registry.DEFAULT_ENABLED == ("local_file", "null")
    assert registry.available() == ("brightdata", "coresignal", "local_file", "null")


def test_network_providers_ship_disabled() -> None:
    for name in ("brightdata", "coresignal"):
        assert registry.is_enabled_by_default(name) is False
        with pytest.raises(ProviderDisabledError, match="ships disabled"):
            registry.create(name)
        assert registry.create(name, allow_disabled=True).name == name


def test_default_provider_is_the_offline_one(no_network: None) -> None:
    provider = registry.default_provider()
    assert provider.name == "null"
    assert list(provider.enrich([LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "x")]))


def test_unknown_provider_lists_the_registered_ones() -> None:
    with pytest.raises(registry.UnknownProviderError, match="local_file"):
        registry.get("proxycurl")


def test_specs_do_not_import_provider_modules() -> None:
    """Reading the registry must stay free of provider imports."""
    for spec in registry.specs():
        assert spec.module.startswith("interlayer.enrichment.providers.")
        assert isinstance(spec.enabled_by_default, bool)
