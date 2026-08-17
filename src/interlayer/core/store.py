"""SQLite persistence, entirely under the state root.

Design constraints, each one a privacy control rather than a preference:

* **One root.** The database, the audit log and every derived artefact live
  under ``config.state_root``. :meth:`Store.path_for` is the only sanctioned way
  to name a file, and it refuses anything that escapes the root (PRIV-03).
* **An allowlist, not a schema.** The persisted columns of ``members``,
  ``targets`` and ``edges`` are generated from
  :data:`interlayer.core.models.ALL_FIELD_ALLOWLISTS` and checked against it at
  open time. Writing a field that is not on the list raises. Adding a column
  therefore requires editing the frozen contract, which is exactly the review
  gate PRIV-08 asks for.
* **Provenance is mandatory.** No row is written without ``source`` and
  ``collected_at`` (PRIV-10); a record with no provenance is refused rather
  than defaulted, because a guessed timestamp defeats the retention sweep.
* **Emails are not kept by default.** ``retain_emails=False`` writes NULL even
  when the in-memory ``Member`` carries an address (PRIV-06).
* **Non-bridges do not survive the run.** :meth:`Store.prune_non_bridges`
  deletes every person with no edge and every target nobody bridges to
  (PRIV-07).
* **Erasure sticks.** Purging a person writes a tombstone — a salt-free SHA-256
  of the key, so the tombstone table itself holds no readable identity — and
  every later write consults it, so re-ingesting the same ``Connections.csv``
  does not resurrect them (PRIV-13).

Standard library only. Nothing in this module opens a socket.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from interlayer.core.audit import AuditLog
from interlayer.core.config import DERIVED_DIRNAMES, Config
from interlayer.core.errors import StoreError
from interlayer.core.ids import normalize_slug
from interlayer.core.models import (
    ALL_FIELD_ALLOWLISTS,
    CompanyMatch,
    CompliancePosture,
    Edge,
    EdgeOrigin,
    Firm,
    MatchStatus,
    Member,
    Provenance,
    Target,
)

_ColumnSpec = tuple[str, str]

# Column order is fixed here; the *set* is verified against the frozen
# allowlist in ``_validate_schema``. Both halves matter: order keeps the INSERT
# statements stable, the set keeps PRIV-08 honest.
_MEMBER_COLUMNS: Final[tuple[_ColumnSpec, ...]] = (
    ("member_id", "TEXT PRIMARY KEY"),
    ("first_name", "TEXT NOT NULL DEFAULT ''"),
    ("last_name", "TEXT NOT NULL DEFAULT ''"),
    ("linkedin_slug", "TEXT"),
    ("company_raw", "TEXT"),
    ("position", "TEXT"),
    ("connected_on", "TEXT"),
    ("email", "TEXT"),
    ("source", "TEXT NOT NULL"),
    ("posture", "TEXT NOT NULL DEFAULT 'first_party_export'"),
    ("collected_at", "TEXT NOT NULL"),
)

_TARGET_COLUMNS: Final[tuple[_ColumnSpec, ...]] = (
    ("target_id", "TEXT PRIMARY KEY"),
    ("firm", "TEXT NOT NULL"),
    ("first_name", "TEXT NOT NULL DEFAULT ''"),
    ("last_name", "TEXT NOT NULL DEFAULT ''"),
    ("linkedin_slug", "TEXT"),
    ("title", "TEXT"),
    ("team", "TEXT"),
    ("harvested", "INTEGER NOT NULL DEFAULT 0"),
    ("source", "TEXT NOT NULL"),
    ("posture", "TEXT NOT NULL DEFAULT 'first_party_export'"),
    ("collected_at", "TEXT NOT NULL"),
)

_EDGE_COLUMNS: Final[tuple[_ColumnSpec, ...]] = (
    ("member_id", "TEXT NOT NULL"),
    ("target_id", "TEXT NOT NULL"),
    ("origin", "TEXT NOT NULL"),
    ("confidence", "REAL NOT NULL DEFAULT 1.0"),
    ("evidence", "TEXT"),
    ("source", "TEXT NOT NULL"),
    ("posture", "TEXT NOT NULL DEFAULT 'first_party_export'"),
    ("collected_at", "TEXT NOT NULL"),
    ("truncated", "INTEGER NOT NULL DEFAULT 0"),
)

#: kind -> (table name, columns, primary key columns)
_TABLES: Final[dict[str, tuple[str, tuple[_ColumnSpec, ...], tuple[str, ...]]]] = {
    "member": ("members", _MEMBER_COLUMNS, ("member_id",)),
    "target": ("targets", _TARGET_COLUMNS, ("target_id",)),
    "edge": ("edges", _EDGE_COLUMNS, ("member_id", "target_id")),
}

#: Tables holding no person fields. They still carry ``source`` and
#: ``collected_at`` so a PRIV-10 sweep over every table finds them.
_REVIEW_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS company_review (
    raw           TEXT PRIMARY KEY,
    normalised    TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    firm          TEXT,
    rule          TEXT NOT NULL DEFAULT '',
    score         REAL,
    member_count  INTEGER NOT NULL DEFAULT 0,
    decided       INTEGER NOT NULL DEFAULT 0,
    decided_firm  TEXT,
    decided_at    TEXT,
    source        TEXT NOT NULL,
    collected_at  TEXT NOT NULL
)
"""

_TOMBSTONE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS tombstones (
    key_hash      TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,
    collected_at  TEXT NOT NULL
)
"""

#: Registry entity ids (R5) that do not spell their firm the way ``Firm`` does.
_FIRM_ALIASES: Final[dict[str, str]] = {
    "citadel_llc": Firm.CITADEL.value,
    "citadel-llc": Firm.CITADEL.value,
    "citadel-securities": Firm.CITADEL_SECURITIES.value,
    "jane-street": Firm.JANE_STREET.value,
    "jane_street_capital": Firm.JANE_STREET.value,
}


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Outcome of a bulk write."""

    written: int = 0
    suppressed: int = 0
    """Rows refused because the subject is tombstoned (PRIV-13)."""

    @property
    def total(self) -> int:
        return self.written + self.suppressed


@dataclass(frozen=True, slots=True)
class PruneResult:
    """Outcome of :meth:`Store.prune_non_bridges` (PRIV-07)."""

    members_deleted: int = 0
    targets_deleted: int = 0

    @property
    def total(self) -> int:
        return self.members_deleted + self.targets_deleted


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Outcome of a person or target purge (PRIV-13, PRIV-14)."""

    members_deleted: int = 0
    targets_deleted: int = 0
    edges_deleted: int = 0
    tombstones_written: int = 0

    @property
    def total(self) -> int:
        return self.members_deleted + self.targets_deleted + self.edges_deleted


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One queued company-string adjudication (PRIV-20)."""

    raw: str
    normalised: str
    status: MatchStatus
    firm: Firm | None
    rule: str
    score: float | None
    member_count: int
    decided: bool
    decided_firm: Firm | None
    source: str
    collected_at: datetime


class Store:
    """The on-disk state for one ``state_root``.

    Open it, use it, close it — or use it as a context manager. Opening creates
    the root (mode 0700) and the database if they do not exist.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._root = config.state_root
        self._closed = False
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit = AuditLog(self._root)
        self._conn = sqlite3.connect(str(config.db_path))
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
        self._validate_schema()

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._conn.close()
            self._closed = True

    @property
    def state_root(self) -> Path:
        return self._root

    @property
    def db_path(self) -> Path:
        return self.config.db_path

    def _require_open(self) -> sqlite3.Connection:
        if self._closed:
            raise StoreError("store is closed")
        return self._conn

    # -- paths (PRIV-03) -------------------------------------------------

    def path_for(self, *parts: str) -> Path:
        """A path under the state root, creating parent directories.

        Refuses anything that would land outside the root — an absolute path,
        or one that climbs out with ``..``. This is the only path helper other
        layers should use for persistent state.
        """
        if not parts:
            raise StoreError("path_for() needs at least one path component")
        candidate = self._root.joinpath(*parts)
        normalised = Path(os.path.normpath(candidate))
        root = Path(os.path.normpath(self._root))
        if normalised != root and root not in normalised.parents:
            raise StoreError(
                f"refusing to write outside the state root: {candidate} is not under {root} "
                "(PRIV-03)"
            )
        normalised.parent.mkdir(parents=True, exist_ok=True)
        return normalised

    # -- schema ----------------------------------------------------------

    def _create_schema(self) -> None:
        conn = self._require_open()
        for table, columns, primary_key in _TABLES.values():
            body = ",\n    ".join(f"{name} {decl}" for name, decl in columns)
            if len(primary_key) > 1:
                body += ",\n    PRIMARY KEY ({})".format(", ".join(primary_key))
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n    {body}\n)")
        conn.execute(_REVIEW_DDL)
        conn.execute(_TOMBSTONE_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_member ON edges(member_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_members_slug ON members(linkedin_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_targets_firm ON targets(firm)")
        conn.commit()

    def _validate_schema(self) -> None:
        """PRIV-08, enforced at runtime rather than only in a test.

        Compares the columns actually present in the database against the frozen
        allowlist. Catches both a code change here and an older database file
        whose shape predates the current contract.
        """
        for kind, (table, columns, _) in _TABLES.items():
            allowed = ALL_FIELD_ALLOWLISTS[kind]
            declared = {name for name, _ in columns}
            if declared != set(allowed):
                raise StoreError(
                    f"{table} declares {sorted(declared)!r} but the {kind} allowlist is "
                    f"{sorted(allowed)!r}; a new persisted field must be reviewed against "
                    "GDPR Art. 9 first (PRIV-08)"
                )
            actual = self._columns_of(table)
            if actual != set(allowed):
                raise StoreError(
                    f"database table {table} has columns {sorted(actual)!r}, expected "
                    f"{sorted(allowed)!r} (PRIV-08). Purge the state root to rebuild it."
                )

    def _columns_of(self, table: str) -> set[str]:
        conn = self._require_open()
        with closing(conn.execute(f"PRAGMA table_info({table})")) as cursor:
            return {str(row["name"]) for row in cursor.fetchall()}

    def schema_columns(self) -> dict[str, frozenset[str]]:
        """Introspected column names per record kind, for PRIV-08 assertions."""
        return {kind: frozenset(self._columns_of(table)) for kind, (table, _, _) in _TABLES.items()}

    # -- generic row writing --------------------------------------------

    def put_row(self, kind: str, row: dict[str, Any]) -> None:
        """Insert-or-replace one row, enforcing the allowlist.

        The single choke point every typed writer funnels through, so PRIV-08
        and PRIV-10 cannot be bypassed by adding a new convenience method.
        """
        if kind not in _TABLES:
            raise StoreError(f"unknown record kind {kind!r}; known kinds are {sorted(_TABLES)!r}")
        table, columns, _ = _TABLES[kind]
        allowed = ALL_FIELD_ALLOWLISTS[kind]
        unlisted = sorted(set(row) - set(allowed))
        if unlisted:
            raise StoreError(
                f"refusing to persist field(s) {unlisted!r} on a {kind}: not in the "
                f"allowlist {sorted(allowed)!r} (PRIV-08)"
            )
        for required in ("source", "collected_at"):
            value = row.get(required)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise StoreError(
                    f"{kind} row is missing {required}; every persisted record carries "
                    "provenance (PRIV-10)"
                )
        names = [name for name, _ in columns]
        values = [row.get(name) for name in names]
        placeholders = ", ".join("?" for _ in names)
        conn = self._require_open()
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
            values,
        )

    # -- members ---------------------------------------------------------

    def add_member(self, member: Member, *, provenance: Provenance | None = None) -> bool:
        """Persist one connection. False when a tombstone suppresses it."""
        prov = _provenance_for("member", member.member_id, provenance or member.provenance)
        slug = normalize_slug(member.linkedin_slug)
        if self.is_tombstoned(member.member_id) or (slug and self.is_tombstoned(f"slug:{slug}")):
            return False
        self.put_row(
            "member",
            {
                "member_id": member.member_id,
                "first_name": member.first_name,
                "last_name": member.last_name,
                "linkedin_slug": slug,
                "company_raw": member.company_raw,
                "position": member.position,
                "connected_on": member.connected_on.isoformat() if member.connected_on else None,
                # PRIV-06: the column exists, but it stays NULL unless the user
                # deliberately opted in. The in-memory Member may still carry an
                # address; it simply does not reach the disk.
                "email": member.email if self.config.retain_emails else None,
                "source": prov.source,
                "posture": prov.posture.value,
                "collected_at": _iso(prov.collected_at),
            },
        )
        self._conn.commit()
        return True

    def add_members(
        self, members: Iterable[Member], *, provenance: Provenance | None = None
    ) -> WriteResult:
        written = suppressed = 0
        for member in members:
            if self.add_member(member, provenance=provenance):
                written += 1
            else:
                suppressed += 1
        return WriteResult(written=written, suppressed=suppressed)

    def members(self) -> list[Member]:
        conn = self._require_open()
        with closing(conn.execute("SELECT * FROM members ORDER BY member_id")) as cursor:
            return [_member_from_row(row) for row in cursor.fetchall()]

    def member_ids(self) -> list[str]:
        conn = self._require_open()
        with closing(conn.execute("SELECT member_id FROM members ORDER BY member_id")) as cursor:
            return [str(row["member_id"]) for row in cursor.fetchall()]

    def member_count(self) -> int:
        return self._count("members")

    # -- targets ---------------------------------------------------------

    def add_target(self, target: Target, *, provenance: Provenance | None = None) -> bool:
        prov = _provenance_for("target", target.target_id, provenance or target.provenance)
        slug = normalize_slug(target.linkedin_slug)
        if self.is_tombstoned(target.target_id) or (slug and self.is_tombstoned(f"slug:{slug}")):
            return False
        self.put_row(
            "target",
            {
                "target_id": target.target_id,
                "firm": target.firm.value,
                "first_name": target.first_name,
                "last_name": target.last_name,
                "linkedin_slug": slug,
                "title": target.title,
                "team": target.team,
                "harvested": int(target.harvested),
                "source": prov.source,
                "posture": prov.posture.value,
                "collected_at": _iso(prov.collected_at),
            },
        )
        self._conn.commit()
        return True

    def add_targets(
        self, targets: Iterable[Target], *, provenance: Provenance | None = None
    ) -> WriteResult:
        written = suppressed = 0
        for target in targets:
            if self.add_target(target, provenance=provenance):
                written += 1
            else:
                suppressed += 1
        return WriteResult(written=written, suppressed=suppressed)

    def targets(self) -> list[Target]:
        conn = self._require_open()
        with closing(conn.execute("SELECT * FROM targets ORDER BY target_id")) as cursor:
            return [_target_from_row(row) for row in cursor.fetchall()]

    def target_count(self) -> int:
        return self._count("targets")

    def mark_harvested(self, target_id: str, harvested: bool = True) -> None:
        conn = self._require_open()
        conn.execute(
            "UPDATE targets SET harvested = ? WHERE target_id = ?", (int(harvested), target_id)
        )
        conn.commit()

    # -- edges -----------------------------------------------------------

    def add_edge(self, edge: Edge, *, provenance: Provenance | None = None) -> bool:
        prov = _provenance_for(
            "edge", f"{edge.member_id}->{edge.target_id}", provenance or edge.provenance
        )
        if self.is_tombstoned(edge.member_id) or self.is_tombstoned(edge.target_id):
            return False
        self.put_row(
            "edge",
            {
                "member_id": edge.member_id,
                "target_id": edge.target_id,
                "origin": edge.origin.value,
                "confidence": float(edge.confidence),
                "evidence": edge.evidence,
                "source": prov.source,
                "posture": prov.posture.value,
                "collected_at": _iso(prov.collected_at),
                "truncated": int(prov.truncated),
            },
        )
        self._conn.commit()
        return True

    def add_edges(
        self, edges: Iterable[Edge], *, provenance: Provenance | None = None
    ) -> WriteResult:
        written = suppressed = 0
        for edge in edges:
            if self.add_edge(edge, provenance=provenance):
                written += 1
            else:
                suppressed += 1
        return WriteResult(written=written, suppressed=suppressed)

    def edges(self, *, observed_only: bool = False) -> list[Edge]:
        conn = self._require_open()
        sql = "SELECT * FROM edges"
        params: tuple[Any, ...] = ()
        if observed_only:
            sql += " WHERE origin = ?"
            params = (EdgeOrigin.OBSERVED.value,)
        sql += " ORDER BY member_id, target_id"
        with closing(conn.execute(sql, params)) as cursor:
            return [_edge_from_row(row) for row in cursor.fetchall()]

    def edge_count(self) -> int:
        return self._count("edges")

    # -- company-match review queue (PRIV-20) ----------------------------

    def queue_review(
        self,
        match: CompanyMatch,
        *,
        provenance: Provenance,
        normalised: str = "",
        member_count: int = 0,
    ) -> None:
        """Record a company string awaiting adjudication."""
        conn = self._require_open()
        conn.execute(
            """
            INSERT INTO company_review
                (raw, normalised, status, firm, rule, score, member_count, source, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw) DO UPDATE SET
                normalised = excluded.normalised,
                status = excluded.status,
                firm = excluded.firm,
                rule = excluded.rule,
                score = excluded.score,
                member_count = excluded.member_count,
                source = excluded.source,
                collected_at = excluded.collected_at
            """,
            (
                match.raw,
                normalised,
                match.status.value,
                match.firm.value if match.firm else None,
                match.rule,
                match.score,
                int(member_count),
                provenance.source,
                _iso(provenance.collected_at),
            ),
        )
        conn.commit()

    def record_review_decision(self, raw: str, firm: Firm | None) -> None:
        """Adjudicate a queued string. ``firm=None`` means "not a target"."""
        conn = self._require_open()
        cursor = conn.execute(
            "UPDATE company_review SET decided = 1, decided_firm = ?, decided_at = ? WHERE raw = ?",
            (firm.value if firm else None, _iso(_utcnow()), raw),
        )
        if cursor.rowcount == 0:
            raise StoreError(f"no queued company string {raw!r} to adjudicate")
        conn.commit()

    def reviews(self, *, pending_only: bool = False) -> list[ReviewItem]:
        conn = self._require_open()
        sql = "SELECT * FROM company_review"
        if pending_only:
            sql += f" WHERE decided = 0 AND status = '{MatchStatus.NEEDS_REVIEW.value}'"
        sql += " ORDER BY raw"
        with closing(conn.execute(sql)) as cursor:
            return [_review_from_row(row) for row in cursor.fetchall()]

    def pending_review_count(self) -> int:
        """Count of unadjudicated ambiguous matches, for the report header
        (PRIV-20)."""
        conn = self._require_open()
        with closing(
            conn.execute(
                "SELECT COUNT(*) AS n FROM company_review WHERE decided = 0 AND status = ?",
                (MatchStatus.NEEDS_REVIEW.value,),
            )
        ) as cursor:
            return int(cursor.fetchone()["n"])

    # -- tombstones (PRIV-13) --------------------------------------------

    def tombstone(self, key: str, *, kind: str = "member", source: str = "purge") -> bool:
        """Record that ``key`` must never be re-persisted.

        Only the SHA-256 of the key is stored, so honouring an erasure request
        does not itself create a durable list of erased people's slugs.
        """
        cleaned = key.strip()
        if not cleaned:
            return False
        conn = self._require_open()
        cursor = conn.execute(
            "INSERT OR IGNORE INTO tombstones (key_hash, kind, source, collected_at) "
            "VALUES (?, ?, ?, ?)",
            (_key_hash(cleaned), kind, source, _iso(_utcnow())),
        )
        conn.commit()
        return cursor.rowcount > 0

    def is_tombstoned(self, key: str | None) -> bool:
        if not key:
            return False
        conn = self._require_open()
        with closing(
            conn.execute("SELECT 1 FROM tombstones WHERE key_hash = ?", (_key_hash(key.strip()),))
        ) as cursor:
            return cursor.fetchone() is not None

    def tombstone_count(self) -> int:
        return self._count("tombstones")

    # -- pruning (PRIV-07) -----------------------------------------------

    def prune_non_bridges(self, *, observed_only: bool | None = None) -> PruneResult:
        """Delete everyone who is not a bridge, and every target nobody bridges
        to.

        The pipeline calls this at the end of a run: a connection with no edge
        into the target set is not needed for the analysis, so keeping them
        would be processing without necessity.

        ``observed_only`` defaults to the inverse of ``config.include_inferred``
        — under the shipped configuration an inferred-only edge does not enter
        the graph, so the person it touches is not a bridge either.

        Coverage figures count unharvested targets, so compute them *before*
        calling this.
        """
        strict = (not self.config.include_inferred) if observed_only is None else observed_only
        where = " WHERE origin = ?" if strict else ""
        params: tuple[Any, ...] = (EdgeOrigin.OBSERVED.value,) if strict else ()
        conn = self._require_open()
        members = conn.execute(
            f"DELETE FROM members WHERE member_id NOT IN (SELECT member_id FROM edges{where})",
            params,
        ).rowcount
        targets = conn.execute(
            f"DELETE FROM targets WHERE target_id NOT IN (SELECT target_id FROM edges{where})",
            params,
        ).rowcount
        conn.commit()
        result = PruneResult(members_deleted=max(members, 0), targets_deleted=max(targets, 0))
        self.audit.append_for(self.config, "prune", count=result.total)
        return result

    # -- retention (PRIV-11) ---------------------------------------------

    def delete_older_than(self, cutoff: datetime) -> dict[str, int]:
        """Delete every record collected before ``cutoff``.

        Timestamps are stored as fixed-width UTC ISO-8601, so the lexicographic
        comparison SQLite performs is also the chronological one. Tombstones are
        exempt: they are the record that an erasure happened and must outlive
        the data they erased.
        """
        marker = _iso(cutoff)
        conn = self._require_open()
        deleted: dict[str, int] = {}
        for table in ("edges", "members", "targets", "company_review"):
            cursor = conn.execute(f"DELETE FROM {table} WHERE collected_at < ?", (marker,))
            deleted[table] = max(cursor.rowcount, 0)
        conn.commit()
        return deleted

    # -- purge (PRIV-12, 13, 14) -----------------------------------------

    def purge_person(self, id_or_slug: str) -> PurgeResult:
        """Erase one person, their edges, and any derived artefact.

        Writes tombstones for every key that identifies them, so a later
        ingest of the same ``Connections.csv`` cannot bring them back. The
        tombstone is written even when nothing matched — an erasure request that
        arrives before the data does must still be honoured.
        """
        key = id_or_slug.strip()
        if not key:
            raise StoreError("purge_person needs an id or slug")
        slug = normalize_slug(key)
        conn = self._require_open()
        with closing(
            conn.execute(
                "SELECT member_id, linkedin_slug FROM members "
                "WHERE member_id = ? OR linkedin_slug = ?",
                (key, slug),
            )
        ) as cursor:
            rows = cursor.fetchall()

        member_ids = [str(row["member_id"]) for row in rows]
        keys: set[str] = {key}
        if slug and not key.startswith("m_"):
            keys.add(f"slug:{slug}")
        for row in rows:
            keys.add(str(row["member_id"]))
            row_slug = row["linkedin_slug"]
            if row_slug:
                keys.add(f"slug:{row_slug}")

        edges_deleted = 0
        members_deleted = 0
        if member_ids:
            placeholders = ", ".join("?" for _ in member_ids)
            edges_deleted = max(
                conn.execute(
                    f"DELETE FROM edges WHERE member_id IN ({placeholders})", member_ids
                ).rowcount,
                0,
            )
            members_deleted = max(
                conn.execute(
                    f"DELETE FROM members WHERE member_id IN ({placeholders})", member_ids
                ).rowcount,
                0,
            )
        conn.commit()

        written = sum(1 for k in sorted(keys) if self.tombstone(k, kind="member"))
        self.clear_derived()
        result = PurgeResult(
            members_deleted=members_deleted,
            edges_deleted=edges_deleted,
            tombstones_written=written,
        )
        self.audit.append_for(self.config, "purge_person", count=members_deleted)
        return result

    def purge_target(self, entity_id: str) -> PurgeResult:
        """Erase target-side records for a target person, a firm, or a registry
        entity id, plus every edge that would be orphaned.

        Accepts ``t_<hash>``, a ``Firm`` value, a registry entity id such as
        ``citadel_llc``, or a LinkedIn slug — the CLI's ``--target`` argument is
        whatever the user has to hand.
        """
        key = entity_id.strip()
        if not key:
            raise StoreError("purge_target needs an entity id")
        firm_value = _FIRM_ALIASES.get(key.casefold(), key.casefold())
        slug = normalize_slug(key)
        conn = self._require_open()
        with closing(
            conn.execute(
                "SELECT target_id, linkedin_slug FROM targets "
                "WHERE target_id = ? OR firm = ? OR linkedin_slug = ?",
                (key, firm_value, slug),
            )
        ) as cursor:
            rows = cursor.fetchall()

        target_ids = [str(row["target_id"]) for row in rows]
        keys: set[str] = {key}
        for row in rows:
            keys.add(str(row["target_id"]))
            row_slug = row["linkedin_slug"]
            if row_slug:
                keys.add(f"slug:{row_slug}")

        edges_deleted = 0
        targets_deleted = 0
        if target_ids:
            placeholders = ", ".join("?" for _ in target_ids)
            edges_deleted = max(
                conn.execute(
                    f"DELETE FROM edges WHERE target_id IN ({placeholders})", target_ids
                ).rowcount,
                0,
            )
            targets_deleted = max(
                conn.execute(
                    f"DELETE FROM targets WHERE target_id IN ({placeholders})", target_ids
                ).rowcount,
                0,
            )
        conn.commit()

        written = sum(1 for k in sorted(keys) if self.tombstone(k, kind="target"))
        self.clear_derived()
        result = PurgeResult(
            targets_deleted=targets_deleted,
            edges_deleted=edges_deleted,
            tombstones_written=written,
        )
        self.audit.append_for(self.config, "purge_target", count=targets_deleted)
        return result

    def purge_all(self) -> list[Path]:
        """Remove everything under the state root.

        The audit line is written *first*, because this is the one sanctioned
        code path that destroys the audit log itself (PRIV-16). Returns the
        paths that survived — empty means success, and a non-empty list is what
        the CLI turns into a non-zero exit (PRIV-12).
        """
        count = 0 if self._closed else self.member_count()
        self.audit.append_for(self.config, "purge_all", count=count)
        self.close()
        return purge_state_root(self._root)

    def clear_derived(self) -> int:
        """Drop cached derived artefacts.

        A cluster label, a rendered report or an enrichment cache can mention a
        person the store no longer holds, so a purge invalidates all of them
        rather than trying to prove which ones are clean.
        """
        removed = 0
        for name in DERIVED_DIRNAMES:
            directory = self._root / name
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
        return removed

    # -- helpers ---------------------------------------------------------

    def _count(self, table: str) -> int:
        conn = self._require_open()
        with closing(conn.execute(f"SELECT COUNT(*) AS n FROM {table}")) as cursor:
            return int(cursor.fetchone()["n"])

    def counts(self) -> dict[str, int]:
        """Row counts per table, for reports and audit lines."""
        return {
            table: self._count(table)
            for table in ("members", "targets", "edges", "company_review", "tombstones")
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def open_store(config: Config) -> Store:
    """Open the store for ``config``."""
    return Store(config)


def purge_state_root(state_root: Path | str) -> list[Path]:
    """Empty a state root, returning whatever survived (PRIV-12).

    The root directory itself is kept — empty — so the next run does not have to
    guess whether it was ever initialised.
    """
    root = Path(state_root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise StoreError(f"state root {root} is not a directory")
    for child in sorted(root.iterdir()):
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError:  # pragma: no cover - surfaced through the return value
            pass
    return sorted(root.rglob("*"))


def _provenance_for(kind: str, identifier: str, provenance: Provenance | None) -> Provenance:
    if provenance is None:
        raise StoreError(
            f"{kind} {identifier!r} has no provenance; every persisted record carries "
            "source and collected_at (PRIV-10)"
        )
    if not provenance.source or not str(provenance.source).strip():
        raise StoreError(f"{kind} {identifier!r} has an empty source (PRIV-10)")
    return provenance


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601, so string ordering is time ordering."""
    if not isinstance(moment, datetime):
        raise StoreError(f"collected_at must be a datetime, got {type(moment).__name__}")
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment
    return aware.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_ts(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise StoreError(f"unparseable collected_at {value!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _key_hash(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest()


def _provenance_from_row(row: sqlite3.Row, *, truncated: bool = False) -> Provenance:
    # ``posture`` is persisted, so a round-tripped record reports the posture it
    # was actually collected under rather than the default (PRIV-04).
    try:
        posture = CompliancePosture(str(row["posture"]))
    except (IndexError, KeyError, ValueError):
        posture = CompliancePosture.FIRST_PARTY_EXPORT
    return Provenance(
        source=str(row["source"]),
        collected_at=_parse_ts(row["collected_at"]),
        posture=posture,
        truncated=truncated,
    )


def _member_from_row(row: sqlite3.Row) -> Member:
    connected = row["connected_on"]
    return Member(
        member_id=str(row["member_id"]),
        first_name=str(row["first_name"]),
        last_name=str(row["last_name"]),
        linkedin_slug=row["linkedin_slug"],
        company_raw=row["company_raw"],
        position=row["position"],
        connected_on=date.fromisoformat(connected) if connected else None,
        email=row["email"],
        provenance=_provenance_from_row(row),
    )


def _target_from_row(row: sqlite3.Row) -> Target:
    return Target(
        target_id=str(row["target_id"]),
        firm=Firm(str(row["firm"])),
        first_name=str(row["first_name"]),
        last_name=str(row["last_name"]),
        linkedin_slug=row["linkedin_slug"],
        title=row["title"],
        team=row["team"],
        harvested=bool(row["harvested"]),
        provenance=_provenance_from_row(row),
    )


def _edge_from_row(row: sqlite3.Row) -> Edge:
    truncated = bool(row["truncated"])
    return Edge(
        member_id=str(row["member_id"]),
        target_id=str(row["target_id"]),
        origin=EdgeOrigin(str(row["origin"])),
        confidence=float(row["confidence"]),
        evidence=row["evidence"],
        provenance=_provenance_from_row(row, truncated=truncated),
    )


def _review_from_row(row: sqlite3.Row) -> ReviewItem:
    firm = row["firm"]
    decided_firm = row["decided_firm"]
    return ReviewItem(
        raw=str(row["raw"]),
        normalised=str(row["normalised"]),
        status=MatchStatus(str(row["status"])),
        firm=Firm(str(firm)) if firm else None,
        rule=str(row["rule"]),
        score=None if row["score"] is None else float(row["score"]),
        member_count=int(row["member_count"]),
        decided=bool(row["decided"]),
        decided_firm=Firm(str(decided_firm)) if decided_firm else None,
        source=str(row["source"]),
        collected_at=_parse_ts(row["collected_at"]),
    )


def record_kinds() -> Sequence[str]:
    return tuple(_TABLES)


__all__ = [
    "PruneResult",
    "PurgeResult",
    "ReviewItem",
    "Store",
    "WriteResult",
    "open_store",
    "purge_state_root",
    "record_kinds",
]
