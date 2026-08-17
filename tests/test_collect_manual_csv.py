"""The hand-fillable CSV path — the floor the whole tool stands on."""

from __future__ import annotations

from pathlib import Path

from interlayer.collect import schemas
from interlayer.collect.interface import CaptureStatus, Code, Severity
from interlayer.collect.manual_csv import ManualCsvCollector, write_template
from interlayer.core.models import CompliancePosture, EdgeOrigin, Firm

TARGETS_HEADER = ",".join(schemas.values("csv.targets_columns"))
MUTUALS_HEADER = ",".join(schemas.values("csv.mutuals_columns"))

TARGETS = f"""{TARGETS_HEADER}
jsmith-quant-9a1,Jordan Smith,https://www.linkedin.com/in/jsmith-quant-9a1/,jane_street,2,2026-08-17T09:12:00Z,people_search:jane_street:london,false,
p-okafor-2c8,Priya Okafor,https://www.linkedin.com/in/p-okafor-2c8/,citadel_securities,3,2026-08-17T09:14:00Z,people_search:citadel_securities:nyc,true,pruned - 3rd degree
kchen-dev-4b2,Kim Chen,https://www.linkedin.com/in/kchen-dev-4b2/,citadel,2,2026-08-17T09:15:00Z,,false,
"""

MUTUALS = f"""{MUTUALS_HEADER}
jsmith-quant-9a1,Alex Rivera,https://www.linkedin.com/in/alexrivera/,Software Engineer at Acme,complete,3,2026-08-17T10:04:00Z,https://www.linkedin.com/in/jsmith-quant-9a1/,manual,
jsmith-quant-9a1,Dana Wu,https://www.linkedin.com/in/danawu-7f3a2/,Quant Researcher at Globex,complete,3,2026-08-17T10:04:00Z,,manual,
jsmith-quant-9a1,Sam Okonkwo,,Trader,complete,3,2026-08-17T10:04:00Z,,manual,no profile url captured
kchen-dev-4b2,,,,empty,0,2026-08-17T10:07:00Z,https://www.linkedin.com/in/kchen-dev-4b2/,manual,profile shows no mutuals
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_accepts_only_the_two_hand_fillable_layouts(tmp_path: Path) -> None:
    collector = ManualCsvCollector()
    targets = _write(tmp_path, "targets.csv", TARGETS)
    mutuals = _write(tmp_path, "mutuals.csv", MUTUALS)
    other = _write(tmp_path, "other.csv", "a,b,c\n1,2,3\n")

    assert collector.accepts(targets)
    assert collector.accepts(mutuals)
    assert not collector.accepts(other)
    assert collector.kind_of(targets) == "targets"
    assert collector.kind_of(mutuals) == "mutuals"


def test_pair_load_produces_edges_with_provenance(tmp_path: Path) -> None:
    collector = ManualCsvCollector()
    result = collector.collect_pair(
        _write(tmp_path, "targets.csv", TARGETS),
        _write(tmp_path, "mutuals.csv", MUTUALS),
    )

    assert len(result.edges) == 3
    for edge in result.edges:
        assert edge.origin is EdgeOrigin.OBSERVED
        assert edge.provenance is not None
        assert edge.provenance.source == "manual_csv"
        assert edge.provenance.posture is CompliancePosture.MANUAL_CAPTURE
        assert edge.provenance.collected_at.isoformat().startswith("2026-08-17T10:04")
        assert edge.evidence


def test_target_records_carry_the_right_firm(tmp_path: Path) -> None:
    collector = ManualCsvCollector()
    result = collector.collect_pair(
        _write(tmp_path, "targets.csv", TARGETS),
        _write(tmp_path, "mutuals.csv", MUTUALS),
    )
    firms = {target.firm for target in result.targets}
    assert Firm.JANE_STREET in firms
    assert Firm.CITADEL in firms


def test_third_degree_target_is_pruned_as_an_inference(tmp_path: Path) -> None:
    """3rd degree means zero shared connections by definition — but say so."""
    collector = ManualCsvCollector()
    _table, result = collector.read_targets(_write(tmp_path, "targets.csv", TARGETS))

    pruned = [c for c in result.captures if c.status is CaptureStatus.SKIPPED_BY_DEGREE]
    assert len(pruned) == 1
    assert pruned[0].target.id == "p-okafor-2c8"
    assert pruned[0].is_inferred
    assert not pruned[0].yields_edges
    assert result.coverage_states()["pruned_by_degree"] == 1
    assert any(d.code is Code.CAPTURE_DEGREE_PRUNED for d in result.diagnostics)


def test_enumeration_truncation_is_carried_onto_the_record(tmp_path: Path) -> None:
    collector = ManualCsvCollector()
    _table, result = collector.read_targets(_write(tmp_path, "targets.csv", TARGETS))
    assert any(d.code is Code.CAPTURE_TRUNCATED for d in result.diagnostics)


def test_observed_empty_is_not_the_same_as_never_looked(tmp_path: Path) -> None:
    collector = ManualCsvCollector()
    result = collector.collect_pair(
        _write(tmp_path, "targets.csv", TARGETS),
        _write(tmp_path, "mutuals.csv", MUTUALS),
    )
    states = result.coverage_states()
    assert states["observed_empty"] == 1
    assert states["observed_complete"] == 1
    empty = [c for c in result.captures if c.status is CaptureStatus.EMPTY]
    assert empty[0].target.id == "kchen-dev-4b2"
    assert empty[0].mutuals == ()


def test_reported_count_above_observed_forces_truncation(tmp_path: Path) -> None:
    body = f"""{MUTUALS_HEADER}
r-almeida-8x1,Alex Rivera,https://www.linkedin.com/in/alexrivera/,SWE,complete,24,2026-08-17T10:11:00Z,,manual,stopped after page 1
"""
    result = ManualCsvCollector(default_firm=Firm.JANE_STREET).read_mutuals(
        _write(tmp_path, "mutuals.csv", body)
    )
    capture = result.captures[0]
    assert capture.is_truncated
    assert result.edges[0].provenance is not None
    assert result.edges[0].provenance.truncated is True
    assert result.truncated_target_ids
    assert any(d.code is Code.CAPTURE_TRUNCATED for d in result.diagnostics)


def test_partial_rows_survive_a_bad_one(tmp_path: Path) -> None:
    body = f"""{MUTUALS_HEADER}
t-1,Alex Rivera,https://www.linkedin.com/in/alexrivera/,,complete,,not-a-date,,manual,
t-1,Dana Wu,,,complete,,,,manual,
,Ghost Row,,,complete,,,,manual,
"""
    result = ManualCsvCollector(default_firm=Firm.CITADEL).read_mutuals(
        _write(tmp_path, "mutuals.csv", body)
    )
    assert len(result.edges) == 2
    codes = {d.code for d in result.diagnostics}
    assert Code.CSV_BAD_TIMESTAMP in codes
    assert Code.CSV_MISSING_REQUIRED_FIELD in codes


def test_conflicting_target_level_columns_warn_but_never_fail(tmp_path: Path) -> None:
    body = f"""{MUTUALS_HEADER}
t-1,Alex Rivera,,,complete,3,2026-08-17T10:00:00Z,,manual,
t-1,Dana Wu,,,truncated,9,2026-08-17T11:00:00Z,,manual,
"""
    result = ManualCsvCollector(default_firm=Firm.CITADEL).read_mutuals(
        _write(tmp_path, "mutuals.csv", body)
    )
    conflicts = [d for d in result.diagnostics if d.code is Code.CSV_CONFLICTING_TARGET_FIELD]
    assert conflicts
    assert result.captures[0].status is CaptureStatus.COMPLETE  # first non-empty wins
    assert len(result.edges) == 2


def test_unknown_capture_status_is_a_diagnostic_not_a_crash(tmp_path: Path) -> None:
    body = f"""{MUTUALS_HEADER}
t-1,Alex Rivera,,,banana,,2026-08-17T10:00:00Z,,manual,
"""
    result = ManualCsvCollector(default_firm=Firm.CITADEL).read_mutuals(
        _write(tmp_path, "mutuals.csv", body)
    )
    bad = [d for d in result.diagnostics if d.code is Code.CSV_BAD_ENUM]
    assert bad and bad[0].schema_key == "csv.capture_status_values"


def test_missing_required_column_is_fatal_and_names_the_schema(tmp_path: Path) -> None:
    path = _write(tmp_path, "mutuals.csv", "target_id,mutual_name\nt-1,Alex\n")
    result = ManualCsvCollector().collect(path)
    assert not result.ok
    fatal = result.of_severity(Severity.FATAL)[0]
    assert fatal.code is Code.CSV_MISSING_HEADER
    assert fatal.schema_key == "csv.mutuals_required_columns"
    assert "capture_status" in fatal.message


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    result = ManualCsvCollector().read_mutuals(tmp_path / "nope.csv")
    assert not result.ok
    assert result.of_severity(Severity.FATAL)[0].code is Code.INPUT_MISSING


def test_bom_and_extra_columns_are_tolerated(tmp_path: Path) -> None:
    body = f"﻿{MUTUALS_HEADER},extra\nt-1,Alex Rivera,,,complete,,2026-08-17T10:00:00Z,,manual,,junk\n"
    result = ManualCsvCollector(default_firm=Firm.JANE_STREET).read_mutuals(
        _write(tmp_path, "mutuals.csv", body)
    )
    assert len(result.edges) == 1
    assert any(d.code is Code.CSV_UNKNOWN_COLUMNS for d in result.diagnostics)


def test_unknown_firm_yields_no_target_but_keeps_the_edge(tmp_path: Path) -> None:
    body = f"""{MUTUALS_HEADER}
t-1,Alex Rivera,,,complete,,2026-08-17T10:00:00Z,,manual,
"""
    result = ManualCsvCollector().read_mutuals(_write(tmp_path, "mutuals.csv", body))
    assert result.targets == ()
    assert len(result.edges) == 1
    assert any(d.code is Code.TARGET_UNKNOWN_FIRM for d in result.diagnostics)


def test_template_writes_the_exact_header(tmp_path: Path) -> None:
    path = tmp_path / "template.csv"
    write_template(path)
    assert path.read_text(encoding="utf-8").strip() == MUTUALS_HEADER
    assert ManualCsvCollector().accepts(path)


def test_collector_declares_manual_capture() -> None:
    assert ManualCsvCollector().compliance_posture is CompliancePosture.MANUAL_CAPTURE
