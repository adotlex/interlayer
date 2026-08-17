"""Ingest layer: CSV parsing, the target registry, and entity resolution.

Pure and offline. Nothing here may import ``httpx``, ``requests``,
``urllib.request`` or ``socket`` (boundary 1, PRIV-01/02).

This module deliberately exports **only the error hierarchy**. Import the
working types from their own modules::

    from interlayer.ingest.connections_csv import parse_connections_csv
    from interlayer.ingest.targets import load_registry
    from interlayer.ingest.company_match import CompanyMatcher
    from interlayer.ingest.person_match import PersonResolver
    from interlayer.ingest.review import ReviewQueue

Keeping ``__init__`` free of submodule imports is what lets every submodule
import :class:`IngestError` from the package without an import cycle.

The two roots come from ``core.errors``, which reserves ``IngestError`` for this
package and ``ComplianceError`` for a stated privacy control. The subclasses
below only narrow them, so ``except InterlayerError`` in the CLI catches
everything ingest raises.
"""

from __future__ import annotations

from interlayer.core.errors import ComplianceError, IngestError

__all__ = [
    "ComplianceError",
    "ConnectionsCsvError",
    "IngestError",
    "NeedsReviewError",
    "RegistryError",
    "ReviewError",
]


class ConnectionsCsvError(IngestError):
    """``Connections.csv`` could not be parsed — header missing, file empty,
    or the bytes are not decodable by any supported encoding."""


class RegistryError(IngestError):
    """``targets.yaml`` is malformed: wrong schema version, missing key,
    duplicate entity id, uncompilable regex, or an id with no ``Firm``."""


class ReviewError(IngestError):
    """A review-queue operation is not valid — adjudicating an unknown key,
    or adjudicating to a firm the registry does not know."""


class NeedsReviewError(ComplianceError):
    """Raised when a ``needs_review`` record is pushed toward the graph.

    PRIV-20 makes ``needs_review`` a hard barrier rather than a hint: the
    caller must not be able to get such a record into the analysis layer
    without a human adjudication. A ``ComplianceError``, not an
    ``IngestError`` — nothing failed to parse; a control refused.
    """
