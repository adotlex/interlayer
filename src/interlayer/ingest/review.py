"""The review queue — the human side of the PRIV-20 barrier.

``needs_review`` is not a warning label. A record classified ``needs_review``
**cannot enter the analysis graph** until a person adjudicates it, and the
report states how many are outstanding. This module is where those items
accumulate, get decided, and get counted.

The economics are deliberately lopsided, and R5 measured them: a false positive
silently corrupts the cluster structure and the user has no way to detect it;
a review item costs one keystroke. So the ladder is tuned for precision on
``confident`` and recall on ``confident`` plus ``needs_review``, and the volume that
lands here is bounded by *distinct* employer strings (80 across a 30,000-row
synthetic corpus), not by rows.

Decisions persist, keyed on the normalised company string, so each distinct
string is adjudicated **once, ever**. A cached decision short-circuits the whole
ladder on the next run — which is also why every cache hit has to reach the
audit log: a wrong past decision must stay traceable.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

from interlayer.core.ids import sha1_hex
from interlayer.core.models import CompanyMatch, Firm, MatchStatus
from interlayer.ingest import ReviewError
from interlayer.ingest.company_match import RULE_CACHED, MatchExplanation, normalise_company
from interlayer.ingest.person_match import PersonReviewPair

#: Sentinel written to YAML for "reviewed, and it is none of our target firms".
NO_FIRM = "none"

DECISIONS_FILENAME = "review_decisions.yaml"
STATE_ROOT_ENV = "INTERLAYER_STATE_ROOT"


def default_decisions_path(state_root: str | os.PathLike[str] | None = None) -> Path:
    """``<state_root>/review_decisions.yaml``.

    ``core.config`` (B1) owns the real state root; this resolves the same
    default independently so ingest does not have to import config.
    """
    if state_root is None:
        state_root = os.environ.get(STATE_ROOT_ENV) or "~/.interlayer"
    return Path(state_root).expanduser() / DECISIONS_FILENAME


@dataclass(frozen=True, slots=True)
class ReviewItem:
    """One distinct employer string awaiting a human decision."""

    key: str
    """The *normalised* company string. The decision cache is keyed on this,
    so every raw spelling that normalises to it is decided at once."""

    raw_examples: tuple[str, ...] = ()
    """Raw spellings seen for this key, deduped, in first-seen order."""

    suggested_firm: Firm | None = None
    """The ladder's leading candidate. A suggestion for the reviewer, nothing
    more — it has explicitly not met the bar to be applied."""

    rule: str = ""
    """Which rung fired and why it stopped short of confident."""

    score: float | None = None
    occurrences: int = 0
    first_seen: datetime | None = None

    @property
    def raw(self) -> str:
        return self.raw_examples[0] if self.raw_examples else self.key


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """A human's answer. ``firm=None`` means "none of our target firms"."""

    key: str
    firm: Firm | None
    decided_at: datetime
    decided_by: str = "user"

    def to_yaml(self) -> dict[str, str]:
        return {
            "entity_id": self.firm.value if self.firm is not None else NO_FIRM,
            "decided_at": self.decided_at.isoformat(),
            "decided_by": self.decided_by,
        }


class ReviewQueue:
    """Pending review items, the decisions made about them, and the counts.

    ``unadjudicated_count`` is the **company** figure — the one that gates the
    graph (PRIV-20) and feeds ``AnalysisResult.unadjudicated_count``. Person
    review pairs are counted separately by :attr:`person_pending_count`,
    because they never block an edge; they only suggest a merge.
    """

    def __init__(
        self,
        *,
        decisions: Iterable[ReviewDecision] = (),
        known_firms: Iterable[Firm] = tuple(Firm),
    ) -> None:
        self._items: dict[str, ReviewItem] = {}
        self._decisions: dict[str, ReviewDecision] = {d.key: d for d in decisions}
        self._known_firms = frozenset(known_firms)
        self._person_pairs: list[PersonReviewPair] = []

    # -- accumulating -----------------------------------------------------

    def observe(
        self,
        match: CompanyMatch | MatchExplanation,
        *,
        normalised: str | None = None,
        seen_at: datetime | None = None,
    ) -> CompanyMatch:
        """Record a classification, and return it with any decision applied.

        Passing a :class:`MatchExplanation` is preferred: it already carries
        the normalised key, so the queue does not have to re-derive it.
        """
        if isinstance(match, MatchExplanation):
            normalised = match.normalised
            match = match.match
        key = normalised if normalised is not None else normalise_company(match.raw)

        decided = self.apply(match, normalised=key)
        if decided.status is not MatchStatus.NEEDS_REVIEW:
            return decided

        now = seen_at or datetime.now(UTC)
        existing = self._items.get(key)
        if existing is None:
            self._items[key] = ReviewItem(
                key=key,
                raw_examples=(match.raw,) if match.raw else (),
                suggested_firm=match.firm,
                rule=match.rule,
                score=match.score,
                occurrences=1,
                first_seen=now,
            )
        else:
            examples = existing.raw_examples
            if match.raw and match.raw not in examples:
                examples = (*examples, match.raw)
            self._items[key] = replace(
                existing,
                raw_examples=examples,
                occurrences=existing.occurrences + 1,
            )
        return decided

    def observe_many(
        self, matches: Iterable[CompanyMatch | MatchExplanation], *, seen_at: datetime | None = None
    ) -> tuple[CompanyMatch, ...]:
        return tuple(self.observe(m, seen_at=seen_at) for m in matches)

    def add_person_pairs(self, pairs: Iterable[PersonReviewPair]) -> None:
        """Queue candidate person merges. These never block an edge."""
        self._person_pairs.extend(pairs)

    # -- reading ----------------------------------------------------------

    def pending(self) -> tuple[ReviewItem, ...]:
        """Undecided company items, commonest first, then alphabetical.

        Deterministic order: the reviewer sees the same list twice, and the
        highest-leverage decision (the string on the most rows) is first.
        """
        undecided = [item for key, item in self._items.items() if key not in self._decisions]
        undecided.sort(key=lambda i: (-i.occurrences, i.key))
        return tuple(undecided)

    def pending_person_pairs(self) -> tuple[PersonReviewPair, ...]:
        return tuple(self._person_pairs)

    def item(self, key: str) -> ReviewItem:
        try:
            return self._items[key]
        except KeyError:
            raise ReviewError(f"no review item keyed {key!r}") from None

    @property
    def unadjudicated_count(self) -> int:
        """Company review items held out of the graph (PRIV-20). This is the
        number the report must state."""
        return len(self.pending())

    @property
    def person_pending_count(self) -> int:
        return len(self._person_pairs)

    @property
    def decisions(self) -> tuple[ReviewDecision, ...]:
        return tuple(self._decisions[key] for key in sorted(self._decisions))

    def decision_map(self) -> dict[str, Firm | None]:
        """Shape :class:`~interlayer.ingest.company_match.CompanyMatcher` wants."""
        return {key: decision.firm for key, decision in self._decisions.items()}

    # -- deciding ---------------------------------------------------------

    def adjudicate(
        self,
        key: str,
        firm: Firm | str | None,
        *,
        decided_at: datetime | None = None,
        decided_by: str = "user",
    ) -> ReviewDecision:
        """Decide one item. ``firm=None`` (or ``"none"``) rejects it.

        Refuses an unknown key and an unknown firm: a typo'd adjudication that
        silently did nothing would be worse than an error, because the item
        would quietly stay out of the graph while the user believed otherwise.
        """
        if key not in self._items:
            raise ReviewError(
                f"cannot adjudicate {key!r}: not in the review queue. "
                f"Pending keys: {[i.key for i in self.pending()]}"
            )
        resolved = self._coerce_firm(firm)
        decision = ReviewDecision(
            key=key,
            firm=resolved,
            decided_at=decided_at or datetime.now(UTC),
            decided_by=decided_by,
        )
        self._decisions[key] = decision
        return decision

    def _coerce_firm(self, firm: Firm | str | None) -> Firm | None:
        if firm is None:
            return None
        if isinstance(firm, Firm):
            candidate = firm
        else:
            text = firm.strip()
            if not text or text.casefold() == NO_FIRM:
                return None
            try:
                candidate = Firm(text)
            except ValueError:
                raise ReviewError(
                    f"{firm!r} is not a known firm; expected one of "
                    f"{[f.value for f in self._known_firms]} or {NO_FIRM!r}"
                ) from None
        if candidate not in self._known_firms:
            raise ReviewError(
                f"{candidate.value!r} is not in this registry; known: "
                f"{[f.value for f in self._known_firms]}"
            )
        return candidate

    def apply(self, match: CompanyMatch, *, normalised: str | None = None) -> CompanyMatch:
        """Rewrite a match using a stored decision, if there is one."""
        key = normalised if normalised is not None else normalise_company(match.raw)
        decision = self._decisions.get(key)
        if decision is None:
            return match
        if decision.firm is None:
            return replace(match, status=MatchStatus.NO_MATCH, firm=None, rule=RULE_CACHED)
        return replace(
            match, status=MatchStatus.CONFIDENT, firm=decision.firm, rule=RULE_CACHED, score=100.0
        )

    def audit_event(self, action: str, key: str) -> dict[str, object]:
        """A name-free audit record (PRIV-15): identifiers and counts only.

        The key is hashed rather than written out, so the log stays free of
        free-text a one-person company might have put their own name into.
        """
        decision = self._decisions.get(key)
        return {
            "action": action,
            "key_hash": sha1_hex(key)[:16],
            "entity_id": (
                decision.firm.value
                if decision is not None and decision.firm is not None
                else (NO_FIRM if decision is not None else None)
            ),
            "pending": self.unadjudicated_count,
        }

    # -- persistence ------------------------------------------------------

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Write the decision cache. Creates the state root if missing."""
        target = Path(path).expanduser() if path is not None else default_decisions_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "decisions": {d.key: d.to_yaml() for d in self.decisions},
        }
        target.write_text(
            yaml.safe_dump(payload, sort_keys=True, allow_unicode=True), encoding="utf-8"
        )
        return target

    def load(self, path: str | os.PathLike[str] | None = None) -> int:
        """Merge a decision cache from disk. Missing file is not an error."""
        source = Path(path).expanduser() if path is not None else default_decisions_path()
        if not source.is_file():
            return 0
        loaded = load_decisions(source, known_firms=self._known_firms)
        self._decisions.update({d.key: d for d in loaded})
        return len(loaded)


def load_decisions(
    path: str | os.PathLike[str],
    *,
    known_firms: Iterable[Firm] = tuple(Firm),
) -> tuple[ReviewDecision, ...]:
    """Read ``review_decisions.yaml``. Raises on a malformed cache."""
    source = Path(path).expanduser()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReviewError(f"cannot read decision cache {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ReviewError(f"decision cache {source} is not valid YAML: {exc}") from exc

    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise ReviewError(f"decision cache {source} must be a mapping")

    entries = raw.get("decisions") or {}
    if not isinstance(entries, Mapping):
        raise ReviewError(f"decision cache {source}: 'decisions' must be a mapping")

    firms = frozenset(known_firms)
    out: list[ReviewDecision] = []
    for key in sorted(entries):
        node = entries[key]
        if not isinstance(node, Mapping):
            raise ReviewError(f"decision cache {source}: entry {key!r} must be a mapping")
        entity = str(node.get("entity_id", NO_FIRM))
        firm: Firm | None
        if entity == NO_FIRM:
            firm = None
        else:
            try:
                firm = Firm(entity)
            except ValueError:
                raise ReviewError(
                    f"decision cache {source}: entry {key!r} names unknown "
                    f"entity_id {entity!r}"
                ) from None
            if firm not in firms:
                raise ReviewError(
                    f"decision cache {source}: entry {key!r} names {entity!r}, "
                    "which this registry does not define"
                )
        out.append(
            ReviewDecision(
                key=str(key),
                firm=firm,
                decided_at=_parse_ts(node.get("decided_at"), source, key),
                decided_by=str(node.get("decided_by", "user")),
            )
        )
    return tuple(out)


def _parse_ts(value: object, source: Path, key: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ReviewError(
                f"decision cache {source}: entry {key!r} has an unparseable decided_at {value!r}"
            ) from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ReviewError(f"decision cache {source}: entry {key!r} is missing decided_at")


__all__ = [
    "DECISIONS_FILENAME",
    "NO_FIRM",
    "STATE_ROOT_ENV",
    "ReviewDecision",
    "ReviewItem",
    "ReviewQueue",
    "default_decisions_path",
    "load_decisions",
]
