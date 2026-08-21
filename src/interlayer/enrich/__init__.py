"""Tier 2: fold hand-collected mutual-connection readings into the pipeline.

Why this stage exists
=====================
LinkedIn exposes no API, and sells no data, that returns another member's
connection list. Every inferred edge elsewhere in this pipeline is therefore a
guess that is *always wrong sometimes*. But LinkedIn's own UI renders

    { the operator's connections } INTERSECT { a target's connections }

as an enumerable list on any 2nd-degree profile, for free, and it survives the
target hiding their connections list -- the privacy setting quant-finance
professionals most commonly enable. A human reading that list produces **ground
truth**. This stage is the doorway that ground truth comes through.

It reads a file a person typed while looking at a browser (see
``docs/collecting-observations.md`` for the workflow and
``docs/observations-template.yaml`` for a file to copy) and emits:

===========================  ==========================================
``targets.jsonl``            ``TargetPerson`` -- who was looked at
``observations.jsonl``       ``MutualObservation`` -- the true bridges
``unresolved_mutuals.jsonl`` entries that could not be matched, and why
===========================  ==========================================

THE TRUNCATION SEMANTIC -- read this before consuming ``observations.jsonl``
===========================================================================
``MutualObservation.complete`` answers exactly one question:

    **May the absence of a person from ``bridge_person_ids`` be treated as
    evidence that no such edge exists?**

``complete=True`` -> yes. The human saw the whole list, every name in it
resolved, and the count matches what LinkedIn stated.

``complete=False`` (equivalently ``truncated=True``, which is True whenever
fewer ids were resolved than LinkedIn stated) -> **no, and this is not a
formality.** The list was paginated, cut short, or partially unresolvable. The
recorded bridges are still true positives -- LinkedIn asserted every one of
them -- but the set is a *sample*, not a census. Any downstream computation of
the form "X is not in the mutual list, therefore X is not adjacent" is invalid
against such a record, and any per-target coverage ratio computed from it is a
lower bound, not a measurement.

This stage fails safe into ``complete=False``. It is set that way whenever:

* fewer mutuals resolved than the count LinkedIn stated;
* **any** recorded entry failed to resolve (a hole in the reading is a hole in
  the set, exactly like a truncated page);
* the human recorded no stated count at all, so completeness is unverifiable;
* the human wrote ``truncated: yes``.

A human writing ``complete: yes`` cannot override arithmetic that contradicts
it: the flag flips back to False and the operator is told. Incomplete
observations also carry a plain-English ``note`` saying so, so the fact survives
into the report even if a consumer ignores the boolean.

NO SCRAPER, NO NETWORK
======================
This stage does not import ``requests``, ``httpx``, ``urllib``, ``aiohttp``,
``selenium`` or ``playwright``, and it never will -- not optionally, not behind
a flag. URL handling is hand-rolled in ``parsing`` for exactly that reason.
Automating a logged-in LinkedIn session breaches the User Agreement, and the
dominant risk is not litigation: it is permanently losing the account this whole
workflow depends on.

MISSING INPUT IS NOT AN ERROR
=============================
Tier 2 is optional. A user may run the entire pipeline with only their CSV
export. With no observation file, this stage writes empty artifacts, says so,
and returns.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from interlayer.config import Settings
from interlayer.enrich.parsing import (
    ENV_VAR,
    RawTarget,
    find_observation_file,
    infer_seniority,
    line_index,
    parse_observations,
)
from interlayer.enrich.resolve import MatchCandidate, PersonIndex, UnresolvedMutual
from interlayer.io import read_jsonl, write_jsonl
from interlayer.models import MutualObservation, Person, TargetPerson

__all__ = [
    "UNRESOLVED_FILENAME",
    "MatchCandidate",
    "UnresolvedMutual",
    "run",
    "unresolved_path",
]

UNRESOLVED_FILENAME = "unresolved_mutuals.jsonl"
"""Entries the operator must fix by hand. Owned by this stage."""

_MAX_PRINTED = 10

console = Console(stderr=True)


def unresolved_path(cfg: Settings) -> Path:
    """Where unmatched mutuals are written for the operator to repair."""
    return cfg.artifact(UNRESOLVED_FILENAME)


# --------------------------------------------------------------------------
# accumulation
# --------------------------------------------------------------------------


@dataclass
class _Collected:
    """One target's readings, possibly merged across several file entries.

    A human may record the same profile twice -- page one on Monday, page two on
    Tuesday, or simply by accident. Emitting two records for one
    ``target_person_id`` would double-count that person's bridges downstream, so
    entries are merged here and the conservative value wins every conflict.
    """

    target: TargetPerson
    bridges: set[str] = field(default_factory=set)
    stated: int | None = None
    override: bool | None = None
    observed_on: date | None = None
    notes: list[str] = field(default_factory=list)
    recorded: int = 0
    unresolved: int = 0
    duplicates: int = 0
    contradicted: bool = False

    def absorb(self, other: TargetPerson) -> None:
        """Keep whichever record carries more, without inventing anything."""
        merged = self.target.model_copy(
            update={
                "linkedin_url": self.target.linkedin_url or other.linkedin_url,
                "linkedin_slug": self.target.linkedin_slug or other.linkedin_slug,
                "title_raw": self.target.title_raw or other.title_raw,
            }
        )
        if merged.seniority.value == "unknown":
            merged = merged.model_copy(update={"seniority": other.seniority})
        self.target = merged


def _build_target(raw: RawTarget) -> TargetPerson:
    seniority = raw.seniority or infer_seniority(raw.title)
    return TargetPerson.make(
        raw.name,
        raw.firm,
        linkedin_url=raw.url,
        title_raw=raw.title,
        seniority=seniority,
        mutual_count=raw.stated,
    )


def _completeness(item: _Collected) -> tuple[bool, list[str]]:
    """Decide ``complete`` and explain it. Fails safe to False.

    Returns the flag plus the human-readable reasons, which are appended to the
    observation's ``note`` so the fact survives into the report even for a
    consumer that ignores the boolean.
    """
    resolved = len(item.bridges)
    reasons: list[str] = []

    if item.stated is None:
        complete = item.override is True
        if not complete:
            reasons.append(
                f"no stated mutual count was recorded, so a reading of {resolved} cannot be "
                "confirmed complete"
            )
    else:
        derived = resolved >= item.stated
        complete = derived if item.override is not False else False
        if not derived:
            reasons.append(
                f"resolved {resolved} of the {item.stated} mutual connections LinkedIn stated"
            )
        if item.override is False:
            reasons.append("recorded as truncated by the collector")

    if item.unresolved:
        complete = False
        reasons.append(
            f"{item.unresolved} recorded name(s) could not be matched to a connection "
            f"(see {UNRESOLVED_FILENAME})"
        )

    if item.override is True and not complete:
        item.contradicted = True
        reasons.append("marked complete by the collector, but the counts say otherwise")

    return complete, reasons


def _finalise(item: _Collected) -> MutualObservation:
    complete, reasons = _completeness(item)
    parts = list(item.notes)
    if not complete:
        parts.append(
            "INCOMPLETE: " + "; ".join(reasons) + ". Absence from this list is NOT evidence "
            "of absence."
        )
    return MutualObservation(
        target_person_id=item.target.target_person_id,
        bridge_person_ids=tuple(sorted(item.bridges)),
        observed_on=item.observed_on,
        stated_count=item.stated,
        complete=complete,
        note="; ".join(parts) or None,
    )


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def _describe(entry: UnresolvedMutual, source: str) -> str:
    """One line an operator can act on: where they typed it, and what went wrong.

    Printed with ``markup=False`` -- a person's name is arbitrary text and rich
    would eat anything in square brackets as a style tag.
    """
    where = f"{source}:{entry.source_line}" if entry.source_line else source
    near = ", ".join(f"{c.full_name} ({c.score:g})" for c in entry.candidates[:2])
    tail = f" -- closest: {near}" if near else ""
    return f"  {where}  {entry.raw!r}  ({entry.reason}){tail}"


def _empty(cfg: Settings, reason: str) -> None:
    """Write the empty artifact set. Tier 2 is optional; downstream stages read
    these files unconditionally, so they must exist even when there is nothing
    in them."""
    write_jsonl(cfg.targets_path, [])
    write_jsonl(cfg.observations_path, [])
    write_jsonl(unresolved_path(cfg), [])
    console.print(f"[yellow]enrich:[/] {reason}")
    console.print(
        "[dim]enrich: results will be INFERRED only. See docs/collecting-observations.md "
        "to add ground truth.[/]"
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def run(cfg: Settings) -> None:
    """Read the hand-collected observation file and emit Tier 2 artifacts."""
    lookup = find_observation_file(cfg)

    if lookup.path is None:
        if lookup.explicit is not None:
            console.print(
                f"[red]enrich: {ENV_VAR}={lookup.explicit} does not exist[/] -- check the path; "
                "no observations were read"
            )
            _empty(cfg, "no mutual-connection observations were read (Tier 2 absent)")
            return
        searched = ", ".join(str(p) for p in lookup.searched)
        _empty(cfg, f"no observation file found (Tier 2 absent). Looked in: {searched}")
        return

    source = str(lookup.path)
    text = lookup.path.read_text(encoding="utf-8")
    raw_targets = parse_observations(text, source=source)
    if not raw_targets:
        _empty(cfg, f"{source} contains no targets (Tier 2 absent)")
        return

    people: list[Person] = read_jsonl(cfg.people_path, Person, produced_by="ingest")
    index = PersonIndex(people)
    lines = line_index(text)

    collected: dict[str, _Collected] = {}
    unresolved: list[UnresolvedMutual] = []
    reasons: Counter[str] = Counter()
    recorded_total = 0

    for raw in raw_targets:
        target = _build_target(raw)
        item = collected.get(target.target_person_id)
        if item is None:
            item = _Collected(target=target)
            collected[target.target_person_id] = item
        else:
            item.absorb(target)

        if raw.stated is not None:
            item.stated = raw.stated if item.stated is None else max(item.stated, raw.stated)
        if raw.observed_on is not None:
            item.observed_on = (
                raw.observed_on
                if item.observed_on is None
                else max(item.observed_on, raw.observed_on)
            )
        if raw.note:
            item.notes.append(raw.note)
        if raw.complete is not None:
            item.override = (
                raw.complete if item.override is None else (item.override and raw.complete)
            )

        for entry in raw.mutuals:
            recorded_total += 1
            item.recorded += 1
            outcome = index.resolve(entry, accept=cfg.match_accept, review=cfg.match_review)
            if outcome.person_id is not None:
                if outcome.person_id in item.bridges:
                    item.duplicates += 1
                item.bridges.add(outcome.person_id)
                continue
            item.unresolved += 1
            reason = outcome.reason or "no_match"
            reasons[reason] += 1
            unresolved.append(
                UnresolvedMutual(
                    raw=entry,
                    reason=reason,
                    target_person_id=target.target_person_id,
                    target_name=target.full_name,
                    firm=target.firm,
                    source_line=lines.get(entry),
                    candidates=tuple(outcome.candidates[:3]),
                    hint=outcome.hint,
                )
            )

    items = sorted(collected.values(), key=lambda c: c.target.target_person_id)
    observations = [_finalise(item) for item in items]
    targets = [item.target for item in items]
    unresolved.sort(key=lambda u: u.sort_key)

    write_jsonl(cfg.targets_path, targets)
    write_jsonl(cfg.observations_path, observations)
    write_jsonl(unresolved_path(cfg), unresolved)

    _summarise(
        cfg,
        source=source,
        raw_count=len(raw_targets),
        items=items,
        observations=observations,
        unresolved=unresolved,
        reasons=reasons,
        recorded_total=recorded_total,
        pool=len(index),
    )


def _summarise(
    cfg: Settings,
    *,
    source: str,
    raw_count: int,
    items: list[_Collected],
    observations: list[MutualObservation],
    unresolved: list[UnresolvedMutual],
    reasons: Counter[str],
    recorded_total: int,
    pool: int,
) -> None:
    """Tell the operator what happened. Every unmatched entry is named."""
    bridges = sorted({pid for obs in observations for pid in obs.bridge_person_ids})
    merged = raw_count - len(items)
    duplicates = sum(item.duplicates for item in items)

    console.print(
        f"[green]enrich:[/] {len(items)} target(s) from {source}"
        + (f" ({merged} duplicate entr(y/ies) merged)" if merged else "")
    )
    console.print(
        f"enrich: resolved {recorded_total - len(unresolved)}/{recorded_total} recorded mutuals "
        f"against {pool} connection(s) -> {len(bridges)} distinct bridge(s)"
    )
    if duplicates:
        console.print(f"[yellow]enrich:[/] {duplicates} mutual(s) were recorded more than once")

    incomplete = [obs for obs in observations if not obs.complete]
    if incomplete:
        console.print(
            f"[yellow]enrich: {len(incomplete)}/{len(observations)} observation(s) are "
            f"INCOMPLETE[/] -- absence from those lists is NOT evidence of absence"
        )
    contradicted = [item for item in items if item.contradicted]
    for item in contradicted:
        console.print(
            f"[red]enrich:[/] {escape(repr(item.target.full_name))} is marked complete but only "
            f"{len(item.bridges)} of {item.stated} mutuals resolved; treating it as INCOMPLETE"
        )

    if not unresolved:
        console.print(f"[dim]enrich: every recorded mutual resolved; {unresolved_path(cfg)}[/]")
        return

    breakdown = ", ".join(f"{n} {reason}" for reason, n in sorted(reasons.items()))
    console.print(
        f"[yellow]enrich: {len(unresolved)} mutual(s) NOT resolved[/] ({breakdown}); "
        f"written to {unresolved_path(cfg)}. Nothing was guessed and nothing was dropped -- "
        "paste the profile URL into the observation file and re-run."
    )
    for entry in unresolved[:_MAX_PRINTED]:
        console.print(_describe(entry, source), markup=False)
    if len(unresolved) > _MAX_PRINTED:
        console.print(f"  ... and {len(unresolved) - _MAX_PRINTED} more")
