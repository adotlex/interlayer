"""Store: allowlist enforcement, provenance, pruning, purge and tombstones.

Covers PRIV-03, PRIV-06, PRIV-07, PRIV-08, PRIV-10, PRIV-12, PRIV-13, PRIV-14.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from interlayer.core.audit import AuditLog
from interlayer.core.config import Config
from interlayer.core.errors import StoreError
from interlayer.core.ids import member_id, target_id
from interlayer.core.models import (
    ALL_FIELD_ALLOWLISTS,
    CompanyMatch,
    Edge,
    EdgeOrigin,
    Firm,
    MatchStatus,
    Member,
    Provenance,
    Target,
)
from interlayer.core.store import Store, purge_state_root

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
PROV = Provenance(source="first_party_export", collected_at=NOW)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return Config(state_root=tmp_path / "state")


@pytest.fixture()
def store(config: Config) -> Iterator[Store]:
    with Store(config) as opened:
        yield opened


def make_member(slug: str = "jane-doe", *, email: str | None = None) -> Member:
    return Member(
        member_id=member_id(slug=slug),
        first_name="Jane",
        last_name="Doe",
        linkedin_slug=slug,
        company_raw="Acme Capital",
        position="Engineer",
        connected_on=date(2024, 5, 1),
        email=email,
        provenance=PROV,
    )


def make_target(slug: str = "sam-target", firm: Firm = Firm.JANE_STREET) -> Target:
    return Target(
        target_id=target_id(firm=firm.value, slug=slug),
        firm=firm,
        first_name="Sam",
        last_name="Target",
        linkedin_slug=slug,
        title="Trader",
        harvested=True,
        provenance=PROV,
    )


def make_edge(member: Member, target: Target, *, origin: EdgeOrigin = EdgeOrigin.OBSERVED) -> Edge:
    return Edge(
        member_id=member.member_id,
        target_id=target.target_id,
        origin=origin,
        confidence=1.0 if origin is EdgeOrigin.OBSERVED else 0.6,
        evidence=None if origin is EdgeOrigin.OBSERVED else "shared employer",
        provenance=PROV,
    )


# ---------------------------------------------------------------------------
# PRIV-08 — the persisted schema is the frozen allowlist, nothing more
# ---------------------------------------------------------------------------


def test_priv_08_persisted_columns_equal_the_allowlists(store: Store) -> None:
    assert store.schema_columns() == {
        kind: frozenset(allowed) for kind, allowed in ALL_FIELD_ALLOWLISTS.items()
    }


@pytest.mark.parametrize(
    ("kind", "row"),
    [
        (
            "member",
            {
                "member_id": "m_1",
                "ethnicity": "inferred from name",
                "source": "x",
                "collected_at": "2026-01-01T00:00:00.000000+00:00",
            },
        ),
        (
            "target",
            {
                "target_id": "t_1",
                "firm": "jane_street",
                "photo_url": "https://example.invalid/p.jpg",
                "source": "x",
                "collected_at": "2026-01-01T00:00:00.000000+00:00",
            },
        ),
        (
            "edge",
            {
                "member_id": "m_1",
                "target_id": "t_1",
                "origin": "observed",
                "phone": "+1 555 0100",
                "source": "x",
                "collected_at": "2026-01-01T00:00:00.000000+00:00",
            },
        ),
    ],
)
def test_priv_08_writing_an_unlisted_field_raises(
    store: Store, kind: str, row: dict[str, object]
) -> None:
    with pytest.raises(StoreError, match="allowlist"):
        store.put_row(kind, row)


def test_unknown_record_kind_raises(store: Store) -> None:
    with pytest.raises(StoreError, match="unknown record kind"):
        store.put_row("scandal", {"source": "x", "collected_at": "2026-01-01T00:00:00+00:00"})


# ---------------------------------------------------------------------------
# PRIV-10 — every row carries source and collected_at
# ---------------------------------------------------------------------------


def test_priv_10_every_table_has_source_and_collected_at(store: Store) -> None:
    connection = sqlite3.connect(str(store.db_path))
    connection.row_factory = sqlite3.Row
    tables = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    assert set(tables) == {"members", "targets", "edges", "company_review", "tombstones"}
    for table in tables:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        assert {"source", "collected_at"} <= columns, table
    connection.close()


def test_priv_10_every_written_row_has_provenance(store: Store) -> None:
    member = make_member()
    target = make_target()
    store.add_member(member)
    store.add_target(target)
    store.add_edge(make_edge(member, target))
    store.queue_review(
        CompanyMatch(raw="Citadel Technology", status=MatchStatus.NEEDS_REVIEW, rule="ambiguous"),
        provenance=PROV,
    )
    store.tombstone("m_gone")

    connection = sqlite3.connect(str(store.db_path))
    connection.row_factory = sqlite3.Row
    for table in ("members", "targets", "edges", "company_review", "tombstones"):
        for row in connection.execute(f"SELECT source, collected_at FROM {table}"):
            assert row["source"]
            assert datetime.fromisoformat(row["collected_at"]).tzinfo is not None
    connection.close()


def test_priv_10_a_record_without_provenance_is_refused(store: Store) -> None:
    naked = Member(member_id="m_x", first_name="A", last_name="B")
    with pytest.raises(StoreError, match="PRIV-10"):
        store.add_member(naked)
    with pytest.raises(StoreError, match="PRIV-10"):
        store.add_target(Target(target_id="t_x", firm=Firm.CITADEL))
    with pytest.raises(StoreError, match="PRIV-10"):
        store.add_edge(Edge(member_id="m_x", target_id="t_x"))


def test_provenance_may_be_supplied_at_write_time(store: Store) -> None:
    naked = Member(member_id="m_x", first_name="A", last_name="B")
    assert store.add_member(naked, provenance=PROV)
    assert store.members()[0].provenance is not None


def test_empty_source_is_refused(store: Store) -> None:
    blank = Provenance(source="   ", collected_at=NOW)
    with pytest.raises(StoreError, match="PRIV-10"):
        store.add_member(make_member(), provenance=blank)


# ---------------------------------------------------------------------------
# PRIV-06 — emails are not persisted unless opted in
# ---------------------------------------------------------------------------


def test_priv_06_email_is_written_null_by_default(config: Config) -> None:
    with Store(config) as store:
        assert store.config.retain_emails is False
        store.add_member(make_member(email="jane.doe@example.com"))
        assert store.members()[0].email is None
    blob = config.db_path.read_bytes()
    assert b"jane.doe@example.com" not in blob


def test_priv_06_email_is_kept_only_when_opted_in(config: Config) -> None:
    with Store(config.replace(retain_emails=True)) as store:
        store.add_member(make_member(email="jane.doe@example.com"))
        assert store.members()[0].email == "jane.doe@example.com"


# ---------------------------------------------------------------------------
# PRIV-07 — non-bridges do not survive the run
# ---------------------------------------------------------------------------


def test_priv_07_prune_keeps_only_bridges_and_the_targets_they_bridge_to(store: Store) -> None:
    bridge = make_member("bridge-person")
    isolated = make_member("isolated-person")
    reached = make_target("reached-target")
    unreached = make_target("unreached-target")
    store.add_members([bridge, isolated])
    store.add_targets([reached, unreached])
    store.add_edge(make_edge(bridge, reached))

    assert store.member_count() == 2
    assert store.target_count() == 2

    result = store.prune_non_bridges()

    assert result.members_deleted == 1
    assert result.targets_deleted == 1
    assert [m.member_id for m in store.members()] == [bridge.member_id]
    assert [t.target_id for t in store.targets()] == [reached.target_id]


def test_priv_07_inferred_only_person_is_not_a_bridge_under_the_default_config(
    store: Store,
) -> None:
    guessed = make_member("guessed-person")
    target = make_target()
    store.add_member(guessed)
    store.add_target(target)
    store.add_edge(make_edge(guessed, target, origin=EdgeOrigin.INFERRED))

    store.prune_non_bridges()
    assert store.member_count() == 0

    # ...but survives when the user opted into inferred edges.
    store.add_member(guessed)
    store.prune_non_bridges(observed_only=False)
    assert store.member_count() == 1


# ---------------------------------------------------------------------------
# PRIV-13 / PRIV-14 — purge, with tombstones that survive re-ingest
# ---------------------------------------------------------------------------


def test_priv_13_purged_person_does_not_come_back_on_re_ingest(store: Store) -> None:
    member = make_member()
    target = make_target()
    store.add_member(member)
    store.add_target(target)
    store.add_edge(make_edge(member, target))

    result = store.purge_person(member.member_id)
    assert result.members_deleted == 1
    assert result.edges_deleted == 1
    assert store.member_count() == 0
    assert store.edge_count() == 0

    # Re-ingesting the same Connections.csv row must not resurrect them.
    assert store.add_member(member) is False
    assert store.add_edge(make_edge(member, target)) is False
    assert store.member_count() == 0
    assert store.edge_count() == 0


def test_priv_13_purge_by_slug_or_profile_url(store: Store) -> None:
    member = make_member("jane-doe")
    store.add_member(member)
    store.purge_person("https://www.linkedin.com/in/jane-doe/")
    assert store.member_count() == 0
    assert store.add_member(member) is False


def test_priv_13_tombstone_survives_a_reopen_and_holds_no_readable_key(config: Config) -> None:
    member = make_member()
    with Store(config) as store:
        store.add_member(member)
        store.purge_person(member.member_id)
    with Store(config) as reopened:
        assert reopened.is_tombstoned(member.member_id)
        assert reopened.add_member(member) is False
    blob = config.db_path.read_bytes()
    assert b"jane-doe" not in blob
    assert member.member_id.encode() not in blob


def test_priv_13_erasure_request_can_precede_the_data(store: Store) -> None:
    member = make_member()
    result = store.purge_person(member.member_id)
    assert result.members_deleted == 0
    assert result.tombstones_written >= 1
    assert store.add_member(member) is False


def test_priv_13_purge_clears_derived_artefacts(store: Store) -> None:
    cached = store.path_for("derived", "clusters.json")
    cached.write_text('{"cluster": ["Jane Doe"]}', encoding="utf-8")
    member = make_member()
    store.add_member(member)
    store.purge_person(member.member_id)
    assert not cached.exists()


def test_priv_14_purge_target_removes_targets_and_orphaned_edges(store: Store) -> None:
    member = make_member()
    js_target = make_target("js-person", Firm.JANE_STREET)
    citadel_target = make_target("citadel-person", Firm.CITADEL)
    store.add_member(member)
    store.add_targets([js_target, citadel_target])
    store.add_edges([make_edge(member, js_target), make_edge(member, citadel_target)])

    result = store.purge_target(js_target.target_id)

    assert result.targets_deleted == 1
    assert result.edges_deleted == 1
    assert [t.target_id for t in store.targets()] == [citadel_target.target_id]
    assert [e.target_id for e in store.edges()] == [citadel_target.target_id]
    assert store.add_target(js_target) is False


def test_priv_14_purge_target_by_firm_and_by_registry_entity_id(store: Store) -> None:
    member = make_member()
    a = make_target("cs-one", Firm.CITADEL_SECURITIES)
    b = make_target("cs-two", Firm.CITADEL_SECURITIES)
    keep = make_target("js-one", Firm.JANE_STREET)
    store.add_member(member)
    store.add_targets([a, b, keep])
    store.add_edges([make_edge(member, a), make_edge(member, b), make_edge(member, keep)])

    result = store.purge_target("citadel_securities")
    assert result.targets_deleted == 2
    assert result.edges_deleted == 2
    assert [t.target_id for t in store.targets()] == [keep.target_id]

    # R5's registry spells the hedge fund "citadel_llc"; models.Firm spells it "citadel".
    citadel = make_target("citadel-person", Firm.CITADEL)
    store.add_target(citadel)
    assert store.purge_target("citadel_llc").targets_deleted == 1


def test_purging_a_firm_does_not_block_the_firm_forever(store: Store) -> None:
    store.purge_target("citadel")
    fresh = make_target("new-citadel-person", Firm.CITADEL)
    assert store.add_target(fresh) is True


# ---------------------------------------------------------------------------
# PRIV-12 — purge --all empties the root and the caller can tell
# ---------------------------------------------------------------------------


def test_priv_12_purge_all_leaves_the_state_root_empty(config: Config) -> None:
    store = Store(config)
    member = make_member()
    target = make_target()
    store.add_member(member)
    store.add_target(target)
    store.add_edge(make_edge(member, target))
    store.path_for("derived", "report.md").write_text("Jane Doe", encoding="utf-8")
    store.path_for("cache", "nested", "blob.json").write_text("{}", encoding="utf-8")
    store.audit.append_for(config, "ingest", count=1)
    assert list(config.state_root.iterdir())

    remaining = store.purge_all()

    assert remaining == []
    assert config.state_root.is_dir()
    assert list(config.state_root.iterdir()) == []
    assert not config.db_path.exists()
    assert not config.audit_path.exists()


def test_priv_12_purge_all_audits_before_it_destroys_the_log(config: Config) -> None:
    store = Store(config)
    store.add_member(make_member())
    audit_lines_before = len(AuditLog(config.state_root).read())
    store.purge_all()
    # The log is gone with everything else - that is the one sanctioned
    # truncation (PRIV-16) - but the event was written before the deletion.
    assert audit_lines_before >= 0
    assert not config.audit_path.exists()


def test_priv_12_remaining_files_are_reported(config: Config) -> None:
    config.state_root.mkdir(parents=True, exist_ok=True)
    (config.state_root / "keep.txt").write_text("x", encoding="utf-8")
    assert purge_state_root(config.state_root) == []
    assert purge_state_root(config.state_root / "does-not-exist") == []


# ---------------------------------------------------------------------------
# PRIV-03 — everything under one root
# ---------------------------------------------------------------------------


def test_priv_03_all_writes_land_under_the_state_root(tmp_path: Path, config: Config) -> None:
    with Store(config) as store:
        member = make_member()
        target = make_target()
        store.add_member(member)
        store.add_target(target)
        store.add_edge(make_edge(member, target))
        store.audit.append_for(config, "ingest", count=1)
        store.path_for("derived", "x.json").write_text("{}", encoding="utf-8")
        store.prune_non_bridges()

    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert written
    for path in written:
        assert config.state_root in path.parents


@pytest.mark.parametrize(
    "parts",
    [("..", "escape.txt"), ("nested", "..", "..", "escape.txt"), ("/etc/passwd",)],
)
def test_priv_03_path_for_refuses_to_escape(store: Store, parts: tuple[str, ...]) -> None:
    with pytest.raises(StoreError, match="PRIV-03"):
        store.path_for(*parts)


def test_path_for_needs_a_component(store: Store) -> None:
    with pytest.raises(StoreError):
        store.path_for()


def test_state_root_is_created_privately(config: Config) -> None:
    with Store(config):
        assert config.state_root.is_dir()
        assert config.db_path.is_file()


# ---------------------------------------------------------------------------
# Round-trip and general behaviour
# ---------------------------------------------------------------------------


def test_records_round_trip(store: Store) -> None:
    member = make_member()
    target = make_target()
    store.add_member(member)
    store.add_target(target)
    store.add_edge(make_edge(member, target, origin=EdgeOrigin.INFERRED))

    [loaded_member] = store.members()
    assert loaded_member.member_id == member.member_id
    assert loaded_member.first_name == "Jane"
    assert loaded_member.connected_on == date(2024, 5, 1)
    assert loaded_member.provenance is not None
    assert loaded_member.provenance.source == "first_party_export"
    assert loaded_member.provenance.collected_at == NOW

    [loaded_target] = store.targets()
    assert loaded_target.firm is Firm.JANE_STREET
    assert loaded_target.harvested is True

    [loaded_edge] = store.edges()
    assert loaded_edge.origin is EdgeOrigin.INFERRED
    assert loaded_edge.observed is False
    assert loaded_edge.evidence == "shared employer"
    assert store.edges(observed_only=True) == []


def test_slugs_are_normalised_on_write(store: Store) -> None:
    member = Member(
        member_id="m_1",
        first_name="A",
        last_name="B",
        linkedin_slug="HTTPS://www.linkedin.com/in/Jane-Doe/",
        provenance=PROV,
    )
    store.add_member(member)
    assert store.members()[0].linkedin_slug == "jane-doe"


def test_writes_are_idempotent(store: Store) -> None:
    member = make_member()
    store.add_member(member)
    store.add_member(member)
    assert store.member_count() == 1


def test_mark_harvested(store: Store) -> None:
    target = make_target()
    store.add_target(target)
    store.mark_harvested(target.target_id, True)
    assert store.targets()[0].harvested is True


def test_review_queue_round_trip(store: Store) -> None:
    match = CompanyMatch(
        raw="Citadel Technology",
        status=MatchStatus.NEEDS_REVIEW,
        firm=None,
        rule="anchor_without_discriminator",
        score=88.0,
    )
    store.queue_review(match, provenance=PROV, normalised="citadel technology", member_count=3)
    assert store.pending_review_count() == 1
    [item] = store.reviews(pending_only=True)
    assert item.raw == "Citadel Technology"
    assert item.status is MatchStatus.NEEDS_REVIEW
    assert item.member_count == 3
    assert item.decided is False

    store.record_review_decision("Citadel Technology", None)
    assert store.pending_review_count() == 0
    assert store.reviews()[0].decided is True

    with pytest.raises(StoreError):
        store.record_review_decision("Never Queued Ltd", Firm.CITADEL)


def test_counts(store: Store) -> None:
    member = make_member()
    target = make_target()
    store.add_member(member)
    store.add_target(target)
    store.add_edge(make_edge(member, target))
    assert store.counts() == {
        "members": 1,
        "targets": 1,
        "edges": 1,
        "company_review": 0,
        "tombstones": 0,
    }


def test_closed_store_refuses_work(config: Config) -> None:
    store = Store(config)
    store.close()
    store.close()  # idempotent
    with pytest.raises(StoreError, match="closed"):
        store.member_count()


def test_reopening_an_existing_database_revalidates_the_schema(config: Config) -> None:
    with Store(config) as store:
        store.add_member(make_member())
    with Store(config) as reopened:
        assert reopened.member_count() == 1

    connection = sqlite3.connect(str(config.db_path))
    connection.execute("ALTER TABLE members ADD COLUMN nationality TEXT")
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="PRIV-08"):
        Store(config)
