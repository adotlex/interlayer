"""Real-world firm headcounts, for the size-damping term.

``n_f`` -- how many of the operator's connections sit at firm f -- is the wrong
denominator for damping: twenty connections at a forty-person startup and twenty
at a 200,000-person bank produce identical Newman weights but wildly different
odds of actually knowing each other. The gazetteer carries ``approx_headcount``
for exactly this, and this module is the lookup from a normalised
:class:`~interlayer.models.Org` back to it.

**The fallback is biased and the bias is one-directional.** An org the gazetteer
does not know falls back to ``size_f = n_f``, which is a *lower bound* on the
true headcount, so unknown orgs are systematically **under**-damped: a
200,000-person employer that never made it into the gazetteer, with 40 people in
the export, is damped as if it had 40 employees (1/log(40+e) = 0.27 rather than
0.08) and keeps roughly three times the weight it deserves. That is the correct
direction to fail -- it preserves a possibly-real edge rather than deleting it --
but it means the headline "one employer dominates" failure mode can only be
fully fixed for orgs the gazetteer knows. ``graph_stats.json`` reports how many
orgs took the fallback so the operator can see how much of the graph is exposed
to it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from interlayer.errors import GraphError
from interlayer.models import Org, normalize_key

__all__ = ["FirmSizes", "load_firm_sizes"]


@dataclass(frozen=True)
class FirmSizes:
    """Headcount lookup: gazetteer key first, then normalised name/alias."""

    by_key: dict[str, int]
    by_alias: dict[str, int]

    def headcount(self, org: Org) -> int | None:
        """Best available real-world headcount for ``org``, or None if unknown.

        ``TargetFirm`` members and gazetteer keys are the same strings by
        construction (``jane_street``, ``citadel_llc``, ``citadel_securities``),
        so a resolved target firm is an exact, alias-free hit.
        """
        if org.target_firm is not None:
            known = self.by_key.get(str(org.target_firm))
            if known is not None:
                return known
        for candidate in (org.name, *org.aliases):
            known = self.by_alias.get(normalize_key(candidate))
            if known is not None:
                return known
        return None

    def __len__(self) -> int:
        return len(self.by_key)


def _entities(raw: object, path: Path) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        raise GraphError(f"gazetteer {path} must contain a mapping")
    entities = raw.get("entities") or []
    if not isinstance(entities, list):
        raise GraphError(f"gazetteer {path}: 'entities' must be a list")
    return [e for e in entities if isinstance(e, dict)]


def load_firm_sizes(path: Path) -> FirmSizes:
    """Index ``approx_headcount`` by gazetteer key and by normalised alias.

    A *missing* gazetteer degrades to an empty index -- every org then takes the
    documented ``n_f`` fallback and the graph still builds -- but an
    *unparseable* one raises, because silently weighting the whole graph wrong is
    worse than failing loudly.

    Headcounts of 0 (defunct entities such as Citadel Broadcasting) are treated
    as unknown rather than as "tiny": 0 would sail through the damping term as
    the maximum possible multiplier and hand a dead company the strongest edges
    in the graph.

    Aliases shared by entities with *different* headcounts are dropped from the
    index entirely. "Citadel" is the canonical name of the fund and a prefix of
    six unrelated firms; guessing which one an ambiguous string meant is Agent
    2's job, behind the negative gazetteer, not something to redo here with a
    different and worse answer.
    """
    if not path.is_file():
        return FirmSizes({}, {})
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GraphError(f"gazetteer {path} could not be read: {exc}") from exc

    by_key: dict[str, int] = {}
    alias_counts: defaultdict[str, set[int]] = defaultdict(set)
    for entity in _entities(raw, path):
        key = entity.get("key")
        headcount = entity.get("approx_headcount")
        if not isinstance(key, str) or not isinstance(headcount, int) or headcount <= 0:
            continue
        by_key[key] = headcount
        names = [entity.get("canonical_name"), *(entity.get("aliases") or [])]
        for name in names:
            if isinstance(name, str) and name.strip():
                alias_counts[normalize_key(name)].add(headcount)

    by_alias = {
        alias: next(iter(sizes)) for alias, sizes in alias_counts.items() if len(sizes) == 1
    }
    return FirmSizes(by_key, by_alias)
