"""Exception hierarchy shared by every stage.

Stages raise these rather than bare exceptions so the CLI can map failures to
stable exit codes without inspecting messages.
"""

from __future__ import annotations

__all__ = [
    "ConfigError",
    "EvidenceError",
    "GraphError",
    "IngestError",
    "InterlayerError",
    "NetworkEgressError",
    "RedactionLeakError",
    "ResolutionError",
    "StageInputMissingError",
]


class InterlayerError(Exception):
    """Base for every error this package raises deliberately."""


class ConfigError(InterlayerError):
    """Configuration is missing, malformed, or internally inconsistent."""


class IngestError(InterlayerError):
    """An input file could not be parsed as the format it claims to be."""


class StageInputMissingError(InterlayerError):
    """A stage was run before the stage that produces its input."""

    def __init__(self, path: str, produced_by: str) -> None:
        super().__init__(f"missing input {path!r}; run `interlayer {produced_by}` first")
        self.path = path
        self.produced_by = produced_by


class ResolutionError(InterlayerError):
    """Entity resolution could not proceed (e.g. an unloadable gazetteer)."""


class GraphError(InterlayerError):
    """The graph is malformed or an algorithm was given an unusable input."""


class EvidenceError(InterlayerError):
    """A scored result was assembled without the provenance required to justify it."""


class RedactionLeakError(InterlayerError):
    """Redacted output was found to contain identifying data.

    Raised before anything is written: a redaction that leaks once has already
    failed, so the write must not happen at all.
    """


class NetworkEgressError(InterlayerError):
    """The core pipeline attempted network access. It must never do this."""
