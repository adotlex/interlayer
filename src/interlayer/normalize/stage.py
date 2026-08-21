"""The ``normalize`` stage: raw strings in, canonical orgs and affiliations out.

Reads ``people.jsonl`` (so running out of order raises ``StageInputMissingError``
rather than producing an empty graph), resolves every employer and school string
against the gazetteer, and writes:

* ``orgs.jsonl`` -- one canonical ``Org`` per resolved entity, plus one per
  distinct unresolved surface form so that people who share an employer the
  gazetteer has never heard of still share an org.
* ``affiliations.jsonl`` -- one ``Affiliation`` per position and per education,
  carrying the dates through unchanged. The co-tenure filter downstream removes
  around 62% of candidate edges, and it can only do that if the dates survive
  this stage.
* ``review_queue.jsonl`` -- every REVIEW verdict, with the candidates and the
  reason. A match nobody can see is a match nobody can correct.

Everything is sorted before it is written. ``PYTHONHASHSEED`` produced four
distinct set-iteration orders across four values during Wave 1, so an unsorted
artifact is not reproducible even from identical input.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from interlayer.config import Settings
from interlayer.io import read_jsonl, write_jsonl
from interlayer.models import (
    SCHEMA_VERSION,
    Affiliation,
    AffiliationKind,
    ApproxDate,
    MatchVerdict,
    Org,
    OrgKind,
    Person,
    Seniority,
    TargetFirm,
    stable_id,
)
from interlayer.normalize.gazetteer import Entity, Gazetteer, load_gazetteer
from interlayer.normalize.match import MatchResult, resolve
from interlayer.normalize.normalize import norm
from interlayer.normalize.titles import extract_role, extract_seniority, role_weight

__all__ = ["ReviewItem", "run"]

log = logging.getLogger(__name__)

_FINANCE_TYPES = frozenset({"fund", "market_maker", "bank", "broker"})


class ReviewItem(BaseModel):
    """One resolution a human should look at before it is believed.

    Not part of ``models.py``: ``review_queue.jsonl`` is this stage's own
    artifact and no other stage consumes it. It exists because the alternative
    to a review queue is a silent accept, and a silent accept of ``The Citadel``
    in an employer field puts a military-college alumnus into a hedge-fund
    cluster.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    person_id: str
    field_kind: AffiliationKind
    raw_text: str
    normalized: str
    verdict: MatchVerdict
    score: float
    reason: str
    candidate_key: str | None = None
    candidate_name: str | None = None
    candidate_names: tuple[str, ...] = ()
    """Every entity the string could be, when it could be more than one."""

    suggested_target_firm: TargetFirm | None = None
    title_raw: str = ""
    seniority: Seniority = Seniority.UNKNOWN

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (self.person_id, str(self.field_kind), self.raw_text, self.reason)


@dataclass(slots=True)
class _OrgDraft:
    """An org under construction, before its display name is settled."""

    kind: OrgKind
    surface_forms: Counter[str] = field(default_factory=Counter)
    canonical_name: str | None = None
    linkedin_url: str | None = None
    domain: str | None = None
    is_target: bool = False
    target_firm: TargetFirm | None = None

    def display_name(self) -> str:
        """Gazetteer name when we have one, else the commonest spelling seen.

        Ties break lexicographically rather than by insertion order, because
        insertion order depends on input ordering and this has to be stable.
        """
        if self.canonical_name:
            return self.canonical_name
        return min(self.surface_forms.items(), key=lambda item: (-item[1], item[0]))[0]

    def merge_kind(self, other: OrgKind) -> None:
        if self.kind is other:
            return
        # A name used as both an employer and a school is not confidently either.
        self.kind = OrgKind.UNKNOWN if OrgKind.UNKNOWN not in (self.kind, other) else self.kind


@dataclass(frozen=True, slots=True)
class _Claim:
    """One resolved position or education, awaiting an org id."""

    person_id: str
    group_key: str
    kind: AffiliationKind
    title: str | None
    start: ApproxDate | None
    end: ApproxDate | None
    is_current: bool
    weight: float


def _org_entity(result: MatchResult, gaz: Gazetteer) -> Entity | None:
    """The entity this string's Org should be built from, if any.

    A bucket entry (``jane_street_generic``, ``citadel_security_generic``) is a
    veto, not an organisation: matching one tells us the string is *not* the
    firm, and nothing about who the employer actually is. Those fall back to a
    surface-form Org so a coffee shop and a dental practice on the same road
    stay separate employers.

    A REVIEW verdict likewise builds no gazetteer Org. Review means the match is
    not established, and ``Settings.match_review`` says such a match is "never
    auto-applied" -- but the candidate key travels on the result for the queue's
    benefit, and reading it here silently attached the person to the target firm
    at full strength anyway. The suggestion belongs in the queue; the attribution
    waits for a human.
    """
    if result.entity_key is None or result.verdict is MatchVerdict.REVIEW:
        return None
    entity = gaz.entities.get(result.entity_key)
    return entity if entity is not None and entity.is_specific_org else None


def _group_key(entity: Entity | None, folded: str) -> str:
    """Where this string's org lives: a gazetteer entity, or its own surface form."""
    return f"gaz:{entity.key}" if entity is not None else f"raw:{folded}"


def _confidence(result: MatchResult, entity: Entity | None) -> float:
    """How sure we are that the affiliation points at the right org.

    A string with no gazetteer candidate at all gets 1.0: the person really did
    write that employer, and the org built from it is that string and nothing
    more. Only a gazetteer identification can be *wrong*, so only a gazetteer
    identification is damped.

    A review-band match is damped even though it builds no gazetteer Org. The
    identification is uncertain, and dropping the damping along with the Org
    would report that uncertainty as if it were an ordinary unresolved employer.
    """
    if entity is None and result.entity_key is None:
        return 1.0
    return min(1.0, result.score / 100.0)


def _is_finance(entity: Entity | None) -> bool:
    return entity is not None and entity.entity_type in _FINANCE_TYPES


def _resolve_person_strings(
    person: Person,
    gaz: Gazetteer,
    cfg: Settings,
    cache: dict[tuple[str, AffiliationKind], MatchResult],
) -> list[tuple[AffiliationKind, str, str, MatchResult]]:
    """Resolve every string on one person, in two passes.

    Pass one runs with the quarantined aliases disabled. Pass two re-runs only
    the strings that resolved to nothing, this time allowing an exact hit on a
    weak alias -- but only when some *other* string on the same profile already
    named that firm. ``JS`` on its own is JavaScript far more often than it is
    Jane Street; ``JS`` next to a confirmed Jane Street position is not.
    """
    strings: list[tuple[AffiliationKind, str, str]] = []
    for position in person.positions:
        if position.company_raw.strip():
            strings.append((AffiliationKind.EMPLOYMENT, position.company_raw, position.title_raw))
    for education in person.educations:
        if education.school_raw.strip():
            title = education.degree or education.field_of_study or ""
            strings.append((AffiliationKind.EDUCATION, education.school_raw, title))

    first: list[MatchResult] = []
    for kind, raw_text, _title in strings:
        key = (raw_text, kind)
        result = cache.get(key)
        if result is None:
            result = resolve(
                raw_text,
                field=kind,
                gaz=gaz,
                accept=cfg.match_accept,
                review=cfg.match_review,
            )
            cache[key] = result
        first.append(result)

    corroborating = frozenset(firm for r in first if (firm := r.firm_key) is not None)
    out: list[tuple[AffiliationKind, str, str, MatchResult]] = []
    for (kind, raw_text, title), result in zip(strings, first, strict=True):
        # A quarantined initialism resolves to REVIEW, not REJECT -- that is the
        # quarantine working. Gating the retry on REJECT alone meant corroboration
        # could never promote one, so "JS" beside "Jane Street" on the same
        # profile stayed unresolved.
        weak_pending = result.reason.startswith("weak_alias_uncorroborated")
        retryable = (
            corroborating
            and (result.entity_key is None or weak_pending)
            and (result.verdict is MatchVerdict.REJECT or weak_pending)
            and not result.reason.startswith("negative_")
        )
        if retryable:
            retry = resolve(
                raw_text,
                field=kind,
                gaz=gaz,
                accept=cfg.match_accept,
                review=cfg.match_review,
                allow_weak=True,
                corroborating_keys=corroborating,
            )
            if retry.entity_key is not None:
                result = retry
        out.append((kind, raw_text, title, result))
    return out


def _record_org(
    drafts: dict[str, _OrgDraft],
    group: str,
    surface: str,
    kind: AffiliationKind,
    entity: Entity | None,
) -> None:
    org_kind = entity.org_kind if entity else _default_kind(kind)
    draft = drafts.get(group)
    if draft is None:
        draft = _OrgDraft(kind=org_kind)
        if entity is not None:
            draft.canonical_name = entity.canonical_name
            draft.linkedin_url = entity.linkedin_url
            draft.domain = entity.domain
            draft.is_target = entity.is_target
            draft.target_firm = entity.target_firm
        drafts[group] = draft
    else:
        draft.merge_kind(org_kind)
    draft.surface_forms[surface] += 1


def _default_kind(kind: AffiliationKind) -> OrgKind:
    return OrgKind.SCHOOL if kind is AffiliationKind.EDUCATION else OrgKind.COMPANY


def _build_orgs(drafts: dict[str, _OrgDraft]) -> tuple[dict[str, str], list[Org]]:
    """Turn drafts into ``Org``s, returning the group -> org_id mapping too.

    Two drafts can land on the same ``org_id`` when their display names differ
    only in punctuation, because ``stable_id`` folds the name it hashes. That is
    a merge, not a collision, so the surviving Org keeps the union of what both
    knew -- but a gazetteer-derived record wins on ``kind``. Somebody listing
    "Jane Street" in an education field (the firm runs teaching programmes) must
    not turn the canonical Jane Street company record into an ``UNKNOWN``.
    Gazetteer groups are keyed ``gaz:`` and surface forms ``raw:``, so iterating
    in sorted order puts the canonical record first every time.
    """
    by_id: dict[str, Org] = {}
    canonical_ids: set[str] = set()
    group_to_id: dict[str, str] = {}
    for group in sorted(drafts):
        draft = drafts[group]
        name = draft.display_name()
        org_id = stable_id("org", name)
        aliases = tuple(sorted(draft.surface_forms))
        from_gazetteer = draft.canonical_name is not None
        existing = by_id.get(org_id)
        if existing is not None:
            merged_aliases = tuple(sorted(set(existing.aliases) | set(aliases)))
            if org_id in canonical_ids:
                kind = existing.kind
            elif from_gazetteer:
                kind = draft.kind
            else:
                kind = existing.kind if existing.kind is draft.kind else OrgKind.UNKNOWN
            org = existing.model_copy(
                update={
                    "aliases": merged_aliases,
                    "is_target": existing.is_target or draft.is_target,
                    "target_firm": existing.target_firm or draft.target_firm,
                    "kind": kind,
                    "linkedin_url": existing.linkedin_url or draft.linkedin_url,
                    "domain": existing.domain or draft.domain,
                }
            )
        else:
            org = Org(
                org_id=org_id,
                name=name,
                kind=draft.kind,
                aliases=aliases,
                linkedin_url=draft.linkedin_url,
                domain=draft.domain,
                is_target=draft.is_target,
                target_firm=draft.target_firm,
            )
        by_id[org_id] = org
        if from_gazetteer:
            canonical_ids.add(org_id)
        group_to_id[group] = org_id
    return group_to_id, [by_id[k] for k in sorted(by_id)]


def _date_key(value: ApproxDate | None) -> int:
    return value.sort_key if value is not None else -1


def run(cfg: Settings) -> None:
    """Resolve employers and schools, then write orgs, affiliations and reviews."""
    people = read_jsonl(cfg.people_path, Person, produced_by="ingest")
    gaz = load_gazetteer(cfg.gazetteer_path)

    drafts: dict[str, _OrgDraft] = {}
    claims: list[_Claim] = []
    reviews: list[ReviewItem] = []
    cache: dict[tuple[str, AffiliationKind], MatchResult] = {}
    verdicts: Counter[str] = Counter()
    skipped = 0

    for person in people:
        resolved = _resolve_person_strings(person, gaz, cfg, cache)
        spans = _spans(person)
        for index, (kind, raw_text, title, result) in enumerate(resolved):
            folded = norm(raw_text)
            if not folded:
                # Punctuation-only placeholders ("-", "N/A" once folded to
                # nothing) name no employer. Building an Org from one would give
                # everybody who typed the same placeholder a shared-employer
                # edge, which is a fabricated relationship.
                skipped += 1
                continue
            named = gaz.entities.get(result.entity_key) if result.entity_key else None
            entity = _org_entity(result, gaz)
            group = _group_key(entity, folded)
            _record_org(drafts, group, raw_text.strip(), kind, entity)
            verdicts[str(result.verdict)] += 1

            # Read the title against the CANDIDATE, even when the verdict is
            # review and no gazetteer Org is built from it. Interpreting a title
            # is not attributing a person: "MD" only reads as Managing Director
            # because the candidate is a fund, and the reviewer needs that
            # reading in order to judge the match at all.
            finance = _is_finance(entity or named)
            seniority = (
                extract_seniority(title, finance_context=finance)
                if kind is AffiliationKind.EMPLOYMENT
                else Seniority.UNKNOWN
            )
            role = (
                extract_role(title, finance_context=finance)
                if kind is AffiliationKind.EMPLOYMENT
                else None
            )
            weight = _confidence(result, entity) * (role_weight(role) if role else 1.0)
            start, end, current_role = spans[index]
            claims.append(
                _Claim(
                    person_id=person.person_id,
                    group_key=group,
                    kind=kind,
                    title=title.strip() or None,
                    start=start,
                    end=end,
                    is_current=current_role and end is None,
                    weight=round(weight, 6),
                )
            )
            if _needs_review(result, cfg):
                reviews.append(
                    ReviewItem(
                        person_id=person.person_id,
                        field_kind=kind,
                        raw_text=raw_text.strip(),
                        normalized=folded,
                        verdict=result.verdict,
                        score=round(result.score, 4),
                        reason=result.reason,
                        candidate_key=result.entity_key or (result.alternatives or (None,))[0],
                        candidate_name=named.canonical_name if named else None,
                        candidate_names=tuple(
                            gaz.entities[k].canonical_name
                            for k in result.alternatives
                            if k in gaz.entities
                        ),
                        suggested_target_firm=named.target_firm if named else None,
                        title_raw=title.strip(),
                        seniority=seniority,
                    )
                )

    group_to_id, orgs = _build_orgs(drafts)
    affiliations = _build_affiliations(claims, group_to_id)

    write_jsonl(cfg.orgs_path, orgs)
    write_jsonl(cfg.affiliations_path, affiliations)
    ordered_reviews: list[ReviewItem] = sorted(reviews, key=lambda item: item.sort_key)
    write_jsonl(_review_path(cfg), ordered_reviews)

    log.info(
        "normalize: %d people -> %d orgs (%d target), %d affiliations, %d for review, "
        "%d unusable strings skipped (%s)",
        len(people),
        len(orgs),
        sum(1 for o in orgs if o.is_target),
        len(affiliations),
        len(reviews),
        skipped,
        ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())),
    )


def _review_path(cfg: Settings) -> Path:
    """This stage owns ``review_queue.jsonl``; no other stage writes it."""
    return cfg.artifact("review_queue.jsonl")


def _needs_review(result: MatchResult, cfg: Settings) -> bool:
    """REVIEW verdicts, plus the near-misses the guard stopped.

    A guard rejection at or above the review threshold is the interesting kind
    of miss: something scored like a firm and was blocked by its extra tokens.
    That set is the alias-curation backlog. Ordinary sub-threshold rejects are
    every unremarkable employer in the export and would drown it.
    """
    if result.verdict is MatchVerdict.REVIEW:
        return True
    return result.reason.startswith("guard_reject") and result.score >= cfg.match_review


def _spans(person: Person) -> list[tuple[ApproxDate | None, ApproxDate | None, bool]]:
    """Date span and currency, in the order ``_resolve_person_strings`` produced strings.

    Currency travels with the dates because it is part of the same fact. A
    connections export carries only each person's *present* employer, so ingest
    marks every position current; dropping the flag here left every affiliation
    in a real run looking like a job somebody used to have. That understated
    co-tenure between people who are colleagues right now, scored them as former
    employees rather than current ones, and told the operator "worked at" about
    someone their own export says works there today.
    """
    spans: list[tuple[ApproxDate | None, ApproxDate | None, bool]] = []
    for position in person.positions:
        if position.company_raw.strip():
            spans.append((position.start, position.end, position.is_current))
    for education in person.educations:
        # The same emptiness filter _resolve_person_strings applies, or the
        # indices stop lining up with the strings these spans describe.
        if education.school_raw.strip():
            # A degree is not a job; nobody is "currently" an alumnus.
            spans.append((education.start, education.end, False))
    return spans


def _build_affiliations(claims: list[_Claim], group_to_id: dict[str, str]) -> list[Affiliation]:
    """Deduplicate claims onto affiliations and sort them.

    Two identical positions (the same employer twice with the same dates) are
    one affiliation; the stronger weight wins so a titled row is not erased by
    an untitled duplicate.
    """
    merged: dict[tuple[str, str, str, str | None, int, int], Affiliation] = {}
    for claim in claims:
        org_id = group_to_id[claim.group_key]
        key = (
            claim.person_id,
            org_id,
            str(claim.kind),
            claim.title,
            _date_key(claim.start),
            _date_key(claim.end),
        )
        current = merged.get(key)
        if current is not None and current.weight >= claim.weight:
            continue
        merged[key] = Affiliation(
            person_id=claim.person_id,
            org_id=org_id,
            kind=claim.kind,
            title=claim.title,
            start=claim.start,
            end=claim.end,
            is_current=claim.is_current,
            weight=claim.weight,
        )
    return [
        merged[key]
        for key in sorted(merged, key=lambda k: (k[0], k[1], k[2], k[3] or "", k[4], k[5]))
    ]
