"""Compliance guardrails on the enrichment layer.

The central one is :func:`test_no_adapter_claims_connection_edges`. It is not a
style check. Wave 1 established that no commercially available provider sells
LinkedIn connection-graph or mutual-connection data — a member's connection list
is never rendered logged-out, and mutuals are computed per-viewer server-side, so
there is no artefact to scrape and nothing to resell. Vendors sell
``connections_count``: one integer, censored by the source at "500+", therefore
saturated and information-free for exactly the senior population this tool
targets.

If this test ever fails, one of two things has happened: the market changed, or
somebody wired an adapter to a tool that takes the user's session credential and
acts as them. The second is what actually happens in practice, and it is the
reason this file exists.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from interlayer.enrichment import registry
from interlayer.enrichment.interface import LookupKeyType, ProviderCapabilities

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "interlayer"
PROVIDERS_ROOT = PACKAGE_ROOT / "enrichment" / "providers"


def test_no_adapter_claims_connection_edges() -> None:
    checked = 0
    for provider in registry.all_providers():
        assert provider.capabilities.provides_connection_edges is False, (
            f"{provider.name} claims to supply connection edges. No provider sells "
            "the connection graph; verify this against the vendor's contract and a "
            "real payload before shipping it."
        )
        checked += 1
    assert checked == len(registry.available()) == 4


def test_no_adapter_requires_user_credentials() -> None:
    for provider in registry.all_providers():
        assert provider.compliance.requires_user_credentials is False


def test_the_capability_default_is_false() -> None:
    assert ProviderCapabilities().provides_connection_edges is False


def test_every_adapter_declares_a_full_compliance_posture() -> None:
    for provider in registry.all_providers():
        assert provider.compliance.tos_risk is not None
        assert provider.compliance.lawful_basis is not None
        assert provider.compliance.notes.strip(), f"{provider.name} has no posture note"
        assert provider.cost.usd_per_lookup >= 0.0


def test_offline_adapters_declare_no_network_and_keep_the_promise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("an offline provider opened a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    profiles = tmp_path / "profiles.csv"
    profiles.write_text("full_name,linkedin_public_id\nDana Wu,danawu\n", encoding="utf-8")

    from interlayer.enrichment.interface import LookupKey

    key = LookupKey(LookupKeyType.LINKEDIN_PUBLIC_ID, "danawu")
    for name in registry.DEFAULT_ENABLED:
        kwargs = {"path": profiles} if name == "local_file" else {}
        provider = registry.create(name, **kwargs)
        assert provider.capabilities.requires_network is False
        results = list(provider.enrich([key]))
        assert len(results) == 1


def test_no_provider_module_imports_an_http_client_at_module_scope() -> None:
    """Import-time purity is what keeps the offline path free of optional deps."""
    banned = {"httpx", "requests", "urllib", "urllib.request", "http.client", "aiohttp"}
    offenders: list[str] = []
    for path in sorted(PROVIDERS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module scope only — lazy imports live in functions
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            hit = names & banned
            if hit:
                offenders.append(f"{path.name}: {sorted(hit)}")
    assert offenders == []


def test_importing_the_package_imports_no_provider() -> None:
    import subprocess
    import sys

    script = (
        "import sys, interlayer.enrichment, interlayer.enrichment.registry as r;"
        "loaded=[m for m in sys.modules if m.startswith("
        "'interlayer.enrichment.providers.')];"
        "print(loaded)"
    )
    output = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "[]"


def test_registry_never_silently_enables_a_network_adapter() -> None:
    for spec in registry.specs():
        if spec.requires_network:
            assert spec.enabled_by_default is False
