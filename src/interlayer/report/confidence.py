"""Confidence bands (research §3.7 P-34/P-35).

``ScoredPerson.score`` is an unbounded additive quantity whose magnitude depends
on how many weights happened to fire. It is a *ranking*, not a probability, and
showing it as one would be exactly the misrepresentation the accuracy principle
forbids. So the report carries a second number: a bounded confidence in
``[0, 1]`` derived from the *kinds* of evidence present, never from the score.

The design rule that makes provenance unmissable arithmetically, not just
visually:

    confidence == 1.0  if and only if at least one evidence item was OBSERVED
    confidence <= 0.79 for everything else

which leaves the band boundary at 0.80 permanently empty for inference. "Strong
signal" therefore *means* "a human read this off LinkedIn", and no amount of
piled-up inference can reach it. This matches the CLI's own first-run notice —
"results below a confidence of 1.0 are INFERRED".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from interlayer.models import EvidenceItem, Provenance

__all__ = [
    "BANDS",
    "INFERRED_CEILING",
    "SPECULATIVE_MAX",
    "Band",
    "band_for",
    "confidence_for",
    "mean_confidence",
]

#: Base confidence contributed by the strongest evidence kind present. Ordered
#: by how directly the claim rests on something written down about *this* person
#: rather than about someone near them.
_KIND_BASE: dict[str, float] = {
    "observed_mutual": 1.00,
    "direct_employment": 0.72,
    "past_employment": 0.52,
    "shared_employer": 0.42,
    "shared_school": 0.34,
    "path_proximity": 0.32,
    "cluster_membership": 0.30,
    "title_signal": 0.26,
}

#: Each *additional distinct* evidence kind corroborates a little. Small on
#: purpose: five weak inferred signals about the same person are still five weak
#: inferred signals, and stacking them into a confident-looking number is how a
#: guess turns into a "fact" once the report is forwarded.
_CORROBORATION_STEP = 0.04
_MAX_CORROBORATION = 0.16

#: No inferred result may reach the Strong band. See the module docstring.
INFERRED_CEILING = 0.79

#: Below this the row is collapsed rather than shown (P-35).
SPECULATIVE_MAX = 0.30

Band = str

#: (lower_bound_inclusive, slug, label). Descending; first match wins.
BANDS: tuple[tuple[float, str, str], ...] = (
    (0.80, "strong", "Strong signal"),
    (0.55, "moderate", "Moderate signal"),
    (0.30, "weak", "Weak signal"),
    (0.00, "speculative", "Speculative"),
)


def band_for(confidence: float) -> tuple[str, str]:
    """Map a confidence to its ``(slug, label)``. Never returns "confirmed"."""
    for lower, slug, label in BANDS:
        if confidence >= lower:
            return slug, label
    return "speculative", "Speculative"


def confidence_for(evidence: Sequence[EvidenceItem]) -> float:
    """Bounded confidence for one scored person, from evidence kinds alone.

    Deliberately ignores the numeric score: a large score means "many weights
    fired", which is a statement about this tool's configuration, not about how
    sure anyone should be that a real relationship exists.
    """
    if not evidence:
        return 0.0
    if any(e.provenance is Provenance.OBSERVED for e in evidence):
        return 1.0
    kinds = {e.kind for e in evidence}
    base = max(_KIND_BASE.get(k, 0.25) for k in kinds)
    # An `observed_mutual` item that is somehow marked INFERRED must not inherit
    # the observed base; it is an unverified claim like any other.
    base = min(base, INFERRED_CEILING)
    bonus = min(_CORROBORATION_STEP * (len(kinds) - 1), _MAX_CORROBORATION)
    return round(min(base + bonus, INFERRED_CEILING), 4)


def mean_confidence(values: Iterable[float]) -> float:
    """Mean confidence for a cluster header (§3.8 item 5). Empty -> 0.0."""
    vals = list(values)
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)
