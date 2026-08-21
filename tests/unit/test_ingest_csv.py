"""Header sniffing and row mechanics for a LinkedIn ``Connections.csv``.

Ingest is where the real world enters the pipeline, so this file is deliberately
hostile to its input. Two properties are load-bearing and each has burned a
naive parser before:

* the ``Notes:`` preamble has been 2, 3 and 4 lines across export versions, and
  has contained free prose with unbalanced quotes -- so the header must be found
  by scanning physical lines, not by ``skiprows=N`` and not by handing the whole
  file to ``csv.reader``;
* column order is not guaranteed, so a value read out of the wrong slot is a
  silent corruption that nothing downstream can detect.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from interlayer.errors import IngestError
from interlayer.ingest.csv_reader import (
    CANONICAL_HEADER,
    map_columns,
    normalize_header_cell,
    read_rows,
)

EXPORTS = Path(__file__).resolve().parents[1] / "fixtures" / "exports"

HEADER = ",".join(CANONICAL_HEADER) + "\n"
ADA = "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,,Jane Street,Trader,01 Mar 2021\n"

# Five prose lines plus the blank separator: real preambles have been every
# length from zero to four, and the sixth entry exists to prove nothing is
# pinned to "at most four".
PREAMBLE_LINES = (
    '"Notes:"',
    '"When exporting your connection data, you may notice that some of the email addresses are missing."',
    '"You will only see email addresses for connections who have allowed their connections to see them."',
    '"You can learn more here https://www.linkedin.com/help/linkedin/answer/261"',
    '"Export generated 02 Nov 2024."',
    "",
)


def preamble(n: int) -> str:
    return "".join(f"{line}\n" for line in PREAMBLE_LINES[:n])


def write(tmp_path: Path, text: str, name: str = "Connections.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# finding the header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 6])
def test_header_is_found_at_every_preamble_length(tmp_path: Path, n: int) -> None:
    path = write(tmp_path, preamble(n) + HEADER + ADA)

    header, rows, issues = read_rows(path)

    # 1-based physical line, so an operator can open the file and look at it.
    assert header.lineno == n + 1
    assert set(header.columns) == {
        "first_name",
        "last_name",
        "url",
        "email",
        "company",
        "position",
        "connected_on",
    }
    assert len(rows) == 1
    assert rows[0].get("company") == "Jane Street"
    assert issues == []


def test_unbalanced_quote_in_preamble_defeats_a_whole_file_csv_reader() -> None:
    """The reason the header is located by scanning physical lines.

    The fixture's second line opens a quoted field that is never closed, so a
    ``csv.reader`` fed the whole file swallows the header into one giant field.
    """
    path = EXPORTS / "preamble_unbalanced_quote.csv"
    text = path.read_text(encoding="utf-8")

    with path.open(encoding="utf-8", newline="") as handle:
        naive = list(csv.reader(handle))
    folded = [{cell.strip().casefold() for cell in record} for record in naive]
    assert len(naive) < len(text.splitlines())
    assert not any({"first name", "last name"} <= cells for cells in folded), (
        "fixture no longer defeats a naive reader; the test has stopped testing anything"
    )

    header, rows, issues = read_rows(path)

    assert header.lineno == 5
    assert [row.get("last_name") for row in rows] == ["Lovelace", "Hopper"]
    assert issues == []


@pytest.mark.parametrize(
    "prose",
    [
        '"Notes: your First Name and Last Name are included in this export."',
        "Notes: the columns are First Name, Last Name, URL and four more.",
        "Notes: we export, First Name, Last Name.",
    ],
)
def test_prose_naming_the_columns_is_not_mistaken_for_the_header(
    tmp_path: Path, prose: str
) -> None:
    path = write(tmp_path, f'"Notes:"\n{prose}\n\n' + HEADER + ADA)

    header, rows, _ = read_rows(path)

    assert header.lineno == 4
    assert rows[0].get("url") == "https://www.linkedin.com/in/ada-lovelace"


def test_preamble_splitting_into_first_name_last_name_cells_is_not_the_header(
    tmp_path: Path,
) -> None:
    """A preamble line whose comma-split cells happen to be the column names.

    ``_looks_like_header`` accepts any row carrying a ``First Name`` cell and a
    ``Last Name`` cell, so this prose is taken for the header and the real header
    two lines below is never reached -- the file fails as "missing required
    column(s)" despite being perfectly well formed.
    """
    prose = "Notes: the exported columns are, First Name, Last Name, and five others"
    path = write(tmp_path, f'"Notes:"\n{prose}\n\n' + HEADER + ADA)

    header, rows, _ = read_rows(path)

    assert header.lineno == 4
    assert rows[0].get("company") == "Jane Street"


def test_no_header_at_all_raises_ingest_error(tmp_path: Path) -> None:
    path = write(tmp_path, "Ada,Lovelace,Jane Street\nAlan,Turing,Citadel Securities\n")

    with pytest.raises(IngestError) as excinfo:
        read_rows(path)

    message = str(excinfo.value)
    assert "First Name" in message and "Last Name" in message


def test_no_header_error_does_not_echo_row_contents(tmp_path: Path) -> None:
    """Pointed at the wrong export file, the failure must not quote its rows.

    ``messages.csv`` sits next to ``Connections.csv`` in the same LinkedIn
    download and has no name columns, so it lands on this path. ``parse_row``
    states the rule for the rest of the stage -- "never echo the row itself: it
    is third-party personal data and stderr gets captured" -- and the header
    scanner is the one place that breaks it.
    """
    path = write(
        tmp_path,
        "sender,message\njane.doe@example.com,call me on 555-0100\n",
        name="messages.csv",
    )

    with pytest.raises(IngestError) as excinfo:
        read_rows(path)

    assert "jane.doe@example.com" not in str(excinfo.value)


def test_preamble_beyond_the_scan_cap_raises(tmp_path: Path) -> None:
    path = write(tmp_path, ("A note line\n" * 60) + HEADER + ADA)

    with pytest.raises(IngestError):
        read_rows(path)


# ---------------------------------------------------------------------------
# mapping columns by name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dropped", "named"),
    [
        ("First Name", "first name"),
        ("Last Name", "last name"),
        ("URL", "url"),
        ("Company", "company"),
        ("Position", "position"),
        ("Connected On", "connected on"),
    ],
)
def test_a_missing_required_column_is_named(tmp_path: Path, dropped: str, named: str) -> None:
    cells = [c for c in CANONICAL_HEADER if c != dropped]
    path = write(tmp_path, ",".join(cells) + "\n")

    with pytest.raises(IngestError) as excinfo:
        read_rows(path)

    assert named in str(excinfo.value).casefold()


def test_email_column_is_optional_until_keep_emails_is_on(tmp_path: Path) -> None:
    cells = [c for c in CANONICAL_HEADER if c != "Email Address"]
    path = write(
        tmp_path, ",".join(cells) + "\nAda,Lovelace,https://x.linkedin.com/in/a,C,P,01 Mar 2021\n"
    )

    header, rows, _ = read_rows(path)
    assert "email" not in header.columns
    assert len(rows) == 1

    with pytest.raises(IngestError) as excinfo:
        read_rows(path, require_email=True)
    assert "email address" in str(excinfo.value).casefold()
    assert "keep_emails" in str(excinfo.value)


def test_columns_are_bound_by_name_not_by_position(tmp_path: Path) -> None:
    reordered = "Connected On,Position,Company,Email Address,URL,Last Name,First Name\n"
    path = write(
        tmp_path,
        reordered
        + "01 Mar 2021,Trader,Jane Street,ada@example.com,"
        + "https://www.linkedin.com/in/ada-lovelace,Lovelace,Ada\n",
    )

    _, rows, _ = read_rows(path)

    assert dict(rows[0].values) == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "url": "https://www.linkedin.com/in/ada-lovelace",
        "email": "ada@example.com",
        "company": "Jane Street",
        "position": "Trader",
        "connected_on": "01 Mar 2021",
    }


def test_swapping_two_headers_moves_the_values_with_them(tmp_path: Path) -> None:
    """Reading Company out of the Position slot is plausible-looking garbage."""
    swapped = "First Name,Last Name,URL,Email Address,Position,Company,Connected On\n"
    path = write(
        tmp_path,
        swapped
        + "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,,Trader,Jane Street,01 Mar 2021\n",
    )

    _, rows, _ = read_rows(path)

    assert rows[0].get("company") == "Jane Street"
    assert rows[0].get("position") == "Trader"


@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("First Name", "first name"),
        ("  first NAME  ", "first name"),
        ("First  Name", "first name"),
        ("Connected\xa0On", "connected on"),  # NBSP from a spreadsheet re-save
        ("﻿First Name", "first name"),  # BOM that ended up mid-file
        ("First​Name", "firstname"),  # zero-width space
        # The full-width letters are the point: NFKC folds them back to ASCII.
        ("Ｆｉｒｓｔ Ｎａｍｅ", "first name"),  # noqa: RUF001
    ],
)
def test_normalize_header_cell(cell: str, expected: str) -> None:
    assert normalize_header_cell(cell) == expected


def test_header_with_bom_zero_width_odd_casing_and_padding() -> None:
    header, rows, issues = read_rows(EXPORTS / "header_bom_reordered_extra.csv")

    assert header.lineno == 3
    assert dict(rows[0].values) == {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "url": "https://www.linkedin.com/in/ada-lovelace",
        "email": "",
        "company": "Jane Street",
        "position": "Trader",
        "connected_on": "01 Mar 2021",
    }
    assert issues == []


def test_unknown_extra_columns_are_tolerated(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        ",".join([*CANONICAL_HEADER, "Connections", "Websites", ""])
        + "\n"
        + ADA.rstrip("\n")
        + ",500+,https://example.com,\n",
    )

    header, rows, issues = read_rows(path)

    assert header.cells[7:] == ("Connections", "Websites", "")
    assert rows[0].get("connected_on") == "01 Mar 2021"
    assert issues == []


def test_duplicate_header_column_keeps_the_first_and_records_it(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        ",".join([*CANONICAL_HEADER, "Company"]) + "\n" + ADA.rstrip("\n") + ",Citadel\n",
    )

    header, rows, _ = read_rows(path)

    assert header.duplicates == ("company",)
    assert rows[0].get("company") == "Jane Street"


def test_map_columns_accepts_older_and_hand_edited_spellings() -> None:
    header = map_columns(
        [
            "FirstName",
            "Surname",
            "Profile URL",
            "E-Mail",
            "Organisation",
            "Job Title",
            "ConnectedOn",
        ]
    )

    assert header.columns == {
        "first_name": 0,
        "last_name": 1,
        "url": 2,
        "email": 3,
        "company": 4,
        "position": 5,
        "connected_on": 6,
    }


# ---------------------------------------------------------------------------
# row mechanics
# ---------------------------------------------------------------------------


def test_crlf_embedded_separators_and_no_trailing_newline() -> None:
    path = EXPORTS / "crlf_messy_rows.csv"
    raw = path.read_bytes()
    assert raw.count(b"\r\n") > 5, "fixture is meant to be CRLF"
    assert not raw.endswith(b"\n"), "fixture is meant to lack a trailing newline"

    header, rows, issues = read_rows(path)

    assert header.lineno == 1
    assert len(rows) == 10
    assert rows[0].get("company") == "Jane Street Capital, LLC"  # embedded comma
    assert rows[0].get("position") == 'Trader, "Delta One"'  # embedded quotes
    assert rows[-1].get("company") == "Acme,\r\nWidgets"  # embedded newline
    assert rows[-1].get("connected_on") == "09 Mar 2021"  # the row did not shift
    assert [i for i in issues if i.fatal] == []


def test_row_linenos_are_physical_even_after_an_embedded_newline(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        preamble(3)
        + HEADER
        + 'Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,,"Acme,\nWidgets",Trader,01 Mar 2021\n'
        + "Alan,Turing,https://www.linkedin.com/in/alan-turing,,Citadel,Quant,02 Mar 2021\n",
    )

    _, rows, _ = read_rows(path)

    # Header on line 4; the first record spans lines 5-6, the second is line 7.
    assert [row.lineno for row in rows] == [6, 7]


def test_short_row_is_padded_and_flagged_without_being_dropped(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace\n")

    _, rows, issues = read_rows(path)

    assert len(rows) == 1
    assert rows[0].get("company") == ""
    assert [(i.reason, i.fatal) for i in issues] == [("short_row", False)]


def test_row_with_too_many_fields_is_dropped_and_counted(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + ADA.rstrip("\n") + ",surprise\n")

    _, rows, issues = read_rows(path)

    assert rows == []
    assert [(i.lineno, i.reason, i.fatal) for i in issues] == [(2, "too_many_fields", True)]


def test_trailing_empty_cells_are_padding_not_data(tmp_path: Path) -> None:
    path = write(tmp_path, HEADER + ADA.rstrip("\n") + ",,,\n")

    _, rows, issues = read_rows(path)

    assert len(rows) == 1
    assert rows[0].get("connected_on") == "01 Mar 2021"
    assert issues == []


def test_blank_rows_are_flagged_not_silently_swallowed(tmp_path: Path) -> None:
    path = write(
        tmp_path, HEADER + ADA + "\n" + " , , , , , , \n" + ADA.replace("ada-lovelace", "x")
    )

    _, rows, issues = read_rows(path)

    assert len(rows) == 2
    assert [i.reason for i in issues] == ["blank_row", "blank_row"]


def test_header_only_file_yields_no_rows_and_no_error() -> None:
    header, rows, issues = read_rows(EXPORTS / "header_only.csv")

    assert header.lineno == 4
    assert rows == []
    assert issues == []


def test_non_utf8_input_raises_ingest_error(tmp_path: Path) -> None:
    path = tmp_path / "Connections.csv"
    path.write_bytes(HEADER.encode() + "Zo\xeb,M\xfcller,x,,C,P,01 Mar 2021\n".encode("latin-1"))

    with pytest.raises(IngestError) as excinfo:
        read_rows(path)

    assert "UTF-8" in str(excinfo.value)


def test_utf8_bom_at_the_start_of_the_file_is_stripped(tmp_path: Path) -> None:
    path = tmp_path / "Connections.csv"
    path.write_bytes(b"\xef\xbb\xbf" + (HEADER + ADA).encode())

    header, rows, _ = read_rows(path)

    assert header.lineno == 1
    assert rows[0].get("first_name") == "Ada"


# ---------------------------------------------------------------------------
# the shipped fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "header_line", "n_rows"),
    [
        ("preamble_six_lines.csv", 7, 2),
        ("preamble_unbalanced_quote.csv", 5, 2),
        ("header_bom_reordered_extra.csv", 3, 1),
        ("crlf_messy_rows.csv", 1, 10),
        ("header_only.csv", 4, 0),
    ],
)
def test_export_fixtures_parse(name: str, header_line: int, n_rows: int) -> None:
    header, rows, _ = read_rows(EXPORTS / name)

    assert header.lineno == header_line
    assert len(rows) == n_rows


def test_conftest_export_fixture_parses(connections_csv: Path) -> None:
    header, rows, issues = read_rows(connections_csv)

    assert header.lineno == 4  # two note lines, one blank, then the header
    assert len(rows) == 5
    assert rows[0].get("company") == "Jane Street"
    assert issues == []


def test_conftest_preamble_free_export_parses(connections_csv_no_preamble: Path) -> None:
    """The same first row, from an older export shape with no preamble at all.

    Requested in a test of its own on purpose: both conftest fixtures write to
    ``tmp_path / "Connections.csv"``, so asking for both at once silently leaves
    only the second one on disk.
    """
    header, rows, issues = read_rows(connections_csv_no_preamble)

    assert header.lineno == 1
    assert len(rows) == 1
    assert rows[0].get("url") == "https://www.linkedin.com/in/ada-lovelace"
    assert issues == []
