"""Resolving typed names and pasted URLs to the ``Person`` records from ingest.

The rule this module exists to enforce: **an entry that cannot be matched
confidently is reported, never guessed and never dropped.** A silently dropped
mutual makes a true bridge edge vanish; a silently guessed one invents a bridge
edge that does not exist and stamps it ``OBSERVED``, which outranks every
inferred signal in scoring. Both failures are worse than an operator having to
paste a URL. Everything that does not resolve cleanly lands in
``unresolved_mutuals.jsonl`` with the candidates that were considered.

Precedence is slug first, name second. The slug is what the human pasted from
the address bar and is exact; the name is what they read off a screen and is not.

``ACoA…`` member URNs are matched case-sensitively. Casefolding them merges
distinct humans -- the same hazard the ingest contract calls out -- so a URN that
does not match exactly is reported rather than folded.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz, process

from interlayer.enrich.parsing import linkedin_path, looks_like_url, slug_from_url
from interlayer.models import Base, Person, TargetFirm, normalize_key

__all__ = [
    "MatchCandidate",
    "PersonIndex",
    "Resolution",
    "UnresolvedMutual",
    "UnresolvedReason",
]

UnresolvedReason = Literal[
    "ambiguous_name",
    "ambiguous_url",
    "weak_match",
    "no_match",
    "url_not_in_connections",
]

_MEMBER_URN_RE = re.compile(r"^AC[oO][A-Za-z0-9_-]{6,}$")
_MAX_CANDIDATES = 3


class MatchCandidate(Base):
    """One person the matcher considered, with the score it gave them."""

    person_id: str
    full_name: str
    score: float
    linkedin_url: str | None = None


class UnresolvedMutual(Base):
    """A recorded mutual that could not be tied to a person, and why.

    This is an artifact, not a log line: the operator fixes these by pasting the
    profile URL back into the observation file and re-running, so the record has
    to carry enough to act on -- the text as typed, where it was typed, which
    target it belonged to, and who the near misses were.
    """

    raw: str
    reason: UnresolvedReason
    target_person_id: str
    target_name: str
    firm: TargetFirm
    source_line: int | None = None
    candidates: tuple[MatchCandidate, ...] = ()
    hint: str = ""

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.target_person_id, normalize_key(self.raw), self.raw)


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of resolving one typed entry."""

    person_id: str | None
    method: Literal["slug", "slug_ci", "name", "fuzzy", "none"]
    score: float
    reason: UnresolvedReason | None
    candidates: tuple[MatchCandidate, ...]
    hint: str = ""

    @property
    def ok(self) -> bool:
        return self.person_id is not None


def _candidate(person: Person, score: float) -> MatchCandidate:
    return MatchCandidate(
        person_id=person.person_id,
        full_name=person.full_name,
        score=round(float(score), 2),
        linkedin_url=person.linkedin_url,
    )


def _is_member_urn(slug: str) -> bool:
    """True for ``/in/ACoAAB…`` opaque member URNs, which are case-sensitive."""
    return bool(_MEMBER_URN_RE.match(slug))


class PersonIndex:
    """Lookup structures over the ego's connections, built once per run.

    The candidate pool is exactly ``people.jsonl`` minus the ego: every mutual
    connection is by definition a 1st-degree connection of the operator, so a
    name that matches nothing here means the person is genuinely not in the
    export -- usually a connection made after the export was taken.
    """

    def __init__(self, people: Iterable[Person]) -> None:
        self._by_slug: dict[str, list[Person]] = {}
        self._by_slug_ci: dict[str, list[Person]] = {}
        self._by_path: dict[str, list[Person]] = {}
        self._by_path_ci: dict[str, list[Person]] = {}
        self._by_name: dict[str, list[Person]] = {}

        for person in people:
            if person.is_ego or not person.full_name.strip():
                continue
            for slug in {person.linkedin_slug, slug_from_url(person.linkedin_url)}:
                if not slug:
                    continue
                self._by_slug.setdefault(slug, []).append(person)
                if not _is_member_urn(slug):
                    self._by_slug_ci.setdefault(slug.casefold(), []).append(person)
            path = linkedin_path(person.linkedin_url)
            if path:
                self._by_path.setdefault(path, []).append(person)
                if not _is_member_urn(path.rsplit("/", 1)[-1]):
                    self._by_path_ci.setdefault(path.casefold(), []).append(person)
            self._by_name.setdefault(normalize_key(person.full_name), []).append(person)

        # Sorted so rapidfuzz's tie-breaking (first index wins) is deterministic.
        self._names: list[str] = sorted(self._by_name)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_name.values())

    @property
    def names(self) -> Sequence[str]:
        return self._names

    # -- entry points ------------------------------------------------------

    def resolve(self, raw: str, *, accept: float, review: float) -> Resolution:
        """Resolve one cleaned entry to a ``person_id``, or explain why not."""
        if looks_like_url(raw):
            return self._resolve_url(raw)
        return self._resolve_name(raw, accept=accept, review=review)

    # -- url ---------------------------------------------------------------

    def _resolve_url(self, raw: str) -> Resolution:
        slug = slug_from_url(raw)
        path = linkedin_path(raw)
        exact: list[Person] = []
        for key, table in ((path, self._by_path), (slug, self._by_slug)):
            if key and key in table:
                exact = table[key]
                break
        if not exact:
            urn = slug is not None and _is_member_urn(slug)
            if not urn:
                for key, table in ((path, self._by_path_ci), (slug, self._by_slug_ci)):
                    if key and key.casefold() in table:
                        exact = table[key.casefold()]
                        break
        if len(exact) == 1:
            return Resolution(exact[0].person_id, "slug", 100.0, None, ())
        if len(exact) > 1:
            return Resolution(
                None,
                "none",
                0.0,
                "ambiguous_url",
                tuple(_candidate(p, 100.0) for p in sorted(exact, key=lambda p: p.person_id)),
                hint="two connections share this profile URL; de-duplicate people.jsonl",
            )
        return Resolution(
            None,
            "none",
            0.0,
            "url_not_in_connections",
            (),
            hint=(
                "this profile is not in your Connections.csv. Either they are not a 1st-degree "
                "connection, or your export predates the connection -- re-export and re-run ingest."
            ),
        )

    # -- name --------------------------------------------------------------

    def _resolve_name(self, raw: str, *, accept: float, review: float) -> Resolution:
        key = normalize_key(raw)
        if not key:
            return Resolution(None, "none", 0.0, "no_match", (), hint="empty entry")

        exact = self._by_name.get(key, [])
        if len(exact) == 1:
            return Resolution(exact[0].person_id, "name", 100.0, None, ())
        if len(exact) > 1:
            return Resolution(
                None,
                "none",
                100.0,
                "ambiguous_name",
                tuple(_candidate(p, 100.0) for p in sorted(exact, key=lambda p: p.person_id)),
                hint=(
                    f"{len(exact)} of your connections are called {raw!r}; paste the profile URL "
                    "instead of the name"
                ),
            )

        scored = self._score(key)
        if not scored:
            return Resolution(
                None,
                "none",
                0.0,
                "no_match",
                (),
                hint="no connection resembles this name; check the spelling or paste the URL",
            )

        best_score, best_people = scored[0]
        candidates = tuple(
            _candidate(person, score)
            for score, people in scored[:_MAX_CANDIDATES]
            for person in people
        )[:_MAX_CANDIDATES]

        if best_score >= accept:
            contenders = [p for score, people in scored if score >= accept for p in people]
            if len(contenders) > 1:
                return Resolution(
                    None,
                    "none",
                    best_score,
                    "ambiguous_name",
                    candidates,
                    hint=(
                        f"{len(contenders)} connections match {raw!r} at or above {accept:g}; "
                        "paste the profile URL to disambiguate"
                    ),
                )
            return Resolution(best_people[0].person_id, "fuzzy", best_score, None, ())

        if best_score >= review:
            return Resolution(
                None,
                "none",
                best_score,
                "weak_match",
                candidates,
                hint=(
                    f"closest match scores {best_score:.1f}, below the {accept:g} accept "
                    "threshold. Confirm in the browser, then paste the URL or fix the spelling."
                ),
            )

        return Resolution(
            None,
            "none",
            best_score,
            "no_match",
            candidates,
            hint="no connection resembles this name; check the spelling or paste the URL",
        )

    def _score(self, key: str) -> list[tuple[float, list[Person]]]:
        """Top normalised names by WRatio, deterministically ordered."""
        if not self._names:
            return []
        raw_matches = process.extract(
            key,
            self._names,
            scorer=fuzz.WRatio,
            limit=_MAX_CANDIDATES + 2,
        )
        out = [(float(score), self._by_name[name]) for name, score, _ in raw_matches]
        out.sort(key=lambda item: (-item[0], item[1][0].person_id))
        return out
