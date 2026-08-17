"""PRIV-20 — the report states how many needs_review items are unadjudicated.

Those records are held *out* of the graph. Their absence is not a finding, and
without the count a reader would take a provisional result for a complete one.
"""

from __future__ import annotations

import json

import pytest

from interlayer.report import (
    REVIEW_TEMPLATE,
    render_html,
    render_json,
    render_markdown,
)
from test_report_fixtures import make_ctx, make_result

RENDERERS = (render_markdown, render_html, render_json)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("count", [0, 1, 7, 143])
def test_every_renderer_states_the_unadjudicated_count(renderer, count: int) -> None:
    rendered = renderer(make_result(unadjudicated_count=count), make_ctx())
    assert REVIEW_TEMPLATE.format(count=count) in rendered


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_the_statement_says_the_result_is_provisional(renderer) -> None:
    """A bare number is not enough; the reader must know what it implies."""
    rendered = renderer(make_result(unadjudicated_count=4), make_ctx())
    assert "held OUT of the graph" in rendered
    assert "provisional" in rendered


def test_json_exposes_the_count_as_a_number() -> None:
    payload = json.loads(render_json(make_result(unadjudicated_count=12), make_ctx()))
    assert payload["review"]["unadjudicated_count"] == 12


def test_zero_is_stated_rather_than_omitted() -> None:
    """Silence is ambiguous between 'none' and 'not checked'."""
    for renderer in RENDERERS:
        rendered = renderer(make_result(unadjudicated_count=0), make_ctx())
        assert "Unadjudicated needs_review items: 0" in rendered


def test_html_flags_a_non_empty_queue_visually() -> None:
    with_queue = render_html(make_result(unadjudicated_count=3), make_ctx())
    without = render_html(make_result(unadjudicated_count=0), make_ctx())
    assert 'class="banner alert"' in with_queue
    assert 'class="banner alert"' not in without
