"""Exception hierarchy for interlayer.

One root — :class:`InterlayerError` — so a caller (the CLI, chiefly) can catch
everything this package raises without also swallowing ``KeyboardInterrupt`` or a
genuine bug in the standard library.

The whole hierarchy lives here rather than beside the code that raises it,
because several packages raise errors defined for another layer's benefit:
``collect/`` raises :class:`ComplianceError` when an adapter's posture is not
enabled, ``ingest/`` raises :class:`StoreError` indirectly through the store.
Keeping them in one module means no work package has to import another's
implementation just to catch its errors.

Standard library only, by design: every layer imports this.
"""

from __future__ import annotations


class InterlayerError(Exception):
    """Base class for every error raised deliberately by interlayer."""


class ConfigError(InterlayerError):
    """Configuration is missing, malformed, or semantically impossible.

    Raised by :mod:`interlayer.core.config` for unreadable files, unknown keys,
    wrong types, negative retention, unknown adapter postures and any other
    setting whose value cannot be honoured.
    """


class StoreError(InterlayerError):
    """Persistence refused an operation.

    Raised for a write that names a field outside the PRIV-08 allowlist, a
    record without provenance (PRIV-10), a path that would escape the state root
    (PRIV-03), or an underlying SQLite failure.
    """


class RetentionError(InterlayerError):
    """The retention sweep could not be completed.

    A failed sweep is not a warning: PRIV-11 requires expiry to run *before* any
    other work, so a caller that swallows this is retaining data it promised to
    delete.
    """


class ComplianceError(InterlayerError):
    """An operation would breach a stated privacy control.

    Raised when an audit line would carry a field outside the PRIV-15 schema,
    when an adapter whose posture is not in ``config.enabled_adapters`` is asked
    to run (PRIV-04), or when a record classified ``needs_review`` is pushed
    toward the graph (PRIV-20).
    """


class IngestError(InterlayerError):
    """Input could not be parsed into the shared record contract.

    Owned by ``interlayer.ingest``; defined here so other layers can catch it.
    """


class CollectError(InterlayerError):
    """An acquisition adapter could not produce records.

    Owned by ``interlayer.collect``; defined here so other layers can catch it.
    """


__all__ = [
    "CollectError",
    "ComplianceError",
    "ConfigError",
    "IngestError",
    "InterlayerError",
    "RetentionError",
    "StoreError",
]
