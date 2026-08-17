"""SHA-256 over the canonical result — the cheapest regression test available.

Identical inputs must give an identical fingerprint across processes, virtualenvs
and ``PYTHONHASHSEED`` values. R3 demonstrated this end to end: the prototype
pipeline produced byte-identical fingerprints under ``PYTHONHASHSEED=1`` and
``PYTHONHASHSEED=999`` in two different virtualenvs — *provided* node order is
sorted. Sorting is what makes the fingerprint meaningful; the hash only proves it.

Floats are serialised through a fixed 12-decimal format rather than ``repr``.
``repr`` of a float is stable within CPython, but formatting removes any
dependence on it and matches the 12 dp rounding already applied to projection
weights, so a difference in the last bit of a BLAS accumulation cannot flip the
digest.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from interlayer.core.models import Brokerage, Cluster, Coverage, NextTarget

FLOAT_FORMAT = "{:.12f}"


def _fmt(value: float) -> str:
    formatted = FLOAT_FORMAT.format(float(value) + 0.0)  # +0.0 normalises -0.0
    return "0.000000000000" if formatted == "-0.000000000000" else formatted


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def membership_fingerprint(membership: Mapping[str, int]) -> str:
    """R3's definition: SHA-256 of the sorted ``{member_id: cluster_id}`` JSON."""
    return _digest({str(k): int(v) for k, v in membership.items()})


def result_fingerprint(
    *,
    membership: Mapping[str, int],
    clusters: Sequence[Cluster],
    brokerage: Sequence[Brokerage],
    coverage: Coverage,
    next_targets: Sequence[NextTarget],
    params: Mapping[str, Any] | None = None,
) -> str:
    """SHA-256 over the whole canonical result.

    Strictly stronger than :func:`membership_fingerprint`: a change to a score, a
    label, a coverage count or the harvest ordering moves this digest, so it
    catches regressions that leave the partition intact.
    """
    payload = {
        "membership": {str(k): int(v) for k, v in sorted(membership.items())},
        "clusters": [
            {
                "cluster_id": int(c.cluster_id),
                "label": c.label,
                "member_ids": list(c.member_ids),
                "top_targets": list(c.top_targets),
                "stability": _fmt(c.stability),
                "score_sum": _fmt(c.score_sum),
            }
            for c in clusters
        ],
        "brokerage": [
            {
                "member_id": b.member_id,
                "reach": int(b.reach),
                "rarity": _fmt(b.rarity),
                "betweenness": _fmt(b.betweenness),
                "effective_size": _fmt(b.effective_size),
                "constraint": _fmt(b.constraint),
                "autonomy": _fmt(b.autonomy),
                "composite": _fmt(b.composite),
            }
            for b in sorted(brokerage, key=lambda x: x.member_id)
        ],
        "coverage": {
            "n_targets_total": int(coverage.n_targets_total),
            "n_targets_harvested": int(coverage.n_targets_harvested),
            "n_members_total": int(coverage.n_members_total),
            "n_bridges": int(coverage.n_bridges),
        },
        "next_targets": [
            {
                "target_id": t.target_id,
                "priority": _fmt(t.priority),
                "n_known_bridges": int(t.n_known_bridges),
                "n_clusters_touched": int(t.n_clusters_touched),
                "novelty": _fmt(t.novelty),
            }
            for t in next_targets
        ],
        "params": _canonical_params(params or {}),
    }
    return _digest(payload)


def _canonical_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Params affect the result, so they belong in the digest — but only as scalars."""
    out: dict[str, Any] = {}
    for key in sorted(params):
        value = params[key]
        if isinstance(value, float):
            out[key] = _fmt(value)
        elif isinstance(value, bool | int | str) or value is None:
            out[key] = value
        elif isinstance(value, Mapping):
            out[key] = _canonical_params(value)
        elif isinstance(value, Sequence):
            out[key] = [_fmt(v) if isinstance(v, float) else v for v in value]
        else:
            out[key] = str(value)
    return out


__all__ = ["membership_fingerprint", "result_fingerprint"]
