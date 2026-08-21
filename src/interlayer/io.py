"""Artifact persistence: JSONL in, JSONL out, with permissions enforced.

Every pipeline stage reads and writes through this module. Two invariants it
exists to hold:

* **0700 directories, 0600 files, at every instant.** These artifacts hold
  identifiable data about third parties. ``mkdir(exist_ok=True)`` and
  ``touch(mode=...)`` silently leave a pre-existing wrong mode in place, and a
  plain ``write_text`` is world-writable under a permissive umask, so modes are
  set explicitly and asserted rather than assumed.
* **Deterministic ordering.** Set iteration order varies with ``PYTHONHASHSEED``,
  so anything derived from a set is sorted before it is written.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from interlayer.errors import IngestError, StageInputMissingError

__all__ = [
    "hash_email",
    "read_jsonl",
    "read_text",
    "secure_dir",
    "secure_write",
    "seeded",
    "sha256_file",
    "write_jsonl",
]

M = TypeVar("M", bound=BaseModel)

DIR_MODE = 0o700
FILE_MODE = 0o600


def secure_dir(path: Path) -> Path:
    """Create or confirm a 0700 directory.

    The ``chmod`` is unconditional because ``mkdir(exist_ok=True)`` does not
    correct the mode of a directory that already exists.
    """
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    path.chmod(DIR_MODE)
    actual = stat.S_IMODE(path.stat().st_mode)
    if actual != DIR_MODE:
        raise OSError(f"{path} is {actual:o}, expected {DIR_MODE:o}")
    return path


def secure_write(path: Path, data: str) -> None:
    """Write atomically, 0600 from creation.

    ``mkstemp`` creates at 0600 and ``os.replace`` preserves the mode through the
    rename, so the file is never briefly readable by anyone else.
    """
    secure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        tmp_path = Path(tmp)
        tmp_path.chmod(FILE_MODE)
        tmp_path.replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_jsonl(path: Path, records: Iterable[BaseModel]) -> int:
    """Serialise models to JSONL. Returns the number of records written."""
    lines: list[str] = [r.model_dump_json() for r in records]
    secure_write(path, "".join(f"{line}\n" for line in lines))
    return len(lines)


def read_jsonl(path: Path, model: type[M], *, produced_by: str = "run") -> list[M]:
    """Parse JSONL into models, reporting the offending line number on failure."""
    if not path.is_file():
        raise StageInputMissingError(str(path), produced_by)
    out: list[M] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(model.model_validate_json(line))
            except Exception as exc:
                raise IngestError(f"{path}:{lineno}: {exc}") from exc
    return out


def read_text(path: Path, *, produced_by: str = "run") -> str:
    """Read a UTF-8 text artifact, with the same missing-input error as read_jsonl."""
    if not path.is_file():
        raise StageInputMissingError(str(path), produced_by)
    return path.read_text(encoding="utf-8")


def sha256_file(path: Path) -> str:
    """Content hash of an input file, recorded in the run manifest for provenance."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_email(email: str, key: str) -> str:
    """Keyed digest of an email address.

    HMAC rather than a bare hash: the space of email addresses is small enough
    that an unkeyed digest is reversible by brute force, which would defeat the
    point of not storing the address.
    """
    return hmac.new(
        key.encode("utf-8"), email.strip().casefold().encode("utf-8"), hashlib.sha256
    ).hexdigest()


@contextmanager
def seeded(seed: int) -> Iterator[None]:
    """Pin the global RNG for the duration of a block, then restore it.

    Restoring matters: a stage that leaves the global RNG re-seeded makes the
    next stage's output depend on whether this one ran.
    """
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


def dump_json(path: Path, payload: object) -> None:
    """Write pretty, key-sorted JSON so artifacts diff cleanly between runs."""
    secure_write(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
