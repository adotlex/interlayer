"""Regression tests for orchestrator amendments to the frozen Wave 2 contract.

Two changes were made after B1 reported gaps, and both are easy to lose in a
later refactor:

* ``Provenance.posture`` became a persisted field, so the store can say how a
  row was collected instead of assuming the default (PRIV-04 auditability).
* ``Firm.from_registry_id`` became the single place that reconciles the
  spellings the registry, the store and the CLI each arrived at independently.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from interlayer.core.config import default_config
from interlayer.core.ids import member_id, target_id
from interlayer.core.models import (
    ALL_FIELD_ALLOWLISTS,
    CompliancePosture,
    Edge,
    Firm,
    Member,
    Provenance,
    Target,
)
from interlayer.core.store import Store


@pytest.fixture
def store_config(tmp_path: Path):
    return dataclasses.replace(default_config(), state_root=tmp_path)


def test_posture_is_a_persisted_field_on_every_table() -> None:
    for kind, allowlist in ALL_FIELD_ALLOWLISTS.items():
        assert "posture" in allowlist, f"{kind} allowlist lost posture"


def test_posture_survives_the_round_trip(store_config) -> None:
    """A manually captured row must not read back as a first-party export.

    Without this, every record in the store claims the most benign posture
    regardless of how it was actually obtained, which makes the PRIV-04
    guarantee unauditable after the fact.
    """
    prov = Provenance(
        source="har",
        collected_at=datetime.now(UTC),
        posture=CompliancePosture.MANUAL_CAPTURE,
        truncated=True,
    )
    mid = member_id(slug="/in/round-trip")
    tid = target_id(firm="citadel", slug="/in/target")

    with Store(store_config) as store:
        store.add_member(Member(member_id=mid, first_name="A", last_name="B", provenance=prov))
        store.add_target(
            Target(target_id=tid, firm=Firm.CITADEL, harvested=True, provenance=prov)
        )
        store.add_edge(Edge(member_id=mid, target_id=tid, provenance=prov))

    with Store(store_config) as store:
        for record in (
            next(iter(store.members())),
            next(iter(store.targets())),
            next(iter(store.edges())),
        ):
            assert record.provenance is not None
            assert record.provenance.posture is CompliancePosture.MANUAL_CAPTURE


def test_truncation_flag_survives_on_edges(store_config) -> None:
    """A capped result set means absence of an edge is not evidence of no edge."""
    prov = Provenance(
        source="manual_csv",
        collected_at=datetime.now(UTC),
        posture=CompliancePosture.MANUAL_CAPTURE,
        truncated=True,
    )
    mid = member_id(slug="/in/a")
    tid = target_id(firm="jane_street", slug="/in/b")
    with Store(store_config) as store:
        store.add_member(Member(member_id=mid, first_name="A", last_name="B", provenance=prov))
        store.add_target(
            Target(target_id=tid, firm=Firm.JANE_STREET, harvested=True, provenance=prov)
        )
        store.add_edge(Edge(member_id=mid, target_id=tid, provenance=prov))

    with Store(store_config) as store:
        edge = next(iter(store.edges()))
        assert edge.provenance is not None
        assert edge.provenance.truncated is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("citadel", Firm.CITADEL),
        ("citadel_llc", Firm.CITADEL),
        ("citadel-llc", Firm.CITADEL),
        ("Citadel_Enterprise", Firm.CITADEL),
        ("citadel_securities", Firm.CITADEL_SECURITIES),
        ("citadel-securities", Firm.CITADEL_SECURITIES),
        ("citadel_sec", Firm.CITADEL_SECURITIES),
        ("jane_street", Firm.JANE_STREET),
        ("jane-street-global", Firm.JANE_STREET),
        ("Jane_Street_Capital", Firm.JANE_STREET),
    ],
)
def test_firm_registry_aliases_resolve(raw: str, expected: Firm) -> None:
    assert Firm.from_registry_id(raw) is expected


def test_firm_rejects_unknown_identifiers() -> None:
    """`The Citadel` is a military college. Silent coercion is the failure mode."""
    with pytest.raises(ValueError, match="unknown firm identifier"):
        Firm.from_registry_id("the_citadel")


def test_citadel_and_citadel_securities_stay_distinct() -> None:
    """Two legally separate firms. Merging them corrupts every per-firm result."""
    assert Firm.from_registry_id("citadel") is not Firm.from_registry_id("citadel_securities")
    assert target_id(firm="citadel", slug="/in/x") != target_id(
        firm="citadel_securities", slug="/in/x"
    )


def test_enum_str_behaviour_is_unchanged() -> None:
    """Guards the UP042 suppression.

    Converting these to ``StrEnum`` would change ``str(Firm.CITADEL)`` from
    ``"Firm.CITADEL"`` to ``"citadel"`` and silently alter every f-string that
    interpolates one. If that conversion ever happens it must be deliberate.
    """
    assert str(Firm.CITADEL) == "Firm.CITADEL"
    assert Firm.CITADEL.value == "citadel"
    assert CompliancePosture.MANUAL_CAPTURE.value == "manual_capture"
