"""Resolve a free-text employer string to a target firm.

**Fuzzy matching cannot do this job, and that was measured, not reasoned.** R5
ran all eight ``rapidfuzz`` scorers against 28 true positives and 26 true
negatives and every one of them fails: ``token_set_ratio`` scores both
``Jane Street Capital LLC`` and ``Jane Street Entertainment`` at 100;
``partial_ratio`` scores ``The Citadel`` at 100. No threshold separates them.
So this module is a **deterministic ladder** with fuzzy matching demoted to a
typo-only backstop at the bottom:

===  ==========================  ====================================================
1    negative patterns           unconditional veto; beats every other rung
2    review-only aliases         ``JS`` -> review, never confident
3    anchor regex                or a typo'd anchor, which forces review
4    ``requires`` / ``excludes``  the ``securit\\w*`` discriminator pair
5    exact alias                 score 100
6    anchored prefix             score 100, trailing tokens must be expected
7    ``token_sort_ratio``        typo backstop only, per-entity threshold
===  ==========================  ====================================================

Every :class:`~interlayer.core.models.CompanyMatch` records which rung fired in
``rule``, because an unexplained firm assignment is indistinguishable from a
fabrication.

``NEEDS_REVIEW`` is a hard barrier, not a hint (PRIV-20). Use
:func:`is_admissible` / :func:`require_admissible` at the graph boundary; there
is deliberately no code path that turns a review item into an edge without a
human decision.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from interlayer.core.ids import fold_accents
from interlayer.core.models import CompanyMatch, Firm, MatchStatus
from interlayer.ingest import NeedsReviewError
from interlayer.ingest.targets import TargetEntity, TargetRegistry, load_registry

# ---------------------------------------------------------------------------
# Rule names. `rule` on a CompanyMatch names the rung that fired; when the rung
# and the reason for review differ, they are joined as "<rung>/<reason>".
# ---------------------------------------------------------------------------

RULE_EMPTY = "empty"
RULE_CACHED = "cached_decision"
RULE_NEGATIVE = "negative_pattern"
RULE_NO_ANCHOR = "no_anchor_or_negated"
RULE_REVIEW_ONLY = "review_only_alias"
RULE_EXACT_ALIAS = "exact_alias"
RULE_ANCHORED_PREFIX = "anchored_prefix"
RULE_FUZZY = "fuzzy_token_sort"
RULE_FUZZY_ANCHOR = "fuzzy_anchor"
RULE_UNEXPECTED = "unexpected_tokens"
RULE_AMBIGUOUS = "ambiguous_entity"
RULE_BELOW_THRESHOLD = "below_threshold"
RULE_BELOW_FLOOR = "below_floor"

# Steps 2 and 3 of normalise_company: the space, dash and apostrophe families.
_TRANSLATION = str.maketrans(
    {
        0x3000: " ",  # ideographic space
        0x00A0: " ",  # non-breaking space
        0x2010: "-",
        0x2011: "-",
        0x2012: "-",
        0x2013: "-",
        0x2014: "-",
        0x2015: "-",
        0x2212: "-",  # minus sign
        0x2018: "'",
        0x2019: "'",
        0x02BC: "'",
        0x0060: "'",
        0x00B4: "'",
    }
)

_INTRAWORD_HYPHEN = re.compile(r"(?<=\w)-(?=\w)", flags=re.UNICODE)
#: Keep "." and "'" so "B.V." and "L.P." survive to the legal-suffix vocabulary.
_DROP_PUNCT = re.compile(r"[^\w\s.']", flags=re.UNICODE)
_WS = re.compile(r"\s+", flags=re.UNICODE)


def normalise_company(value: str | None) -> str:
    """Canonical form of an employer string, R5 *Entity resolution spec* §A.

    Step 4 of that spec calls for ``unidecode``. This uses NFKD combining-mark
    stripping (``core.ids.fold_accents``) instead: it is a no-op on the
    Latin-script brand strings companies actually use — ``Jané Stréet`` still
    folds to ``jane street`` — and it keeps a single, auditable folding rule
    across companies and person names rather than two that disagree. It also
    keeps ``unidecode`` out of the dependency set entirely, which is what C2
    asked for.
    """
    if not value:
        return ""
    out = unicodedata.normalize("NFKC", value)  # 1: full-width -> ASCII
    out = out.translate(_TRANSLATION)  # 2 + 3: space/dash/apostrophe families
    out = fold_accents(out)  # 4: diacritics, without transliterating
    out = out.casefold()  # 5: not .lower(); correct for ss, dotted I
    out = out.replace("&", " and ")  # 6
    out = _INTRAWORD_HYPHEN.sub(" ", out)  # 7: Citadel-Securities -> two tokens
    out = _DROP_PUNCT.sub(" ", out)  # 8
    return _WS.sub(" ", out).strip()  # 9


def _split_tokens(normalised: str) -> tuple[str, ...]:
    return tuple(t for t in (raw.strip(".'") for raw in normalised.split(" ")) if t)


@dataclass(frozen=True, slots=True)
class NormalisedCompany:
    """One employer string, normalised and tokenised for every entity."""

    raw: str
    text: str
    """The normalised string. Anchors, negatives and discriminators run on it."""
    tokens: tuple[str, ...]
    bare_tokens: tuple[str, ...]
    """``tokens`` minus the shared legal-suffix vocabulary."""

    @property
    def bare(self) -> str:
        return " ".join(self.bare_tokens)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One entity's surviving claim on a company string."""

    entity_id: str
    firm: Firm
    score: float
    rung: str
    alias: str = ""
    forced_review: bool = False
    review_only: bool = False
    unexpected: tuple[str, ...] = ()
    head: str = ""


@dataclass(frozen=True, slots=True)
class MatchExplanation:
    """Everything the ladder saw. Feeds the review queue and the audit log."""

    match: CompanyMatch
    normalised: str
    candidates: tuple[Candidate, ...] = ()
    eliminated: tuple[tuple[str, str], ...] = ()
    """``(entity_id, reason)`` for every entity that dropped out."""
    unexpected_tokens: tuple[str, ...] = ()

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(c.entity_id for c in self.candidates)


class CompanyMatcher:
    """The deterministic ladder, bound to one registry.

    Memoised on the raw string: R5 measured 30,000 rows at 2.07 s uncached and
    9 ms cached (220x), because a real ``Connections.csv`` holds only a few
    hundred distinct employer strings.
    """

    def __init__(
        self,
        registry: TargetRegistry | None = None,
        *,
        decisions: Mapping[str, Firm | None] | None = None,
        memoise: bool | None = None,
    ) -> None:
        self.registry = registry if registry is not None else load_registry()
        self.policy = self.registry.match_policy
        self._decisions: dict[str, Firm | None] = dict(decisions or {})
        self._memoise = self.policy.memoise if memoise is None else memoise
        self._cache: dict[str, MatchExplanation] = {}

    # -- public API -------------------------------------------------------

    def classify(self, raw: str | None) -> CompanyMatch:
        """Resolve one employer string. Never raises on user input."""
        return self.explain(raw).match

    def classify_many(self, values: Iterable[str | None]) -> tuple[CompanyMatch, ...]:
        return tuple(self.classify(v) for v in values)

    def explain(self, raw: str | None) -> MatchExplanation:
        """Resolve, and keep the working — candidates, eliminations, tokens."""
        key = raw or ""
        if self._memoise:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        result = self._classify_uncached(key)
        if self._memoise:
            self._cache[key] = result
        return result

    def set_decision(self, normalised_key: str, firm: Firm | None) -> None:
        """Record a human adjudication, keyed on the *normalised* string.

        A cached decision short-circuits the whole ladder, so each distinct
        string is adjudicated once, ever. Callers must log the hit (PRIV-15) so
        a wrong past decision stays traceable.
        """
        self._decisions[normalised_key] = firm
        self._cache.clear()

    def load_decisions(self, decisions: Mapping[str, Firm | None]) -> None:
        self._decisions.update(decisions)
        self._cache.clear()

    @property
    def decisions(self) -> Mapping[str, Firm | None]:
        return dict(self._decisions)

    # -- the ladder -------------------------------------------------------

    def _classify_uncached(self, raw: str) -> MatchExplanation:
        text = normalise_company(raw)

        # Rung 0. Empty guard.
        if not text:
            return MatchExplanation(
                match=CompanyMatch(raw=raw, status=MatchStatus.NO_MATCH, rule=RULE_EMPTY),
                normalised=text,
            )

        # A human decision, if one exists, outranks the ladder entirely.
        if text in self._decisions:
            firm = self._decisions[text]
            status = MatchStatus.CONFIDENT if firm is not None else MatchStatus.NO_MATCH
            return MatchExplanation(
                match=CompanyMatch(
                    raw=raw,
                    status=status,
                    firm=firm,
                    rule=RULE_CACHED,
                    score=100.0 if firm is not None else None,
                ),
                normalised=text,
            )

        tokens = _split_tokens(text)
        bare_tokens = tuple(
            t for t in tokens if t not in self.registry.normalisation.legal_suffixes
        )
        norm = NormalisedCompany(raw=raw, text=text, tokens=tokens, bare_tokens=bare_tokens)

        candidates: list[Candidate] = []
        eliminated: list[tuple[str, str]] = []
        for entity in self.registry:
            candidate, reason = self._score_entity(entity, norm)
            if candidate is None:
                eliminated.append((entity.id, reason))
            else:
                candidates.append(candidate)

        return self._decide(raw, norm, tuple(candidates), tuple(eliminated))

    def _score_entity(
        self, entity: TargetEntity, norm: NormalisedCompany
    ) -> tuple[Candidate | None, str]:
        """Run one entity down the ladder. Returns its candidate, or why not."""
        # Rung 1. Negative patterns. The only unconditional veto.
        for pattern in entity.negative_patterns:
            if pattern.search(norm.text):
                return None, RULE_NEGATIVE

        # Rung 2. Review-only aliases. Checked against `bare`, not `head`:
        # noise_tokens would otherwise delete the very token being tested (JS).
        bare = norm.bare
        if bare and bare in entity.review_only_aliases:
            return (
                Candidate(
                    entity_id=entity.id,
                    firm=entity.firm,
                    score=100.0,
                    rung=RULE_REVIEW_ONLY,
                    alias=bare,
                    forced_review=True,
                    review_only=True,
                    head=bare,
                ),
                "",
            )

        head_tokens = tuple(
            t
            for t in norm.bare_tokens
            if t not in self.registry.normalisation.modifier_tokens and t not in entity.noise_tokens
        )
        head = " ".join(head_tokens)

        # Rung 3. Anchor, or the typo'd-anchor fallback (review-only).
        forced_review = False
        if not any(pattern.search(norm.text) for pattern in entity.anchors):
            if not self._fuzzy_anchor(entity, head_tokens):
                return None, RULE_NO_ANCHOR
            # A typo'd anchor can reach review. It can never reach confident.
            forced_review = True

        # Rung 4. Discriminators, on the normalised string so they survive
        # concatenation ("citadelsecurities" must still trip `securit\w*`).
        if entity.requires is not None and not entity.requires.search(norm.text):
            return None, "missing_requires"
        if entity.excludes is not None and entity.excludes.search(norm.text):
            return None, "hit_excludes"

        if not head_tokens:
            return None, "empty_head"

        # Rung 5. Exact alias, spaced or concatenated.
        squashed = head.replace(" ", "")
        for alias in entity.aliases:
            if head == alias or squashed == alias.replace(" ", ""):
                return (
                    Candidate(
                        entity_id=entity.id,
                        firm=entity.firm,
                        score=100.0,
                        rung=RULE_EXACT_ALIAS,
                        alias=alias,
                        forced_review=forced_review,
                        head=head,
                    ),
                    "",
                )

        # Rung 6. Anchored prefix, longest alias first. This is what lets
        # "Citadel | Ken Griffin" reach review instead of vanishing.
        for alias_tokens in entity.alias_tokens:
            n = len(alias_tokens)
            if len(head_tokens) > n and head_tokens[:n] == alias_tokens:
                alias = " ".join(alias_tokens)
                return (
                    Candidate(
                        entity_id=entity.id,
                        firm=entity.firm,
                        score=100.0,
                        rung=RULE_ANCHORED_PREFIX,
                        alias=alias,
                        forced_review=forced_review,
                        unexpected=self._unexpected_tokens(entity, alias, head_tokens[n:]),
                        head=head,
                    ),
                    "",
                )

        # Rung 7. Fuzzy backstop. token_sort_ratio, never token_set_ratio:
        # token_set_ratio scored "Citadel Securites" at 100 against the hedge
        # fund and routed a market-maker employee into the wrong bucket.
        best_alias = ""
        best_score = -1.0
        for alias in entity.aliases:
            score = float(fuzz.token_sort_ratio(head, alias))
            if score > best_score:
                best_score, best_alias = score, alias

        return (
            Candidate(
                entity_id=entity.id,
                firm=entity.firm,
                score=max(best_score, 0.0),
                rung=RULE_FUZZY,
                alias=best_alias,
                forced_review=forced_review,
                unexpected=self._unexpected_tokens(entity, best_alias, head_tokens),
                head=head,
            ),
            "",
        )

    def _fuzzy_anchor(self, entity: TargetEntity, head_tokens: Sequence[str]) -> bool:
        """True when every required anchor token has a near-miss in the head.

        Requiring *all* of them is what makes ``Jane Iredale`` (has ``jane``,
        lacks ``street``) fall through while ``Citdel Securities`` survives to
        review.
        """
        if not head_tokens:
            return False
        floor = self.policy.fuzzy_anchor_min
        return all(
            any(fuzz.ratio(required, token) >= floor for token in head_tokens)
            for required in entity.anchor_required
        )

    def _unexpected_tokens(
        self, entity: TargetEntity, alias: str, tokens: Sequence[str]
    ) -> tuple[str, ...]:
        """Tokens that do not belong to the matched alias's extension vocabulary.

        Scoped to aliases extending the *matched* alias, never the whole entity
        vocabulary — that bug scored ``Citadel Capital`` confident because
        ``capital`` appears in ``Surveyor Capital``. The forgiveness ratio
        admits ``Jane Street Captial`` (``captial``/``capital`` = 85.7) while
        still rejecting ``Citadel Technology``.
        """
        if not tokens:
            return ()
        vocab = entity.alias_extension_vocab(alias)
        if not vocab:
            return tuple(tokens)
        forgiveness = self.policy.unexpected_token_forgiveness
        return tuple(
            token
            for token in tokens
            if token not in vocab
            and max(fuzz.ratio(token, known) for known in vocab) < forgiveness
        )

    # -- decision ---------------------------------------------------------

    def _decide(
        self,
        raw: str,
        norm: NormalisedCompany,
        candidates: tuple[Candidate, ...],
        eliminated: tuple[tuple[str, str], ...],
    ) -> MatchExplanation:
        if not candidates:
            vetoed = any(reason == RULE_NEGATIVE for _, reason in eliminated)
            return MatchExplanation(
                match=CompanyMatch(
                    raw=raw,
                    status=MatchStatus.NO_MATCH,
                    rule=RULE_NEGATIVE if vetoed else RULE_NO_ANCHOR,
                ),
                normalised=norm.text,
                eliminated=eliminated,
            )

        # Stable sort: equal scores keep registry order, which is the
        # documented global tie-break.
        ranked = tuple(sorted(candidates, key=lambda c: -c.score))
        leader = ranked[0]
        entity = self.registry.by_id(leader.entity_id)

        def result(status: MatchStatus, rule: str, *, firm: Firm | None) -> MatchExplanation:
            return MatchExplanation(
                match=CompanyMatch(
                    raw=raw, status=status, firm=firm, rule=rule, score=leader.score
                ),
                normalised=norm.text,
                candidates=ranked,
                eliminated=eliminated,
                unexpected_tokens=leader.unexpected,
            )

        # Two entities too close to call: review, with both ids recorded.
        if len(ranked) > 1 and leader.score - ranked[1].score <= self.policy.ambiguity_margin:
            both = "|".join(sorted({leader.entity_id, ranked[1].entity_id}))
            return result(
                MatchStatus.NEEDS_REVIEW, f"{RULE_AMBIGUOUS}:{both}", firm=leader.firm
            )

        if leader.review_only:
            return result(MatchStatus.NEEDS_REVIEW, RULE_REVIEW_ONLY, firm=leader.firm)

        if leader.forced_review:
            return result(
                MatchStatus.NEEDS_REVIEW, f"{leader.rung}/{RULE_FUZZY_ANCHOR}", firm=leader.firm
            )

        if leader.unexpected:
            listed = ",".join(leader.unexpected)
            return result(
                MatchStatus.NEEDS_REVIEW,
                f"{leader.rung}/{RULE_UNEXPECTED}:{listed}",
                firm=leader.firm,
            )

        if leader.score >= entity.threshold:
            return result(MatchStatus.CONFIDENT, leader.rung, firm=leader.firm)

        if leader.score >= entity.review_floor:
            return result(
                MatchStatus.NEEDS_REVIEW, f"{leader.rung}/{RULE_BELOW_THRESHOLD}", firm=leader.firm
            )

        return MatchExplanation(
            match=CompanyMatch(
                raw=raw, status=MatchStatus.NO_MATCH, rule=RULE_BELOW_FLOOR, score=leader.score
            ),
            normalised=norm.text,
            candidates=ranked,
            eliminated=eliminated,
        )


# ---------------------------------------------------------------------------
# The PRIV-20 barrier
# ---------------------------------------------------------------------------


def is_admissible(match: CompanyMatch) -> bool:
    """True only for ``CONFIDENT``.

    ``needs_review`` is a barrier, not a hint: an ambiguous match must not
    enter the analysis graph until a human adjudicates it (PRIV-20).
    """
    return match.status is MatchStatus.CONFIDENT and match.firm is not None


def require_admissible(match: CompanyMatch) -> Firm:
    """Return the firm, or refuse. The only sanctioned way into the graph."""
    if match.status is MatchStatus.NEEDS_REVIEW:
        raise NeedsReviewError(
            f"{match.raw!r} is needs_review ({match.rule}) and cannot enter the graph "
            "until it is adjudicated (PRIV-20); route it through "
            "interlayer.ingest.review.ReviewQueue"
        )
    if match.status is MatchStatus.NO_MATCH or match.firm is None:
        raise ValueError(f"{match.raw!r} resolved to no target firm ({match.rule})")
    return match.firm


@dataclass(frozen=True, slots=True)
class MatchPartition:
    """Company matches split by admissibility, for the graph boundary."""

    confident: tuple[CompanyMatch, ...] = field(default=())
    needs_review: tuple[CompanyMatch, ...] = field(default=())
    no_match: tuple[CompanyMatch, ...] = field(default=())

    @property
    def unadjudicated_count(self) -> int:
        """Held out of the graph and reported alongside the results (PRIV-20)."""
        return len(self.needs_review)


def partition(matches: Iterable[CompanyMatch]) -> MatchPartition:
    """Split matches three ways. Only ``confident`` may reach the graph."""
    buckets: dict[MatchStatus, list[CompanyMatch]] = {
        MatchStatus.CONFIDENT: [],
        MatchStatus.NEEDS_REVIEW: [],
        MatchStatus.NO_MATCH: [],
    }
    for match in matches:
        buckets[match.status].append(match)
    return MatchPartition(
        confident=tuple(buckets[MatchStatus.CONFIDENT]),
        needs_review=tuple(buckets[MatchStatus.NEEDS_REVIEW]),
        no_match=tuple(buckets[MatchStatus.NO_MATCH]),
    )


__all__ = [
    "RULE_AMBIGUOUS",
    "RULE_ANCHORED_PREFIX",
    "RULE_BELOW_FLOOR",
    "RULE_BELOW_THRESHOLD",
    "RULE_CACHED",
    "RULE_EMPTY",
    "RULE_EXACT_ALIAS",
    "RULE_FUZZY",
    "RULE_FUZZY_ANCHOR",
    "RULE_NEGATIVE",
    "RULE_NO_ANCHOR",
    "RULE_REVIEW_ONLY",
    "RULE_UNEXPECTED",
    "Candidate",
    "CompanyMatcher",
    "MatchExplanation",
    "MatchPartition",
    "NormalisedCompany",
    "is_admissible",
    "normalise_company",
    "partition",
    "require_admissible",
]
