"""Privacy controls owned by the core layer, asserted end to end.

Covers PRIV-01/02 (no network in the analysis layer), PRIV-04, PRIV-15, PRIV-16
and PRIV-21. The per-module mechanics live in the sibling ``test_core_*`` files;
this one checks the controls hold when the pieces are used together.
"""

from __future__ import annotations

import re
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from interlayer.core.audit import AuditLog
from interlayer.core.config import Config, default_config
from interlayer.core.models import (
    CompanyMatch,
    Edge,
    Firm,
    MatchStatus,
    Member,
    Provenance,
    Target,
)
from interlayer.core.retention import startup
from interlayer.core.store import Store

REPO_ROOT = Path(__file__).parents[1]
CORE = REPO_ROOT / "src" / "interlayer" / "core"
OWNED_MODULES = ("config.py", "store.py", "audit.py", "retention.py", "errors.py")

FIXTURE_NAMES = ("Jane", "Doe", "Kenneth", "Griffin", "Nguyễn", "jane.doe@example.com")
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
PROV = Provenance(source="first_party_export", collected_at=NOW)


# ---------------------------------------------------------------------------
# PRIV-01 / PRIV-02 — the analysis layer never touches the network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", OWNED_MODULES)
def test_priv_02_core_modules_import_no_network_library(module: str) -> None:
    source = (CORE / module).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", "*"))
    )
    for forbidden in ("httpx", "requests", "urllib.request", "urllib3"):
        assert not re.search(rf"^\s*(import|from)\s+{re.escape(forbidden)}\b", code, re.MULTILINE)
    assert not re.search(r"^\s*import\s+socket\b", code, re.MULTILINE)
    assert not re.search(r"^\s*from\s+socket\b", code, re.MULTILINE)


def test_priv_01_full_core_workflow_runs_with_sockets_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("the analysis layer must not open a network connection (PRIV-01)")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    config = Config(state_root=tmp_path / "state", retention_days=30)
    store, swept = startup(config)
    try:
        member = Member(
            member_id="m_bridge",
            first_name="Jane",
            last_name="Doe",
            linkedin_slug="jane-doe",
            company_raw="Acme Capital",
            connected_on=date(2024, 1, 1),
            provenance=PROV,
        )
        stale = Member(
            member_id="m_stale",
            first_name="Old",
            last_name="Record",
            provenance=Provenance(
                source="first_party_export", collected_at=NOW - timedelta(days=400)
            ),
        )
        target = Target(
            target_id="t_js", firm=Firm.JANE_STREET, first_name="Sam", provenance=PROV
        )
        store.add_members([member, stale])
        store.add_target(target)
        store.add_edge(Edge(member_id="m_bridge", target_id="t_js", provenance=PROV))
        store.queue_review(
            CompanyMatch(raw="Citadel Technology", status=MatchStatus.NEEDS_REVIEW),
            provenance=PROV,
        )
        store.prune_non_bridges()
        store.purge_person("m_bridge")
        assert swept.total == 0
        assert store.member_count() == 0
    finally:
        store.close()


# ---------------------------------------------------------------------------
# PRIV-04 — only the sanctioned adapter posture is enabled out of the box
# ---------------------------------------------------------------------------


def test_priv_04_default_config_enables_only_first_party_export() -> None:
    assert default_config().enabled_adapters == ["first_party_export"]


# ---------------------------------------------------------------------------
# PRIV-15 / PRIV-16 — the audit trail across a whole run
# ---------------------------------------------------------------------------


def _run(config: Config, suffix: str) -> None:
    store, _ = startup(config)
    try:
        member = Member(
            member_id=f"m_{suffix}",
            first_name="Jane",
            last_name="Doe",
            linkedin_slug="jane-doe",
            email="jane.doe@example.com",
            provenance=PROV,
        )
        target = Target(
            target_id=f"t_{suffix}",
            firm=Firm.CITADEL_SECURITIES,
            first_name="Kenneth",
            last_name="Griffin",
            provenance=PROV,
        )
        store.add_member(member)
        store.add_target(target)
        store.add_edge(
            Edge(member_id=member.member_id, target_id=target.target_id, provenance=PROV)
        )
        store.audit.append_for(config, "ingest", adapter="first_party_export", count=1)
        store.audit.append_for(config, "match", count=1)
        store.prune_non_bridges()
        store.audit.append_for(config, "export", adapter="none", count=1)
        store.purge_person(member.member_id)
    finally:
        store.close()


def test_priv_15_audit_log_is_jsonl_with_every_action_and_no_names(tmp_path: Path) -> None:
    config = Config(state_root=tmp_path / "state")
    _run(config, "a")

    log = AuditLog(config.state_root)
    records = log.read()
    assert records
    assert {"retention_sweep", "ingest", "match", "prune", "export", "purge_person"} <= {
        r.action for r in records
    }
    for record in records:
        assert record.config_hash == config.config_hash

    text = log.path.read_text(encoding="utf-8")
    for name in FIXTURE_NAMES:
        assert name.casefold() not in text.casefold()
    assert "@" not in text


def test_priv_16_log_only_grows_across_runs(tmp_path: Path) -> None:
    config = Config(state_root=tmp_path / "state")
    log = AuditLog(config.state_root)
    sizes = [log.size()]
    for index, suffix in enumerate("abc"):
        _run(config, f"{suffix}{index}")
        sizes.append(log.size())
    assert sizes == sorted(sizes)
    assert sizes[0] == 0 < sizes[-1]


def test_priv_16_only_purge_all_removes_the_log(tmp_path: Path) -> None:
    config = Config(state_root=tmp_path / "state")
    _run(config, "a")
    assert config.audit_path.is_file()
    store = Store(config)
    store.purge_all()
    assert not config.audit_path.exists()


# ---------------------------------------------------------------------------
# PRIV-21 — the shipped privacy notice
# ---------------------------------------------------------------------------


def test_priv_21_privacy_md_exists_and_is_substantive() -> None:
    path = REPO_ROOT / "PRIVACY.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert len(text.strip()) > 2000


@pytest.mark.parametrize(
    "needle",
    [
        "controller",
        "purpose",
        "legitimate interest",
        "Art. 6(1)(f)",
        "household exemption",
        "professional",
        "retention",
        "90 days",
        "Art. 14",
        "Art. 15",
        "Art. 17",
        "erasure",
        "purge --person",
        "purge --all",
        "special categor",
    ],
)
def test_priv_21_privacy_md_covers_every_required_topic(needle: str) -> None:
    text = (REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8")
    assert needle.casefold() in text.casefold(), needle


def test_priv_21_privacy_md_does_not_claim_the_household_exemption() -> None:
    text = (REPO_ROOT / "PRIVACY.md").read_text(encoding="utf-8").casefold()
    assert "probably does not apply" in text or "does not apply" in text
