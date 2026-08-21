"""Resolve one raw employer/school string to a gazetteer entity.

``rapidfuzz.fuzz.WRatio``, accept at 90, review 84-89, reject below -- but the
scorer is the least important part of this file. Measured over 94 curated cases,
``WRatio`` used bare was the *worst* option available (precision 0.591). Behind
the negative gazetteer and the containment guard, every scorer tested reached
precision 1.000 at every threshold in the band. The scorer supplies recall; the
gazetteer and the guard supply precision.

Why no subset-tolerant scorer may be used as a bare accept signal: ``Citadel``
against ``Citadel Broadcasting`` scores **100** on ``partial_ratio``,
``token_set_ratio`` *and* ``token_ratio``, because whenever one string's tokens
are a subset of the other's those scorers saturate regardless of how
discriminating the extra tokens are. ``Citadel Broadcasting`` is a defunct radio
network; ``Citadel`` is the fund. The containment guard exists precisely to look
at the extra tokens the scorer is ignoring.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from interlayer.models import AffiliationKind, MatchVerdict
from interlayer.normalize.gazetteer import FIRM_TIERS, Gazetteer
from interlayer.normalize.normalize import STRUCTURAL_NOUNS, initials, norm, norm_raw, tokens

__all__ = [
    "ALLOW_TOKENS",
    "GUARD_TOKEN_THRESHOLD",
    "MatchResult",
    "containment_guard",
    "resolve",
]

GUARD_TOKEN_THRESHOLD = 80.0
"""Per-token alignment cutoff inside the guard. An exact token comparison
collapsed recall to 8% under single-character corruptions; fuzzy alignment at 80
restores it to 68-74% while still rejecting every wrong-firm case measured."""

ALLOW_TOKENS: frozenset[str] = frozenset(
    {
        # geographies -- a firm's country arm is the same firm
        "global",
        "international",
        "americas",
        "us",
        "usa",
        "uk",
        "europe",
        "european",
        "asia",
        "apac",
        "pacific",
        "emea",
        "japan",
        "china",
        "hong",
        "kong",
        "singapore",
        "india",
        "australia",
        "netherlands",
        "ireland",
        "london",
        "york",
        "new",
        "chicago",
        "miami",
        "amsterdam",
        "austin",
        "stamford",
        # generic finance nouns -- non-discriminating between quant firms
        "trading",
        "markets",
        "capital",
        "management",
        "investments",
        "financial",
        # structural words
        "group",
        "holdings",
        "company",
        "a",
        "the",
        "and",
        "at",
        "of",
    }
)
"""Closed list of tokens that may differ between a query and an alias without
changing which firm is meant.

What is *absent* is the point: ``broadcasting``, ``securities``, ``credit``,
``union``, ``defense``, ``servicing``, ``coffee``, ``entertainment``, ``sauer``
and ``ventures`` are all deliberately missing, which is exactly why
``Citadel`` -> ``Citadel Broadcasting`` and ``Two Sigma`` -> ``Two Sigma
Ventures`` are rejected. ``securities`` in particular must never be added: it is
the only token separating Citadel LLC from Citadel Securities.
"""


@dataclass(frozen=True, slots=True)
class MatchResult:
    """What resolution concluded, and enough of why to audit it.

    ``verdict`` is about *target-firm* resolution, per ``MatchVerdict``'s own
    docstring. ``entity_key`` may still be set on a REJECT: identifying a string
    as Citadel Broadcasting is a confident identification and an explicit no at
    the same time, and naming it lets every spelling of it collapse to one Org.
    """

    verdict: MatchVerdict
    entity_key: str | None
    tier: str | None
    score: float
    reason: str
    alias: str | None = None
    alternatives: tuple[str, ...] = ()
    """Entity keys the string could equally be, for a REVIEW an operator has to
    settle. ``entity_key`` stays None in that case on purpose: an ambiguous
    string must not be filed under either candidate while it is still ambiguous.
    """

    @property
    def firm_key(self) -> str | None:
        """The gazetteer key when this is an accepted firm identification."""
        if self.verdict is MatchVerdict.ACCEPT and self.tier in FIRM_TIERS:
            return self.entity_key
        return None


def containment_guard(
    query: str,
    alias: str,
    *,
    token_threshold: float = GUARD_TOKEN_THRESHOLD,
) -> bool:
    """True when every unmatched token on either side is known filler.

    Greedily align each query token to its best unused alias token by
    ``fuzz.ratio``; anything left over on either side must be in
    :data:`ALLOW_TOKENS`. This is what the similarity score cannot see: the
    scorer reports how much of the two strings agree, the guard asks what the
    disagreement *was*.

    Alignment is fuzzy rather than exact so a typo inside an otherwise-aligned
    token does not manufacture an extra.
    """
    q_tokens = query.split()
    a_tokens = alias.split()
    used: set[int] = set()
    extra_q: list[str] = []
    for token in q_tokens:
        best_score = -1.0
        best_index: int | None = None
        for index, candidate in enumerate(a_tokens):
            if index in used:
                continue
            score = fuzz.ratio(token, candidate)
            if score > best_score:
                best_score, best_index = score, index
        if best_index is not None and best_score >= token_threshold:
            used.add(best_index)
        else:
            extra_q.append(token)
    extra_a = [tok for index, tok in enumerate(a_tokens) if index not in used]
    extras = (set(extra_q) | set(extra_a)) - {initials(q_tokens), initials(a_tokens)}
    return extras <= ALLOW_TOKENS


def _in_scope(keys: tuple[str, ...] | None, scope: frozenset[str]) -> tuple[str, ...]:
    return tuple(k for k in keys or () if k in scope)


def _identify_negative(
    gaz: Gazetteer, scope: frozenset[str], *raw_forms: str
) -> tuple[str | None, str | None]:
    """Name the non-target entity a vetoed string actually denotes, if we know it.

    Purely cosmetic for the verdict -- the string is rejected either way -- but
    it lets ``Citadel Broadcasting`` and ``Citadel Broadcasting Corporation``
    land on one canonical Org instead of two look-alike strays. Bucket entries
    are skipped: they veto, they never name.
    """
    for form in raw_forms:
        for key in _in_scope(gaz.exact.get(form), scope):
            entity = gaz.entities[key]
            if not entity.is_firm and entity.is_specific_org:
                return key, entity.tier
    return None, None


def resolve(
    text: str,
    *,
    field: AffiliationKind,
    gaz: Gazetteer,
    accept: float = 90.0,
    review: float = 84.0,
    allow_weak: bool = False,
    corroborating_keys: frozenset[str] = frozenset(),
) -> MatchResult:
    """Resolve ``text``, read from a ``field``-kind column, against the gazetteer.

    The order of the steps is itself a decision. Vetoes run **before** exact
    positive hits because ``The Citadel`` normalises straight onto ``Citadel``'s
    exact key: a veto that ran second would never fire. Field scoping runs before
    everything, because a university is not a candidate for an employer string
    and a market maker is not a candidate for a school.
    """
    raw = norm_raw(text)
    raw_full = norm_raw(text, segment=False)
    folded = norm(text)
    if not folded:
        return MatchResult(MatchVerdict.REJECT, None, None, 0.0, "empty")

    scope = gaz.scope(field)

    # 1. The ambiguous case, before any veto can swallow it. A bare "The Citadel"
    #    in an employer field is genuinely under-determined: the article says
    #    college, the field says employer. Route it to review with both
    #    candidates named -- never silently accept, never silently drop.
    if field is AffiliationKind.EMPLOYMENT:
        colleges = _in_scope(gaz.education_raw.get(raw), gaz.scope(AffiliationKind.EDUCATION))
        firms = [k for k in _in_scope(gaz.exact.get(folded), scope) if gaz.entities[k].is_firm]
        if colleges and firms:
            return MatchResult(
                MatchVerdict.REVIEW,
                None,
                None,
                100.0,
                f"ambiguous:{colleges[0]}|{firms[0]}",
                alias=raw,
                alternatives=(colleges[0], firms[0]),
            )

    # 2-3. Negative gazetteer. A veto only fires for an entity that could have
    #      been matched in this field at all: "The Citadel" is a veto against the
    #      fund, and the fund is not a candidate for a school column.
    for probe in (raw, folded):
        owners = _in_scope(gaz.neg_exact.get(probe), scope)
        if owners:
            key, tier = _identify_negative(gaz, scope, raw, folded)
            return MatchResult(MatchVerdict.REJECT, key, tier, 100.0, f"negative_exact:{owners[0]}")
    for pattern, owner in gaz.neg_patterns:
        if owner not in scope:
            continue
        if pattern.search(raw) or pattern.search(folded) or pattern.search(raw_full):
            key, tier = _identify_negative(gaz, scope, raw, folded)
            return MatchResult(MatchVerdict.REJECT, key, tier, 100.0, f"negative_pattern:{owner}")

    # 4. Exact alias hit, under either normalisation.
    #
    #    A hit on the RAW form matched a listed alias as written. A hit only on
    #    the FOLDED form was reached by discarding tokens, and that is not the
    #    same thing: "Citadel Holdings" and "Citadel Holding Corporation" fold
    #    onto "citadel" and were accepted as the fund at full confidence, though
    #    the latter is a real unrelated company. Where the discarded token could
    #    name a different corporate entity, the answer is genuinely
    #    under-determined, so it goes to review rather than being asserted --
    #    the same treatment "The Citadel" already gets in step 1.
    for probe, tag in ((raw, "exact_raw"), (folded, "exact")):
        keys = _in_scope(gaz.exact.get(probe), scope)
        if not keys:
            continue
        entity = gaz.entities[keys[0]]
        if not entity.is_firm:
            return MatchResult(
                MatchVerdict.REJECT, entity.key, entity.tier, 100.0, f"{tag}:{entity.key}", probe
            )
        discarded = set(tokens(raw)) - set(tokens(folded))
        shifted = discarded & STRUCTURAL_NOUNS
        if probe == folded and shifted:
            return MatchResult(
                MatchVerdict.REVIEW,
                None,
                None,
                100.0,
                f"structural_noun_stripped:{entity.key}:{'|'.join(sorted(shifted))}",
                alias=probe,
                alternatives=(entity.key,),
            )
        return MatchResult(
            MatchVerdict.ACCEPT, entity.key, entity.tier, 100.0, f"{tag}:{entity.key}", probe
        )

    # 5. Quarantined aliases: a bare initialism is never enough on its own.
    #
    #    The quarantine applies whether or not weak matching is enabled. Checking
    #    it only under ``allow_weak`` let the query fall through to the fuzzy
    #    sweep instead, where "SIG" scored against a longer alias containing it
    #    and was accepted outright -- the quarantine bypassed by the very path it
    #    exists to prevent. ``allow_weak`` governs whether corroboration can
    #    PROMOTE a weak hit, not whether the restriction is enforced.
    for probe in (raw, folded):
        keys = _in_scope(gaz.weak_exact.get(probe), scope)
        if not keys:
            continue
        entity = gaz.entities[keys[0]]
        if allow_weak and entity.key in corroborating_keys and entity.is_firm:
            return MatchResult(
                MatchVerdict.ACCEPT,
                entity.key,
                entity.tier,
                100.0,
                f"weak_alias_corroborated:{entity.key}",
                probe,
            )
        # The candidate is named on the result even though the verdict is review:
        # the reviewer needs to know what it was nearly matched to. Attribution is
        # withheld downstream by verdict, not by hiding the candidate here.
        return MatchResult(
            MatchVerdict.REVIEW,
            entity.key,
            entity.tier,
            100.0,
            f"weak_alias_uncorroborated:{entity.key}",
            probe,
            alternatives=(entity.key,),
        )

    # 6. Fuzzy sweep. Every candidate at the top score is kept, not just the
    #    first one seen. A firm with both a long and a short alias scores the
    #    same on each ("jane street" and "jane st" both hit 90 for "Jane Street
    #    London"), and if the short one happens to sort first, the guard then
    #    rejects "street" as an unaligned extra and a real contact disappears.
    #    The index is pre-sorted by preference, so order within a score band
    #    stays deterministic.
    scored: list[tuple[float, str, str]] = []
    for alias, key in gaz.fuzzy:
        if key not in scope:
            continue
        scored.append((fuzz.WRatio(folded, alias), alias, key))

    if not scored:
        return MatchResult(MatchVerdict.REJECT, None, None, 0.0, "below_threshold")

    best_score = max(score for score, _, _ in scored)
    if best_score < review:
        return MatchResult(MatchVerdict.REJECT, None, None, best_score, "below_threshold")

    # 7. The guard, which is not optional: without it this is the worst matcher
    #    measured, and with it the wrong-firm rate is zero. Applied to each
    #    top-scoring candidate in preference order; the first survivor wins.
    tied = [(alias, key) for score, alias, key in scored if score == best_score]
    best_alias = best_key = None
    for alias, key in tied:
        if containment_guard(folded, alias):
            best_alias, best_key = alias, key
            break

    if best_key is None or best_alias is None:
        rejected_alias, rejected_key = tied[0]
        return MatchResult(
            MatchVerdict.REJECT,
            None,
            None,
            best_score,
            f"guard_reject:{rejected_key}({rejected_alias})",
        )

    entity = gaz.entities[best_key]
    if not entity.is_firm:
        # An explicit, auditable no, naming what it was rejected as.
        return MatchResult(
            MatchVerdict.REJECT,
            entity.key,
            entity.tier,
            best_score,
            f"negative_entity:{entity.key}",
            best_alias,
        )
    verdict = MatchVerdict.ACCEPT if best_score >= accept else MatchVerdict.REVIEW
    return MatchResult(
        verdict, entity.key, entity.tier, best_score, f"fuzzy:{entity.key}", best_alias
    )
