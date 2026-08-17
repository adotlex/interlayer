"""Storage limitation, enforced on every startup (PRIV-11).

GDPR Art. 5(1)(e) says personal data is kept "no longer than is necessary".
A documented retention period that nothing enforces is not storage limitation,
so the sweep runs *before* any other work: the CLI's first act, ahead of ingest,
analysis, reporting or purge.

The default is 90 days. That is short enough to be defensible for data about
people who never agreed to be in the store, and long enough that a monthly
harvest — the quota ceiling R4 measured is 150-250 targets/month — accumulates
across three cycles before the oldest rows expire.

Tombstones are deliberately exempt from the sweep. They record that an erasure
happened and hold only a SHA-256 of the key, so expiring them would quietly
re-enable resurrection of someone who asked to be forgotten.

Standard library only. Nothing in this module opens a socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from interlayer.core.config import Config
from interlayer.core.errors import RetentionError, StoreError
from interlayer.core.store import Store


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one retention sweep deleted."""

    cutoff: datetime
    retention_days: int
    deleted: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.deleted.values())

    def __bool__(self) -> bool:
        return self.total > 0


def retention_cutoff(config: Config, *, now: datetime | None = None) -> datetime:
    """The oldest ``collected_at`` that survives this sweep."""
    if config.retention_days < 0:
        raise RetentionError(
            f"retention_days must be >= 0, got {config.retention_days}; refusing to sweep with "
            "an impossible retention period"
        )
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC) - timedelta(days=config.retention_days)


def sweep(
    store: Store, config: Config | None = None, *, now: datetime | None = None
) -> SweepResult:
    """Delete every record older than ``config.retention_days``.

    Call this before anything else touches the store. Returns the per-table
    counts and writes one ``retention_sweep`` audit line, so the log is evidence
    that expiry ran on this startup and not merely that it was configured.
    """
    settings = config if config is not None else store.config
    cutoff = retention_cutoff(settings, now=now)
    try:
        deleted = store.delete_older_than(cutoff)
    except StoreError as exc:
        raise RetentionError(f"retention sweep failed: {exc}") from exc
    result = SweepResult(cutoff=cutoff, retention_days=settings.retention_days, deleted=deleted)
    store.audit.append_for(settings, "retention_sweep", count=result.total)
    return result


def startup(config: Config) -> tuple[Store, SweepResult]:
    """Open the store and sweep it, in that order and with nothing between.

    The single entry point every command should use: it makes "the sweep runs
    first" a property of the code rather than a rule each command has to
    remember.
    """
    store = Store(config)
    try:
        result = sweep(store, config)
    except Exception:
        store.close()
        raise
    return store, result


__all__ = ["SweepResult", "retention_cutoff", "startup", "sweep"]
