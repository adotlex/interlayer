"""Coverage must be prominent, and reach must be a lower bound when it is.

Not a numbered privacy control, but the difference between an honest report and
a misleading one. An unharvested target cannot produce an edge, so a thin
harvest and a thin network produce identical numbers. The renderers refuse to
let those two look alike.
"""

from __future__ import annotations

import json

import pytest

from interlayer.core.models import Coverage
from interlayer.report import (
    COVERAGE_CAVEAT_COMPLETE,
    COVERAGE_CAVEAT_INCOMPLETE,
    harvest_is_complete,
    reach_display,
    render_html,
    render_json,
    render_markdown,
)
from test_report_fixtures import make_ctx, make_result

RENDERERS = (render_markdown, render_html, render_json)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_incomplete_harvest_is_flagged(renderer) -> None:
    rendered = renderer(make_result(n_targets_total=300, n_targets_harvested=141), make_ctx())
    assert COVERAGE_CAVEAT_INCOMPLETE in rendered
    assert COVERAGE_CAVEAT_COMPLETE not in rendered
    assert "141" in rendered and "300" in rendered


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_complete_harvest_is_flagged_differently(renderer) -> None:
    rendered = renderer(make_result(n_targets_total=300, n_targets_harvested=300), make_ctx())
    assert COVERAGE_CAVEAT_COMPLETE in rendered
    assert COVERAGE_CAVEAT_INCOMPLETE not in rendered


def test_reach_is_a_lower_bound_while_the_harvest_is_incomplete() -> None:
    rendered = render_markdown(make_result(n_targets_harvested=141), make_ctx())
    assert "≥ 7" in rendered
    assert "≥ 4" in rendered
    assert "≥ 2" in rendered


def test_reach_is_exact_once_the_harvest_is_complete() -> None:
    rendered = render_markdown(
        make_result(n_targets_total=300, n_targets_harvested=300), make_ctx()
    )
    assert "≥" not in rendered
    assert "| 7 |" in rendered


def test_html_reach_carries_the_lower_bound_sign() -> None:
    incomplete = render_html(make_result(n_targets_harvested=141), make_ctx())
    complete = render_html(make_result(n_targets_harvested=300), make_ctx())
    assert "≥ 7" in incomplete
    assert "reach is a lower bound" in incomplete
    assert "≥" not in complete
    assert "reach is exact" in complete


def test_json_states_the_bound_as_a_boolean_and_a_string() -> None:
    payload = json.loads(render_json(make_result(n_targets_harvested=141), make_ctx()))
    assert payload["coverage"]["harvest_complete"] is False
    assert payload["coverage"]["reach_is_lower_bound"] is True
    alpha = next(b for b in payload["bridges"] if b["member_id"] == "m_alpha")
    assert alpha["reach"] == 7
    assert alpha["reach_is_lower_bound"] is True
    assert alpha["reach_display"] == "≥ 7"


def test_coverage_appears_before_the_bridge_ranking() -> None:
    """The caveat is useless after the reader has drawn a conclusion."""
    rendered = render_markdown(make_result(), make_ctx())
    assert rendered.index("## Coverage") < rendered.index("## Bridge ranking")
    assert rendered.index(COVERAGE_CAVEAT_INCOMPLETE) < rendered.index("## Bridge ranking")


def test_nothing_harvested_is_not_treated_as_complete() -> None:
    """Zero of zero is the weakest evidential state, not a finished job."""
    assert harvest_is_complete(Coverage(0, 0, 0, 0)) is False
    rendered = render_markdown(
        make_result(empty=True, n_targets_total=0, n_targets_harvested=0), make_ctx()
    )
    assert COVERAGE_CAVEAT_INCOMPLETE in rendered


def test_reach_display_helper() -> None:
    assert reach_display(5, complete=True) == "5"
    assert reach_display(5, complete=False) == "≥ 5"
    assert reach_display(0, complete=False) == "≥ 0"
