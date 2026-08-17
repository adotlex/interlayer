"""The registry, and the posture gate the CLI enables collectors through."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from interlayer.collect import registry, schemas
from interlayer.collect.interface import Code as DiagnosticCode
from interlayer.collect.interface import (
    CompliancePostureError,
    MutualCapture,
    TargetRef,
)
from interlayer.core.models import CompliancePosture, Firm


def _mutuals(tmp_path: Path) -> Path:
    header = ",".join(schemas.values("csv.mutuals_columns"))
    path = tmp_path / "mutuals.csv"
    path.write_text(
        f"{header}\nt-1,Alex Rivera,,,complete,,2026-08-17T10:00:00Z,,manual,\n",
        encoding="utf-8",
    )
    return path


def _har(tmp_path: Path) -> Path:
    path = tmp_path / "session.har"
    path.write_text(json.dumps({"log": {"entries": []}}), encoding="utf-8")
    return path


def test_registry_lists_the_shipped_collectors() -> None:
    assert registry.available() == ("har", "manual_csv")


def test_no_shipped_collector_is_automated() -> None:
    """The automated posture exists to be tested against, never used."""
    for spec in registry.specs():
        assert spec.posture not in registry.NEVER_ALLOWED_POSTURES
        assert spec.posture is CompliancePosture.MANUAL_CAPTURE


def test_for_posture_filters_to_what_the_config_permits() -> None:
    first_party_only = registry.for_posture({CompliancePosture.FIRST_PARTY_EXPORT})
    assert first_party_only == ()

    manual = registry.for_posture({CompliancePosture.MANUAL_CAPTURE})
    assert {spec.name for spec in manual} == {"har", "manual_csv"}


def test_for_posture_never_returns_an_automated_collector() -> None:
    everything = registry.for_posture(set(CompliancePosture))
    assert all(spec.posture is not CompliancePosture.AUTOMATED for spec in everything)


def test_create_refuses_a_posture_that_is_not_enabled() -> None:
    with pytest.raises(CompliancePostureError) as excinfo:
        registry.create("har", allowed_postures={CompliancePosture.FIRST_PARTY_EXPORT})
    assert "manual_capture" in str(excinfo.value)


def test_create_returns_a_working_collector() -> None:
    collector = registry.create("manual_csv", default_firm=Firm.JANE_STREET)
    assert collector.name == "manual_csv"


def test_unknown_name_raises_with_the_list_of_known_ones() -> None:
    with pytest.raises(registry.UnknownCollectorError) as excinfo:
        registry.get("scraper")
    assert "manual_csv" in str(excinfo.value)


def test_for_path_picks_by_sniffing(tmp_path: Path) -> None:
    csv_collector = registry.for_path(_mutuals(tmp_path))
    har_collector = registry.for_path(_har(tmp_path))
    assert csv_collector is not None and csv_collector.name == "manual_csv"
    assert har_collector is not None and har_collector.name == "har"


def test_load_all_merges_and_reports_unrecognised_files(tmp_path: Path) -> None:
    junk = tmp_path / "notes.txt"
    junk.write_text("hello", encoding="utf-8")

    result = registry.load_all(
        [_mutuals(tmp_path), junk], default_firm=Firm.JANE_STREET
    )
    assert len(result.edges) == 1
    assert any(d.code is DiagnosticCode.COLLECTOR_UNKNOWN for d in result.diagnostics)


def test_load_all_checks_the_posture_of_every_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collector that declares one posture and emits another must not slip by."""
    from interlayer.collect import manual_csv

    original = manual_csv.ManualCsvCollector.read_mutuals

    def lying_read(self: manual_csv.ManualCsvCollector, path: Path):  # type: ignore[no-untyped-def]
        result = original(self, path)
        forged = MutualCapture(
            target=TargetRef(id="t-9", firm=Firm.CITADEL.value),
            status=result.captures[0].status,
            collector=self.name,
            posture=CompliancePosture.AUTOMATED,
        )
        return result.__class__(
            collector=result.collector,
            posture=result.posture,
            members=result.members,
            targets=result.targets,
            edges=result.edges,
            captures=(*result.captures, forged),
            diagnostics=result.diagnostics,
        )

    monkeypatch.setattr(manual_csv.ManualCsvCollector, "read_mutuals", lying_read)

    with pytest.raises(CompliancePostureError) as excinfo:
        registry.load_all([_mutuals(tmp_path)], default_firm=Firm.JANE_STREET)
    assert "automated" in str(excinfo.value)


def test_registering_an_automated_collector_is_refused() -> None:
    spec = registry.CollectorSpec(
        name="scraper",
        posture=CompliancePosture.AUTOMATED,
        description="not shipped",
        factory=lambda **_: None,  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(Exception, match="does not ship"):
        registry.register(spec)
    assert "scraper" not in registry.available()


def test_default_allowlist_excludes_third_party_and_automated() -> None:
    assert CompliancePosture.AUTOMATED not in registry.DEFAULT_ALLOWED_POSTURES
    assert CompliancePosture.THIRD_PARTY_API not in registry.DEFAULT_ALLOWED_POSTURES
