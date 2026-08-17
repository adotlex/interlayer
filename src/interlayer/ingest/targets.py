"""Load and validate the target registry (``data/targets.yaml``).

The registry is data, not code: aliases, anchor regexes, discriminators,
negative patterns and thresholds all live in YAML so a registry edit is
reviewable in a diff and re-checked by the 100-case fixture. This module turns
that YAML into typed, frozen objects and refuses to load a malformed one —
a half-valid registry silently mis-assigns firms, which is exactly the failure
mode the fixture exists to catch.

Everything is standard library plus ``pyyaml``. No network, ever.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from interlayer.core.models import Firm
from interlayer.ingest import RegistryError

#: Registry shapes this loader understands. Bump on any breaking change.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

#: Environment override, mostly for tests and for a user-maintained registry.
REGISTRY_PATH_ENV = "INTERLAYER_TARGETS"

_REGISTRY_FILENAME = "targets.yaml"


# ---------------------------------------------------------------------------
# Typed registry objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConfusableSlug:
    """A LinkedIn slug that looks like the target but is a different company.

    ``janestreetgroup`` is the canonical example: a ~45-follower corporate
    events business that a slug-pattern crawler would happily ingest as Jane
    Street Group. These are never enumerated.
    """

    slug: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class LinkedInPage:
    """The LinkedIn identity of one target entity."""

    slug: str
    """The live page slug. Authoritative; numeric ids are not publicly indexed."""

    company_id: int | None = None
    """``null`` as shipped. Resolve locally while logged in:
    open ``https://www.linkedin.com/company/<slug>/``, view source and search
    ``urn:li:organization:`` — the trailing digits are the id; or run a job
    search filtered to the company and read ``f_C=<id>`` off the URL. Write it
    back into ``targets.yaml``. A non-null id enables exact joins on any
    surface that emits URNs."""

    page_display_name: str = ""
    secondary_slugs: tuple[str, ...] = ()
    """Additional *real* pages for the same firm. Enumerate, but do not treat
    as separate firms."""

    confusable_slugs: tuple[ConfusableSlug, ...] = ()

    @property
    def all_real_slugs(self) -> tuple[str, ...]:
        return (self.slug, *self.secondary_slugs)

    def is_confusable(self, slug: str) -> bool:
        needle = slug.strip().casefold()
        return any(c.slug.casefold() == needle for c in self.confusable_slugs)


@dataclass(frozen=True, slots=True)
class Normalisation:
    """Global normalisation vocabulary, shared by every entity.

    Do not hand-edit: this is the vocabulary the measured 100/100 fixture
    result depends on.
    """

    legal_suffixes: frozenset[str]
    modifier_tokens: frozenset[str]


@dataclass(frozen=True, slots=True)
class MatchPolicy:
    """Cross-entity match policy knobs, straight from the registry."""

    scorer: str = "rapidfuzz.fuzz.token_sort_ratio"
    library: str = "rapidfuzz>=3.14,<4"
    ambiguity_margin: int = 5
    """Two entities scoring within this margin => ambiguous => review."""
    unexpected_token_forgiveness: int = 85
    """A trailing token is "unexpected" only if it has no partner in the
    matched alias's extension vocabulary at or above this ratio."""
    fuzzy_anchor_min: int = 88
    """A typo'd anchor can reach review, never confident."""
    memoise: bool = True


@dataclass(frozen=True, slots=True)
class TargetEntity:
    """One target firm, fully compiled and ready to match against."""

    id: str
    canonical: str
    group: str
    firm: Firm
    linkedin: LinkedInPage

    aliases: tuple[str, ...] = ()
    review_only_aliases: tuple[str, ...] = ()
    """Matching one of these yields REVIEW and can never yield CONFIDENT.
    ``JS`` is the reason this exists: JS Held, JS Bank, JavaScript, initials."""
    noise_tokens: frozenset[str] = frozenset()

    anchors: tuple[re.Pattern[str], ...] = ()
    anchor_required: tuple[str, ...] = ()
    """*All* of these must be present for the typo fallback to fire. Requiring
    both ``jane`` and ``street`` is what rejects ``Jane Iredale``."""

    requires: re.Pattern[str] | None = None
    excludes: re.Pattern[str] | None = None
    """The discriminator pair. ``securit\\w*`` required by Citadel Securities
    and excluded by Citadel is what keeps the two firms apart."""

    negative_patterns: tuple[re.Pattern[str], ...] = ()
    """Unconditional veto. Negatives beat everything else in the ladder."""

    threshold: int = 88
    review_floor: int = 70

    legal_entities: tuple[str, ...] = ()
    business_units: tuple[str, ...] = ()
    funds: tuple[str, ...] = ()
    offices: tuple[str, ...] = ()
    headcount_estimate: int | None = None
    headcount_as_of: str = ""

    #: Aliases pre-split into tokens, longest first — the anchored-prefix rung
    #: needs longest-match-wins ordering and rebuilding it per call is waste.
    alias_tokens: tuple[tuple[str, ...], ...] = field(default=(), repr=False)

    def alias_extension_vocab(self, alias: str) -> frozenset[str]:
        """Tokens legitimately expected to *follow* ``alias``.

        The vocabulary is the alias's own tokens plus the tokens of every alias
        that extends it as a token-prefix — deliberately **not** the whole
        entity vocabulary. R5 found that bug by measurement: with one shared
        vocabulary, ``Citadel Capital`` scored *confident* because ``capital``
        appears in the sibling alias ``Surveyor Capital``.
        """
        base = tuple(alias.split())
        vocab: set[str] = set(base)
        for tokens in self.alias_tokens:
            if len(tokens) > len(base) and tokens[: len(base)] == base:
                vocab.update(tokens)
        return frozenset(vocab)


@dataclass(frozen=True, slots=True)
class TargetRegistry:
    """The whole registry. Iterating yields entities in registry order, which
    is also the tie-break order for equal scores."""

    schema_version: int
    generated: str
    source: str
    normalisation: Normalisation
    match_policy: MatchPolicy
    entities: tuple[TargetEntity, ...]
    path: Path | None = None

    def __iter__(self) -> Iterator[TargetEntity]:
        return iter(self.entities)

    def __len__(self) -> int:
        return len(self.entities)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(e.id for e in self.entities)

    def by_id(self, entity_id: str) -> TargetEntity:
        for entity in self.entities:
            if entity.id == entity_id:
                return entity
        raise RegistryError(f"unknown target entity id {entity_id!r}; known: {list(self.ids)}")

    def by_firm(self, firm: Firm) -> TargetEntity:
        for entity in self.entities:
            if entity.firm is firm:
                return entity
        raise RegistryError(f"no registry entity for firm {firm!r}")

    def firm_for(self, entity_id: str) -> Firm:
        return self.by_id(entity_id).firm

    def group_of(self, firm: Firm) -> str:
        """Roll-up group. Both Citadels share ``citadel`` so downstream
        analysis can merge them when the split does not matter."""
        return self.by_firm(firm).group


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def default_registry_path() -> Path:
    """Locate ``targets.yaml``.

    ``$INTERLAYER_TARGETS`` wins. Otherwise look beside the package (a future
    packaged copy) and then at the repository root, which is where the shipped
    registry lives in an editable install.
    """
    override = os.environ.get(REGISTRY_PATH_ENV)
    if override:
        return Path(override).expanduser()

    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "data" / _REGISTRY_FILENAME,  # src/interlayer/data/
        here.parents[3] / "data" / _REGISTRY_FILENAME,  # <repo>/data/
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def load_registry(path: str | os.PathLike[str] | None = None) -> TargetRegistry:
    """Read, validate and compile ``targets.yaml``. Raises on anything wrong."""
    resolved = Path(path).expanduser() if path is not None else default_registry_path()
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegistryError(f"cannot read target registry at {resolved}: {exc}") from exc
    return parse_registry(text, path=resolved)


def parse_registry(text: str, *, path: Path | None = None) -> TargetRegistry:
    """Parse registry YAML from a string. Separate from :func:`load_registry`
    so tests can feed malformed registries without touching the filesystem."""
    where = f" ({path})" if path is not None else ""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RegistryError(f"target registry is not valid YAML{where}: {exc}") from exc

    if not isinstance(raw, Mapping):
        raise RegistryError(f"target registry must be a mapping{where}, got {type(raw).__name__}")

    version = raw.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise RegistryError(
            f"unsupported registry schema_version {version!r}{where}; "
            f"supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
        )

    normalisation = _parse_normalisation(raw.get("normalisation"), where)
    policy = _parse_policy(raw.get("match_policy"), where)

    entities_raw = raw.get("entities")
    if not isinstance(entities_raw, Sequence) or isinstance(entities_raw, (str, bytes)):
        raise RegistryError(f"registry key 'entities' must be a list{where}")
    if not entities_raw:
        raise RegistryError(f"registry declares no entities{where}")

    entities: list[TargetEntity] = []
    seen: set[str] = set()
    for index, item in enumerate(entities_raw):
        entity = _parse_entity(item, index=index, where=where)
        if entity.id in seen:
            raise RegistryError(f"duplicate entity id {entity.id!r}{where}")
        seen.add(entity.id)
        entities.append(entity)

    firms = [e.firm for e in entities]
    if len(set(firms)) != len(firms):
        raise RegistryError(f"two registry entities map to the same Firm{where}")

    return TargetRegistry(
        schema_version=int(version),
        generated=str(raw.get("generated", "")),
        source=str(raw.get("source", "")),
        normalisation=normalisation,
        match_policy=policy,
        entities=tuple(entities),
        path=path,
    )


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_normalisation(node: Any, where: str) -> Normalisation:
    if not isinstance(node, Mapping):
        raise RegistryError(f"registry key 'normalisation' must be a mapping{where}")
    legal = _token_set(node.get("legal_suffixes"), "normalisation.legal_suffixes", where)
    modifiers = _token_set(node.get("modifier_tokens"), "normalisation.modifier_tokens", where)
    if not legal or not modifiers:
        raise RegistryError(f"normalisation vocabulary is empty{where}")
    return Normalisation(legal_suffixes=legal, modifier_tokens=modifiers)


#: Fallbacks for a registry that omits ``match_policy`` keys. A slotted
#: dataclass has no readable class-level defaults, so they live here.
_POLICY_DEFAULTS = MatchPolicy()


def _parse_policy(node: Any, where: str) -> MatchPolicy:
    if node is None:
        return _POLICY_DEFAULTS
    if not isinstance(node, Mapping):
        raise RegistryError(f"registry key 'match_policy' must be a mapping{where}")
    policy = MatchPolicy(
        scorer=str(node.get("scorer", _POLICY_DEFAULTS.scorer)),
        library=str(node.get("library", _POLICY_DEFAULTS.library)),
        ambiguity_margin=_int(node, "ambiguity_margin", _POLICY_DEFAULTS.ambiguity_margin, where),
        unexpected_token_forgiveness=_int(
            node,
            "unexpected_token_forgiveness",
            _POLICY_DEFAULTS.unexpected_token_forgiveness,
            where,
        ),
        fuzzy_anchor_min=_int(node, "fuzzy_anchor_min", _POLICY_DEFAULTS.fuzzy_anchor_min, where),
        memoise=bool(node.get("memoise", True)),
    )
    if "token_set_ratio" in policy.scorer:
        raise RegistryError(
            f"match_policy.scorer must not be token_set_ratio{where}: it scores both "
            "'Jane Street Capital LLC' and 'Jane Street Entertainment' at 100 (R5 Findings 2)"
        )
    for name, value in (
        ("ambiguity_margin", policy.ambiguity_margin),
        ("unexpected_token_forgiveness", policy.unexpected_token_forgiveness),
        ("fuzzy_anchor_min", policy.fuzzy_anchor_min),
    ):
        if not 0 <= value <= 100:
            raise RegistryError(f"match_policy.{name} must be within 0..100{where}, got {value}")
    return policy


def _parse_entity(node: Any, *, index: int, where: str) -> TargetEntity:
    label = f"entities[{index}]"
    if not isinstance(node, Mapping):
        raise RegistryError(f"{label} must be a mapping{where}")

    entity_id = _required_str(node, "id", label, where)
    try:
        firm = Firm(entity_id)
    except ValueError as exc:
        raise RegistryError(
            f"{label}.id {entity_id!r}{where} is not a member of core.models.Firm "
            f"({[f.value for f in Firm]}). Firm is frozen for Wave 2; rename the "
            "registry entity, not the enum."
        ) from exc

    aliases = _str_tuple(node.get("aliases"), f"{label}.aliases", where)
    if not aliases:
        raise RegistryError(f"{label}.aliases must not be empty{where}")
    for alias in aliases:
        if alias != alias.casefold().strip() or "  " in alias:
            raise RegistryError(
                f"{label}.aliases entry {alias!r}{where} must be lowercase, "
                "stripped and single-spaced — aliases are compared against the "
                "normalised head verbatim"
            )

    anchors = _pattern_tuple(node.get("anchors"), f"{label}.anchors", where)
    if not anchors:
        raise RegistryError(f"{label}.anchors must not be empty{where}")
    anchor_required = _str_tuple(node.get("anchor_required"), f"{label}.anchor_required", where)
    if not anchor_required:
        raise RegistryError(
            f"{label}.anchor_required must not be empty{where}: the fuzzy-anchor "
            "fallback would otherwise fire on every string"
        )

    threshold = _int(node, "threshold", 88, where)
    review_floor = _int(node, "review_floor", 70, where)
    if not 0 <= review_floor <= threshold <= 100:
        raise RegistryError(
            f"{label}{where} requires 0 <= review_floor ({review_floor}) "
            f"<= threshold ({threshold}) <= 100"
        )

    alias_tokens = tuple(
        sorted(
            (tuple(a.split()) for a in aliases),
            key=lambda t: (-len(t), t),
        )
    )

    return TargetEntity(
        id=entity_id,
        canonical=_required_str(node, "canonical", label, where),
        group=str(node.get("group") or entity_id),
        firm=firm,
        linkedin=_parse_linkedin(node.get("linkedin"), label=label, where=where),
        aliases=aliases,
        review_only_aliases=_str_tuple(
            node.get("review_only_aliases"), f"{label}.review_only_aliases", where
        ),
        noise_tokens=_token_set(node.get("noise_tokens"), f"{label}.noise_tokens", where),
        anchors=anchors,
        anchor_required=anchor_required,
        requires=_optional_pattern(node.get("requires"), f"{label}.requires", where),
        excludes=_optional_pattern(node.get("excludes"), f"{label}.excludes", where),
        negative_patterns=_pattern_tuple(
            node.get("negative_patterns"), f"{label}.negative_patterns", where
        ),
        threshold=threshold,
        review_floor=review_floor,
        legal_entities=_str_tuple(node.get("legal_entities"), f"{label}.legal_entities", where),
        business_units=_str_tuple(node.get("business_units"), f"{label}.business_units", where),
        funds=_str_tuple(node.get("funds"), f"{label}.funds", where),
        offices=_str_tuple(node.get("offices"), f"{label}.offices", where),
        headcount_estimate=(
            int(node["headcount_estimate"]) if node.get("headcount_estimate") is not None else None
        ),
        headcount_as_of=str(node.get("headcount_as_of", "")),
        alias_tokens=alias_tokens,
    )


def _parse_linkedin(node: Any, *, label: str, where: str) -> LinkedInPage:
    if not isinstance(node, Mapping):
        raise RegistryError(f"{label}.linkedin must be a mapping{where}")
    slug = _required_str(node, "slug", f"{label}.linkedin", where)

    company_id = node.get("company_id")
    if company_id is not None and not isinstance(company_id, int):
        raise RegistryError(
            f"{label}.linkedin.company_id{where} must be an integer or null "
            "(resolve it locally: view-source -> urn:li:organization:<id>)"
        )

    secondary = _str_tuple(node.get("secondary_slugs"), f"{label}.linkedin.secondary_slugs", where)

    confusables: list[ConfusableSlug] = []
    raw_confusables = node.get("confusable_slugs") or []
    if not isinstance(raw_confusables, Sequence) or isinstance(raw_confusables, (str, bytes)):
        raise RegistryError(f"{label}.linkedin.confusable_slugs must be a list{where}")
    for entry in raw_confusables:
        if not isinstance(entry, Mapping):
            raise RegistryError(
                f"{label}.linkedin.confusable_slugs entries must be mappings "
                f"with a 'slug' key{where}"
            )
        confusables.append(
            ConfusableSlug(
                slug=_required_str(entry, "slug", f"{label}.linkedin.confusable_slugs", where),
                note=str(entry.get("note", "")),
            )
        )

    real = {slug.casefold(), *(s.casefold() for s in secondary)}
    overlap = sorted(real & {c.slug.casefold() for c in confusables})
    if overlap:
        raise RegistryError(
            f"{label}.linkedin{where} lists {overlap} as both a real and a confusable slug"
        )

    return LinkedInPage(
        slug=slug,
        company_id=company_id,
        page_display_name=str(node.get("page_display_name", "")),
        secondary_slugs=secondary,
        confusable_slugs=tuple(confusables),
    )


# ---------------------------------------------------------------------------
# Small typed helpers
# ---------------------------------------------------------------------------


def _required_str(node: Mapping[str, Any], key: str, label: str, where: str) -> str:
    value = node.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{label}.{key} must be a non-empty string{where}")
    return value.strip()


def _int(node: Mapping[str, Any], key: str, default: int, where: str) -> int:
    value = node.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryError(f"{key} must be an integer{where}, got {value!r}")
    return value


def _str_tuple(node: Any, label: str, where: str) -> tuple[str, ...]:
    if node is None:
        return ()
    if isinstance(node, (str, bytes)) or not isinstance(node, Iterable):
        raise RegistryError(f"{label} must be a list of strings{where}")
    out: list[str] = []
    for item in node:
        if not isinstance(item, str):
            raise RegistryError(f"{label} must contain strings{where}, got {item!r}")
        text = item.strip()
        if text:
            out.append(text)
    return tuple(out)


def _token_set(node: Any, label: str, where: str) -> frozenset[str]:
    return frozenset(t.casefold() for t in _str_tuple(node, label, where))


def _compile(pattern: str, label: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags=re.IGNORECASE | re.UNICODE)
    except re.error as exc:
        raise RegistryError(f"{label}{where} is not a valid regex ({pattern!r}): {exc}") from exc


def _pattern_tuple(node: Any, label: str, where: str) -> tuple[re.Pattern[str], ...]:
    return tuple(_compile(p, label, where) for p in _str_tuple(node, label, where))


def _optional_pattern(node: Any, label: str, where: str) -> re.Pattern[str] | None:
    if node is None:
        return None
    if not isinstance(node, str) or not node.strip():
        raise RegistryError(f"{label} must be a non-empty regex string or null{where}")
    return _compile(node.strip(), label, where)


__all__ = [
    "REGISTRY_PATH_ENV",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ConfusableSlug",
    "LinkedInPage",
    "MatchPolicy",
    "Normalisation",
    "TargetEntity",
    "TargetRegistry",
    "default_registry_path",
    "load_registry",
    "parse_registry",
]
