"""Step 11: percentile ranks, the weighted composite, and the Wilson lower bound.

Explainability was the stated priority, so the score is a weighted mean of
percentile ranks rather than a z-score blend or a learned model. Every component
answers "what percentile of your network is this person on this axis", which
composes into a sentence a user can act on. The weights are a documented prior,
not a fit — there is no labelled data — and they are config-overridable.

Reach dominates at 0.35 because it is the thing the user can most directly act
on; rarity takes 0.25 because it separates a valuable path from a crowded one;
the three structural measures split the remaining 0.40 and are correlated with
each other, so no single one dominates.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from interlayer.graph.brokerage import RawMetrics

#: R3 §3.2. Must sum to 1.0 so the composite stays in [0, 1].
DEFAULT_WEIGHTS: dict[str, float] = {
    "reach": 0.35,
    "rarity": 0.25,
    "betweenness": 0.15,
    "effective_size": 0.15,
    "autonomy": 0.10,
}

#: z for a 95% Wilson interval.
WILSON_Z = 1.96


def percentile_rank(values: Mapping[str, float]) -> dict[str, float]:
    """Average-rank percentiles in ``(0, 1]``, ties sharing the mean rank.

    Sorted by ``(value, key)`` so that the ordering — and therefore the ranks — is
    stable regardless of dict insertion order. Ties genuinely share a rank: three
    members on the same reach must not be separated by an accident of iteration.

    A single member scores 1.0 rather than NaN (T8.2).
    """
    n = len(values)
    if n == 0:
        return {}

    ordered = sorted(values.items(), key=lambda kv: (kv[1], kv[0]))
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and ordered[j + 1][1] == ordered[i][1]:
            j += 1
        # ranks are 1-based; a tie block spanning positions i..j shares their mean
        mean_rank = (i + 1 + j + 1) / 2.0
        pct = mean_rank / n
        for k in range(i, j + 1):
            out[ordered[k][0]] = pct
        i = j + 1
    return out


def percentile_components(
    metrics: Mapping[str, RawMetrics],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> dict[str, dict[str, float]]:
    """Percentile-rank each weighted component across the bridge set.

    Returns ``{component: {member_id: percentile}}``.
    """
    components = {name: {} for name in weights}  # type: dict[str, dict[str, float]]
    for name in weights:
        raw = {mid: m.as_components()[name] for mid, m in metrics.items()}
        components[name] = percentile_rank(raw)
    return components


def composite_score(
    metrics: Mapping[str, RawMetrics],
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> dict[str, float]:
    """``0.35*reach + 0.25*rarity + 0.15*betweenness + 0.15*eff_size + 0.10*autonomy``.

    All five terms are percentile ranks, so the result is in ``[0, 1]`` and is
    unit-free.
    """
    if not metrics:
        return {}
    components = percentile_components(metrics, weights)
    total = sum(weights.values())
    scale = 1.0 / total if total else 0.0
    return {
        mid: sum(weights[name] * components[name][mid] for name in weights) * scale
        for mid in metrics
    }


def wilson_lb(k: int, n: int, z: float = WILSON_Z) -> float:
    """Wilson score interval lower bound on ``k/n``.

    Used for reach: with only 5 targets harvested, observing 3 hits supports a
    true reach share of only >= 23%, which is the honest way to say how little 5
    samples tell you. ``wilson_lb(0, 0)`` is 0.0 rather than an error — no
    observations is a legitimate state, not a bug.
    """
    if n <= 0:
        return 0.0
    p = k / n
    denominator = 1.0 + (z * z) / n
    centre = p + (z * z) / (2 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + (z * z) / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


__all__ = [
    "DEFAULT_WEIGHTS",
    "WILSON_Z",
    "composite_score",
    "percentile_components",
    "percentile_rank",
    "wilson_lb",
]
