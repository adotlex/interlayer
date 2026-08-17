"""Acquisition boundary — the only layer permitted to read outside data.

Everything here reads files. Nothing here opens a socket, and nothing here
accepts, reads, stores or transmits a session credential of any kind. That is an
architectural invariant enforced by tests, not a policy note.

The one declarative table of externally-owned shapes lives in
:mod:`interlayer.collect.schemas`. No parser in this package — or in
:mod:`interlayer.enrichment` — may write a LinkedIn URL, query parameter or JSON
path as a bare literal anywhere else.
"""

from interlayer.collect.interface import (
    CaptureStatus,
    Code,
    CollectionResult,
    Collector,
    CollectorError,
    CompliancePostureError,
    Degree,
    Diagnostic,
    MutualCapture,
    PersonRef,
    Severity,
    TargetRef,
)

__all__ = [
    "CaptureStatus",
    "Code",
    "CollectionResult",
    "Collector",
    "CollectorError",
    "CompliancePostureError",
    "Degree",
    "Diagnostic",
    "MutualCapture",
    "PersonRef",
    "Severity",
    "TargetRef",
]
