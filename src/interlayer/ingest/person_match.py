"""Resolve the same human across ``Connections.csv``, mutuals captures and targets.

Ruling C2 splits this cleanly from company matching, because they are matching
different things:

* **Identity is decided by an exact key**, never by a score. The key is
  ``core.ids.name_key`` — the order-free sorted-token key — paired with the
  resolved employer, or a LinkedIn slug/URN when one is present.
* **``rapidfuzz.WRatio >= 92`` runs only *inside* a block, and only to rank
  review candidates.** It never auto-accepts anything, and it never runs across
  blocks. A score is a suggestion to a human here, not a decision.

Two Wave 1 measurements are load-bearing and must not be undone:

* **No ``unidecode``.** It renders ``李明`` as ``"Li Ming "`` and ``محمد`` as
  ``"mHmd"`` — it invents romanisations. ``core.ids`` folds combining marks
  instead: ``José García -> jose garcia`` and ``李明 -> 李明``.
* **No ``nameparser``.** It encodes a Western given-family assumption and
  inverts ``Nguyễn Văn An`` and ``Kim Min-jun``. Sorting the tokens means
  nobody has to decide which one is the surname.

The sorted-token key's own limitation is why it can only ever *block*: it
merges ``Li Ming`` with ``Ming Li``, who may well be two different people.
That pair reaches a human, not the graph.

Not implemented, deliberately: ``jellyfish`` phonetic keys and the ``nicknames``
lexicon. Neither library is in the pinned dependency set, and both are
Anglo/Latin-only — R5 gates them behind an ``is_latin_script`` check for
exactly that reason. Their absence costs recall on ``Smith``/``Smyth`` and
``bill``/``william``, not precision. See ``docs/wave2-notes/B2.md``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from interlayer.core.ids import name_key, name_tokens, normalize_slug, normalize_text
from interlayer.core.models import Firm, Member, Target

#: R3's threshold, kept for its C2-sanctioned purpose only: ranking candidates
#: *within* a block for the review queue. Never an auto-merge.
WRATIO_REVIEW_MIN = 92.0

#: Blocking-key namespaces. A pair is compared only if it shares at least one.
BLOCK_URN = "urn"
BLOCK_SLUG = "slug"
BLOCK_NAME = "name"
BLOCK_INITIALS = "initials"


@dataclass(frozen=True, slots=True)
class PersonRecord:
    """One sighting of a person, from one source.

    ``display_name`` is the original string, unmodified — it is the only form
    that ever reaches a report. Every derived key below is a blocking key and
    is barred from rendered output (PRIV-09).
    """

    record_id: str
    first_name: str = ""
    last_name: str = ""
    slug: str | None = None
    urn: str | None = None
    """Numeric member URN, if an adapter supplied one. Immutable when present,
    unlike the slug (5 changes / 180 days, then the old URL 404s)."""
    company_key: str = ""
    """Resolved company entity id where known, else the normalised employer.
    Part of the last-resort identity key, so it must be a *resolved* value —
    never a raw free-text employer string that varies by row."""
    firm: Firm | None = None
    source: str = ""

    @property
    def display_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @property
    def name_key(self) -> str:
        """Order-free sorted-token key. ``Smith, Robert`` == ``Robert Smith``."""
        return name_key(self.first_name, self.last_name)

    @property
    def folded(self) -> str:
        """Normalised comparison form, for scoring only."""
        return normalize_text(self.display_name)

    @property
    def canonical_slug(self) -> str | None:
        return normalize_slug(self.slug)


def from_member(member: Member, *, company_key: str = "", source: str = "") -> PersonRecord:
    """Adapt a ``Connections.csv`` row. Pass the *resolved* company entity id
    as ``company_key`` where one exists; a raw employer string makes the
    last-resort identity key unstable."""
    return PersonRecord(
        record_id=member.member_id,
        first_name=member.first_name,
        last_name=member.last_name,
        slug=member.linkedin_slug,
        company_key=company_key or normalize_text(member.company_raw),
        source=source or (member.provenance.source if member.provenance else ""),
    )


def from_target(target: Target, *, source: str = "") -> PersonRecord:
    """Adapt a target-firm person. The firm is part of the identity key: the
    same human under two firms is two records, because Citadel and Citadel
    Securities are distinct entities."""
    return PersonRecord(
        record_id=target.target_id,
        first_name=target.first_name,
        last_name=target.last_name,
        slug=target.linkedin_slug,
        company_key=target.firm.value,
        firm=target.firm,
        source=source or (target.provenance.source if target.provenance else ""),
    )


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def identity_key(record: PersonRecord) -> str:
    """The *only* thing that merges two records automatically.

    Priority order, from R5: URN (immutable when present), then slug, then the
    ``(name_key, company)`` composite. No score participates.
    """
    if record.urn:
        return f"{BLOCK_URN}:{record.urn.strip().casefold()}"
    slug = record.canonical_slug
    if slug:
        return f"{BLOCK_SLUG}:{slug}"
    return f"{BLOCK_NAME}:{record.name_key}|company:{record.company_key}"


def initials_key(record: PersonRecord) -> str:
    """First character of each sorted token. Catches ``R. Smith``.

    Low precision on its own, which is why a pair blocked only on initials can
    reach the review queue but can never merge.
    """
    return "".join(token[0] for token in sorted(name_tokens(record.first_name, record.last_name)))


def blocking_keys(record: PersonRecord) -> frozenset[str]:
    """Candidate-generation keys. Never a match decision (PRIV-09)."""
    keys: set[str] = set()
    if record.urn:
        keys.add(f"{BLOCK_URN}:{record.urn.strip().casefold()}")
    slug = record.canonical_slug
    if slug:
        keys.add(f"{BLOCK_SLUG}:{slug}")
    key = record.name_key
    if key:
        keys.add(f"{BLOCK_NAME}:{key}")
    tokens = name_tokens(record.first_name, record.last_name)
    if len(tokens) >= 2:
        keys.add(f"{BLOCK_INITIALS}:{initials_key(record)}")
    return frozenset(keys)


def person_id(key: str) -> str:
    """Surrogate id, minted from the first stable key and never recomputed.

    Keying application state on the slug directly is the bug that silently
    duplicates a third of the graph six months later, when vanity URLs change.
    Persist this value; do not re-derive it after a slug changes.
    """
    return "p_" + hashlib.blake2b(key.encode("utf-8"), digest_size=8).hexdigest()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PersonCluster:
    """Records that share an identity key — one human, several sightings."""

    person_id: str
    identity_key: str
    record_ids: tuple[str, ...]
    display_name: str = ""
    name_key: str = ""
    slugs: tuple[str, ...] = ()
    """Slug history. A profile whose slug changed is re-linked by matching any
    historical slug, or by ``(name_key, company)`` plus human confirmation."""
    firm: Firm | None = None
    sources: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.record_ids)


@dataclass(frozen=True, slots=True)
class PersonReviewPair:
    """Two clusters that a human should look at. Never merged automatically."""

    left: str
    right: str
    score: float
    shared_blocks: tuple[str, ...]
    left_display: str = ""
    right_display: str = ""

    @property
    def reason(self) -> str:
        return (
            f"shares blocking key(s) {list(self.shared_blocks)}; "
            f"WRatio {self.score:.1f} >= {WRATIO_REVIEW_MIN:.0f} within block"
        )


@dataclass(frozen=True, slots=True)
class DedupeResult:
    """Clusters that merged, plus the pairs that need a human."""

    clusters: tuple[PersonCluster, ...] = ()
    review_pairs: tuple[PersonReviewPair, ...] = ()
    blocks: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Block key -> person ids. Diagnostic; a block with hundreds of members
    means the blocking key has collapsed and scoring will be quadratic."""

    def cluster_for(self, person: str) -> PersonCluster:
        for cluster in self.clusters:
            if cluster.person_id == person:
                return cluster
        raise KeyError(person)

    @property
    def person_ids(self) -> tuple[str, ...]:
        return tuple(c.person_id for c in self.clusters)

    @property
    def unadjudicated_count(self) -> int:
        return len(self.review_pairs)


# ---------------------------------------------------------------------------
# Dedupe with blocking
# ---------------------------------------------------------------------------


def dedupe(records: Iterable[PersonRecord]) -> DedupeResult:
    """Group records into people, and rank the near-misses for review.

    Merging is exact-key only. Scoring happens afterwards, inside blocks, and
    produces review candidates — nothing it returns changes a cluster.
    """
    grouped: dict[str, list[PersonRecord]] = {}
    for record in records:
        grouped.setdefault(identity_key(record), []).append(record)

    clusters: list[PersonCluster] = []
    for key in sorted(grouped):
        members = grouped[key]
        slugs = sorted({s for s in (m.canonical_slug for m in members) if s})
        firms = [m.firm for m in members if m.firm is not None]
        clusters.append(
            PersonCluster(
                person_id=person_id(key),
                identity_key=key,
                record_ids=tuple(sorted(m.record_id for m in members)),
                display_name=members[0].display_name,
                name_key=members[0].name_key,
                slugs=tuple(slugs),
                firm=firms[0] if firms else None,
                sources=tuple(sorted({m.source for m in members if m.source})),
            )
        )

    representatives = {c.person_id: grouped[c.identity_key][0] for c in clusters}
    blocks = _build_blocks(clusters, representatives)
    pairs = _rank_review_pairs(clusters, representatives, blocks)
    return DedupeResult(clusters=tuple(clusters), review_pairs=pairs, blocks=blocks)


def _build_blocks(
    clusters: Sequence[PersonCluster], representatives: Mapping[str, PersonRecord]
) -> dict[str, tuple[str, ...]]:
    blocks: dict[str, list[str]] = {}
    for cluster in clusters:
        for key in blocking_keys(representatives[cluster.person_id]):
            blocks.setdefault(key, []).append(cluster.person_id)
    return {key: tuple(sorted(ids)) for key, ids in sorted(blocks.items())}


def _rank_review_pairs(
    clusters: Sequence[PersonCluster],
    representatives: Mapping[str, PersonRecord],
    blocks: Mapping[str, tuple[str, ...]],
) -> tuple[PersonReviewPair, ...]:
    """Score only pairs that already share a block. Never across blocks."""
    shared: dict[tuple[str, str], set[str]] = {}
    for key, ids in blocks.items():
        for i, left in enumerate(ids):
            for right in ids[i + 1 :]:
                shared.setdefault((left, right), set()).add(key)

    by_id = {c.person_id: c for c in clusters}
    pairs: list[PersonReviewPair] = []
    for (left, right), keys in shared.items():
        a, b = representatives[left], representatives[right]
        # A differing, non-null URN is a hard veto: same score, still not the
        # same person. Matching URNs already merged into one cluster above.
        if a.urn and b.urn and a.urn != b.urn:
            continue
        score = float(fuzz.WRatio(a.folded, b.folded))
        if score < WRATIO_REVIEW_MIN:
            continue
        pairs.append(
            PersonReviewPair(
                left=left,
                right=right,
                score=score,
                shared_blocks=tuple(sorted(keys)),
                left_display=by_id[left].display_name,
                right_display=by_id[right].display_name,
            )
        )
    pairs.sort(key=lambda p: (-p.score, p.left, p.right))
    return tuple(pairs)


class PersonResolver:
    """Incremental face of :func:`dedupe`, for callers assembling sources."""

    def __init__(self, records: Iterable[PersonRecord] = ()) -> None:
        self._records: list[PersonRecord] = list(records)
        self._result: DedupeResult | None = None

    def add(self, record: PersonRecord) -> None:
        self._records.append(record)
        self._result = None

    def extend(self, records: Iterable[PersonRecord]) -> None:
        self._records.extend(records)
        self._result = None

    def add_members(self, members: Iterable[Member], *, company_key: str = "") -> None:
        self.extend(from_member(m, company_key=company_key) for m in members)

    def add_targets(self, targets: Iterable[Target]) -> None:
        self.extend(from_target(t) for t in targets)

    @property
    def records(self) -> tuple[PersonRecord, ...]:
        return tuple(self._records)

    def resolve(self) -> DedupeResult:
        if self._result is None:
            self._result = dedupe(self._records)
        return self._result

    def person_id_for(self, record_id: str) -> str:
        for cluster in self.resolve().clusters:
            if record_id in cluster.record_ids:
                return cluster.person_id
        raise KeyError(record_id)


__all__ = [
    "BLOCK_INITIALS",
    "BLOCK_NAME",
    "BLOCK_SLUG",
    "BLOCK_URN",
    "WRATIO_REVIEW_MIN",
    "DedupeResult",
    "PersonCluster",
    "PersonRecord",
    "PersonResolver",
    "PersonReviewPair",
    "blocking_keys",
    "dedupe",
    "from_member",
    "from_target",
    "identity_key",
    "initials_key",
    "person_id",
]
