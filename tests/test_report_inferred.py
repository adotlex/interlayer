"""PRIV-19 — an inferred value never renders the way an observed one does.

An inferred tie was never seen. It is a co-affiliation guess, and a guess that
looks like an observation is worse than no output at all: the whole point of
the report is to tell someone whom they can actually ask for an introduction.
"""

from __future__ import annotations

import json
import re

import pytest

from interlayer.report import (
    INFERRED_BANNER,
    INFERRED_MARKER,
    OBSERVED_ONLY_BANNER,
    render_html,
    render_json,
    render_markdown,
)
from test_report_fixtures import make_ctx, make_result

RENDERERS = (render_markdown, render_html, render_json)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_inferred_result_is_announced(renderer) -> None:
    rendered = renderer(make_result(used_inferred_edges=True), make_ctx())
    assert INFERRED_BANNER in rendered
    assert INFERRED_MARKER in rendered


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_observed_only_result_is_not_marked_as_inferred(renderer) -> None:
    """The marker must mean something, so it cannot be always-on."""
    rendered = renderer(make_result(used_inferred_edges=False), make_ctx())
    assert OBSERVED_ONLY_BANNER in rendered
    assert INFERRED_BANNER not in rendered


def test_markdown_row_for_an_inferred_bridge_differs_from_an_observed_one() -> None:
    result = make_result(used_inferred_edges=True)
    ctx = make_ctx(
        names={"m_alpha": "Dana Wu", "m_beta": "Alex Rivera", "m_gamma": "Priya Nair"},
        inferred_members=frozenset({"m_beta"}),
    )
    rendered = render_markdown(result, ctx)

    observed_row = _row_containing(rendered, "Dana Wu")
    inferred_row = _row_containing(rendered, "Alex Rivera")

    assert INFERRED_MARKER in inferred_row
    assert INFERRED_MARKER not in observed_row
    # Not merely a different label: the cells themselves must not be
    # interchangeable once the names are removed.
    assert _shape(observed_row) != _shape(inferred_row)


def test_html_row_for_an_inferred_bridge_carries_a_distinct_class() -> None:
    result = make_result(used_inferred_edges=True)
    ctx = make_ctx(
        names={"m_alpha": "Dana Wu", "m_beta": "Alex Rivera", "m_gamma": "Priya Nair"},
        inferred_members=frozenset({"m_beta"}),
    )
    rendered = render_html(result, ctx)

    inferred_row = _row_containing(rendered, "Alex Rivera", sep="<tr")
    observed_row = _row_containing(rendered, "Dana Wu", sep="<tr")

    assert 'class="inferred"' in inferred_row
    assert 'class="observed"' in observed_row
    assert "pill inferred" in inferred_row
    assert "pill inferred" not in observed_row
    # The stylesheet must actually give that class a different appearance.
    assert "tr.inferred" in rendered
    assert ".pill.inferred" in rendered


def test_json_marks_each_row_with_an_explicit_boolean() -> None:
    result = make_result(used_inferred_edges=True)
    ctx = make_ctx(inferred_members=frozenset({"m_beta"}))
    payload = json.loads(render_json(result, ctx))

    flags = {b["member_id"]: b["inferred"] for b in payload["bridges"]}
    assert flags == {"m_alpha": False, "m_beta": True, "m_gamma": False}
    assert payload["inference"]["used_inferred_edges"] is True
    basis = {b["member_id"]: b["basis"] for b in payload["bridges"]}
    assert basis["m_beta"] == INFERRED_MARKER
    assert basis["m_alpha"] != INFERRED_MARKER


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_unlocalisable_inference_marks_every_row(renderer) -> None:
    """When the caller cannot say which ties were guessed, mark them all.

    Over-marking costs the reader a caveat. Under-marking presents a guess as a
    fact, which is the failure this control exists to prevent.
    """
    rendered = renderer(make_result(used_inferred_edges=True), make_ctx())
    if renderer is render_json:
        payload = json.loads(rendered)
        assert all(b["inferred"] for b in payload["bridges"])
    else:
        assert rendered.count(INFERRED_MARKER) >= 3


def test_cluster_membership_is_marked_too() -> None:
    """Inferred membership of a cluster is still an inference about a person."""
    ctx = make_ctx(
        names={"m_alpha": "Dana Wu", "m_beta": "Alex Rivera"},
        inferred_members=frozenset({"m_beta"}),
    )
    result = make_result(used_inferred_edges=True)

    md = render_markdown(result, ctx)
    cluster_block = md[md.index("### options | market making") : md.index("### infrastructure")]
    assert f"Alex Rivera_ [{INFERRED_MARKER}]" in cluster_block
    assert f"Dana Wu [{INFERRED_MARKER}]" not in cluster_block

    payload = json.loads(render_json(result, ctx))
    members = {m["member_id"]: m["inferred"] for m in payload["clusters"][0]["members"]}
    assert members == {"m_alpha": False, "m_beta": True}


def _row_containing(text: str, needle: str, sep: str = "\n") -> str:
    chunks = text.split(sep)
    matches = [c for c in chunks if needle in c]
    assert matches, f"no {sep!r}-delimited chunk containing {needle!r}"
    return matches[0]


def _shape(row: str) -> str:
    """The row with proper nouns removed, so only the presentation remains."""
    return re.sub(r"[A-Za-z]+", "", row)
