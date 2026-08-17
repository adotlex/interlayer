"""PRIV-18 — every rendered report carries the mandatory header block.

Purpose, retention period in force, a statement that the document contains
third-party personal data, and a statement that it must not be redistributed.
All four, in all three formats, near the top.
"""

from __future__ import annotations

import json

import pytest

from interlayer.report import (
    PURPOSE_NOTICE,
    REDISTRIBUTION_NOTICE,
    RETENTION_TEMPLATE,
    THIRD_PARTY_NOTICE,
    render_html,
    render_json,
    render_markdown,
)
from test_report_fixtures import make_ctx, make_result

RENDERERS = (render_markdown, render_html, render_json)

REQUIRED = (
    pytest.param(PURPOSE_NOTICE, id="purpose"),
    pytest.param(THIRD_PARTY_NOTICE, id="third-party-personal-data"),
    pytest.param(REDISTRIBUTION_NOTICE, id="no-redistribution"),
)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("statement", REQUIRED)
def test_every_renderer_states_every_notice(renderer, statement: str) -> None:
    rendered = renderer(make_result(), make_ctx())
    assert statement in rendered


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_every_renderer_states_the_retention_period_in_force(renderer) -> None:
    """The number stated must be the configured one, not a hard-coded 90."""
    rendered = renderer(make_result(), make_ctx(retention_days=30))
    assert RETENTION_TEMPLATE.format(days=30) in rendered
    assert RETENTION_TEMPLATE.format(days=90) not in rendered


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_notices_survive_html_escaping_and_json_encoding(renderer) -> None:
    """A notice that only survives one format is not a notice.

    The constants avoid characters an escaping template engine would rewrite,
    and this test is what keeps them that way.
    """
    rendered = renderer(make_result(), make_ctx())
    for statement in (PURPOSE_NOTICE, THIRD_PARTY_NOTICE, REDISTRIBUTION_NOTICE):
        assert statement in rendered, f"{renderer.__name__} mangled a notice"


def test_header_precedes_any_finding_in_markdown() -> None:
    rendered = render_markdown(make_result(), make_ctx())
    assert rendered.index(THIRD_PARTY_NOTICE) < rendered.index("## Bridge ranking")
    assert rendered.index(REDISTRIBUTION_NOTICE) < rendered.index("## Bridge ranking")


def test_header_precedes_any_finding_in_html() -> None:
    rendered = render_html(make_result(), make_ctx())
    assert rendered.index(THIRD_PARTY_NOTICE) < rendered.index("Bridge ranking")


def test_json_carries_the_notices_as_data_not_prose() -> None:
    payload = json.loads(render_json(make_result(), make_ctx(retention_days=45)))
    notice = payload["notice"]
    assert notice["purpose"] == PURPOSE_NOTICE
    assert notice["third_party_personal_data"] == THIRD_PARTY_NOTICE
    assert notice["redistribution"] == REDISTRIBUTION_NOTICE
    assert notice["retention_days"] == 45
    assert notice["retention"] == RETENTION_TEMPLATE.format(days=45)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_notice_present_even_when_there_is_nothing_to_report(renderer) -> None:
    """An empty result is still a document about other people."""
    rendered = renderer(make_result(empty=True), make_ctx())
    assert THIRD_PARTY_NOTICE in rendered
    assert REDISTRIBUTION_NOTICE in rendered
