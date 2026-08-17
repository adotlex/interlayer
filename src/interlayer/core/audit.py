"""Append-only JSONL audit log (PRIV-15, PRIV-16).

GDPR Art. 5(2) accountability in one file: the log is the evidence that
minimisation, retention and erasure actually happened rather than merely being
documented.

Two properties are enforced by construction, not by convention:

**No personal data.** A record has exactly five keys — ``ts``, ``action``,
``adapter``, ``entity_or_person_count``, ``config_hash`` — and there is no
free-text field to put a name in. ``action`` and ``adapter`` must match a
conservative slug pattern (no spaces, no ``@``, no punctuation beyond
``._:-``), counts must be integers and ``config_hash`` must be hex. Anything
else raises :class:`ComplianceError` at write time.

**Append-only.** The writer opens with mode ``"a"`` and nothing here truncates,
rewrites or deletes. The single sanctioned exception is ``purge --all``, which
removes the whole state root including this file — and writes its purge line
*before* doing so, so the last thing the log ever says is why it ended.

Standard library only. Nothing in this module opens a socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from interlayer.core.config import AUDIT_FILENAME, Config
from interlayer.core.errors import ComplianceError

#: Event vocabulary. Not a closed set — an unlisted action is accepted if it is
#: slug-shaped — but every action this build emits is named here so a test can
#: assert the expected ones are present.
ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "init",
        "config",
        "collect",
        "ingest",
        "match",
        "review",
        "analyse",
        "cluster",
        "export",
        "report",
        "prune",
        "retention_sweep",
        "purge_all",
        "purge_person",
        "purge_target",
    }
)

#: Sentinel for events with no adapter (analysis-side work).
NO_ADAPTER: Final[str] = "none"

#: Deliberately narrow: a human name fails this the moment it contains a space,
#: a capital letter or an apostrophe, and an email fails on the ``@``.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
_HASH_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")

#: The complete record schema. Adding a key here is a privacy decision.
RECORD_KEYS: Final[tuple[str, ...]] = (
    "ts",
    "action",
    "adapter",
    "entity_or_person_count",
    "config_hash",
)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """One line of the log."""

    ts: str
    action: str
    adapter: str
    entity_or_person_count: int
    config_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "action": self.action,
            "adapter": self.adapter,
            "entity_or_person_count": self.entity_or_person_count,
            "config_hash": self.config_hash,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def _validate_token(field: str, value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.match(value):
        raise ComplianceError(
            f"audit {field}={value!r} is not a bare identifier. The audit log carries counts "
            "and identifiers only, never names or free text (PRIV-15)."
        )
    return value


def _validate_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComplianceError(
            f"audit entity_or_person_count must be an int, got {value!r} (PRIV-15)"
        )
    if value < 0:
        raise ComplianceError(f"audit entity_or_person_count must be >= 0, got {value}")
    return value


def _validate_hash(value: str) -> str:
    if value == "":
        return value
    if not isinstance(value, str) or not _HASH_RE.match(value):
        raise ComplianceError(
            f"audit config_hash must be a 64-char sha256 hex digest or empty, got {value!r}"
        )
    return value


class AuditLog:
    """The log at ``<state_root>/audit.log``.

    Cheap to construct and stateless between calls: each append opens, writes
    one line and closes, so two processes appending concurrently interleave
    whole lines rather than corrupting each other.
    """

    def __init__(self, state_root: Path | str) -> None:
        self._root = Path(state_root)
        self._path = self._root / AUDIT_FILENAME

    @classmethod
    def for_config(cls, config: Config) -> AuditLog:
        return cls(config.state_root)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def size(self) -> int:
        """Bytes on disk, 0 when absent. PRIV-16 asserts this never decreases
        except across ``purge --all``."""
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def append(
        self,
        action: str,
        *,
        adapter: str = NO_ADAPTER,
        count: int = 0,
        config_hash: str = "",
        ts: datetime | None = None,
    ) -> AuditRecord:
        """Append one event. Returns the record written.

        ``count`` is the number of entities or persons the event touched — the
        only quantity the log carries about people.
        """
        record = AuditRecord(
            ts=_now_iso(ts),
            action=_validate_token("action", action),
            adapter=_validate_token("adapter", adapter),
            entity_or_person_count=_validate_count(count),
            config_hash=_validate_hash(config_hash),
        )
        self._root.mkdir(parents=True, exist_ok=True)
        # Mode "a" is the whole of PRIV-16. Do not change it, and do not add a
        # code path here that opens this file any other way.
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json() + "\n")
        return record

    def append_for(
        self,
        config: Config,
        action: str,
        *,
        adapter: str = NO_ADAPTER,
        count: int = 0,
        ts: datetime | None = None,
    ) -> AuditRecord:
        """:meth:`append`, filling ``config_hash`` from ``config``."""
        return self.append(
            action, adapter=adapter, count=count, config_hash=config.config_hash, ts=ts
        )

    def read(self) -> list[AuditRecord]:
        """Parse the whole log. Raises :class:`ComplianceError` on a line that
        is not a valid record — a malformed audit trail is not evidence."""
        return list(self.iter_records())

    def iter_records(self) -> Iterator[AuditRecord]:
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                yield _parse_line(stripped, lineno, self._path)

    def actions(self) -> set[str]:
        return {record.action for record in self.iter_records()}


def _parse_line(line: str, lineno: int, path: Path) -> AuditRecord:
    try:
        payload: Any = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ComplianceError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ComplianceError(f"{path}:{lineno} is not a JSON object")
    missing = [key for key in RECORD_KEYS if key not in payload]
    extra = [key for key in payload if key not in RECORD_KEYS]
    if missing or extra:
        raise ComplianceError(
            f"{path}:{lineno} has the wrong keys (missing={missing!r}, unexpected={extra!r}); "
            f"the audit schema is exactly {list(RECORD_KEYS)!r}"
        )
    return AuditRecord(
        ts=str(payload["ts"]),
        action=str(payload["action"]),
        adapter=str(payload["adapter"]),
        entity_or_person_count=int(payload["entity_or_person_count"]),
        config_hash=str(payload["config_hash"]),
    )


def _now_iso(ts: datetime | None = None) -> str:
    moment = ts if ts is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def audit(
    state_root: Path | str,
    action: str,
    *,
    adapter: str = NO_ADAPTER,
    count: int = 0,
    config_hash: str = "",
) -> AuditRecord:
    """Module-level convenience for callers that hold a root, not a log."""
    return AuditLog(state_root).append(
        action, adapter=adapter, count=count, config_hash=config_hash
    )


__all__ = [
    "ACTIONS",
    "NO_ADAPTER",
    "RECORD_KEYS",
    "AuditLog",
    "AuditRecord",
    "audit",
]
