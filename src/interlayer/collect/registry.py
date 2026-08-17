"""Name → collector factory, and the posture gate the CLI enables adapters through.

``load_all`` is the only entry point into acquisition, and it checks the
compliance posture **twice**: once on the collector class and once on every record
that collector produces. A collector that declares one posture and emits another
is precisely the failure this gate exists to catch, and checking only the
declaration would let it through.

Widening the allowlist is an explicit, logged user act (PRIV-04, ruling C5). It is
never inferred from the contents of an input file, because the file is exactly the
thing that might be lying.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from interlayer.collect.interface import (
    Code,
    CollectionResult,
    Collector,
    CollectorError,
    Diagnostic,
    Severity,
    check_posture,
    merge_results,
)
from interlayer.core.models import CompliancePosture

CollectorFactory = Callable[..., Collector]


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    """Registry metadata about one collector, readable without importing it."""

    name: str
    posture: CompliancePosture
    description: str
    factory: CollectorFactory
    extensions: tuple[str, ...] = ()

    def create(self, **kwargs: object) -> Collector:
        return self.factory(**kwargs)


def _manual_csv_factory(**kwargs: object) -> Collector:
    from interlayer.collect.manual_csv import ManualCsvCollector

    return ManualCsvCollector(**kwargs)  # type: ignore[arg-type]


def _har_factory(**kwargs: object) -> Collector:
    from interlayer.collect.har import HarCollector

    return HarCollector(**kwargs)  # type: ignore[arg-type]


_REGISTRY: dict[str, CollectorSpec] = {
    "manual_csv": CollectorSpec(
        name="manual_csv",
        posture=CompliancePosture.MANUAL_CAPTURE,
        description=(
            "Hand-fillable targets.csv / mutuals.csv. Works with nothing but a text "
            "editor; the guaranteed floor."
        ),
        factory=_manual_csv_factory,
        extensions=(".csv",),
    ),
    "har": CollectorSpec(
        name="har",
        posture=CompliancePosture.MANUAL_CAPTURE,
        description=(
            "Offline parser for a HAR the user exported from their own browser "
            "session. Makes no network calls."
        ),
        factory=_har_factory,
        extensions=(".har", ".json"),
    ),
}

#: What the shipped configuration turns on. First-party export is the mandatory
#: backbone (it is where the set of connections comes from) and manual capture
#: covers the two file parsers here, both of which read only files the user
#: already has.
DEFAULT_ALLOWED_POSTURES: frozenset[CompliancePosture] = frozenset(
    {CompliancePosture.FIRST_PARTY_EXPORT, CompliancePosture.MANUAL_CAPTURE}
)

#: Never enabled, by any config key or flag. The enum member exists so the
#: guardrail is testable and so nobody can slip a collector past the gate by
#: inventing a new posture string. No collector in this repository declares it.
NEVER_ALLOWED_POSTURES: frozenset[CompliancePosture] = frozenset(
    {CompliancePosture.AUTOMATED}
)


class UnknownCollectorError(CollectorError):
    """Asked for a collector name that is not registered."""


def register(spec: CollectorSpec) -> None:
    """Add a collector. Refuses a posture that can never be enabled."""
    if spec.posture in NEVER_ALLOWED_POSTURES:
        raise CollectorError(
            f"collector {spec.name!r} declares posture {spec.posture.value!r}, which "
            "this project does not ship and cannot enable"
        )
    _REGISTRY[spec.name] = spec


def available() -> tuple[str, ...]:
    """Every registered collector name, sorted."""
    return tuple(sorted(_REGISTRY))


def specs() -> tuple[CollectorSpec, ...]:
    """Every registered spec, sorted by name."""
    return tuple(_REGISTRY[name] for name in available())


def get(name: str) -> CollectorSpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownCollectorError(
            f"no collector named {name!r}; registered: {', '.join(available())}"
        ) from None


def create(
    name: str,
    *,
    allowed_postures: Iterable[CompliancePosture] = DEFAULT_ALLOWED_POSTURES,
    **kwargs: object,
) -> Collector:
    """Instantiate a collector, refusing one whose posture is not enabled."""
    spec = get(name)
    check_posture(spec.posture, frozenset(allowed_postures), what=f"collector {name!r}")
    return spec.create(**kwargs)


def for_posture(
    allowed: Iterable[CompliancePosture] = DEFAULT_ALLOWED_POSTURES,
) -> tuple[CollectorSpec, ...]:
    """The specs the config permits — the CLI's adapter-enabling filter (PRIV-04)."""
    permitted = frozenset(allowed) - NEVER_ALLOWED_POSTURES
    return tuple(spec for spec in specs() if spec.posture in permitted)


def for_path(
    path: Path,
    *,
    allowed_postures: Iterable[CompliancePosture] = DEFAULT_ALLOWED_POSTURES,
    **kwargs: object,
) -> Collector | None:
    """First enabled collector that accepts ``path``, or ``None``."""
    for spec in for_posture(allowed_postures):
        collector = spec.create(**kwargs)
        if collector.accepts(path):
            return collector
    return None


def load_all(
    paths: Sequence[Path],
    *,
    allowed_postures: Iterable[CompliancePosture] = DEFAULT_ALLOWED_POSTURES,
    **kwargs: object,
) -> CollectionResult:
    """The only entry point into acquisition.

    Enforces the posture allowlist on the collector and again on every record it
    produced, then merges everything into one result. Unrecognised files become
    diagnostics; they never raise, because a directory of mixed inputs is the
    normal case.
    """
    permitted = frozenset(allowed_postures) - NEVER_ALLOWED_POSTURES
    results: list[CollectionResult] = []
    diagnostics: list[Diagnostic] = []

    for path in paths:
        collector = for_path(path, allowed_postures=permitted, **kwargs)
        if collector is None:
            diagnostics.append(
                Diagnostic(
                    code=Code.COLLECTOR_UNKNOWN,
                    message=(
                        f"no enabled collector recognised {path.name}. Enabled: "
                        + ", ".join(spec.name for spec in for_posture(permitted))
                    ),
                    severity=Severity.ERROR,
                    location=str(path),
                    remedy="check the file's header row, or enable another posture",
                )
            )
            continue

        check_posture(
            collector.compliance_posture, permitted, what=f"collector {collector.name!r}"
        )
        result = collector.collect(path)
        for capture in result.captures:
            check_posture(
                capture.posture,
                permitted,
                what=f"a record from {collector.name!r} in {path.name}",
            )
        results.append(result)

    base = CollectionResult(diagnostics=tuple(diagnostics))
    return merge_results([base, *results], collector="load_all")


__all__ = [
    "DEFAULT_ALLOWED_POSTURES",
    "NEVER_ALLOWED_POSTURES",
    "CollectorSpec",
    "UnknownCollectorError",
    "available",
    "create",
    "for_path",
    "for_posture",
    "get",
    "load_all",
    "register",
    "specs",
]
