"""Step 7: sparse one-mode projection of the bipartite graph onto the members.

``P = A @ diags(1 / max(d_T, 1)) @ A.T`` — resource-allocation weighting, Zhou et
al. 2007. Measured at 0.02s on 8,000 x 1,600.

**Why RA and not a raw co-count.** A single high-degree target hands every pair of
its neighbours a co-count of 1 — full weight — even when they share nothing else.
R3 measured this directly: a degree-100 target in a graph with median target degree
7 gives an unrelated pair weight **1.0000** under raw count, **0.2171** under
Adamic-Adar and **0.0100** under RA. Raw count manufactures a fake cluster out of
one popular recruiter; AA under-corrects because ``1/log d`` decays too slowly.
Jaccard and cosine normalise by *member* degree, which is the wrong axis entirely.

``count`` and ``adamic_adar`` are kept as options because they are the contrast
arms of the T1.3/T1.4/T4 regression tests, not because either is a good default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import scipy.sparse as sp

from interlayer.graph.build import BipartiteGraph

Weighting = Literal["ra", "count", "adamic_adar"]

#: R3 §2.4 rule 3. Float accumulation order in a sparse matmul is not stable
#: across BLAS builds; 12 dp is well inside double precision and well outside any
#: plausible accumulation drift, so rounding here removes the last source of
#: platform-dependent Leiden tie-breaks.
ROUND_DP = 12


@dataclass(frozen=True, slots=True)
class Projection:
    """Weighted member-member projection plus the biadjacency it came from."""

    matrix: sp.csr_matrix
    """|B| x |B|, symmetric, zero diagonal, weights rounded to ``round_dp``."""

    biadjacency: sp.csr_matrix
    """|B| x |T_harvested|."""

    member_ids: tuple[str, ...]
    target_ids: tuple[str, ...]
    weighting: Weighting
    target_degrees: np.ndarray
    """Column sums of ``biadjacency``, aligned to ``target_ids``."""

    def weight(self, u: str, v: str) -> float:
        """Projection weight between two members. Convenience for tests."""
        i = self.member_ids.index(u)
        j = self.member_ids.index(v)
        return float(self.matrix[i, j])

    @property
    def n_edges(self) -> int:
        """Undirected edge count."""
        return int(self.matrix.nnz // 2)


def biadjacency_matrix(bg: BipartiteGraph, *, use_confidence: bool = True) -> sp.csr_matrix:
    """Build the CSR biadjacency ``A`` over (bridges x harvested targets).

    ``use_confidence`` follows R3's function signature. On the default
    observed-only path every confidence is exactly 1.0, so ``A`` is the {0,1}
    matrix of R3 §1.1 and the flag is a no-op. It only bites once
    ``include_inferred=True``, where letting a 0.62-confidence guess push the same
    weight as an observation would be precisely the blending PRIV-19 forbids.

    Note that ``reach`` and ``rarity`` are *not* read off this matrix — they come
    from the unweighted bipartite graph, so a confidence-weighted run cannot
    quietly restate someone's reach as a fraction.
    """
    m_index = {m: i for i, m in enumerate(bg.member_ids)}
    t_index = {t: i for i, t in enumerate(bg.target_ids)}

    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    for e in sorted(bg.edges, key=lambda x: (x.member_id, x.target_id)):
        i = m_index.get(e.member_id)
        j = t_index.get(e.target_id)
        if i is None or j is None:
            continue
        rows.append(i)
        cols.append(j)
        data.append(float(e.confidence) if use_confidence else 1.0)

    shape = (len(bg.member_ids), len(bg.target_ids))
    if not rows:
        return sp.csr_matrix(shape, dtype=np.float64)
    A = sp.csr_matrix((np.asarray(data, dtype=np.float64), (rows, cols)), shape=shape)
    A.sum_duplicates()
    return A


def target_weight_vector(degrees: np.ndarray, weighting: Weighting) -> np.ndarray:
    """Per-target contribution weight, i.e. the diagonal of the middle matrix.

    ``max(d_t, 1)`` and ``max(d_t, e)`` are the divide-by-zero and log-zero guards
    for a harvested target with no mutuals, which is a legal state (T8.6).
    """
    if weighting == "ra":
        return 1.0 / np.maximum(degrees, 1.0)
    if weighting == "count":
        return np.ones_like(degrees)
    if weighting == "adamic_adar":
        return 1.0 / np.log(np.maximum(degrees, np.e))
    raise ValueError(f"unknown weighting {weighting!r}")


def project(
    bg: BipartiteGraph,
    *,
    weighting: Weighting = "ra",
    use_confidence: bool = True,
    round_dp: int = ROUND_DP,
) -> Projection:
    """Project onto the member side. ``P = A @ diags(w(d_T)) @ A.T``."""
    A = biadjacency_matrix(bg, use_confidence=use_confidence)
    degrees = np.asarray(A.sum(axis=0), dtype=np.float64).ravel()
    w = target_weight_vector(degrees, weighting)

    P = (A @ sp.diags(w) @ A.T).tocsr()
    P.setdiag(0.0)
    P.eliminate_zeros()

    # Round *before* anything downstream sees the weights (R3 §2.4 rule 3), then
    # drop anything that rounded to zero so the edge count stays honest.
    if P.nnz:
        P.data = np.round(P.data, round_dp)
        P.eliminate_zeros()

    return Projection(
        matrix=P,
        biadjacency=A,
        member_ids=bg.member_ids,
        target_ids=bg.target_ids,
        weighting=weighting,
        target_degrees=degrees,
    )


__all__ = [
    "ROUND_DP",
    "Projection",
    "Weighting",
    "biadjacency_matrix",
    "project",
    "target_weight_vector",
]
