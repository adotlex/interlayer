"""The JSON report, and the analysis snapshot that survives between commands."""

from __future__ import annotations

import json

import pytest

from interlayer.report import ANALYSIS_SCHEMA_VERSION, SCHEMA_VERSION, render_json
from interlayer.report.json_out import dump_result, load_result
from test_report_fixtures import make_ctx, make_result


def test_report_json_is_valid_and_versioned() -> None:
    payload = json.loads(render_json(make_result(), make_ctx()))
    assert payload["schema"] == SCHEMA_VERSION
    assert {"notice", "review", "inference", "coverage", "bridges", "clusters"} <= set(payload)


def test_bridges_are_ranked_deterministically() -> None:
    payload = json.loads(render_json(make_result(), make_ctx()))
    ranks = [b["rank"] for b in payload["bridges"]]
    composites = [b["composite"] for b in payload["bridges"]]
    assert ranks == [1, 2, 3]
    assert composites == sorted(composites, reverse=True)


def test_rendering_is_stable_across_calls() -> None:
    result, ctx = make_result(), make_ctx()
    assert render_json(result, ctx) == render_json(result, ctx)


def test_snapshot_round_trips_exactly() -> None:
    original = make_result(used_inferred_edges=True, unadjudicated_count=5)
    names = {"m_alpha": "Dana Wu", "t_two": "K. Chen"}

    restored, restored_names = load_result(json.loads(json.dumps(dump_result(original, names))))

    assert restored == original
    assert restored_names == names


def test_snapshot_round_trips_an_empty_result() -> None:
    original = make_result(empty=True)
    restored, names = load_result(dump_result(original))
    assert restored == original
    assert names == {}


def test_snapshot_is_versioned_and_rejects_foreign_files() -> None:
    assert dump_result(make_result())["schema"] == ANALYSIS_SCHEMA_VERSION
    with pytest.raises(ValueError, match="not an interlayer analysis snapshot"):
        load_result({"schema": "something.else/v9"})
    with pytest.raises(ValueError, match="not an interlayer analysis snapshot"):
        load_result({})


def test_snapshot_carries_no_contact_details() -> None:
    """The snapshot lands on disk, so it gets the same treatment as a report."""
    from interlayer.report.redact import has_contact_details

    payload = dump_result(make_result(), {"m_alpha": "Dana Wu dana@example.com"})
    assert not has_contact_details(json.dumps(payload))


def test_json_report_is_utf8_clean_not_escaped() -> None:
    """The lower-bound sign must survive as a character, not as \\u2265."""
    rendered = render_json(make_result(n_targets_harvested=1), make_ctx())
    assert "≥" in rendered
    assert "\\u2265" not in rendered
