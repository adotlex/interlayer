"""Step 12: TF-IDF cluster labels, plus the cluster's top shared targets.

One pseudo-document per cluster, built from member attributes; ``TfidfVectorizer``
fitted across the cluster documents; top-3 terms per cluster. Because IDF is
computed *across clusters*, a term everybody shares ("engineer", when everyone is
an engineer) is suppressed automatically, which is exactly the desired behaviour.

**The sentinel bug this module exists to prevent.** R3's prototype concatenated
attribute fields with a plain space and emitted ``"engineer wharton"`` as a top
term — a nonsense position/school hybrid produced by a bigram straddling two
unrelated fields. Joining with a ``" | "`` sentinel is *not on its own* a fix:
sklearn's default token pattern discards ``|`` as a non-word character, so the
bigram window closes straight over the join and the straddle happens silently.
The fix is a field-aware analyzer that tokenises each field separately and only
ever emits bigrams from within one field. Verified: with a plain-space join the
vocabulary contains ``infra google`` and ``2016 beta``; with the analyzer below it
contains neither.

A cluster whose top TF-IDF weight falls below ``min_weight`` has no coherent
theme, and gets ``"mixed (n=...)"`` rather than a misleading term.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from itertools import pairwise

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from interlayer.core.models import Member
from interlayer.graph.build import BipartiteGraph

#: Field separator. Never appears in an emitted label.
SENTINEL = " | "

#: LinkedIn boilerplate. Kept short on purpose — an over-eager stoplist starts
#: eating the seniority signal that makes a label useful.
STOPWORDS: frozenset[str] = frozenset(
    {
        "senior", "junior", "lead", "staff", "principal",
        "the", "at", "of", "inc", "llc", "ltd", "corp",
    }
)

#: R3's verified token pattern.
TOKEN_RE = re.compile(r"(?u)\b\w[\w\-\.]+\b")

DEFAULT_TOP_N = 3
DEFAULT_MIN_WEIGHT = 0.15
DEFAULT_TOP_TARGETS = 5

#: Attribute accessor. ``Member`` carries employer and title; a caller with richer
#: data (school, city) supplies its own callable.
AttributeFn = Callable[[Member], Sequence[str]]


def default_attributes(member: Member) -> tuple[str, ...]:
    """Employer and title — the two attribute fields the frozen model carries."""
    return tuple(v for v in (member.company_raw, member.position) if v)


def _field_analyzer(document: str) -> list[str]:
    """Unigrams and bigrams, **never crossing a field boundary**.

    This replaces ``token_pattern`` + ``ngram_range=(1,2)`` rather than
    supplementing them: sklearn ignores both when a custom analyzer is supplied,
    so the stopword filter has to live here too.
    """
    terms: list[str] = []
    for field in document.split(SENTINEL):
        tokens = [t for t in TOKEN_RE.findall(field.lower()) if t not in STOPWORDS]
        terms.extend(tokens)
        terms.extend(f"{a} {b}" for a, b in pairwise(tokens))
    return terms


def cluster_documents(
    members_by_id: Mapping[str, Member],
    clusters: Mapping[int, Sequence[str]],
    *,
    attributes: AttributeFn = default_attributes,
) -> dict[int, str]:
    """One sentinel-joined pseudo-document per cluster, in sorted member order."""
    documents: dict[int, str] = {}
    for cluster_id in sorted(clusters):
        fields: list[str] = []
        for member_id in sorted(clusters[cluster_id]):
            member = members_by_id.get(member_id)
            if member is None:
                continue
            fields.extend(value for value in attributes(member) if value and value.strip())
        documents[cluster_id] = SENTINEL.join(fields)
    return documents


def label_clusters(
    members_by_id: Mapping[str, Member],
    clusters: Mapping[int, Sequence[str]],
    *,
    top_n: int = DEFAULT_TOP_N,
    min_weight: float = DEFAULT_MIN_WEIGHT,
    attributes: AttributeFn = default_attributes,
) -> dict[int, str]:
    """Top-``top_n`` TF-IDF terms per cluster, or ``"mixed (n=...)"``."""
    if not clusters:
        return {}

    documents = cluster_documents(members_by_id, clusters, attributes=attributes)
    cluster_ids = sorted(documents)
    corpus = [documents[c] for c in cluster_ids]
    sizes = {c: len(clusters[c]) for c in cluster_ids}

    if not any(doc.strip() for doc in corpus):
        return {c: f"mixed (n={sizes[c]})" for c in cluster_ids}

    vectoriser = TfidfVectorizer(
        analyzer=_field_analyzer,
        sublinear_tf=True,
        min_df=1,
        norm="l2",
    )
    try:
        matrix = vectoriser.fit_transform(corpus)
    except ValueError:
        # empty vocabulary — every document was stopwords or punctuation
        return {c: f"mixed (n={sizes[c]})" for c in cluster_ids}

    terms = np.asarray(vectoriser.get_feature_names_out())
    labels: dict[int, str] = {}
    for row, cluster_id in enumerate(cluster_ids):
        weights = matrix.getrow(row).toarray().ravel()
        if weights.size == 0 or float(weights.max()) < min_weight:
            labels[cluster_id] = f"mixed (n={sizes[cluster_id]})"
            continue
        # descending weight, ties broken alphabetically for determinism
        order = np.lexsort((terms, -weights))
        chosen = [str(terms[i]) for i in order[:top_n] if weights[i] > 0.0]
        # Belt and braces: the analyzer cannot emit a sentinel-bearing term, but a
        # label that leaked one would be a silent regression of the R3 bug.
        chosen = [t for t in chosen if SENTINEL.strip() not in t]
        labels[cluster_id] = ", ".join(chosen) if chosen else f"mixed (n={sizes[cluster_id]})"
    return labels


def top_shared_targets(
    bg: BipartiteGraph,
    clusters: Mapping[int, Sequence[str]],
    *,
    top_n: int = DEFAULT_TOP_TARGETS,
) -> dict[int, tuple[str, ...]]:
    """Targets adjacent to the most members of each cluster.

    Emitted alongside the text label because "these 6 people all reach t_A, t_B,
    t_C" is usually more actionable than any label, and it costs nothing.
    """
    out: dict[int, tuple[str, ...]] = {}
    for cluster_id in sorted(clusters):
        counts: dict[str, int] = {}
        for member_id in sorted(clusters[cluster_id]):
            if member_id not in bg.graph:
                continue
            for target in bg.neighbours(member_id):
                counts[target] = counts.get(target, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        out[cluster_id] = tuple(target for target, _ in ranked[:top_n])
    return out


__all__ = [
    "DEFAULT_MIN_WEIGHT",
    "DEFAULT_TOP_N",
    "DEFAULT_TOP_TARGETS",
    "SENTINEL",
    "STOPWORDS",
    "TOKEN_RE",
    "AttributeFn",
    "cluster_documents",
    "default_attributes",
    "label_clusters",
    "top_shared_targets",
]
