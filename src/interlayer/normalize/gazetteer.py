"""Load ``data/gazetteer/firms.yaml`` and build the lookup indexes.

Precision in this problem comes from the gazetteer, not from the scorer. With
the negative gazetteer and the containment guard in place every scorer measured
scored 1.000/1.000 at every threshold from 84 to 100; without them the best
scorer available managed 0.591. So the indexes built here -- and in particular
*which normalisation each one is keyed on* -- are the load-bearing part.

The one rule that must never regress: **negative aliases are indexed under
``norm_raw`` and nothing else.** ``norm("The Citadel Group") == "citadel"``, so
indexing negatives under ``norm`` as well would put the bare token ``citadel``
into the veto table and reject every genuine Citadel employee. That bug was hit
and fixed once already during research; :func:`audit_index` exists so it cannot
come back silently, and :func:`load_gazetteer` runs it on every load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from interlayer.errors import ResolutionError
from interlayer.models import AffiliationKind, OrgKind, TargetFirm
from interlayer.normalize.normalize import (
    LEGAL_SUFFIXES,
    MULTI_TOKEN_SUFFIXES,
    norm,
    norm_raw,
)

__all__ = [
    "FIRM_TIERS",
    "Entity",
    "Gazetteer",
    "audit_index",
    "load_gazetteer",
]

SUPPORTED_SCHEMA_VERSION = 1

FIRM_TIERS: frozenset[str] = frozenset({"target", "adjacent"})
"""Tiers whose entities are real firm identifications. ``negative`` entities are
identified too, but identifying one is an explicit *no*."""

_TIER_RANK = {"target": 0, "adjacent": 1, "negative": 2}

# entity_type -> the fields a string may be matched from. Nobody is *educated*
# at a market maker, and nobody is *employed* by a university in the sense this
# tool cares about, so the field a string came from prunes the candidate set
# before any scoring happens. This is rule C2 and it is what makes "The Citadel"
# resolvable at all.
_EDUCATION_TYPES = frozenset({"university", "school", "college"})
_EMPLOYMENT_TYPES = frozenset({"fund", "market_maker", "bank", "broker"})

_BOTH_FIELDS = frozenset({AffiliationKind.EMPLOYMENT, AffiliationKind.EDUCATION})


@dataclass(frozen=True, slots=True)
class Entity:
    """One gazetteer record, with the derived bits the matcher needs."""

    key: str
    canonical_name: str
    entity_type: str
    tier: str
    priority: int
    aliases: tuple[str, ...]
    weak_aliases: tuple[str, ...]
    negative_aliases: tuple[str, ...]
    negative_patterns: tuple[str, ...]
    domains: tuple[str, ...]
    linkedin_slug: str | None
    linkedin_slug_verified: bool
    linkedin_url_kind: str
    approx_headcount: int | None
    fields: frozenset[AffiliationKind]
    collision_with: tuple[str, ...]

    @property
    def is_firm(self) -> bool:
        return self.tier in FIRM_TIERS

    @property
    def is_target(self) -> bool:
        return self.tier == "target"

    @property
    def target_firm(self) -> TargetFirm | None:
        """The ``TargetFirm`` member for this entity, when it is one.

        Citadel LLC and Citadel Securities are separate members here because
        they are separate companies; a key that is not a member returns None
        rather than being coerced onto a nearby one.
        """
        if not self.is_target:
            return None
        try:
            return TargetFirm(self.key)
        except ValueError:
            return None

    @property
    def is_specific_org(self) -> bool:
        """True when this record names one organisation rather than a category.

        Two negative entries are deliberately *buckets*: ``jane_street_generic``
        stands for every business on a road called Jane Street, and
        ``citadel_security_generic`` for the thousands of unrelated security
        firms. Both carry ``linkedin_slug: null`` because no single page exists
        for them. Building an Org out of a bucket would put a barista and a
        dentist in the same company and hand the graph stage a co-employment
        edge between them, so a bucket may veto a string but may never name one.
        """
        return self.is_firm or self.linkedin_slug is not None

    @property
    def org_kind(self) -> OrgKind:
        return OrgKind.SCHOOL if self.entity_type in _EDUCATION_TYPES else OrgKind.COMPANY

    @property
    def linkedin_url(self) -> str | None:
        """A profile URL only for a slug someone has actually checked.

        Every slug but one carries ``linkedin_slug_verified: false`` because
        linkedin.com was egress-blocked from the research sandbox. Rendering an
        unverified slug as *the firm's page* would hand the operator a link that
        may point at the wrong company, so unverified slugs stay internal.
        """
        if not self.linkedin_slug or not self.linkedin_slug_verified:
            return None
        segment = "school" if self.linkedin_url_kind == "school" else "company"
        return f"https://www.linkedin.com/{segment}/{self.linkedin_slug}"

    @property
    def domain(self) -> str | None:
        return self.domains[0] if self.domains else None


@dataclass(frozen=True, slots=True)
class Gazetteer:
    """The entity table plus every index the matcher consults."""

    entities: dict[str, Entity]
    exact: dict[str, tuple[str, ...]]
    """``norm`` **and** ``norm_raw`` of every positive alias -> entity keys, in
    preference order (target before adjacent before negative)."""

    weak_exact: dict[str, tuple[str, ...]]
    """Quarantined aliases (``JS``, ``SIG``, ``CS``...). Exact hit only, and
    still not accepted without corroboration -- ``JS`` alone matches JavaScript
    and several thousand initialisms."""

    fuzzy: tuple[tuple[str, str], ...]
    """(alias ``norm``, entity key) in preference order, for the WRatio sweep."""

    neg_exact: dict[str, tuple[str, ...]]
    """``norm_raw`` of every negative alias -> the entities it vetoes. RAW ONLY."""

    neg_patterns: tuple[tuple[re.Pattern[str], str], ...]
    education_raw: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """``norm_raw`` of aliases of education-only entities. Used to detect the
    ambiguous ``The Citadel``-in-a-job-field case without letting a bare
    ``Citadel`` fall into it."""

    def scope(self, kind: AffiliationKind) -> frozenset[str]:
        """Entity keys matchable from this field kind."""
        return frozenset(k for k, e in self.entities.items() if kind in e.fields)

    def preference(self, key: str) -> tuple[int, int, str]:
        entity = self.entities[key]
        return (_TIER_RANK.get(entity.tier, 3), entity.priority, key)


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _entity_fields(raw: dict[str, Any], entity_type: str) -> frozenset[AffiliationKind]:
    """Explicit ``field_scope`` wins; otherwise the entity type decides."""
    declared = _as_str_tuple(raw.get("field_scope"))
    if declared:
        mapping = {
            "education": AffiliationKind.EDUCATION,
            "position": AffiliationKind.EMPLOYMENT,
            "employment": AffiliationKind.EMPLOYMENT,
            "company": AffiliationKind.EMPLOYMENT,
        }
        chosen = {mapping[d] for d in declared if d in mapping}
        if chosen:
            return frozenset(chosen)
    if entity_type in _EDUCATION_TYPES:
        return frozenset({AffiliationKind.EDUCATION})
    if entity_type in _EMPLOYMENT_TYPES:
        return frozenset({AffiliationKind.EMPLOYMENT})
    return _BOTH_FIELDS


def _build_entity(raw: dict[str, Any]) -> Entity:
    try:
        key = str(raw["key"])
        canonical = str(raw["canonical_name"])
    except KeyError as exc:  # pragma: no cover - malformed gazetteer
        raise ResolutionError(f"gazetteer entity missing {exc}") from exc
    entity_type = str(raw.get("entity_type", "unrelated"))
    headcount = raw.get("approx_headcount")
    return Entity(
        key=key,
        canonical_name=canonical,
        entity_type=entity_type,
        tier=str(raw.get("tier", "negative")),
        priority=int(raw.get("priority", 9)),
        aliases=_as_str_tuple(raw.get("aliases")),
        weak_aliases=_as_str_tuple(raw.get("weak_aliases")),
        negative_aliases=_as_str_tuple(raw.get("negative_aliases")),
        negative_patterns=_as_str_tuple(raw.get("negative_patterns")),
        domains=_as_str_tuple(raw.get("domains")),
        linkedin_slug=(str(raw["linkedin_slug"]) if raw.get("linkedin_slug") else None),
        linkedin_slug_verified=bool(raw.get("linkedin_slug_verified", False)),
        linkedin_url_kind=str(raw.get("linkedin_url_kind", "company")),
        approx_headcount=int(headcount) if headcount is not None else None,
        fields=_entity_fields(raw, entity_type),
        collision_with=_as_str_tuple(raw.get("collision_with")),
    )


def _check_suffix_coverage(declared: tuple[str, ...]) -> None:
    """Fail loudly if the gazetteer names a legal suffix the folder does not know.

    The suffix list lives in code (seeded from cleanco) while the gazetteer also
    declares one. If a curator adds a term to the YAML expecting it to take
    effect, it must actually take effect rather than silently doing nothing.
    """
    missing = sorted(
        term
        for term in declared
        if (folded := " ".join(term.replace(".", " ").split()).casefold())
        and folded not in LEGAL_SUFFIXES
        and folded not in MULTI_TOKEN_SUFFIXES
        and folded.replace(" ", "") not in LEGAL_SUFFIXES
    )
    if missing:
        raise ResolutionError(
            "gazetteer declares legal suffixes the normaliser does not implement: "
            f"{missing}; add them to interlayer.normalize.normalize.LEGAL_SUFFIXES"
        )


def load_gazetteer(path: Path) -> Gazetteer:
    """Parse the YAML and build every index, auditing before returning.

    Raises ``ResolutionError`` rather than returning a half-built index: a
    matcher running on a silently-degraded gazetteer produces confident wrong
    answers, which is the one failure mode this stage exists to prevent.
    """
    if not path.is_file():
        raise ResolutionError(
            f"gazetteer not found at {path}; set `gazetteer:` in the config or run "
            "from the repository root"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ResolutionError(f"gazetteer is not valid YAML: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ResolutionError(f"gazetteer must be a mapping, got {type(data).__name__}")

    version = data.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ResolutionError(
            f"gazetteer schema_version {version!r} is not supported "
            f"(this build understands {SUPPORTED_SCHEMA_VERSION}); field semantics "
            "change with the version, so refusing rather than guessing"
        )
    raw_entities = data.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raise ResolutionError(f"gazetteer has no entities: {path}")

    normalization = data.get("normalization") or {}
    _check_suffix_coverage(_as_str_tuple(normalization.get("legal_suffixes")))

    entities: dict[str, Entity] = {}
    for raw in raw_entities:
        if not isinstance(raw, dict):
            raise ResolutionError(f"gazetteer entity must be a mapping, got {type(raw).__name__}")
        entity = _build_entity(raw)
        if entity.key in entities:
            raise ResolutionError(f"duplicate gazetteer key: {entity.key!r}")
        entities[entity.key] = entity

    gaz = _build_indexes(entities)
    conflicts = audit_index(gaz)
    if conflicts:
        raise ResolutionError(
            "gazetteer audit failed -- these negative aliases collide with a "
            f"positive firm alias and would veto real employees: {conflicts}"
        )
    return gaz


def _build_indexes(entities: dict[str, Entity]) -> Gazetteer:
    def preference(key: str) -> tuple[int, int, str]:
        entity = entities[key]
        return (_TIER_RANK.get(entity.tier, 3), entity.priority, key)

    exact: dict[str, set[str]] = {}
    weak: dict[str, set[str]] = {}
    education_raw: dict[str, set[str]] = {}
    neg_exact: dict[str, set[str]] = {}
    fuzzy: set[tuple[str, str]] = set()
    patterns: list[tuple[re.Pattern[str], str]] = []

    for key, entity in entities.items():
        education_only = entity.fields == frozenset({AffiliationKind.EDUCATION})
        for alias in (*entity.aliases, entity.canonical_name):
            folded, folded_raw = norm(alias), norm_raw(alias)
            for form in (folded, folded_raw):
                if form:
                    exact.setdefault(form, set()).add(key)
            if folded:
                fuzzy.add((folded, key))
            if education_only and folded_raw:
                education_raw.setdefault(folded_raw, set()).add(key)
        for alias in entity.weak_aliases:
            for form in (norm(alias), norm_raw(alias)):
                if form:
                    weak.setdefault(form, set()).add(key)
        for alias in entity.negative_aliases:
            # RAW ONLY -- see the module docstring. This single line is the
            # difference between vetoing "The Citadel Group" and vetoing every
            # person who works at Citadel.
            folded_raw = norm_raw(alias)
            if folded_raw:
                neg_exact.setdefault(folded_raw, set()).add(key)
        for pattern in entity.negative_patterns:
            try:
                patterns.append((re.compile(pattern, re.IGNORECASE), key))
            except re.error as exc:
                raise ResolutionError(
                    f"gazetteer entity {key!r} has an invalid negative_pattern {pattern!r}: {exc}"
                ) from exc

    # A declared weak alias must not be reachable through the ordinary indexes,
    # or the quarantine is decorative. It leaks by folding: "IMC B.V." is an
    # ordinary alias, but stripping its legal suffix reduces it to "imc", which
    # is the very initialism the entity quarantines -- so the bare form entered
    # the strong index by the back door and matched with no corroboration.
    # Anything colliding with the entity's own weak form is moved, not copied.
    for key, entity in entities.items():
        quarantined = {
            form for alias in entity.weak_aliases for form in (norm(alias), norm_raw(alias)) if form
        }
        for form in quarantined:
            if form in exact:
                exact[form].discard(key)
                if not exact[form]:
                    del exact[form]
            fuzzy.discard((form, key))
            weak.setdefault(form, set()).add(key)

    def ordered(table: dict[str, set[str]]) -> dict[str, tuple[str, ...]]:
        return {k: tuple(sorted(v, key=preference)) for k, v in sorted(table.items())}

    return Gazetteer(
        entities=entities,
        exact=ordered(exact),
        weak_exact=ordered(weak),
        fuzzy=tuple(sorted(fuzzy, key=lambda pair: (preference(pair[1]), pair[0]))),
        neg_exact=ordered(neg_exact),
        neg_patterns=tuple(patterns),
        education_raw=ordered(education_raw),
    )


def audit_index(gaz: Gazetteer) -> list[str]:
    """Assert no negative alias collides with a positive firm alias.

    Returns the colliding keys, which must be empty. A collision means the veto
    table contains a string that also identifies a real firm, so the veto would
    fire on legitimate employees of that firm -- the exact bug that indexing
    negatives under ``norm`` produces. Checked under **both** normalisations,
    because a collision under either one is fatal.
    """
    positive: set[str] = set()
    for entity in gaz.entities.values():
        if not entity.is_firm:
            continue
        for alias in (*entity.aliases, entity.canonical_name):
            positive.update({norm(alias), norm_raw(alias)})
    positive.discard("")
    return sorted(key for key in gaz.neg_exact if key in positive)
