"""Tier-2 observed bridges: the only edges here that are not inferences.

A :class:`~interlayer.models.MutualObservation` is a human reading of
``{ego's connections} INTERSECT {target's connections}`` off a 2nd-degree
profile. Every ``bridge_person_id`` in one is *provably* connected to that
target -- LinkedIn rendered it. So any two bridges in the same reading are
provably connected to the same person, which is direct evidence of shared
context and is categorically stronger than "you both list the same employer".

Two consequences, both load-bearing:

* These edges are **exempt from the disparity filter**. A statistical backbone
  is a device for deciding which *guesses* to keep; running it over ground truth
  can only throw ground truth away.
* They are weighted above every possible inferred edge (see
  :data:`~interlayer.graph.weights.OBSERVED_FLOOR`), mirroring
  ``ScoreWeights.observed_mutual`` outranking every inferred signal downstream.

On ``complete`` / ``truncated``: a truncated reading is a partial *list*, not a
set of doubtful names. Presence in it is still something a human saw, so
truncation does not damp the weight. What truncation forbids is treating
absence as evidence of absence -- and this module only ever adds edges for
names that are present, so it never makes that inference.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from interlayer.graph.weights import OBSERVED_FLOOR, observed_bonus
from interlayer.models import MutualObservation

__all__ = ["ObservedEvidence", "ObservedResult", "observed_pairs"]

Pair = tuple[str, str]


@dataclass
class ObservedEvidence:
    """Accumulated observed evidence for one unordered pair of bridge people."""

    weight: float = 0.0
    org_ids: set[str] = field(default_factory=set)
    target_ids: set[str] = field(default_factory=set)


@dataclass
class ObservedResult:
    pairs: dict[Pair, ObservedEvidence] = field(default_factory=dict)
    people: set[str] = field(default_factory=set)
    n_observations: int = 0
    n_truncated: int = 0
    n_unpaired: int = 0


def observed_pairs(
    observations: Iterable[MutualObservation],
    *,
    target_orgs: Mapping[str, str] | None = None,
) -> ObservedResult:
    """Bridge-to-bridge edges implied by each mutual-connections reading.

    ``target_orgs`` maps a ``target_person_id`` to the ``org_id`` of the firm
    that target works for, so the edge can name the context the two people share
    in ``GraphEdge.shared_org_ids``. It is optional: ``targets.jsonl`` is Agent
    3's artifact and the graph must still build without it.

    A reading with a single bridge yields no edge but still contributes a node --
    that person is a real, observed neighbour of a target and must not vanish
    from the graph merely because nobody else was on the list with them.
    """
    orgs = target_orgs or {}
    out = ObservedResult()
    for observation in sorted(observations, key=lambda o: o.target_person_id):
        bridges = tuple(sorted(set(observation.bridge_person_ids)))
        out.n_observations += 1
        out.n_truncated += observation.truncated
        out.people.update(bridges)
        if len(bridges) < 2:
            out.n_unpaired += 1
            continue
        bonus = observed_bonus(len(bridges))
        org_id = orgs.get(observation.target_person_id)
        for i, left in enumerate(bridges):
            for right in bridges[i + 1 :]:
                if left == right:  # pragma: no cover - deduped above; a self-loop raises
                    continue
                evidence = out.pairs.get((left, right))
                if evidence is None:
                    evidence = ObservedEvidence(weight=OBSERVED_FLOOR)
                    out.pairs[(left, right)] = evidence
                evidence.weight += bonus
                evidence.target_ids.add(observation.target_person_id)
                if org_id is not None:
                    evidence.org_ids.add(org_id)
    return out
