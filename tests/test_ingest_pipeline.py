"""The ingest layer end to end, plus the boundaries it must not cross."""

from __future__ import annotations

import importlib
import pkgutil
from datetime import UTC, datetime

import pytest

import interlayer.ingest
from interlayer.core.models import Firm, MatchStatus
from interlayer.ingest.company_match import CompanyMatcher, is_admissible, require_admissible
from interlayer.ingest.connections_csv import parse_connections_csv
from interlayer.ingest.person_match import PersonResolver, from_member
from interlayer.ingest.review import ReviewQueue
from interlayer.ingest.targets import load_registry

STAMP = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

CSV = (
    b"Notes:\n"
    b'"When exporting your connection data, you may notice that some of the '
    b'email addresses are missing."\n'
    b"\n"
    b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    b"Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,ada@example.com,"
    b"Jane Street Capital LLC,Quantitative Trader,14 Mar 2021\n"
    b"Grace,Hopper,https://www.linkedin.com/in/ghopper,,"
    b"Citadel Securities,Software Engineer,1 Jan 2019\n"
    b"Katherine,Johnson,https://www.linkedin.com/in/kjohnson,,"
    b"Citadel,Portfolio Manager,3 Feb 2020\n"
    b"Alan,Turing,https://www.linkedin.com/in/aturing,,"
    b"Citadel Technology,Director,9 Sep 2018\n"
    b"Mary,Jackson,https://www.linkedin.com/in/mjackson,,"
    b"Jane Street Entertainment,Producer,2 Feb 2022\n"
    b"Dorothy,Vaughan,https://www.linkedin.com/in/dvaughan,,"
    b"Goldman Sachs,VP,5 May 2017\n"
)


@pytest.fixture(scope="module")
def parsed():
    return parse_connections_csv(CSV, collected_at=STAMP)


def test_the_whole_ingest_path(parsed) -> None:
    matcher = CompanyMatcher(load_registry())
    queue = ReviewQueue()

    admitted: dict[str, Firm] = {}
    for member in parsed.members:
        explanation = matcher.explain(member.company_raw)
        match = queue.observe(explanation, seen_at=STAMP)
        if is_admissible(match):
            admitted[member.member_id] = require_admissible(match)

    firms = sorted(f.value for f in admitted.values())
    assert firms == ["citadel", "citadel_securities", "jane_street"]

    # Jane Street Entertainment and Goldman Sachs are out; Citadel Technology
    # is held for a human rather than guessed at.
    assert queue.unadjudicated_count == 1
    assert queue.pending()[0].key == "citadel technology"
    assert len(admitted) == 3


def test_a_review_item_only_enters_the_graph_after_a_human_says_so(parsed) -> None:
    matcher = CompanyMatcher(load_registry())
    queue = ReviewQueue()
    queue.observe_many((matcher.explain(m.company_raw) for m in parsed.members), seen_at=STAMP)

    assert queue.unadjudicated_count == 1
    queue.adjudicate("citadel technology", None, decided_at=STAMP)
    assert queue.unadjudicated_count == 0

    warm = CompanyMatcher(load_registry(), decisions=queue.decision_map())
    admitted = [m for m in parsed.members if is_admissible(warm.classify(m.company_raw))]
    assert len(admitted) == 3  # the rejection did not smuggle it in

    queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    warmer = CompanyMatcher(load_registry(), decisions=queue.decision_map())
    admitted = [m for m in parsed.members if is_admissible(warmer.classify(m.company_raw))]
    assert len(admitted) == 4


def test_members_resolve_to_people_with_stable_ids(parsed) -> None:
    matcher = CompanyMatcher(load_registry())
    resolver = PersonResolver()
    for member in parsed.members:
        match = matcher.classify(member.company_raw)
        key = match.firm.value if match.status is MatchStatus.CONFIDENT and match.firm else ""
        resolver.add(from_member(member, company_key=key))
    result = resolver.resolve()
    assert len(result.clusters) == len(parsed.members)
    assert result.review_pairs == ()
    assert all(c.person_id.startswith("p_") for c in result.clusters)


def test_ingest_is_deterministic_end_to_end() -> None:
    def run() -> list[tuple[str, str, str]]:
        parsed = parse_connections_csv(CSV, collected_at=STAMP)
        matcher = CompanyMatcher(load_registry())
        return [
            (m.member_id, matcher.classify(m.company_raw).status.value, m.linkedin_slug or "")
            for m in parsed.members
        ]

    assert run() == run()


# ---------------------------------------------------------------------------
# The error hierarchy hangs off core.errors
# ---------------------------------------------------------------------------


def test_ingest_errors_are_catchable_as_interlayer_errors() -> None:
    from interlayer.core.errors import ComplianceError, IngestError, InterlayerError
    from interlayer.ingest import (
        ConnectionsCsvError,
        NeedsReviewError,
        RegistryError,
        ReviewError,
    )

    for cls in (ConnectionsCsvError, RegistryError, ReviewError):
        assert issubclass(cls, IngestError)
        assert issubclass(cls, InterlayerError)
    # PRIV-20 is a control refusing, not a parse failing.
    assert issubclass(NeedsReviewError, ComplianceError)
    assert not issubclass(NeedsReviewError, IngestError)


def test_the_barrier_raises_a_compliance_error() -> None:
    from interlayer.core.errors import ComplianceError

    matcher = CompanyMatcher(load_registry())
    with pytest.raises(ComplianceError):
        require_admissible(matcher.classify("Citadel Technology"))


# ---------------------------------------------------------------------------
# Boundary 1 — the analysis layer never touches the network (PRIV-01/02)
# ---------------------------------------------------------------------------


def test_no_module_under_ingest_imports_a_network_library() -> None:
    forbidden = ("httpx", "requests", "urllib.request", "urllib3", "socket", "aiohttp")
    for info in pkgutil.iter_modules(interlayer.ingest.__path__):
        module = importlib.import_module(f"interlayer.ingest.{info.name}")
        source = module.__file__ or ""
        assert source
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for name in forbidden:
            assert f"import {name}" not in text, f"{info.name} imports {name}"
            assert f"from {name}" not in text, f"{info.name} imports from {name}"


def test_ingest_runs_with_sockets_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the ingest layer opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    parsed = parse_connections_csv(CSV, collected_at=STAMP)
    matcher = CompanyMatcher(load_registry())
    queue = ReviewQueue()
    queue.observe_many(matcher.explain(m.company_raw) for m in parsed.members)
    assert len(parsed.members) == 6
    assert queue.unadjudicated_count == 1
