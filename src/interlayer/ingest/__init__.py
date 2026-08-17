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

**Note for B1 (``core/errors.py``).** Every exception raised by this package
derives from :class:`IngestError`, which derives from :class:`ValueError`.
``core.errors.IngestError`` did not exist when this package was written, so it
is not imported here. When it lands, either (a) make it an alias of this class,
or (b) make it a ``ValueError`` subclass and re-export ours from it — both keep
``except ValueError`` working for existing callers. Recorded in
``docs/wave2-notes/B2.md``.
"""

from __future__ import annotations


class IngestError(ValueError):
    """Base class for every ingest-layer failure.

    A ``ValueError`` subclass on purpose: the ingest layer's failures are all
    "this input is not what it claims to be", and callers written before
    ``core.errors`` existed can still catch them.
    """


class ConnectionsCsvError(IngestError):
    """``Connections.csv`` could not be parsed — header missing, file empty,
    or the bytes are not decodable by any supported encoding."""


class RegistryError(IngestError):
    """``targets.yaml`` is malformed: wrong schema version, missing key,
    duplicate entity id, uncompilable regex, or an id with no ``Firm``."""


class ReviewError(IngestError):
    """A review-queue operation is not valid — adjudicating an unknown key,
    or adjudicating to a firm the registry does not know."""


class NeedsReviewError(IngestError):
    """Raised when a ``needs_review`` record is pushed toward the graph.

    PRIV-20 makes ``needs_review`` a hard barrier rather than a hint: the
    caller must not be able to get such a record into the analysis layer
    without a human adjudication.
    """


__all__ = [
    "ConnectionsCsvError",
    "IngestError",
    "NeedsReviewError",
    "RegistryError",
    "ReviewError",
]
