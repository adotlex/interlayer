"""`interlayer analyse` — driving the graph engine and storing the snapshot.

Run against the real store and the real graph engine, because the thing worth
testing is that the CLI's half of the contract lines up with theirs: load the
records, pass them through, refuse a wrong return type, write a snapshot the
rendering commands can read, and surface incomplete coverage and a non-empty
review queue at the moment the numbers are produced rather than only in the
report.
"""

from __future__ import annotations

import json
import types
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from interlayer import cli
from interlayer.core.models import (
    CompanyMatch,
    Edge,
    Firm,
    MatchStatus,
    Member,
    Provenance,
    Target,
)
from interlayer.report.json_out import load_result

runner = CliRunner()

PROV = Provenance(source="test", collected_at=datetime(2026, 8, 17, tzinfo=UTC))


def _seed(state_root, *, n_targets: int = 4, n_harvested: int = 2, review: int = 0):
    """A small real graph: three connections, some targets, some edges."""
    from interlayer.core.config import load_config
    from interlayer.core.store import open_store

    config = load_config(overrides={"state_root": state_root})
    store = open_store(config)
    try:
        members = [
            Member(member_id=f"m_{i}", first_name=f"First{i}", last_name=f"Last{i}",
                   provenance=PROV)
            for i in range(3)
        ]
        targets = [
            Target(target_id=f"t_{i}", firm=Firm.JANE_STREET, first_name="T",
                   last_name=str(i), provenance=PROV)
            for i in range(n_targets)
        ]
        store.add_members(members)
        store.add_targets(targets)

        edges = []
        for ti in range(n_harvested):
            store.mark_harvested(f"t_{ti}")
            for mi in range(3):
                if (mi + ti) % 3 != 2:
                    edges.append(Edge(member_id=f"m_{mi}", target_id=f"t_{ti}", provenance=PROV))
        store.add_edges(edges)

        for i in range(review):
            store.queue_review(
                CompanyMatch(raw=f"Citadel Technology {i}", status=MatchStatus.NEEDS_REVIEW,
                             rule="ambiguous"),
                provenance=PROV,
                normalised=f"citadel technology {i}",
                member_count=1,
            )
    finally:
        store.close()
    return state_root


def test_writes_a_snapshot_the_report_command_can_read(tmp_path) -> None:
    _seed(tmp_path)

    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output

    snapshot = tmp_path / "analysis.json"
    assert snapshot.exists()
    restored, names = load_result(json.loads(snapshot.read_text(encoding="utf-8")))
    assert restored.brokerage
    assert restored.coverage.n_members_total == 3
    # Names come from the store, so the report can print people rather than ids.
    assert names["m_0"] == "First0 Last0"

    rendered = runner.invoke(cli.app, ["report", "--state-root", str(tmp_path)])
    assert rendered.exit_code == 0, rendered.output
    assert "First0 Last0" in rendered.output


def test_incomplete_coverage_is_warned_about_immediately(tmp_path) -> None:
    _seed(tmp_path, n_targets=4, n_harvested=2)
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "2/4 targets harvested" in result.output
    assert "lower bound" in result.output


def test_complete_coverage_is_not_warned_about(tmp_path) -> None:
    _seed(tmp_path, n_targets=2, n_harvested=2)
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "2/2 targets harvested" in result.output
    assert "lower bound" not in result.output


def test_a_non_empty_review_queue_is_surfaced(tmp_path) -> None:
    _seed(tmp_path, review=3)
    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "3 needs_review item(s) held out of the graph" in result.output
    assert "interlayer review" in result.output


def test_the_snapshot_location_can_be_overridden(tmp_path) -> None:
    _seed(tmp_path)
    out = tmp_path / "nested" / "run-1.json"
    result = runner.invoke(
        cli.app, ["analyse", "--state-root", str(tmp_path), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_inference_is_off_by_default_and_opt_in_by_flag(tmp_path) -> None:
    """The flag must reach the engine; the default must not enable inference."""
    record: dict = {}
    module = types.ModuleType("interlayer.graph")

    def analyse(members, targets, edges, **kwargs):
        record.update(kwargs)
        record["n_members"] = len(list(members))
        from interlayer.core.models import AnalysisResult

        return AnalysisResult()

    module.analyse = analyse  # type: ignore[attr-defined]

    _seed(tmp_path)
    real_import = cli._import

    def fake_import(name: str):
        return module if name == "interlayer.graph" else real_import(name)

    import pytest as _pytest  # local alias keeps the monkeypatch scope obvious

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_import", fake_import)
        runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])
        assert record["include_inferred"] is False
        assert record["n_members"] == 3

        record.clear()
        runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path), "--include-inferred"])
        assert record["include_inferred"] is True


@pytest.fixture
def stub_graph(monkeypatch):
    def _install(fn):
        module = types.ModuleType("interlayer.graph")
        module.analyse = fn  # type: ignore[attr-defined]
        real_import = cli._import
        monkeypatch.setattr(
            cli,
            "_import",
            lambda name: module if name == "interlayer.graph" else real_import(name),
        )

    return _install


def test_a_component_returning_the_wrong_type_is_explained(stub_graph, tmp_path) -> None:
    _seed(tmp_path)
    stub_graph(lambda *a, **k: {"not": "an AnalysisResult"})

    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "not an AnalysisResult" in result.output
    assert "Traceback" not in result.output


def test_a_component_that_raises_is_explained_not_traced(stub_graph, tmp_path) -> None:
    _seed(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("igraph exploded")

    stub_graph(boom)

    result = runner.invoke(cli.app, ["analyse", "--state-root", str(tmp_path)])

    assert result.exit_code == 2
    assert "failed inside interlayer.graph" in result.output
    assert "RuntimeError: igraph exploded" in result.output
    assert "Traceback" not in result.output
