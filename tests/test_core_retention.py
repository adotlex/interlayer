"""Retention sweep (PRIV-11)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interlayer.core.config import Config
from interlayer.core.errors import RetentionError
from interlayer.core.models import (
    CompanyMatch,
    Edge,
    Firm,
    MatchStatus,
    Member,
    Provenance,
    Target,
)
from interlayer.core.retention import retention_cutoff, startup, sweep
from interlayer.core.store import Store

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(state_root=tmp_path / "state")


@pytest.fixture()
def store(config: Config) -> Iterator[Store]:
    with Store(config) as opened:
        yield opened


def _prov(days_ago: int) -> Provenance:
    return Provenance(source="first_party_export", collected_at=NOW - timedelta(days=days_ago))


def _seed(store: Store, days_ago: int, suffix: str) -> None:
    provenance = _prov(days_ago)
    member = Member(member_id=f"m_{suffix}", first_name="A", last_name="B", provenance=provenance)
    target = Target(target_id=f"t_{suffix}", firm=Firm.JANE_STREET, provenance=provenance)
    store.add_member(member)
    store.add_target(target)
    store.add_edge(
        Edge(member_id=member.member_id, target_id=target.target_id, provenance=provenance)
    )


def test_priv_11_records_older_than_the_retention_period_are_deleted(store: Store) -> None:
    _seed(store, 91, "old")
    _seed(store, 89, "fresh")
    assert store.member_count() == 2

    result = sweep(store, store.config, now=NOW)

    assert result.retention_days == 90
    assert result.deleted == {"members": 1, "targets": 1, "edges": 1, "company_review": 0}
    assert result.total == 3
    assert [m.member_id for m in store.members()] == ["m_fresh"]
    assert [t.target_id for t in store.targets()] == ["t_fresh"]
    assert [e.member_id for e in store.edges()] == ["m_fresh"]


def test_priv_11_the_boundary_is_exactly_retention_days(store: Store) -> None:
    _seed(store, 90, "edge")
    # 90 days ago is the cutoff instant itself; strictly-older rows go.
    assert sweep(store, store.config, now=NOW + timedelta(seconds=1)).total == 3


def test_priv_11_review_queue_expires_too(store: Store) -> None:
    store.queue_review(
        CompanyMatch(raw="Citadel Technology", status=MatchStatus.NEEDS_REVIEW),
        provenance=_prov(120),
    )
    assert store.pending_review_count() == 1
    assert sweep(store, store.config, now=NOW).deleted["company_review"] == 1
    assert store.pending_review_count() == 0


def test_priv_11_tombstones_are_exempt(store: Store) -> None:
    _seed(store, 200, "old")
    store.purge_person("m_old")
    assert store.tombstone_count() >= 1
    sweep(store, store.config, now=NOW)
    assert store.tombstone_count() >= 1
    assert store.is_tombstoned("m_old")


def test_priv_11_sweep_runs_on_startup_before_any_other_work(config: Config) -> None:
    with Store(config) as seeded:
        _seed(seeded, 400, "ancient")
    store, result = startup(config)
    try:
        assert result.total == 3
        assert store.member_count() == 0
    finally:
        store.close()


def test_priv_11_sweep_is_audited(store: Store) -> None:
    _seed(store, 400, "ancient")
    sweep(store, store.config, now=NOW)
    records = store.audit.read()
    assert [r.action for r in records] == ["retention_sweep"]
    assert records[0].entity_or_person_count == 3
    assert records[0].config_hash == store.config.config_hash


def test_zero_retention_expires_everything(config: Config) -> None:
    with Store(config.replace(retention_days=0)) as store:
        _seed(store, 0, "today")
        assert sweep(store, now=NOW + timedelta(seconds=1)).total == 3


def test_long_retention_keeps_everything(config: Config) -> None:
    with Store(config.replace(retention_days=3650)) as store:
        _seed(store, 400, "old")
        assert sweep(store, now=NOW).total == 0
        assert store.member_count() == 1


def test_cutoff_is_utc_and_honours_the_period() -> None:
    config = Config(retention_days=30)
    assert retention_cutoff(config, now=NOW) == NOW - timedelta(days=30)
    naive = retention_cutoff(config, now=datetime(2026, 8, 17, 12, 0))
    assert naive.tzinfo is UTC


def test_cutoff_refuses_an_impossible_period(config: Config) -> None:
    class Impossible:
        retention_days = -1

    with pytest.raises(RetentionError):
        retention_cutoff(Impossible())  # type: ignore[arg-type]


def test_sweep_defaults_to_the_stores_own_config(store: Store) -> None:
    _seed(store, 400, "old")
    assert sweep(store, now=NOW).total == 3


def test_sweep_result_is_falsey_when_nothing_expired(store: Store) -> None:
    _seed(store, 1, "new")
    assert not sweep(store, now=NOW)
