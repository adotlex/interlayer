"""The hand-fillable observation file: what a human types, and how it is read.

Why YAML and not CSV
--------------------
The person filling this in is sitting in front of a browser reading a
mutual-connections list off the screen. Every keystroke is their time, and every
punctuation character is a chance to get it wrong at 11pm on the fortieth
profile. CSV forces one row per ``(target, mutual)`` pair, which means retyping
the target's URL on every single line, and it breaks the moment a name or a
title contains a comma -- which job titles constantly do. YAML lets the shape of
the file match the shape of the task: one block per target, and the mutuals as a
literal block scalar where the human types **one name per line, no quoting, no
commas, no brackets**. ``pyyaml`` is already a dependency, so this costs nothing.

Tolerance is deliberate and asymmetric
-------------------------------------
* Key aliases are accepted freely (``url``/``profile``/``link`` all mean the
  same thing) because a wrong-but-guessable key costs the operator a re-run.
* An **unknown** key is a hard error. A typo'd ``mutual:`` would otherwise
  silently produce a target with no bridges at all -- data loss that looks
  exactly like a real result. Failing loudly is the whole point.
* A bare ``citadel`` firm value is rejected with an explanation. Citadel LLC and
  Citadel Securities are separate companies with separate staff; guessing which
  one the human meant corrupts every downstream result.

Nothing in this module touches the network. See ``interlayer.enrich`` for why.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from interlayer.config import Settings
from interlayer.errors import IngestError
from interlayer.models import Seniority, TargetFirm

__all__ = [
    "ENV_VAR",
    "InputLookup",
    "RawTarget",
    "clean_mutual",
    "find_observation_file",
    "infer_seniority",
    "line_index",
    "normalize_url",
    "parse_observations",
    "slug_from_url",
]

# --------------------------------------------------------------------------
# where the file lives
# --------------------------------------------------------------------------

ENV_VAR = "INTERLAYER_OBSERVATIONS"
"""Explicit override for the observation file path."""

_STEMS: tuple[str, ...] = ("data/observations", "observations")
_SUFFIXES: tuple[str, ...] = (".yaml", ".yml")


@dataclass(frozen=True, slots=True)
class InputLookup:
    """Result of hunting for the human-authored file.

    ``explicit`` is set only when the operator named a path via ``ENV_VAR``; a
    named path that does not exist is a typo, not an absent Tier 2, and the
    caller warns differently for the two cases.
    """

    path: Path | None
    explicit: Path | None
    searched: tuple[Path, ...]


def observation_candidates(cfg: Settings) -> tuple[Path, ...]:
    """Ordered search path for the observation file.

    ``artifact_dir`` is deliberately **not** searched: ``interlayer purge``
    deletes that directory wholesale, and it must never be able to destroy hours
    of hand-collected typing. ``data/`` is the primary home because ``.gitignore``
    already excludes ``/data/*``, so third-party names cannot reach git by
    accident.
    """
    roots: list[Path] = [cfg.artifact_dir.parent]
    with suppress(OSError):  # cwd can be gone underneath a test that chdir'd
        roots.insert(0, Path.cwd())
    out: list[Path] = []
    for root in roots:
        for stem in _STEMS:
            for suffix in _SUFFIXES:
                candidate = (root / f"{stem}{suffix}").resolve()
                if candidate not in out:
                    out.append(candidate)
    return tuple(out)


def find_observation_file(cfg: Settings) -> InputLookup:
    """Locate the observation file, or report that Tier 2 is simply absent."""
    named = os.environ.get(ENV_VAR, "").strip()
    if named:
        explicit = Path(named).expanduser()
        found = explicit if explicit.is_file() else None
        return InputLookup(path=found, explicit=explicit, searched=(explicit,))
    searched = observation_candidates(cfg)
    for candidate in searched:
        if candidate.is_file():
            return InputLookup(path=candidate, explicit=None, searched=searched)
    return InputLookup(path=None, explicit=None, searched=searched)


# --------------------------------------------------------------------------
# key / value vocabularies
# --------------------------------------------------------------------------

_TARGET_ALIASES: Mapping[str, str] = {
    "name": "name",
    "full_name": "name",
    "fullname": "name",
    "person": "name",
    "target": "name",
    "firm": "firm",
    "company": "firm",
    "employer": "firm",
    "target_firm": "firm",
    "url": "url",
    "link": "url",
    "profile": "url",
    "profile_url": "url",
    "linkedin": "url",
    "linkedin_url": "url",
    "title": "title",
    "title_raw": "title",
    "role": "title",
    "position": "title",
    "headline": "title",
    "stated": "stated",
    "count": "stated",
    "mutual_count": "stated",
    "mutuals_count": "stated",
    "mutuals_stated": "stated",
    "stated_count": "stated",
    "shared_count": "stated",
    "mutuals": "mutuals",
    "mutual": "mutuals",
    "mutual_connections": "mutuals",
    "shared": "mutuals",
    "shared_connections": "mutuals",
    "bridges": "mutuals",
    "complete": "complete",
    "truncated": "truncated",
    "seniority": "seniority",
    "observed": "observed_on",
    "observed_on": "observed_on",
    "date": "observed_on",
    "note": "note",
    "notes": "note",
}

_FILE_ALIASES: Mapping[str, str] = {
    "targets": "targets",
    "people": "targets",
    "observations": "targets",
    "entries": "targets",
    "firm": "firm",
    "company": "firm",
    "default_firm": "firm",
    "observed": "observed_on",
    "observed_on": "observed_on",
    "date": "observed_on",
    "note": "note",
    "notes": "note",
}

_FIRM_ALIASES: Mapping[str, TargetFirm] = {
    "jane_street": TargetFirm.JANE_STREET,
    "janestreet": TargetFirm.JANE_STREET,
    "jane_st": TargetFirm.JANE_STREET,
    "jane_street_capital": TargetFirm.JANE_STREET,
    "jane_street_group": TargetFirm.JANE_STREET,
    "js": TargetFirm.JANE_STREET,
    "citadel_llc": TargetFirm.CITADEL_LLC,
    "citadel_l_l_c": TargetFirm.CITADEL_LLC,
    "citadel_advisors": TargetFirm.CITADEL_LLC,
    "citadel_enterprise": TargetFirm.CITADEL_LLC,
    "citadel_fund": TargetFirm.CITADEL_LLC,
    "citadel_hedge_fund": TargetFirm.CITADEL_LLC,
    "citadel_securities": TargetFirm.CITADEL_SECURITIES,
    "citadel_securities_llc": TargetFirm.CITADEL_SECURITIES,
    "citadel_sec": TargetFirm.CITADEL_SECURITIES,
    "citsec": TargetFirm.CITADEL_SECURITIES,
}

_AMBIGUOUS_FIRM_MESSAGE = (
    "firm {value!r} is ambiguous. Citadel LLC (the hedge fund, "
    "linkedin.com/company/citadel-llc) and Citadel Securities (the market maker, "
    "linkedin.com/company/citadel-securities) are SEPARATE companies with separate "
    "staff; merging them corrupts every downstream result. Write citadel_llc or "
    "citadel_securities."
)
_AMBIGUOUS_FIRMS = frozenset({"citadel", "citadel_group", "the_citadel"})


def _norm_key(raw: object) -> str:
    """Fold a YAML key to its canonical spelling (case, spaces and dashes are free)."""
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().casefold()).strip("_")


def _parse_firm(value: object, *, where: str) -> TargetFirm:
    key = _norm_key(value)
    if key in _AMBIGUOUS_FIRMS:
        raise IngestError(f"{where}: " + _AMBIGUOUS_FIRM_MESSAGE.format(value=str(value).strip()))
    firm = _FIRM_ALIASES.get(key)
    if firm is None:
        allowed = ", ".join(f.value for f in TargetFirm)
        raise IngestError(
            f"{where}: unknown firm {str(value).strip()!r}; expected one of {allowed}"
        )
    return firm


# --------------------------------------------------------------------------
# mutual-name cleanup
# --------------------------------------------------------------------------

# Bullets, middots and dashes people type or paste in front of a name.
_BULLET_RE = re.compile("^[-*\u2022\u00b7\u2013\u2014]+\\s+")
_DEGREE_RE = re.compile("[\\s,|\u00b7\u2013\u2014-]*\\b(?:1st|2nd|3rd\\+?)\\b\\s*$", re.IGNORECASE)
_TAIL_SEPARATORS: tuple[str, ...] = (" \u00b7 ", " \u2014 ", " \u2013 ", " | ", " - ", "\t")


def clean_mutual(raw: str) -> str:
    """Reduce one typed line to a bare name or profile URL.

    People paste what LinkedIn renders -- ``"Sarah Chen - 2nd - Trader at Foo"`` --
    and they type bullets out of habit. Stripping that here means the operator is
    never punished for pasting rather than retyping.

    Trailing ``# \u2026`` is dropped too. Inside a YAML block scalar that is literal
    text rather than a comment, but the human who typed it plainly meant a note
    to themselves, and honouring their intent beats honouring the spec.
    """
    text = raw.replace("\ufeff", "").strip()
    if not text or text.startswith("#"):
        return ""
    text = text.split(" #", 1)[0].strip()
    text = _BULLET_RE.sub("", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    if "linkedin.com/" not in text.casefold():
        for separator in _TAIL_SEPARATORS:
            head = text.split(separator, 1)[0].strip()
            if head:
                text = head
        text = _DEGREE_RE.sub("", text).strip()
    return text.strip().strip(",").strip()


def _iter_mutual_items(value: object, *, where: str) -> Iterator[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield from value.splitlines()
        return
    if isinstance(value, Sequence):
        for item in value:
            if isinstance(item, str):
                yield item
            elif isinstance(item, Mapping):
                normalised = {_norm_key(k): v for k, v in item.items()}
                picked = normalised.get("url") or normalised.get("name")
                if picked is None:
                    raise IngestError(f"{where}: mutual entry {item!r} has neither name nor url")
                yield str(picked)
            else:
                raise IngestError(f"{where}: mutual entry {item!r} is not a name or a URL")
        return
    raise IngestError(
        f"{where}: 'mutuals' must be a block of one name per line, or a list; got "
        f"{type(value).__name__}"
    )


def _parse_mutuals(value: object, *, where: str) -> tuple[str, ...]:
    out: list[str] = []
    for item in _iter_mutual_items(value, where=where):
        cleaned = clean_mutual(item)
        if cleaned:
            out.append(cleaned)
    return tuple(out)


# --------------------------------------------------------------------------
# URLs (hand-rolled: importing urllib is forbidden in this stage)
# --------------------------------------------------------------------------


def normalize_url(raw: str | None) -> str | None:
    """Drop the query string, fragment and trailing slash from a profile URL.

    Two operators typing the same profile with and without a trailing slash must
    produce byte-identical artifacts, so this runs before any URL is stored.
    """
    if not raw:
        return None
    text = raw.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return text or None


def slug_from_url(raw: str | None) -> str | None:
    """Extract the identifying segment of a LinkedIn profile URL.

    Legacy ``/pub/`` URLs keep their whole path -- the trailing segments are what
    disambiguate them -- while ``/in/`` URLs reduce to the vanity slug or the
    ``ACoA…`` member URN. Case is preserved: those URNs are case-sensitive and
    casefolding them merges distinct humans.
    """
    url = normalize_url(raw)
    if not url:
        return None
    lowered = url.casefold()
    if "/pub/" in lowered:
        return "pub/" + url[lowered.index("/pub/") + len("/pub/") :]
    return url.rsplit("/", 1)[-1] or None


def linkedin_path(raw: str | None) -> str | None:
    """The ``/in/slug`` portion of a LinkedIn URL, host and locale stripped."""
    url = normalize_url(raw)
    if not url:
        return None
    lowered = url.casefold()
    marker = "linkedin.com"
    if marker not in lowered:
        return None
    return url[lowered.index(marker) + len(marker) :] or None


def looks_like_url(text: str) -> bool:
    """True when the human pasted a link rather than typing a name."""
    lowered = text.casefold()
    return "linkedin.com/" in lowered or lowered.startswith(
        ("http://", "https://", "/in/", "/pub/")
    )


# --------------------------------------------------------------------------
# seniority
# --------------------------------------------------------------------------

_SENIORITY_MARKERS: tuple[tuple[Seniority, tuple[str, ...]], ...] = (
    (Seniority.INTERN, ("intern", "internship", "summer analyst", "co-op", "trainee")),
    (Seniority.FOUNDER, ("founder", "co-founder", "cofounder")),
    (
        Seniority.EXECUTIVE,
        (
            "chief ",
            "ceo",
            "cto",
            "cfo",
            "coo",
            "head of",
            "global head",
            "managing director",
            "partner",
            "president",
        ),
    ),
    (Seniority.LEAD, ("vice president", " vp", "vp ", "director", "lead ", " lead", "principal")),
    (Seniority.SENIOR, ("senior", "sr.", "sr ", "staff ")),
    (Seniority.JUNIOR, ("junior", "jr.", "jr ", "graduate", "entry level")),
)


def infer_seniority(title: str) -> Seniority:
    """Map a title to a coarse seniority band, conservatively.

    Nothing else in the pipeline populates ``TargetPerson.seniority`` -- normalize
    only sees the ego's own connections -- so the field is dead weight unless it
    is filled here. Only unambiguous markers are honoured: a bare "Analyst" at a
    hedge fund is not junior, so it stays UNKNOWN rather than being guessed.
    An explicit ``seniority:`` key in the file always wins over this.
    """
    text = f" {title.casefold().strip()} "
    for level, markers in _SENIORITY_MARKERS:
        if any(marker in text for marker in markers):
            return level
    return Seniority.UNKNOWN


# --------------------------------------------------------------------------
# the parsed record
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RawTarget:
    """One target block, validated but not yet resolved against ``people.jsonl``."""

    index: int
    name: str
    firm: TargetFirm
    url: str | None = None
    title: str = ""
    stated: int | None = None
    mutuals: tuple[str, ...] = ()
    complete: bool | None = None
    seniority: Seniority | None = None
    observed_on: date | None = None
    note: str | None = None


def _parse_date(value: object, *, where: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise IngestError(f"{where}: {value!r} is not a date; write it as YYYY-MM-DD") from exc


def _parse_count(value: object, *, where: str) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    text = re.sub(r"(?i)\s*(mutual|shared)?\s*connections?\s*$", "", text).strip()
    text = text.removeprefix("+").rstrip("+")
    if not text:
        return None
    try:
        count = int(text)
    except ValueError as exc:
        raise IngestError(
            f"{where}: mutual count {value!r} is not a whole number; copy the number LinkedIn "
            "shows, or leave it out"
        ) from exc
    if count < 0:
        raise IngestError(f"{where}: mutual count {value!r} is negative")
    return count


def _parse_bool(value: object, *, where: str, field: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"yes", "y", "true", "t", "1"}:
        return True
    if text in {"no", "n", "false", "f", "0"}:
        return False
    raise IngestError(f"{where}: {field}={value!r} is not yes/no")


def _parse_seniority(value: object, *, where: str) -> Seniority | None:
    if value is None:
        return None
    text = _norm_key(value)
    try:
        return Seniority(text)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in Seniority)
        raise IngestError(
            f"{where}: unknown seniority {value!r}; expected one of {allowed}"
        ) from exc


def _canonical_fields(block: Mapping[Any, Any], *, where: str) -> dict[str, Any]:
    """Rename keys to their canonical form; reject anything unrecognised.

    Unknown keys are fatal on purpose: a typo'd ``mutual:`` silently discards the
    only data the human came here to record, and a silent discard is
    indistinguishable from a target that genuinely has no mutuals.
    """
    out: dict[str, Any] = {}
    for raw_key, value in block.items():
        key = _norm_key(raw_key)
        canonical = _TARGET_ALIASES.get(key)
        if canonical is None:
            known = ", ".join(sorted(set(_TARGET_ALIASES.values())))
            raise IngestError(f"{where}: unknown field {str(raw_key).strip()!r}; expected: {known}")
        if canonical in out and canonical != "truncated":
            raise IngestError(f"{where}: field {canonical!r} is given twice")
        out[canonical] = value
    return out


def _parse_target(
    block: Any,
    *,
    index: int,
    source: str,
    default_firm: TargetFirm | None,
    default_observed: date | None,
) -> RawTarget:
    where = f"{source}: target #{index}"
    if not isinstance(block, Mapping):
        raise IngestError(
            f"{where}: expected a block of 'key: value' lines, got {type(block).__name__}"
        )
    fields = _canonical_fields(block, where=where)

    name = str(fields.get("name") or "").strip()
    url = normalize_url(fields.get("url"))
    if not name:
        raise IngestError(f"{where}: 'name' is required (the target's name as LinkedIn shows it)")
    where = f"{source}: target #{index} ({name})"

    firm_value = fields.get("firm")
    if firm_value is None:
        if default_firm is None:
            raise IngestError(
                f"{where}: 'firm' is required; set it on this target, or once at the top of the "
                "file as a default"
            )
        firm = default_firm
    else:
        firm = _parse_firm(firm_value, where=where)

    complete = _parse_bool(fields.get("complete"), where=where, field="complete")
    truncated = _parse_bool(fields.get("truncated"), where=where, field="truncated")
    if truncated is not None:
        if complete is not None and complete is not (not truncated):
            raise IngestError(
                f"{where}: complete={complete} contradicts truncated={truncated}; give one or the "
                "other"
            )
        complete = not truncated

    title = str(fields.get("title") or "").strip()
    return RawTarget(
        index=index,
        name=name,
        firm=firm,
        url=url,
        title=title,
        stated=_parse_count(fields.get("stated"), where=where),
        mutuals=_parse_mutuals(fields.get("mutuals"), where=where),
        complete=complete,
        seniority=_parse_seniority(fields.get("seniority"), where=where),
        observed_on=_parse_date(fields.get("observed_on"), where=where) or default_observed,
        note=(str(fields["note"]).strip() or None) if fields.get("note") is not None else None,
    )


def parse_observations(text: str, *, source: str = "observations.yaml") -> tuple[RawTarget, ...]:
    """Parse the whole file into validated, not-yet-resolved target records.

    Accepts either a bare list of target blocks or a mapping with a ``targets:``
    key plus file-level ``firm:`` / ``observed_on:`` defaults. The mapping form
    exists because a real collection session is one company's People tab in one
    sitting: naming the firm once at the top removes a line of typing from every
    single target.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise IngestError(f"{source}: not valid YAML: {exc}") from exc

    if loaded is None:
        return ()

    default_firm: TargetFirm | None = None
    default_observed: date | None = None
    if isinstance(loaded, Mapping):
        fields: dict[str, Any] = {}
        for raw_key, value in loaded.items():
            key = _norm_key(raw_key)
            canonical = _FILE_ALIASES.get(key)
            if canonical is None:
                known = ", ".join(sorted(set(_FILE_ALIASES.values())))
                raise IngestError(
                    f"{source}: unknown top-level key {str(raw_key).strip()!r}; expected: {known}"
                )
            fields[canonical] = value
        if "targets" not in fields:
            raise IngestError(
                f"{source}: no 'targets:' list found. The file is either a list of target blocks, "
                "or a mapping with a 'targets:' key."
            )
        if fields.get("firm") is not None:
            default_firm = _parse_firm(fields["firm"], where=f"{source}: top-level firm")
        default_observed = _parse_date(fields.get("observed_on"), where=f"{source}: observed_on")
        blocks = fields["targets"]
    else:
        blocks = loaded

    if blocks is None:
        return ()
    if isinstance(blocks, Mapping) or not isinstance(blocks, Sequence) or isinstance(blocks, str):
        raise IngestError(f"{source}: 'targets' must be a list of target blocks")

    return tuple(
        _parse_target(
            block,
            index=i,
            source=source,
            default_firm=default_firm,
            default_observed=default_observed,
        )
        for i, block in enumerate(blocks, start=1)
    )


# --------------------------------------------------------------------------
# source line lookup (so an unresolved name points at where it was typed)
# --------------------------------------------------------------------------


def line_index(text: str) -> dict[str, int]:
    """Map each typed line, raw and cleaned, to its first line number.

    ``yaml.safe_load`` discards positions, and "which of my 400 lines is wrong"
    is the operator's actual question, so it is reconstructed by hand.
    """
    out: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        for form in (stripped, clean_mutual(stripped)):
            if form:
                out.setdefault(form, lineno)
    return out
