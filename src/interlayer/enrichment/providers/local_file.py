"""User-supplied CSV/JSONL enrichment. Offline, free, and the honest P0 answer.

If a user already has profile data — an export from a CRM, a spreadsheet a
colleague maintains, a dataset they licensed themselves — this reads it. The
lawful basis is ``USER_SUPPLIED``: they brought the data, they take
responsibility for it, and the tool does not pretend to have vetted it.

Contact fields in the file are dropped at this boundary like any other provider's,
and the drop is recorded on each record so it is visible rather than silent.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from interlayer.core.models import CompliancePosture
from interlayer.enrichment.interface import (
    EnrichedPerson,
    EnrichmentResult,
    LawfulBasis,
    LookupKey,
    ProviderCapabilities,
    ProviderCompliance,
    ProviderConfigError,
    ProviderCost,
    ToSRisk,
)
from interlayer.enrichment.normalize import (
    CANONICAL_FIELD_MAP,
    FieldMap,
    match_tokens,
    normalize_person,
)

NAME = "local_file"


class LocalFileEnrichmentProvider:
    """Indexes a CSV or JSONL file of canonical-ish columns and serves lookups."""

    name = NAME
    capabilities = ProviderCapabilities(
        # The user's own file cannot contain LinkedIn's connection graph either:
        # there is no export, first-party or otherwise, that carries it.
        provides_connection_edges=False,
        provides_connections_count=False,
        provides_employment_history=True,
        provides_education_history=True,
        provides_skills=True,
        provides_location=True,
        supports_batch=True,
        max_batch_size=10_000,
        requires_network=False,
    )
    compliance = ProviderCompliance(
        tos_risk=ToSRisk.NONE,
        lawful_basis=LawfulBasis.USER_SUPPLIED,
        requires_user_credentials=False,
        stores_data_offshore=False,
        vendor_opt_out_url=None,
        posture=CompliancePosture.FIRST_PARTY_EXPORT,
        notes=(
            "Reads a file the user supplied. No network. The user is the "
            "controller of whatever is in it."
        ),
    )
    cost = ProviderCost(usd_per_lookup=0.0, notes="Free, offline.")

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        field_map: FieldMap = CANONICAL_FIELD_MAP,
        allow_contact_fields: bool = False,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.field_map = field_map
        self.allow_contact_fields = allow_contact_fields
        self._index: dict[str, EnrichedPerson] | None = None

    # -- loading -----------------------------------------------------------

    def _load(self) -> dict[str, EnrichedPerson]:
        if self._index is not None:
            return self._index
        if self.path is None:
            raise ProviderConfigError(
                f"{self.name}: no file configured. Point it at a CSV or JSONL of "
                "profile records, or select the 'null' provider."
            )
        if not self.path.exists():
            raise ProviderConfigError(f"{self.name}: {self.path} does not exist")

        retrieved_at = _file_mtime(self.path)
        index: dict[str, EnrichedPerson] = {}
        for payload in _read_records(self.path):
            person = normalize_person(
                payload,
                provider=self.name,
                retrieved_at=retrieved_at,
                field_map=self.field_map,
                allow_contact_fields=self.allow_contact_fields,
                source_url=str(self.path),
                license_note="user-supplied file",
            )
            for token in _index_tokens(person):
                index.setdefault(token, person)
        self._index = index
        return index

    # -- protocol ----------------------------------------------------------

    def enrich(self, keys: Sequence[LookupKey]) -> Iterator[EnrichmentResult]:
        index = self._load()
        for key in keys:
            person = None
            for token in match_tokens(key):
                person = index.get(token)
                if person is not None:
                    break
            yield EnrichmentResult(key=key, person=person, cost_usd=0.0)

    def estimate_cost(self, n_keys: int) -> float:
        return 0.0


def _index_tokens(person: EnrichedPerson) -> tuple[str, ...]:
    from interlayer.core import ids

    tokens: list[str] = []
    if person.linkedin_public_id:
        tokens.append(f"slug:{person.linkedin_public_id}")
    if person.full_name:
        name_key = ids.name_key(person.full_name)
        employer = ids.normalize_text(person.current_employer_name)
        tokens.append(f"name:{name_key}|{employer}")
        tokens.append(f"name:{name_key}")
    return tuple(tokens)


def _read_records(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix in {".jsonl", ".ndjson"}:
        yield from _read_jsonl(path)
    elif suffix == ".json":
        yield from _read_json(path)
    else:
        yield from _read_csv(path)


def _read_csv(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            yield {key: value for key, value in row.items() if key}


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderConfigError(
                    f"{path}:{line_no} is not valid JSON: {exc.msg}"
                ) from exc
            if isinstance(payload, dict):
                yield payload


def _read_json(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ProviderConfigError(f"{path} is not valid JSON: {exc.msg}") from exc
    records = payload if isinstance(payload, list) else [payload]
    for record in records:
        if isinstance(record, dict):
            yield record


def _file_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


def factory(**kwargs: Any) -> LocalFileEnrichmentProvider:
    return LocalFileEnrichmentProvider(**kwargs)


__all__ = ["NAME", "LocalFileEnrichmentProvider", "factory"]
