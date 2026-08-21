"""Shared fixtures.

The network guard is autouse and deliberately hard to opt out of: the core
pipeline must never touch the network, and a test that could silently disable
that check would defeat the point of having it.
"""

from __future__ import annotations

import socket
from datetime import date
from pathlib import Path

import pytest

from interlayer.config import Settings
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Connection,
    Org,
    OrgKind,
    Person,
    Position,
    TargetFirm,
)


class NetworkEgressAttemptedError(RuntimeError):
    """Raised when code under test tries to open a socket."""


@pytest.fixture(autouse=True)
def block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block all outbound network access for every test.

    Patches socket *methods* rather than ``socket.socket`` itself: ``ssl.SSLSocket``
    subclasses ``socket.socket``, so replacing the class raises a TypeError at
    import time if ``ssl`` has not already been imported. Patching ``getaddrinfo``
    blocks DNS as well, so a hostname lookup fails just as loudly as a connect.

    Mark a test ``@pytest.mark.allow_network`` to opt out. Nothing in the core
    pipeline may use that marker.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    def _blocked(*args: object, **kwargs: object) -> None:
        raise NetworkEgressAttemptedError(
            "network access attempted during a test; the core pipeline must be offline"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_network: permit socket access (core pipeline may not)"
    )


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


@pytest.fixture
def artifact_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def settings(artifact_dir: Path) -> Settings:
    """Default settings pointed at a throwaway artifact directory.

    ``consensus_runs`` is lowered so unit tests stay fast; tests that assert on
    clustering stability should override it back up.
    """
    return Settings(
        artifact_dir=artifact_dir,
        gazetteer=Path("data/gazetteer/firms.yaml"),
        consensus_runs=5,
    )


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def gazetteer_path(repo_root: Path) -> Path:
    return repo_root / "data" / "gazetteer" / "firms.yaml"


# ---------------------------------------------------------------------------
# model factories
# ---------------------------------------------------------------------------


@pytest.fixture
def make_person():
    """Build a Person with sensible defaults; override any field by keyword."""

    def _make(first: str = "Ada", last: str = "Lovelace", **kw: object) -> Person:
        return Person.make(first, last, **kw)

    return _make


@pytest.fixture
def make_position():
    def _make(
        company: str,
        start_year: int | None = None,
        end_year: int | None = None,
        title: str = "",
        current: bool = False,
    ) -> Position:
        return Position(
            company_raw=company,
            title_raw=title,
            start=ApproxDate(year=start_year) if start_year else None,
            end=ApproxDate(year=end_year) if end_year else None,
            is_current=current,
        )

    return _make


@pytest.fixture
def sample_people(make_person, make_position) -> list[Person]:
    """A small, hand-checkable cast spanning targets, decoys and neutrals."""
    return [
        make_person(
            "Ada",
            "Lovelace",
            linkedin_url="https://www.linkedin.com/in/ada-lovelace/",
            positions=(make_position("Jane Street", 2019, current=True),),
        ),
        make_person(
            "Alan",
            "Turing",
            linkedin_url="https://www.linkedin.com/in/alan-turing/",
            positions=(make_position("Citadel Securities", 2020, current=True),),
        ),
        make_person(
            "Grace",
            "Hopper",
            linkedin_url="https://www.linkedin.com/in/grace-hopper/",
            positions=(make_position("Citadel LLC", 2018, 2022),),
        ),
        # Decoy: the military college, not the fund.
        make_person(
            "Edsger",
            "Dijkstra",
            linkedin_url="https://www.linkedin.com/in/edsger/",
            positions=(make_position("Citadel Broadcasting", 2015, 2019),),
        ),
        make_person(
            "Barbara",
            "Liskov",
            linkedin_url="https://www.linkedin.com/in/barbara/",
            positions=(make_position("Acme Widgets", 2017, current=True),),
        ),
    ]


@pytest.fixture
def sample_connections(sample_people) -> list[Connection]:
    return [
        Connection(person_id=p.person_id, connected_on=date(2021, 3, i + 1))
        for i, p in enumerate(sample_people)
    ]


@pytest.fixture
def sample_orgs() -> list[Org]:
    return [
        Org.make(
            "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
        ),
        Org.make(
            "Citadel Securities",
            OrgKind.COMPANY,
            is_target=True,
            target_firm=TargetFirm.CITADEL_SECURITIES,
        ),
        Org.make(
            "Citadel LLC", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.CITADEL_LLC
        ),
        Org.make("Citadel Broadcasting", OrgKind.COMPANY),
        Org.make("Acme Widgets", OrgKind.COMPANY),
    ]


@pytest.fixture
def sample_affiliations(sample_people, sample_orgs) -> list[Affiliation]:
    by_name = {o.name: o for o in sample_orgs}
    out: list[Affiliation] = []
    for person in sample_people:
        for pos in person.positions:
            org = by_name.get(pos.company_raw)
            if org is None:
                continue
            out.append(
                Affiliation(
                    person_id=person.person_id,
                    org_id=org.org_id,
                    kind=AffiliationKind.EMPLOYMENT,
                    title=pos.title_raw or None,
                    start=pos.start,
                    end=pos.end,
                )
            )
    return out


# ---------------------------------------------------------------------------
# LinkedIn export fixture
# ---------------------------------------------------------------------------

LINKEDIN_PREAMBLE = (
    '"Notes:"\n'
    '"When exporting your connection data, you may notice that some of the '
    "email addresses are missing. You will only see email addresses for "
    "connections who have allowed their connections to see or download their "
    "email address using this setting https://www.linkedin.com/psettings/privacy/email. "
    'You can learn more here https://www.linkedin.com/help/linkedin/answer/261"\n'
    "\n"
)


@pytest.fixture
def connections_csv(tmp_path: Path) -> Path:
    """A realistic Connections.csv, preamble and all.

    The preamble length has changed across LinkedIn export versions, so parsers
    must sniff for the header row rather than skipping a fixed number of lines.
    """
    body = (
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,,Jane Street,Trader,01 Mar 2021\n"
        "Alan,Turing,https://www.linkedin.com/in/alan-turing,,Citadel Securities,Quant,02 Mar 2021\n"
        "Grace,Hopper,https://www.linkedin.com/in/grace-hopper,gh@example.com,Citadel,Engineer,03 Mar 2021\n"
        "Edsger,Dijkstra,https://www.linkedin.com/in/edsger,,The Citadel,Professor,04 Mar 2021\n"
        "Barbara,Liskov,https://www.linkedin.com/in/barbara,,Acme Widgets,CTO,05 Mar 2021\n"
    )
    path = tmp_path / "Connections.csv"
    path.write_text(LINKEDIN_PREAMBLE + body, encoding="utf-8")
    return path


@pytest.fixture
def connections_csv_no_preamble(tmp_path: Path) -> Path:
    """Same data with no preamble at all -- an older export shape."""
    path = tmp_path / "Connections.csv"
    path.write_text(
        "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
        "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,,Jane Street,Trader,01 Mar 2021\n",
        encoding="utf-8",
    )
    return path
