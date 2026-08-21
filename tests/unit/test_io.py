"""Tests for :mod:`interlayer.io`.

Two invariants this module exists to hold, and both are easy to hold *almost*:

* 0700 directories and 0600 files **at every instant**, not eventually.
  ``mkdir(exist_ok=True)`` leaves a pre-existing wrong mode in place and a plain
  ``write_text`` obeys the umask, so the tests here set the wrong mode first and
  then check it was corrected, rather than only testing the fresh-create path.
* Nothing half-written. A stage that dies mid-write must leave the previous
  artifact intact and no temp file behind, because the next stage cannot tell a
  truncated JSONL file from a short one.
"""

from __future__ import annotations

import hashlib
import os
import random
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from interlayer import io as ilio
from interlayer.errors import IngestError, InterlayerError, StageInputMissingError
from interlayer.models import ApproxDate, Person, Position

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextmanager
def permissive_umask() -> Iterator[None]:
    """Run a block with ``umask(0)``: the mode must come from the code, not luck."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def people() -> list[Person]:
    return [
        Person.make(
            "Ada",
            "Lovelace",
            linkedin_url="https://www.linkedin.com/in/ada-lovelace",
            positions=(Position(company_raw="Jane Street", start=ApproxDate(year=2019)),),
        ),
        Person.make("Alan", "Turing", linkedin_url="https://www.linkedin.com/in/alan-turing"),
    ]


# ---------------------------------------------------------------------------
# secure_dir
# ---------------------------------------------------------------------------


def test_secure_dir_creates_a_private_directory(tmp_path: Path) -> None:
    created = ilio.secure_dir(tmp_path / "arts")
    assert created.is_dir()
    assert mode(created) == 0o700
    assert created == tmp_path / "arts"


def test_secure_dir_repairs_a_preexisting_world_readable_directory(tmp_path: Path) -> None:
    """The case ``mkdir(exist_ok=True)`` silently gets wrong.

    An artifact directory left at 0755 by an earlier run, a different tool, or a
    permissive umask stays 0755 forever unless the mode is set unconditionally.
    """
    existing = tmp_path / "arts"
    existing.mkdir(mode=0o755)
    assert mode(existing) == 0o755
    ilio.secure_dir(existing)
    assert mode(existing) == 0o700


@pytest.mark.parametrize("start_mode", [0o777, 0o755, 0o750, 0o500, 0o000])
def test_secure_dir_repairs_any_preexisting_mode(tmp_path: Path, start_mode: int) -> None:
    existing = tmp_path / "arts"
    existing.mkdir(mode=0o700)
    existing.chmod(start_mode)
    ilio.secure_dir(existing)
    assert mode(existing) == 0o700


def test_secure_dir_ignores_a_permissive_umask(tmp_path: Path) -> None:
    with permissive_umask():
        created = ilio.secure_dir(tmp_path / "arts")
    assert mode(created) == 0o700


def test_secure_dir_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "arts"
    assert ilio.secure_dir(target) == ilio.secure_dir(target)
    assert mode(target) == 0o700


def test_secure_dir_preserves_existing_contents(tmp_path: Path) -> None:
    existing = tmp_path / "arts"
    existing.mkdir(mode=0o755)
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    ilio.secure_dir(existing)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_secure_dir_rejects_a_path_that_is_a_file(tmp_path: Path) -> None:
    clash = tmp_path / "arts"
    clash.write_text("i am not a directory", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ilio.secure_dir(clash)


def test_secure_dir_makes_the_directories_it_creates_private(tmp_path: Path) -> None:
    """Intermediate directories are created by ``parents=True`` and never chmod'd.

    ``Path.mkdir(mode=…, parents=True)`` applies the mode to the leaf only; the
    parents it creates take the umask. With ``umask(0)`` they land 0777 --
    world-writable directories holding third-party personal data -- and with a
    normal 0022 umask they land 0755, world-readable. ``secure_write`` inherits
    the same hole via ``secure_dir(path.parent)``, so any nested ``artifact_dir``
    (``out/run-1/``) publishes its parent.
    """
    with permissive_umask():
        leaf = ilio.secure_dir(tmp_path / "out" / "run-1")
    assert mode(leaf) == 0o700
    assert mode(leaf.parent) == 0o700, f"parent left at {mode(leaf.parent):o}"


# ---------------------------------------------------------------------------
# secure_write
# ---------------------------------------------------------------------------


def test_secure_write_creates_a_private_file(tmp_path: Path) -> None:
    target = tmp_path / "arts" / "out.txt"
    ilio.secure_write(target, "payload")
    assert target.read_text(encoding="utf-8") == "payload"
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700


def test_secure_write_ignores_a_permissive_umask(tmp_path: Path) -> None:
    """With ``umask(0)`` a plain ``write_text`` would land 0666."""
    target = tmp_path / "arts" / "out.txt"
    with permissive_umask():
        ilio.secure_write(target, "payload")
    assert mode(target) == 0o600


def test_secure_write_tightens_a_preexisting_world_readable_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)
    ilio.secure_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert mode(target) == 0o600


def test_secure_write_replaces_rather_than_appends(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    ilio.secure_write(target, "first and longer")
    ilio.secure_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_secure_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    ilio.secure_write(tmp_path / "out.txt", "payload")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.txt"]


def test_secure_write_round_trips_unicode(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    payload = "Ada Lovelace — café 中文\n"
    ilio.secure_write(target, payload)
    assert target.read_text(encoding="utf-8") == payload


def test_a_failed_rename_leaves_the_previous_artifact_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next stage cannot tell a truncated artifact from a short one."""
    target = tmp_path / "out.jsonl"
    ilio.secure_write(target, "good\n")

    def boom(self: Path, other: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="no space"):
        ilio.secure_write(target, "replacement\n")
    assert target.read_text(encoding="utf-8") == "good\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.jsonl"]


def test_a_failure_mid_write_leaves_neither_partial_file_nor_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "arts" / "out.jsonl"
    real_fdopen = os.fdopen

    class HalfWriter:
        def __init__(self, fh: object) -> None:
            self._fh = fh

        def __enter__(self) -> HalfWriter:
            return self

        def __exit__(self, *exc: object) -> bool:
            self._fh.close()  # type: ignore[attr-defined]
            return False

        def write(self, data: str) -> int:
            self._fh.write(data[: len(data) // 2])  # type: ignore[attr-defined]
            raise OSError("disk went away")

    monkeypatch.setattr(os, "fdopen", lambda *a, **k: HalfWriter(real_fdopen(*a, **k)))
    with pytest.raises(OSError, match="disk went away"):
        ilio.secure_write(target, "x" * 100)
    monkeypatch.undo()
    assert not target.exists()
    assert list(target.parent.iterdir()) == []


# ---------------------------------------------------------------------------
# write_jsonl / read_jsonl
# ---------------------------------------------------------------------------


def test_jsonl_round_trip_preserves_records_and_order(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    written = people()
    assert ilio.write_jsonl(path, written) == 2
    assert ilio.read_jsonl(path, Person) == written


def test_write_jsonl_writes_one_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    ilio.write_jsonl(path, people())
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert len(text.splitlines()) == 2
    assert "\n\n" not in text


def test_write_jsonl_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "arts" / "people.jsonl"
    with permissive_umask():
        ilio.write_jsonl(path, people())
    assert mode(path) == 0o600


def test_write_jsonl_accepts_an_empty_iterable(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    assert ilio.write_jsonl(path, []) == 0
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == ""
    assert ilio.read_jsonl(path, Person) == []


def test_write_jsonl_accepts_a_generator(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    assert ilio.write_jsonl(path, (p for p in people())) == 2


def test_a_generator_that_raises_writes_no_file_at_all(tmp_path: Path) -> None:
    """Serialisation happens before the write, so a bad record aborts cleanly."""
    path = tmp_path / "people.jsonl"

    def records() -> Iterator[Person]:
        yield people()[0]
        raise RuntimeError("record 2 exploded")

    with pytest.raises(RuntimeError, match="exploded"):
        ilio.write_jsonl(path, records())
    assert not path.exists()


def test_read_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    first, second = people()
    path.write_text(
        f"\n{first.model_dump_json()}\n\n   \n{second.model_dump_json()}\n\n", encoding="utf-8"
    )
    assert ilio.read_jsonl(path, Person) == [first, second]


def test_malformed_line_names_its_line_number(tmp_path: Path) -> None:
    """Blank lines count, so the number points at the line an editor shows."""
    path = tmp_path / "people.jsonl"
    path.write_text(f"{people()[0].model_dump_json()}\n\n{{not json}}\n", encoding="utf-8")
    with pytest.raises(IngestError) as exc:
        ilio.read_jsonl(path, Person)
    assert ":3:" in str(exc.value)
    assert str(path) in str(exc.value)


def test_schema_violation_names_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    first, second = people()
    path.write_text(
        f"{first.model_dump_json()}\n{second.model_dump_json()}\n"
        '{"person_id": "p_1", "unexpected": true}\n',
        encoding="utf-8",
    )
    with pytest.raises(IngestError) as exc:
        ilio.read_jsonl(path, Person)
    assert ":3:" in str(exc.value)


def test_the_first_bad_line_is_the_one_reported(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    path.write_text("nope\nalso nope\n", encoding="utf-8")
    with pytest.raises(IngestError, match=":1:"):
        ilio.read_jsonl(path, Person)


def test_ingest_error_chains_the_underlying_failure(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(IngestError) as exc:
        ilio.read_jsonl(path, Person)
    assert exc.value.__cause__ is not None


def test_missing_input_names_the_stage_that_produces_it(tmp_path: Path) -> None:
    path = tmp_path / "people.jsonl"
    with pytest.raises(StageInputMissingError) as exc:
        ilio.read_jsonl(path, Person, produced_by="ingest")
    assert exc.value.produced_by == "ingest"
    assert exc.value.path == str(path)
    assert "interlayer ingest" in str(exc.value)
    assert isinstance(exc.value, InterlayerError)


def test_missing_input_has_a_default_producer(tmp_path: Path) -> None:
    with pytest.raises(StageInputMissingError) as exc:
        ilio.read_jsonl(tmp_path / "x.jsonl", Person)
    assert exc.value.produced_by == "run"


def test_a_directory_is_not_a_readable_artifact(tmp_path: Path) -> None:
    with pytest.raises(StageInputMissingError):
        ilio.read_jsonl(tmp_path, Person)


def test_read_text_round_trips_and_reports_a_missing_stage(tmp_path: Path) -> None:
    path = tmp_path / "report.html"
    ilio.secure_write(path, "<html></html>")
    assert ilio.read_text(path) == "<html></html>"
    with pytest.raises(StageInputMissingError) as exc:
        ilio.read_text(tmp_path / "nope.html", produced_by="report")
    assert exc.value.produced_by == "report"


def test_jsonl_is_byte_identical_across_repeated_writes(tmp_path: Path) -> None:
    """Same records, same bytes: artifacts must diff cleanly between runs."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    ilio.write_jsonl(a, people())
    ilio.write_jsonl(b, people())
    assert a.read_bytes() == b.read_bytes()


def test_dump_json_is_sorted_indented_and_private(tmp_path: Path) -> None:
    path = tmp_path / "arts" / "manifest.json"
    with permissive_umask():
        ilio.dump_json(path, {"b": 1, "a": {"d": 2, "c": 3}})
    text = path.read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"b"')
    assert text.index('"c"') < text.index('"d"')
    assert text.endswith("\n")
    assert mode(path) == 0o600


# ---------------------------------------------------------------------------
# hash_email
# ---------------------------------------------------------------------------


def test_hash_email_is_keyed() -> None:
    """An unkeyed digest of an email address is reversible by brute force."""
    assert ilio.hash_email("ada@example.com", "key-1") != ilio.hash_email(
        "ada@example.com", "key-2"
    )


def test_hash_email_is_deterministic_and_hex() -> None:
    digest = ilio.hash_email("ada@example.com", "k")
    assert digest == ilio.hash_email("ada@example.com", "k")
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "variant",
    ["ada@example.com", "  ada@example.com  ", "Ada@Example.COM", "\tADA@EXAMPLE.COM\n"],
)
def test_hash_email_normalises_case_and_surrounding_whitespace(variant: str) -> None:
    assert ilio.hash_email(variant, "k") == ilio.hash_email("ada@example.com", "k")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a.b+tag@gmail.com", "ab@gmail.com"),
        ("a.b@gmail.com", "ab@gmail.com"),
        ("ada+work@gmail.com", "ada@gmail.com"),
        ("ada@gmail.com", "ada@googlemail.com"),
        ("ada@example.com", "ada@example.org"),
        ("ad a@example.com", "ada@example.com"),
    ],
)
def test_hash_email_does_not_link_distinct_addresses(left: str, right: str) -> None:
    """Stripping Gmail dots or ``+tags`` is deliberate cross-identity linkage.

    It would merge accounts the operator was never told were the same person, so
    normalisation stops at ``strip()`` and ``casefold()``.
    """
    assert ilio.hash_email(left, "k") != ilio.hash_email(right, "k")


def test_hash_email_is_stable_across_processes() -> None:
    """The digest ends up in artifacts, so it must not depend on process state."""
    script = "from interlayer.io import hash_email;print(hash_email(' Ada@Example.COM ', 'key-1'))"
    env = {**os.environ, "PYTHONHASHSEED": "random"}
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()
    assert out == ilio.hash_email("ada@example.com", "key-1")


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------


def test_sha256_of_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert ilio.sha256_file(path) == EMPTY_SHA256


def test_sha256_spans_the_chunk_boundary(tmp_path: Path) -> None:
    """The reader chunks at 64 KiB; a file must hash the same as one shot."""
    payload = bytes(range(256)) * 8192  # 2 MiB, 32 chunks
    path = tmp_path / "big.bin"
    path.write_bytes(payload)
    assert ilio.sha256_file(path) == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("size", [1, 65535, 65536, 65537, 131072])
def test_sha256_matches_hashlib_at_every_size(tmp_path: Path, size: int) -> None:
    payload = bytes((i * 7) % 251 for i in range(size))
    path = tmp_path / "x.bin"
    path.write_bytes(payload)
    assert ilio.sha256_file(path) == hashlib.sha256(payload).hexdigest()


def test_sha256_detects_a_one_byte_change(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(b"First Name,Last Name\n")
    before = ilio.sha256_file(path)
    path.write_bytes(b"First Name,Last NameX\n")
    assert ilio.sha256_file(path) != before


# ---------------------------------------------------------------------------
# seeded
# ---------------------------------------------------------------------------


def test_seeded_makes_a_block_reproducible() -> None:
    with ilio.seeded(42):
        first = [random.random() for _ in range(5)]
    with ilio.seeded(42):
        second = [random.random() for _ in range(5)]
    assert first == second


def test_different_seeds_give_different_streams() -> None:
    with ilio.seeded(1):
        first = [random.random() for _ in range(5)]
    with ilio.seeded(2):
        second = [random.random() for _ in range(5)]
    assert first != second


def test_seeded_restores_the_previous_global_rng() -> None:
    """A stage that leaves the RNG re-seeded makes the next stage's output
    depend on whether this one ran."""
    random.seed(1234)
    expected = [random.random() for _ in range(3)]
    random.seed(1234)
    with ilio.seeded(42):
        random.random()
    assert [random.random() for _ in range(3)] == expected


def test_seeded_restores_the_previous_global_rng_when_the_block_raises() -> None:
    random.seed(1234)
    before = random.getstate()
    with pytest.raises(ValueError, match="boom"), ilio.seeded(42):
        random.random()
        raise ValueError("boom")
    assert random.getstate() == before


def test_seeded_blocks_nest() -> None:
    random.seed(1234)
    before = random.getstate()
    with ilio.seeded(1):
        outer = random.random()
        with ilio.seeded(2):
            random.random()
        assert random.random() != outer
    assert random.getstate() == before


def test_mode_constants_are_the_documented_ones() -> None:
    assert ilio.DIR_MODE == 0o700
    assert ilio.FILE_MODE == 0o600
