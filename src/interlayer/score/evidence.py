"""Evidence assembly: the audit trail that has to exist before a score may.

``ScoredPerson`` refuses to validate with a non-zero score and no evidence, and
that is the design, not a nuisance: a score the operator cannot audit is a score
they cannot act on. So this module inverts the usual order -- evidence items are
built first, each carrying its own contribution, and the numeric components are
derived by summing them. It is then structurally impossible for the total to move
without a sentence explaining why.

Two rules the report depends on:

* ``detail`` is rendered verbatim to a human. Write sentences, not debug strings.
* ``Provenance.OBSERVED`` belongs to mutual-connection readings and nothing else.
  A human reading a name off LinkedIn's own UI is ground truth; everything this
  tool derives from shared affiliations is a guess that is sometimes wrong, and
  the model enforces the pairing so the two can never be quietly conflated.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, get_args

from interlayer.models import EvidenceItem, TargetFirm

__all__ = ["Bucket", "EvidenceKind", "Signal", "condense", "damped"]

EvidenceKind = Literal[
    "observed_mutual",
    "direct_employment",
    "past_employment",
    "shared_employer",
    "shared_school",
    "cluster_membership",
    "title_signal",
    "path_proximity",
]
EVIDENCE_KINDS: tuple[str, ...] = get_args(EvidenceKind)

Bucket = Literal["direct", "alumni", "proximity", "cluster", "title"]
"""Which ``ScoreComponents`` field a signal lands in.

``ScoreComponents`` has five fields and ``ScoreWeights`` has eight weights, so the
mapping is stated once, here, rather than being re-guessed at each call site:
``direct`` collects the adjacency evidence (observed mutuals and every
employment-derived link to a target firm), ``alumni`` collects shared schooling,
``proximity`` the random-walk mass, ``cluster`` the community signal and
``title`` the role heuristic.
"""

MAX_ITEMS_PER_KIND = 6
"""Beyond this the trail stops being readable. The overflow is summed into one
item rather than dropped, so the evidence contributions still add up to the score."""


@dataclass(frozen=True)
class Signal:
    """One evidence item plus where its number should be counted.

    ``firms`` is separate from the item because ``EvidenceItem`` has no firm field
    and must not grow one on this agent's say-so; it drives ``per_firm``, where
    Citadel LLC and Citadel Securities are kept apart because they are different
    companies with different staff.
    """

    item: EvidenceItem
    bucket: Bucket
    firms: tuple[TargetFirm, ...] = field(default=())


def damped(weight: float, rank: int) -> float:
    """Contribution of the ``rank``-th (1-based) independent link of one kind.

    Harmonic damping: the first link is worth the full weight, the second half,
    the third a third. Without it, twenty weak shared-employer links (1.0 each)
    would out-vote a confirmed mutual connection (10.0), inverting the hierarchy
    the weights exist to express. With it, twenty weak links reach ~3.6 -- real,
    but never able to shout down observed evidence.
    """
    if rank < 1:
        raise ValueError("rank is 1-based")
    return weight / rank


def condense(
    signals: Sequence[Signal],
    *,
    summary: Callable[[int, float], str],
    cap: int = MAX_ITEMS_PER_KIND,
) -> list[Signal]:
    """Keep the first ``cap`` signals verbatim and roll the rest into one item.

    The remainder is summarised rather than discarded because the evidence
    contributions must still sum to the score; a trail that quietly omits half the
    total is worse than no trail, since it looks complete.
    """
    if len(signals) <= cap:
        return list(signals)
    head, tail = list(signals[:cap]), list(signals[cap:])
    total = round(sum(s.item.contribution for s in tail), 6)
    firms: list[TargetFirm] = []
    for signal in tail:
        for firm in signal.firms:
            if firm not in firms:
                firms.append(firm)
    template = head[0].item
    head.append(
        Signal(
            item=EvidenceItem(
                kind=template.kind,
                detail=summary(len(tail), total),
                contribution=total,
                provenance=template.provenance,
            ),
            bucket=head[0].bucket,
            firms=tuple(sorted(firms)),
        )
    )
    return head
