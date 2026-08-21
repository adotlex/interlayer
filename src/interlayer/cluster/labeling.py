"""Naming clusters, because a cluster nobody can name is a cluster nobody can use.

A partition is only half an answer. "These 38 people" is not actionable; "these
38 people are the Two Sigma / Jane Street quant-research cohort" is. Every label
here is derived from evidence already in the artifacts -- dominant organisations
and dominant job titles among the members -- so a label can always be checked
against ``top_orgs`` and ``top_titles`` on the same record.

One deliberate asymmetry: ``top_orgs`` reports raw dominance (honest counts the
operator can audit), but the *label* prefers organisations that are
over-represented in this cluster relative to the graph as a whole. Wave 1
measured a single employer producing 65% of all edges before weighting; without
that lift correction the largest employer would end up in the name of nearly
every cluster, which is exactly as useless as no name at all.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from interlayer.models import Affiliation, Person, normalize_key

__all__ = ["ClusterLabel", "label_for", "orgs_by_person", "titles_by_person"]

MAX_LABEL_ORGS = 2
"""Two organisations is a name; four is a list, and lists do not get remembered."""

TOP_N = 5


def orgs_by_person(affiliations: Iterable[Affiliation]) -> dict[str, frozenset[str]]:
    """Map person -> the set of orgs they have ever been affiliated with.

    Deduplicated per person so that three consecutive stints at one employer count
    once: cluster labels describe *how many people* share an employer, not how
    many rows the export happened to contain.
    """
    acc: dict[str, set[str]] = {}
    for aff in affiliations:
        acc.setdefault(aff.person_id, set()).add(aff.org_id)
    return {pid: frozenset(orgs) for pid, orgs in acc.items()}


def titles_by_person(
    people: Iterable[Person], affiliations: Iterable[Affiliation]
) -> dict[str, frozenset[str]]:
    """Map person -> the distinct raw titles they have held.

    Titles are taken from both sources because either can be missing: the export's
    ``Position`` column populates ``Person.positions``, while ``normalize`` may
    carry a cleaner title onto the ``Affiliation``.
    """
    acc: dict[str, set[str]] = {}
    for person in people:
        for pos in person.positions:
            if pos.title_raw.strip():
                acc.setdefault(person.person_id, set()).add(pos.title_raw.strip())
    for aff in affiliations:
        if aff.title and aff.title.strip():
            acc.setdefault(aff.person_id, set()).add(aff.title.strip())
    return {pid: frozenset(titles) for pid, titles in acc.items()}


def _rank(
    counts: Counter[str], display: Mapping[str, str] | None = None
) -> tuple[tuple[str, int], ...]:
    """Top items by count, ties broken by name so the output never flaps."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    if display is None:
        return tuple(ranked)
    return tuple((display.get(key, key), count) for key, count in ranked)


def _modal_display(raw_values: Iterable[str]) -> dict[str, str]:
    """Choose one display form per normalised key: the most common raw spelling.

    "Quantitative Researcher", "quantitative researcher" and "Quantitative
    Researcher " are one title; which spelling the operator is shown should not
    depend on which row happened to be read first.
    """
    variants: dict[str, Counter[str]] = {}
    for raw in raw_values:
        variants.setdefault(normalize_key(raw), Counter())[raw] += 1
    return {
        key: sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        for key, counter in variants.items()
    }


class ClusterLabel:
    """A rendered cluster name alongside the counts that justify it."""

    __slots__ = ("label", "top_orgs", "top_titles")

    def __init__(
        self,
        label: str,
        top_orgs: tuple[tuple[str, int], ...],
        top_titles: tuple[tuple[str, int], ...],
    ) -> None:
        self.label = label
        self.top_orgs = top_orgs
        self.top_titles = top_titles


def label_for(
    member_ids: Sequence[str],
    *,
    person_orgs: Mapping[str, frozenset[str]],
    person_titles: Mapping[str, frozenset[str]],
    org_names: Mapping[str, str],
    global_org_counts: Mapping[str, int],
    n_people: int,
) -> ClusterLabel:
    """Name one cluster from the organisations and titles of its members.

    ``global_org_counts`` and ``n_people`` supply the background rate an
    organisation is measured against, so a 200,000-person employer that everybody
    passed through does not out-shout the 40-person shop that actually explains
    why these people know each other.
    """
    size = len(member_ids)
    org_counts: Counter[str] = Counter()
    title_raws: list[str] = []
    title_counts: Counter[str] = Counter()
    for pid in member_ids:
        for org_id in sorted(person_orgs.get(pid, frozenset())):
            org_counts[org_id] += 1
        for title in sorted(person_titles.get(pid, frozenset())):
            title_raws.append(title)
            title_counts[normalize_key(title)] += 1

    top_orgs = _rank(org_counts, display=org_names)
    top_titles = _rank(title_counts, display=_modal_display(title_raws))

    if not org_counts and not title_counts:
        return ClusterLabel(f"Unlabelled group of {size}", top_orgs, top_titles)

    # Lift = share inside this cluster over share across the whole network. The
    # count multiplier keeps a single person's obscure employer from winning on
    # lift alone.
    def lift_score(item: tuple[str, int]) -> tuple[float, str]:
        org_id, count = item
        background = max(global_org_counts.get(org_id, count), 1) / max(n_people, 1)
        return (-(count / size) / background * count, org_id)

    lifted = sorted(org_counts.items(), key=lift_score)[:MAX_LABEL_ORGS]
    org_part = " + ".join(org_names.get(org_id, org_id) for org_id, _ in lifted)
    title_part = top_titles[0][0] if top_titles else ""

    if org_part and title_part:
        return ClusterLabel(f"{org_part} - {title_part}", top_orgs, top_titles)
    return ClusterLabel(
        org_part or title_part or f"Unlabelled group of {size}", top_orgs, top_titles
    )
