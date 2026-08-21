"""Row parsing, privacy and determinism for stage 1.

The properties under test here are the ones every later stage inherits:

* a date is either right or absent -- never guessed, and never dependent on the
  clock the pipeline happened to run on;
* an email address that was not explicitly retained appears in no artifact, in
  no form;
* a dropped row is *counted*, so an operator can see what the tool refused;
* the same export produces byte-identical artifacts, including across
  ``PYTHONHASHSEED`` values.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from freezegun import freeze_time

from interlayer import ingest
from interlayer.config import Settings
from interlayer.errors import ConfigError, IngestError
from interlayer.ingest.records import parse_connected_on
from interlayer.io import hash_email

EXPORTS = Path(__file__).resolve().parents[1] / "fixtures" / "exports"

HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"

HMAC_KEY = "test-key-not-a-secret"

# Two dates far enough apart that any clock-derived component differs: a
# different day, month, year, century and weekday.
EARLY = "1999-01-04"
LATE = "2087-11-30"


def export(tmp_path: Path, body: str, name: str = "Connections.csv") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def make_settings(tmp_path: Path, csv_path: Path, **kw: object) -> Settings:
    return Settings(input_csv=csv_path, artifact_dir=tmp_path / "artifacts", **kw)


def row(
    first: str = "Ada",
    last: str = "Lovelace",
    url: str = "https://www.linkedin.com/in/ada-lovelace",
    email: str = "",
    company: str = "Jane Street",
    position: str = "Trader",
    connected: str = "01 Mar 2021",
) -> str:
    return f"{first},{last},{url},{email},{company},{position},{connected}\n"


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01 Mar 2021", date(2021, 3, 1)),
        (" 01 Mar 2021 ", date(2021, 3, 1)),
        ("1 March 2021", date(2021, 3, 1)),
        ("31 Dec 2003", date(2003, 12, 31)),
        ("29 Feb 2024", date(2024, 2, 29)),
    ],
)
def test_the_export_format_parses(raw: str, expected: date) -> None:
    assert parse_connected_on(raw) == expected


def test_iso_dates_are_not_read_day_first() -> None:
    """Regression: ``dayfirst=True`` turns 2021-03-01 into the 3rd of January.

    A leading four-digit year means the string is ISO-ordered, and reading it
    day-first is a silent two-month error on every row.
    """
    assert parse_connected_on("2021-03-01") == date(2021, 3, 1)
    assert parse_connected_on("2021/03/01") == date(2021, 3, 1)
    assert parse_connected_on("2021-12-31") == date(2021, 12, 31)


def test_compact_iso_dates_are_not_read_day_first() -> None:
    """The year-first guard only fires when a separator follows the year.

    ``20210301`` leads with a four-digit year just as plainly as ``2021-03-01``
    does, but the guard's regex requires ``[-/.]`` after it, so the string is
    parsed day-first and silently becomes the 3rd of January. Silently wrong is
    worse than the ``None`` this module returns for everything else it cannot
    read.
    """
    assert parse_connected_on("20210301") == date(2021, 3, 1)


def test_ambiguous_all_numeric_dates_follow_the_export_convention() -> None:
    """No leading year, so day-first -- which is what the export writes."""
    assert parse_connected_on("01/03/2021") == date(2021, 3, 1)
    assert parse_connected_on("13/03/2021") == date(2021, 3, 13)


def test_partial_date_is_none_and_does_not_depend_on_the_clock() -> None:
    """Regression: missing components were filled in from the system clock.

    ``Mar 2021`` names no day. A parser that defaults the missing day to today's
    produces a different artifact every day it runs, which breaks the
    byte-identical-output guarantee for reasons no operator could ever diagnose.
    """
    with freeze_time(EARLY):
        early = parse_connected_on("Mar 2021")
    with freeze_time(LATE):
        late = parse_connected_on("Mar 2021")

    assert early == late
    assert early is None


@pytest.mark.parametrize("raw", ["Mar 2021", "2021", "March", "01 Mar 2021", "2021-03-01", ""])
def test_parsing_is_clock_independent_for_every_shape(raw: str) -> None:
    with freeze_time(EARLY):
        early = parse_connected_on(raw)
    with freeze_time(LATE):
        late = parse_connected_on(raw)

    assert early == late


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not a date", "sometime in 2021", "31 Feb 2021", "Mar 2021", "2021", "n/a"],
)
def test_unreadable_dates_become_none(raw: str) -> None:
    assert parse_connected_on(raw) is None


@pytest.mark.parametrize("raw", ["01 Mar 1021", "01 Mar 3021"])
def test_implausible_years_are_dropped(raw: str) -> None:
    assert parse_connected_on(raw) is None


def test_an_unreadable_date_costs_the_date_not_the_row(tmp_path: Path) -> None:
    path = export(tmp_path, row(connected="sometime in 2021"))

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert len(result.people) == 1
    assert result.connections[0].connected_on is None
    assert result.counts["noted_unparseable_date"] == 1
    assert result.counts["skipped_total"] == 0


# ---------------------------------------------------------------------------
# emails
# ---------------------------------------------------------------------------


def artifact_bytes(artifact_dir: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(artifact_dir.rglob("*")) if p.is_file())


def test_emails_reach_no_artifact_by_default(tmp_path: Path) -> None:
    path = export(
        tmp_path,
        row(email="ada@example.com")
        + row("Alan", "Turing", "https://www.linkedin.com/in/alan-turing", "ALAN@Example.COM"),
    )
    cfg = make_settings(tmp_path, path)

    ingest.run(cfg)

    written = artifact_bytes(cfg.artifact_dir).lower()
    assert written  # the artifacts are not empty, so the scan means something
    for needle in (b"ada@example.com", b"alan@example.com", b"@example.com", b"example.com"):
        assert needle not in written
    assert json.loads(cfg.artifact(ingest.STATS_FILENAME).read_text())["noted_email_dropped"] == 2


def test_the_conftest_fixture_email_reaches_no_artifact(
    tmp_path: Path, connections_csv: Path
) -> None:
    assert b"gh@example.com" in connections_csv.read_bytes()
    cfg = make_settings(tmp_path, connections_csv)

    ingest.run(cfg)

    assert b"gh@example.com" not in artifact_bytes(cfg.artifact_dir)


def test_keep_emails_without_a_key_fails_at_config_time(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings(artifact_dir=tmp_path, keep_emails=True)

    assert "email_hmac_key" in str(excinfo.value)


def test_keep_emails_stores_an_hmac_not_the_address(tmp_path: Path) -> None:
    path = export(tmp_path, row(email="ada@example.com"))
    cfg = make_settings(tmp_path, path, keep_emails=True, email_hmac_key=HMAC_KEY)

    result = ingest.ingest_csv(cfg)
    ingest.run(cfg)

    stored = result.people[0].email
    assert stored == hash_email("ada@example.com", HMAC_KEY)
    assert stored is not None
    assert "@" not in stored
    assert b"ada@example.com" not in artifact_bytes(cfg.artifact_dir)
    assert stored.encode() in cfg.people_path.read_bytes()


def test_keep_emails_needs_the_email_column(tmp_path: Path) -> None:
    path = tmp_path / "Connections.csv"
    path.write_text(
        "First Name,Last Name,URL,Company,Position,Connected On\n"
        "Ada,Lovelace,https://www.linkedin.com/in/ada-lovelace,Jane Street,Trader,01 Mar 2021\n",
        encoding="utf-8",
    )
    cfg = make_settings(tmp_path, path, keep_emails=True, email_hmac_key=HMAC_KEY)

    with pytest.raises(IngestError):
        ingest.ingest_csv(cfg)


@pytest.mark.parametrize(
    ("left", "right"),
    [("A@B.com", "a@b.com "), ("  ADA@Example.COM", "ada@example.com"), ("X@Y.io", "x@y.IO")],
)
def test_hashing_normalises_by_strip_and_casefold(left: str, right: str) -> None:
    assert hash_email(left, HMAC_KEY) == hash_email(right, HMAC_KEY)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a.b+tag@gmail.com", "ab@gmail.com"),
        ("a.b@gmail.com", "ab@gmail.com"),
        ("ab+tag@gmail.com", "ab@gmail.com"),
    ],
)
def test_hashing_never_strips_gmail_dots_or_tags(left: str, right: str) -> None:
    """Collapsing these would link identities their owner chose to keep apart."""
    assert hash_email(left, HMAC_KEY) != hash_email(right, HMAC_KEY)


def test_the_hmac_is_keyed(tmp_path: Path) -> None:
    assert hash_email("ada@example.com", "k1") != hash_email("ada@example.com", "k2")
    assert hash_email("ada@example.com", HMAC_KEY) != hashlib.sha256(b"ada@example.com").hexdigest()


# ---------------------------------------------------------------------------
# rows: what is kept, what is dropped, and whether anyone is told
# ---------------------------------------------------------------------------


def test_duplicate_slug_keeps_the_first_row_and_counts_the_skip(tmp_path: Path) -> None:
    path = export(
        tmp_path,
        row(company="Jane Street", connected="01 Mar 2021")
        + row(
            url="https://uk.linkedin.com/in/ADA-Lovelace/?originalSubdomain=uk",
            company="Citadel Securities",
            connected="05 Mar 2021",
        ),
    )

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert len(result.people) == 1
    assert result.people[0].current_company == "Jane Street"
    assert result.connections[0].connected_on == date(2021, 3, 1)
    assert result.counts["skipped_duplicate_slug"] == 1
    assert result.counts["skipped_total"] == 1
    assert [(i.lineno, i.reason) for i in result.skipped] == [(3, "duplicate_slug")]
    assert "line 2" in result.skipped[0].detail


def test_skips_are_reported_on_stderr_not_swallowed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = export(tmp_path, row() + row(first="Nourl", last="Person", url=""))

    ingest.run(make_settings(tmp_path, path))

    err = capsys.readouterr().err
    assert "missing_url" in err
    assert "skipped 1 row(s)" in err


def test_a_row_without_a_url_is_skipped_and_counted(tmp_path: Path) -> None:
    path = export(tmp_path, row() + row(first="Nourl", last="Person", url=""))

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert [p.first_name for p in result.people] == ["Ada"]
    assert result.counts["skipped_missing_url"] == 1


def test_a_non_profile_url_is_counted_under_its_own_reason(tmp_path: Path) -> None:
    path = export(
        tmp_path,
        row()
        + row(first="Company", last="Page", url="https://www.linkedin.com/company/jane-street")
        + row(first="Junk", last="Url", url="not on linkedin"),
    )

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert len(result.people) == 1
    assert result.counts["skipped_unrecognised_url"] == 2
    assert "skipped_missing_url" not in result.counts


def test_member_urns_differing_in_case_stay_two_people(tmp_path: Path) -> None:
    path = export(
        tmp_path,
        row("Upper", "Case", "https://www.linkedin.com/in/ACoAAB123")
        + row("Lower", "Case", "https://www.linkedin.com/in/acoaab123"),
    )

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert len(result.people) == 2
    assert len({p.person_id for p in result.people}) == 2
    assert {p.linkedin_slug for p in result.people} == {"ACoAAB123", "acoaab123"}
    assert result.counts["skipped_total"] == 0


def test_case_only_vanity_url_difference_is_one_person(tmp_path: Path) -> None:
    path = export(
        tmp_path,
        row(url="https://www.linkedin.com/in/Ada-Lovelace")
        + row(url="https://www.linkedin.com/in/ada-lovelace"),
    )

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert len(result.people) == 1
    assert result.counts["skipped_duplicate_slug"] == 1


def test_non_ascii_names_survive_byte_for_byte(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, EXPORTS / "crlf_messy_rows.csv")

    ingest.run(cfg)

    written = cfg.people_path.read_bytes()
    assert b"Zo\xc3\xab M\xc3\xbcller" in written  # composed U+00EB / U+00FC
    assert "Café Größe GmbH".encode() in written
    assert "李 雷".encode() in written
    assert "工程师".encode() in written
    assert "אבי כהן".encode() in written


def test_decomposed_names_are_not_silently_recomposed(tmp_path: Path) -> None:
    """Ingest must not normalise a name into a different byte sequence.

    Whatever the export said is what the operator will search for in the report.
    """
    decomposed, composed = "Zoë Müller", "Zoë Müller"
    assert decomposed != composed, "the source file has been unicode-normalised"

    path = export(tmp_path, row(first="Zoë", last="Müller"))
    cfg = make_settings(tmp_path, path)

    ingest.run(cfg)

    written = cfg.people_path.read_bytes()
    assert decomposed.encode() in written
    assert composed.encode() not in written


def test_embedded_separators_survive_into_the_record(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, EXPORTS / "crlf_messy_rows.csv")

    result = ingest.ingest_csv(cfg)
    by_slug = {p.linkedin_slug: p for p in result.people}

    assert by_slug["ada-lovelace"].current_company == "Jane Street Capital, LLC"
    assert by_slug["ada-lovelace"].positions[0].title_raw == 'Trader, "Delta One"'
    assert by_slug["multi-line"].current_company == "Acme,\r\nWidgets"


def test_empty_company_and_position_cells_are_not_invented(tmp_path: Path) -> None:
    path = export(tmp_path, row(company="", position=""))

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert result.people[0].positions == ()
    assert result.people[0].current_company is None
    assert result.counts["skipped_total"] == 0
    assert "noted_position_without_company" not in result.counts


def test_a_title_with_no_employer_is_noted(tmp_path: Path) -> None:
    path = export(tmp_path, row(company="", position="Trader"))

    result = ingest.ingest_csv(make_settings(tmp_path, path))

    assert result.people[0].positions == ()
    assert result.counts["noted_position_without_company"] == 1


def test_the_messy_export_accounts_for_every_row(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, EXPORTS / "crlf_messy_rows.csv")

    result = ingest.ingest_csv(cfg)

    assert result.counts["rows_total"] == 10
    assert result.counts["people"] == 7
    assert result.counts["connections"] == 7
    assert result.counts["skipped_total"] == 3
    assert result.counts["rows_total"] == result.counts["people"] + result.counts["skipped_total"]
    assert {
        "skipped_duplicate_slug": 1,
        "skipped_missing_url": 1,
        "skipped_unrecognised_url": 1,
    }.items() <= result.counts.items()


# ---------------------------------------------------------------------------
# determinism and the artifacts themselves
# ---------------------------------------------------------------------------


MANY_ROWS = "".join(
    row(
        first=f"First{i}",
        last=f"Last{i}",
        url=f"https://www.linkedin.com/in/person-{i}",
        company=f"Company {i % 3}",
        position=f"Title {i % 4}",
        connected=f"{i:02d} Mar 2021",
    )
    for i in range(1, 13)
)


def test_output_is_sorted_by_person_id(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, export(tmp_path, MANY_ROWS))

    ingest.run(cfg)

    ids = [json.loads(line)["person_id"] for line in cfg.people_path.read_text().splitlines()]
    assert ids == sorted(ids)
    assert len(ids) == 12
    connection_ids = [
        json.loads(line)["person_id"]
        for line in cfg.artifact(ingest.CONNECTIONS_FILENAME).read_text().splitlines()
    ]
    assert connection_ids == ids


def test_shuffling_the_input_rows_does_not_change_the_bytes(tmp_path: Path) -> None:
    lines = MANY_ROWS.splitlines(keepends=True)
    forward = make_settings(tmp_path / "f", export(tmp_path / "f", "".join(lines)))
    backward = make_settings(tmp_path / "b", export(tmp_path / "b", "".join(reversed(lines))))

    ingest.run(forward)
    ingest.run(backward)

    assert forward.people_path.read_bytes() == backward.people_path.read_bytes()


def test_two_runs_of_the_same_export_are_byte_identical(tmp_path: Path) -> None:
    path = export(tmp_path, MANY_ROWS)
    first = make_settings(tmp_path / "one", path)
    second = make_settings(tmp_path / "two", path)

    ingest.run(first)
    ingest.run(second)

    for name in ("people.jsonl", ingest.CONNECTIONS_FILENAME, ingest.STATS_FILENAME):
        assert first.artifact(name).read_bytes() == second.artifact(name).read_bytes()


def _digest(artifact_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(artifact_dir.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


RUN_STAGE = (
    "import sys;from pathlib import Path;"
    "from interlayer.config import Settings;from interlayer import ingest;"
    "ingest.run(Settings(input_csv=Path(sys.argv[1]), artifact_dir=Path(sys.argv[2])))"
)


def test_artifacts_are_identical_across_pythonhashseed_values(tmp_path: Path) -> None:
    """Set iteration order varies with the seed; the artifacts must not.

    Run in subprocesses because ``PYTHONHASHSEED`` is fixed at interpreter
    start -- setting it inside this process would prove nothing.
    """
    path = export(tmp_path, MANY_ROWS)
    digests = set()
    for seed in ("0", "1", "12345", "999"):
        out = tmp_path / f"artifacts-{seed}"
        completed = subprocess.run(
            [sys.executable, "-c", RUN_STAGE, str(path), str(out)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        digests.add(_digest(out))

    assert len(digests) == 1

    in_process = make_settings(tmp_path, path)
    ingest.run(in_process)
    assert _digest(in_process.artifact_dir) in digests


def test_run_writes_every_artifact_at_0600(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, export(tmp_path, MANY_ROWS))

    ingest.run(cfg)

    assert stat.S_IMODE(cfg.artifact_dir.stat().st_mode) == 0o700
    written = sorted(p.name for p in cfg.artifact_dir.iterdir())
    assert written == ["connections.jsonl", "ingest_stats.json", "people.jsonl"]
    for name in written:
        assert stat.S_IMODE(cfg.artifact(name).stat().st_mode) == 0o600, name


def test_an_export_with_no_rows_produces_valid_empty_artifacts(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, EXPORTS / "header_only.csv")

    ingest.run(cfg)

    assert cfg.people_path.read_bytes() == b""
    assert cfg.artifact(ingest.CONNECTIONS_FILENAME).read_bytes() == b""
    counts = json.loads(cfg.artifact(ingest.STATS_FILENAME).read_text())
    assert counts == {"connections": 0, "people": 0, "rows_total": 0, "skipped_total": 0}


def test_a_missing_input_file_is_an_ingest_error(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, tmp_path / "nope.csv")

    with pytest.raises(IngestError) as excinfo:
        ingest.ingest_csv(cfg)

    assert "nope.csv" in str(excinfo.value)


def test_no_input_at_all_is_a_config_error(tmp_path: Path) -> None:
    cfg = Settings(artifact_dir=tmp_path / "artifacts")

    with pytest.raises(ConfigError):
        ingest.ingest_csv(cfg)


def test_the_conftest_fixture_ingests_end_to_end(tmp_path: Path, connections_csv: Path) -> None:
    cfg = make_settings(tmp_path, connections_csv)

    ingest.run(cfg)

    people = [json.loads(line) for line in cfg.people_path.read_text().splitlines()]
    assert len(people) == 5
    assert {p["linkedin_slug"] for p in people} == {
        "ada-lovelace",
        "alan-turing",
        "grace-hopper",
        "edsger",
        "barbara",
    }
    assert all(p["email"] is None for p in people)
    assert all(p["source"] == "connections_csv" for p in people)
    connections = [
        json.loads(line)
        for line in cfg.artifact(ingest.CONNECTIONS_FILENAME).read_text().splitlines()
    ]
    assert {c["connected_on"] for c in connections} == {
        "2021-03-01",
        "2021-03-02",
        "2021-03-03",
        "2021-03-04",
        "2021-03-05",
    }
    assert all(c["degree"] == 1 for c in connections)
