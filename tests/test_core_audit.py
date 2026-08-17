"""Audit log: schema, append-only behaviour, and the no-personal-data rule.

Covers PRIV-15 and PRIV-16.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from interlayer.core.audit import ACTIONS, RECORD_KEYS, AuditLog, audit
from interlayer.core.config import Config
from interlayer.core.errors import ComplianceError


def _log(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path)


def test_line_has_exactly_the_five_documented_keys(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("ingest", adapter="first_party_export", count=42, config_hash="a" * 64)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload) == set(RECORD_KEYS)
    assert payload["action"] == "ingest"
    assert payload["adapter"] == "first_party_export"
    assert payload["entity_or_person_count"] == 42
    assert payload["config_hash"] == "a" * 64


def test_log_is_valid_jsonl_and_round_trips(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("collect", adapter="manual_capture", count=3)
    log.append("match", count=7)
    log.append("export", count=1)
    records = log.read()
    assert [r.action for r in records] == ["collect", "match", "export"]
    assert [r.entity_or_person_count for r in records] == [3, 7, 1]
    assert log.actions() == {"collect", "match", "export"}


def test_timestamp_is_utc_iso8601(tmp_path: Path) -> None:
    log = _log(tmp_path)
    record = log.append("ingest")
    parsed = datetime.fromisoformat(record.ts)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == datetime.now(UTC).utcoffset()


@pytest.mark.parametrize(
    "action",
    ["Ingest", "in gest", "ingest for Jane Smith", "jane@example.com", "", "9lives", "x" * 65],
)
def test_priv_15_a_name_shaped_action_is_refused(tmp_path: Path, action: str) -> None:
    with pytest.raises(ComplianceError):
        _log(tmp_path).append(action)


@pytest.mark.parametrize(
    "adapter", ["Jane Smith", "jane.smith@example.com", "first party export", "É"]
)
def test_priv_15_a_name_shaped_adapter_is_refused(tmp_path: Path, adapter: str) -> None:
    with pytest.raises(ComplianceError):
        _log(tmp_path).append("ingest", adapter=adapter)


def test_priv_15_counts_must_be_non_negative_ints(tmp_path: Path) -> None:
    log = _log(tmp_path)
    with pytest.raises(ComplianceError):
        log.append("ingest", count=-1)
    with pytest.raises(ComplianceError):
        log.append("ingest", count="many")  # type: ignore[arg-type]
    with pytest.raises(ComplianceError):
        log.append("ingest", count=True)


def test_config_hash_must_be_a_digest(tmp_path: Path) -> None:
    log = _log(tmp_path)
    with pytest.raises(ComplianceError):
        log.append("ingest", config_hash="not-a-digest")
    log.append("ingest", config_hash="")  # empty is allowed, for pre-config events


def test_priv_15_no_fixture_name_survives_a_full_run(tmp_path: Path) -> None:
    """The log carries counts and identifiers only, never names."""
    log = _log(tmp_path)
    names = ["Jane", "Street", "Kenneth", "Griffin", "jane.street@example.com"]
    for action, count in [
        ("collect", 12),
        ("ingest", 34),
        ("match", 34),
        ("export", 1),
        ("purge_person", 1),
    ]:
        log.append(action, adapter="first_party_export", count=count, config_hash="b" * 64)
    text = log.path.read_text(encoding="utf-8")
    for name in names:
        assert name.casefold() not in text.casefold()
    assert log.actions() <= ACTIONS


def test_priv_16_size_is_monotonically_non_decreasing(tmp_path: Path) -> None:
    log = _log(tmp_path)
    sizes = [log.size()]
    for index in range(5):
        log.append("ingest", count=index)
        sizes.append(log.size())
    assert sizes == sorted(sizes)
    assert sizes[0] == 0
    assert sizes[-1] > 0
    assert len(log.read()) == 5


def test_priv_16_a_second_log_object_appends_rather_than_truncates(tmp_path: Path) -> None:
    AuditLog(tmp_path).append("ingest", count=1)
    first = AuditLog(tmp_path).size()
    AuditLog(tmp_path).append("analyse", count=2)
    assert AuditLog(tmp_path).size() > first
    assert [r.action for r in AuditLog(tmp_path).read()] == ["ingest", "analyse"]


def test_priv_16_module_opens_in_append_mode_only() -> None:
    """Grep-level guard: the writer must not gain a ``w`` or ``w+`` open."""
    source = Path(__file__).parents[1] / "src" / "interlayer" / "core" / "audit.py"
    text = source.read_text(encoding="utf-8")
    assert 'open("a"' in text
    for forbidden in ('open("w"', "open('w'", 'open("w+"', "truncate("):
        assert forbidden not in text


def test_malformed_line_is_rejected_rather_than_ignored(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("ingest")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": "now", "who": "Jane Smith"}\n')
    with pytest.raises(ComplianceError):
        log.read()


def test_append_for_config_fills_the_hash(tmp_path: Path) -> None:
    config = Config(state_root=tmp_path)
    record = AuditLog.for_config(config).append_for(config, "analyse", count=5)
    assert record.config_hash == config.config_hash


def test_module_level_helper(tmp_path: Path) -> None:
    audit(tmp_path, "init", count=0)
    assert AuditLog(tmp_path).actions() == {"init"}


def test_log_directory_is_created_on_demand(tmp_path: Path) -> None:
    root = tmp_path / "deeper" / "root"
    AuditLog(root).append("init")
    assert (root / "audit.log").is_file()
