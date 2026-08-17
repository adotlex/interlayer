"""Name → provider factory, resolved lazily.

No provider module is imported when this module is imported. A missing HTTP
library, an absent API key or a blocked network therefore cannot break
``import interlayer`` — and the offline path never pays for machinery it does not
use.

Two adapters are enabled by default (``null``, ``local_file``) and both are
offline. Bright Data and Coresignal are present and disabled: ruling C5 keeps them
available for a user who chooses one, while PRIV-04 keeps the shipped default to
first-party sources. :func:`create` refuses a disabled provider unless the caller
passes ``allow_disabled=True``, which is what makes enabling one an explicit act
rather than a side effect.

:func:`all_providers` deliberately bypasses that gate. It exists so the compliance
tests can instantiate every shipped adapter — including the disabled ones — and
assert that none of them claims to sell connection edges.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib import import_module
from typing import Any

from interlayer.enrichment.interface import (
    EnrichmentProvider,
    ProviderDisabledError,
    ProviderError,
)

ProviderFactory = Callable[..., EnrichmentProvider]


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """Registry metadata. Reading this does not import the provider."""

    name: str
    module: str
    enabled_by_default: bool
    description: str
    requires_network: bool

    def load(self) -> ProviderFactory:
        module = import_module(self.module)
        factory: ProviderFactory = module.factory
        return factory


_REGISTRY: dict[str, ProviderSpec] = {
    "null": ProviderSpec(
        name="null",
        module="interlayer.enrichment.providers.null",
        enabled_by_default=True,
        description="Returns nothing. Offline, free, and the shipped default.",
        requires_network=False,
    ),
    "local_file": ProviderSpec(
        name="local_file",
        module="interlayer.enrichment.providers.local_file",
        enabled_by_default=True,
        description="Reads a CSV/JSONL of profiles the user supplied. Offline.",
        requires_network=False,
    ),
    "brightdata": ProviderSpec(
        name="brightdata",
        module="interlayer.enrichment.providers.brightdata",
        enabled_by_default=False,
        description=(
            "Bright Data dataset API. Present but DISABLED: enabling a network "
            "adapter is an explicit, logged user act (ruling C5)."
        ),
        requires_network=True,
    ),
    "coresignal": ProviderSpec(
        name="coresignal",
        module="interlayer.enrichment.providers.coresignal",
        enabled_by_default=False,
        description=(
            "Coresignal member API. Present but DISABLED: enabling a network "
            "adapter is an explicit, logged user act (ruling C5)."
        ),
        requires_network=True,
    ),
}

#: What the shipped configuration turns on. Both are offline.
DEFAULT_ENABLED: tuple[str, ...] = tuple(
    sorted(name for name, spec in _REGISTRY.items() if spec.enabled_by_default)
)


class UnknownProviderError(ProviderError):
    """Asked for a provider name that is not registered."""


def available() -> tuple[str, ...]:
    """Every registered provider name, sorted."""
    return tuple(sorted(_REGISTRY))


def specs() -> tuple[ProviderSpec, ...]:
    """Every registered spec, sorted by name. Imports nothing."""
    return tuple(_REGISTRY[name] for name in available())


def get(name: str) -> ProviderSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(
            f"no enrichment provider named {name!r}; registered: {', '.join(available())}"
        ) from None


def is_enabled_by_default(name: str) -> bool:
    return get(name).enabled_by_default


def create(
    name: str, *, allow_disabled: bool = False, **kwargs: Any
) -> EnrichmentProvider:
    """Instantiate a provider by name.

    A provider that ships disabled requires ``allow_disabled=True``, which the CLI
    only passes when the user has explicitly enabled it in config.
    """
    spec = get(name)
    if not spec.enabled_by_default and not allow_disabled:
        raise ProviderDisabledError(
            f"provider {name!r} ships disabled: {spec.description} Enable it in "
            "config, which records the choice, rather than selecting it implicitly."
        )
    return spec.load()(**kwargs)


def default_provider(**kwargs: Any) -> EnrichmentProvider:
    """The offline default. The pipeline must run to completion with this."""
    return create("null", **kwargs)


def all_providers(**kwargs: Any) -> Iterator[EnrichmentProvider]:
    """Instantiate every shipped adapter, disabled ones included.

    For introspection and the compliance guardrail tests only. Constructing an
    adapter performs no I/O and needs no credentials; that is a requirement on
    adapters, and iterating them here is what proves it.
    """
    for spec in specs():
        yield spec.load()(**kwargs)


def all_factories() -> Iterator[ProviderFactory]:
    """Every provider factory, disabled ones included."""
    for spec in specs():
        yield spec.load()


def register(spec: ProviderSpec) -> None:
    """Add a provider spec. Used by tests and by out-of-tree extensions."""
    _REGISTRY[spec.name] = spec


__all__ = [
    "DEFAULT_ENABLED",
    "ProviderSpec",
    "UnknownProviderError",
    "all_factories",
    "all_providers",
    "available",
    "create",
    "default_provider",
    "get",
    "is_enabled_by_default",
    "register",
    "specs",
]
