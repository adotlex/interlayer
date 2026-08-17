"""Connections.csv parsing — header sniffing (C1), field whitelist, dates."""

from __future__ import annotations

import dataclasses
import zipfile
from datetime import UTC, date, datetime

import pytest

from interlayer.core.models import MEMBER_FIELD_ALLOWLIST, CompliancePosture, Member
from interlayer.ingest import ConnectionsCsvError
from interlayer.ingest.connections_csv import (
    CANONICAL_COLUMNS,
    RETAINED_COLUMNS,
    parse_connected_on,
    parse_connections_csv,
    sniff_header,
)

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On"

ROWS = [
    "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,ada@example.com,"
    "Jane Street,Quantitative Trader,14 Mar 2021",
    "Grace,Hopper,https://www.linkedin.com/in/ghopper,grace@example.com,"
    "Citadel Securities,Software Engineer,01 Jan 2019",
    "Katherine,Johnson,https://uk.linkedin.com/in/kjohnson?trk=x,,Citadel,Analyst,3 Feb 2020",
]

#: LinkedIn's own preamble is a "Notes:" line plus a quoted paragraph that has
#: itself contained commas and quotes. Length has changed release to release.
PREAMBLES: dict[int, str] = {
    0: "",
    2: 'Notes:\n"When exporting, some email addresses may be missing."\n',
    3: 'Notes:\n"When exporting, some email addresses may be missing."\n\n',
    5: (
        "Notes:\n"
        '"When exporting your connection data, you may notice that some of the '
        'email addresses are missing."\n'
        '"You will only see email addresses for connections who have allowed this."\n'
        "\n"
        '"See our help center for more, including ""Manage your settings""."\n'
    ),
}


def build_csv(
    preamble_lines: int = 0,
    *,
    header: str = HEADER,
    rows: list[str] | None = None,
    newline: str = "\n",
    bom: bool = False,
    encoding: str = "utf-8",
) -> bytes:
    body = PREAMBLES[preamble_lines] + header + "\n" + "\n".join(rows or ROWS) + "\n"
    if newline != "\n":
        body = body.replace("\n", newline)
    if bom:
        body = "﻿" + body
    return body.encode(encoding)


# ---------------------------------------------------------------------------
# C1 — sniff the header, never skiprows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preamble", [0, 2, 3, 5])
def test_header_is_sniffed_for_every_attested_preamble_length(preamble: int) -> None:
    result = parse_connections_csv(build_csv(preamble))
    assert result.header is not None
    assert result.header.preamble_lines == preamble
    assert [m.first_name for m in result.members] == ["Ada", "Grace", "Katherine"]


def test_a_fixed_skiprows_3_would_have_corrupted_three_of_the_four_fixtures() -> None:
    """The negative control for C1: this is the bug we are not shipping."""
    corrupted = 0
    for preamble in (0, 2, 3, 5):
        text = build_csv(preamble).decode("utf-8")
        naive_header = text.splitlines()[3] if len(text.splitlines()) > 3 else ""
        if not naive_header.startswith("First Name"):
            corrupted += 1
    assert corrupted == 3


def test_bom_and_crlf_together() -> None:
    result = parse_connections_csv(build_csv(2, newline="\r\n", bom=True))
    assert result.encoding == "utf-8-sig"
    assert result.header is not None
    assert result.header.preamble_lines == 2
    assert len(result.members) == 3


def test_latin_1_fallback() -> None:
    rows = [
        "Bj\xf6rn,\xc5kesson,https://www.linkedin.com/in/bakesson,,"
        "Jane Street,Trader,2 May 2022"
    ]
    result = parse_connections_csv(build_csv(0, rows=rows, encoding="latin-1"))
    assert result.encoding == "latin-1"
    assert result.members[0].first_name == "Björn"


def test_header_line_index_is_reported() -> None:
    scan = sniff_header(build_csv(5).decode("utf-8"))
    assert scan.line_index == 5
    assert scan.raw_columns == CANONICAL_COLUMNS


def test_missing_required_column_is_a_clear_diagnostic_not_a_crash() -> None:
    payload = build_csv(0, header="First Name,Last Name,Company,Position")
    with pytest.raises(ConnectionsCsvError) as excinfo:
        parse_connections_csv(payload)
    message = str(excinfo.value)
    assert "URL" in message
    assert "closest candidate" in message


def test_no_header_at_all_names_what_it_scanned() -> None:
    with pytest.raises(ConnectionsCsvError, match=r"no Connections\.csv header found"):
        parse_connections_csv(b"Notes:\nsome,random,text\n1,2,3\n")


def test_renamed_columns_are_resolved_and_unknown_ones_warned() -> None:
    payload = build_csv(
        0,
        header="first_name,last_name,Profile URL,Email,Company,Title,Connected,Follower Count",
        rows=[
            "Ada,Lovelace,https://www.linkedin.com/in/ada,a@b.c,"
            "Jane Street,Trader,14 Mar 2021,9"
        ],
    )
    result = parse_connections_csv(payload)
    member = result.members[0]
    assert member.first_name == "Ada"
    assert member.linkedin_slug == "ada"
    assert member.position == "Trader"
    assert member.connected_on == date(2021, 3, 14)
    assert any("Follower Count" in w for w in result.warnings)


def test_missing_optional_column_warns_and_leaves_the_field_empty() -> None:
    payload = build_csv(
        0,
        header="First Name,Last Name,URL",
        rows=["Ada,Lovelace,https://www.linkedin.com/in/ada"],
    )
    result = parse_connections_csv(payload)
    assert result.members[0].company_raw is None
    assert any("Connected On" in w for w in result.warnings)


def test_reads_connections_csv_out_of_the_archive_zip(tmp_path) -> None:
    archive = tmp_path / "Basic_LinkedInDataExport.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Contacts.csv", "should,never,be,parsed\n")
        zf.writestr("subdir/connections.CSV", build_csv(3))
    result = parse_connections_csv(archive)
    assert len(result.members) == 3


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14 Mar 2021", date(2021, 3, 14)),
        ("1 Jan 2019", date(2019, 1, 1)),
        ("03 September 2020", date(2020, 9, 3)),
        ("14 Sept 2021", date(2021, 9, 14)),
        ("14 Mar 21", date(2021, 3, 14)),
        ("Mar 14, 2021", date(2021, 3, 14)),
        ("2021-03-14", date(2021, 3, 14)),
        ("2021/03/14", date(2021, 3, 14)),
        ("", None),
        (None, None),
        ("not a date", None),
        ("31 Feb 2021", None),
        ("14 Smarch 2021", None),
    ],
)
def test_connected_on_variants(raw: str | None, expected: date | None) -> None:
    assert parse_connected_on(raw) == expected


def test_an_unparseable_date_keeps_the_row() -> None:
    rows = ["Ada,Lovelace,https://www.linkedin.com/in/ada,,Jane Street,Trader,soon"]
    result = parse_connections_csv(build_csv(0, rows=rows))
    assert len(result.members) == 1
    assert result.members[0].connected_on is None
    assert result.unparsed_dates == 1


def test_dates_do_not_depend_on_the_process_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """``strptime('%b')`` reads LC_TIME; the explicit month table does not."""
    monkeypatch.setenv("LC_TIME", "de_DE.UTF-8")
    assert parse_connected_on("14 Mar 2021") == date(2021, 3, 14)


# ---------------------------------------------------------------------------
# PRIV-05 / PRIV-06 — minimisation
# ---------------------------------------------------------------------------


def test_priv05_only_whitelisted_fields_survive() -> None:
    payload = build_csv(
        0,
        header=HEADER + ",Follower Count,Notes,Tags",
        rows=[ROWS[0] + ",512,SECRET-NOTE,SECRET-TAG"],
    )
    result = parse_connections_csv(payload)
    member = result.members[0]

    field_names = {f.name for f in dataclasses.fields(Member)} - {"provenance"}
    assert field_names <= MEMBER_FIELD_ALLOWLIST
    assert set(RETAINED_COLUMNS) < set(CANONICAL_COLUMNS)
    blob = repr(dataclasses.replace(member, provenance=None))
    assert "SECRET-NOTE" not in blob
    assert "SECRET-TAG" not in blob
    assert "512" not in blob


def test_priv06_email_is_dropped_at_parse_time_by_default() -> None:
    result = parse_connections_csv(build_csv(2))
    assert result.emails_retained is False
    assert all(m.email is None for m in result.members)
    assert "ada@example.com" not in repr(result.members)


def test_priv06_email_retained_only_on_explicit_opt_in() -> None:
    result = parse_connections_csv(build_csv(2), retain_emails=True)
    assert result.emails_retained is True
    assert result.members[0].email == "ada@example.com"
    # An empty cell stays None rather than becoming "".
    assert result.members[2].email is None


def test_email_is_never_the_join_key() -> None:
    """Two rows, same email, different people: still two members."""
    rows = [
        "Ada,Lovelace,https://www.linkedin.com/in/ada,shared@example.com,Jane Street,T,1 Jan 2020",
        "Bob,Lovelace,https://www.linkedin.com/in/bob,shared@example.com,Jane Street,T,1 Jan 2020",
    ]
    result = parse_connections_csv(build_csv(0, rows=rows), retain_emails=True)
    assert len({m.member_id for m in result.members}) == 2


# ---------------------------------------------------------------------------
# Identity and dedupe
# ---------------------------------------------------------------------------


def test_member_ids_come_from_the_slug_and_ignore_url_decoration() -> None:
    rows = [
        "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace/,,Jane Street,T,1 Jan 2020",
        "Ada,Lovelace,http://uk.linkedin.com/in/ada-lovelace?trk=nav,,Jane Street,T,4 Apr 2018",
    ]
    result = parse_connections_csv(build_csv(0, rows=rows))
    assert len(result.members) == 1
    assert result.duplicates_merged == 1
    # R1: dedupe on the public id, keep the earliest Connected On.
    assert result.members[0].connected_on == date(2018, 4, 4)


def test_rows_without_a_url_get_a_weak_identity() -> None:
    rows = ["Ada,Lovelace,,,Jane Street,Trader,1 Jan 2020"]
    result = parse_connections_csv(build_csv(0, rows=rows))
    assert result.members[0].linkedin_slug is None
    assert result.weak_identity_ids == (result.members[0].member_id,)
    assert any("weak identity" in w for w in result.warnings)


def test_blank_rows_are_skipped_not_counted() -> None:
    rows = [ROWS[0], "", ",,,,,,", ROWS[1]]
    result = parse_connections_csv(build_csv(0, rows=rows))
    assert result.rows_read == 2
    assert result.rows_skipped == 2


def test_quoted_field_containing_a_newline_survives() -> None:
    rows = [
        'Ada,Lovelace,https://www.linkedin.com/in/ada,,'
        '"Jane Street\nCapital",Trader,1 Jan 2020'
    ]
    result = parse_connections_csv(build_csv(0, rows=rows))
    assert len(result.members) == 1
    assert result.members[0].company_raw == "Jane Street\nCapital"


# ---------------------------------------------------------------------------
# Provenance (boundary 2)
# ---------------------------------------------------------------------------


def test_every_member_carries_provenance() -> None:
    stamp = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    result = parse_connections_csv(build_csv(3), collected_at=stamp)
    for member in result.members:
        assert member.provenance is not None
        assert member.provenance.collected_at == stamp
        assert member.provenance.posture is CompliancePosture.FIRST_PARTY_EXPORT


def test_parsing_is_deterministic() -> None:
    stamp = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    first = parse_connections_csv(build_csv(3), collected_at=stamp)
    second = parse_connections_csv(build_csv(3), collected_at=stamp)
    assert first.members == second.members
