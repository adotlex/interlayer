"""Offline HAR parser — the recommended primary edge collector.

The user browses their own LinkedIn session with DevTools recording, exports a
``.har`` file, and hands the file to ``interlayer``. Everything from that point on
happens on their disk. **This module makes no network calls of any kind**: it
imports no HTTP client, opens no socket, and resolves no hostname. The traffic it
reads was made by the user's browser, by the user, before the tool ever ran, and
is therefore identical to what a person clicking around would produce.

That is not squeamishness. Enforcement precedent tracks *where the code runs*:
every entity that has been sued or shut down was running code against LinkedIn's
servers or inside LinkedIn's pages. Parsing a file the user already has is the one
capture category with no enforcement precedent at all, and it costs nothing in
coverage, because the monthly quota — not throughput — is what caps every mode.

**Failure modes are first-class here.** A HAR arrives with no response bodies, or
base64 bodies, or gzipped bodies, or bodies whose JSON shape no longer matches
what the research recorded. Each of those gets its own diagnostic naming the
``schemas.py`` entry involved. None of them produces a silent empty result: "0
mutual connections" and "LinkedIn moved the field" must never look the same to
the person reading the output.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from interlayer.collect import schemas
from interlayer.collect.interface import (
    COLLECTOR_SCHEMA_VERSION,
    CaptureStatus,
    Code,
    CollectionResult,
    Degree,
    Diagnostic,
    MutualCapture,
    PersonRef,
    Severity,
    TargetRef,
    merge_results,
    records_from_capture,
)
from interlayer.core import ids
from interlayer.core.models import CompliancePosture, Firm

COLLECTOR_NAME = "har"

#: Ceiling on recursive-fallback traversal. A session HAR is routinely >100 MB and
#: a malformed document must not become an unbounded walk.
_MAX_WALK_NODES = 200_000
_MAX_WALK_DEPTH = 40

_GZIP_MAGIC = b"\x1f\x8b"

#: Built from the schema table rather than written out, so the URN shape lives in
#: exactly one place like every other external shape.
_PROFILE_URN_RE = re.compile(
    re.escape(schemas.value("token.profile_urn_prefix")) + r"[A-Za-z0-9_\-]+"
)

#: Likewise for the Rest.li key/value pairs packed into the ``variables`` blob.
_RESTLI_KEY, _RESTLI_VALUE, _RESTLI_LIST = schemas.values("token.restli_query_pair")
_RESTLI_PAIR_RE = re.compile(
    re.escape(_RESTLI_KEY)
    + r"([A-Za-z0-9_]+)"
    + re.escape(_RESTLI_VALUE)
    + re.escape(_RESTLI_LIST)
    + r"([^)]*)\)"
)


class HarCollector:
    """Parses ``.har`` exports into mutual-connection captures.

    ``targets`` maps an opaque ``connectionOf`` id — or a profile slug — to a known
    target. Without it, ``default_firm`` lets a whole-file capture be attributed to
    one firm; without either, the records are still parsed and reported, but the
    edges cannot be attached and say so loudly rather than vanishing.
    """

    name = COLLECTOR_NAME
    version = COLLECTOR_SCHEMA_VERSION
    compliance_posture = CompliancePosture.MANUAL_CAPTURE

    def __init__(
        self,
        *,
        targets: Mapping[str, TargetRef] | None = None,
        default_firm: Firm | None = None,
    ) -> None:
        self.targets: dict[str, TargetRef] = {
            _index_key(key): ref for key, ref in (targets or {}).items()
        }
        self.default_firm = default_firm

    # -- discovery ---------------------------------------------------------

    def accepts(self, path: Path) -> bool:
        """Extension plus a small header sniff — never a full parse."""
        if path.suffix.casefold() not in {".har", ".json"}:
            return False
        try:
            with path.open("rb") as handle:
                head = handle.read(4096).decode("utf-8", errors="replace")
        except OSError:
            return False
        log_key, entries_key = schemas.path("har.entries")
        return f'"{log_key}"' in head and f'"{entries_key}"' in head

    # -- collection --------------------------------------------------------

    def collect(self, path: Path) -> CollectionResult:
        if not path.exists():
            return _empty(
                Diagnostic(
                    code=Code.INPUT_MISSING,
                    message=f"{path} does not exist",
                    severity=Severity.FATAL,
                    location=str(path),
                )
            )

        try:
            raw = path.read_bytes()
        except OSError as exc:
            return _empty(
                Diagnostic(
                    code=Code.INPUT_UNREADABLE,
                    message=f"{path} could not be read: {exc}",
                    severity=Severity.FATAL,
                    location=str(path),
                )
            )

        if not raw.strip():
            return _empty(
                Diagnostic(
                    code=Code.INPUT_EMPTY,
                    message=f"{path.name} is empty",
                    severity=Severity.FATAL,
                    location=str(path),
                )
            )

        digest = hashlib.sha256(raw).hexdigest()
        try:
            document = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return _empty(
                Diagnostic(
                    code=Code.INPUT_NOT_JSON,
                    message=(
                        f"{path.name} is not valid JSON ({exc.msg} at line {exc.lineno}); "
                        "a HAR is a JSON document"
                    ),
                    severity=Severity.FATAL,
                    schema_key="har.entries",
                    location=str(path),
                    remedy="re-export from DevTools with 'Export HAR'",
                )
            )

        entries = schemas.dig(document, "har.entries")
        if not isinstance(entries, list):
            return _empty(
                Diagnostic(
                    code=Code.HAR_MISSING_ENTRIES,
                    message=(
                        f"{path.name} has no request list where a HAR should have one"
                    ),
                    severity=Severity.FATAL,
                    schema_key="har.entries",
                    location=str(path),
                    remedy="check that this file is a HAR export and not another JSON file",
                )
            )

        # Rule V8: refuse an unsanitised export BEFORE parsing anything from it.
        unsanitised = _sensitive_entry_indices(entries)
        if unsanitised:
            return _empty(
                Diagnostic(
                    code=Code.HAR_UNSANITISED,
                    message=(
                        f"{path.name} contains request or response credentials on "
                        f"{len(unsanitised)} of {len(entries)} entries "
                        f"(first at entry {unsanitised[0]}). This file is a credential "
                        "file. It was not parsed and nothing from it was stored. "
                        "interlayer never reads, stores or transmits a session "
                        "credential, and will not start by reading one out of a HAR."
                    ),
                    severity=Severity.FATAL,
                    schema_key="har.sensitive_header_names",
                    location=str(path),
                    remedy=(
                        "re-export the HAR with sanitisation enabled (the default in "
                        "Chrome 130+), then delete this copy"
                    ),
                )
            )

        return self._parse_entries(
            entries,
            path=path,
            digest=digest,
            creator=_creator(document),
        )

    # -- internals ---------------------------------------------------------

    def _parse_entries(
        self,
        entries: list[Any],
        *,
        path: Path,
        digest: str,
        creator: str,
    ) -> CollectionResult:
        diagnostics: list[Diagnostic] = []
        groups: dict[str, _CaptureGroup] = {}
        order: list[str] = []

        scanned = 0
        api_entries = 0
        bodyless = 0
        unrelated = 0

        for index, entry in enumerate(entries):
            scanned += 1
            if not isinstance(entry, Mapping):
                continue
            where = f"{path.name} entry[{index}]"

            if not _is_api_get(entry):
                continue
            api_entries += 1

            content = schemas.dig(entry, "har.entry_response_content")
            status = schemas.dig(entry, "har.entry_response_status")
            if _as_int(status) != _as_int(schemas.value("har.expected_status")):
                diagnostics.append(
                    Diagnostic(
                        code=Code.HAR_NO_MATCHING_ENTRIES,
                        message=(
                            f"internal-API request returned status {status!r}; skipped"
                        ),
                        severity=Severity.INFO,
                        schema_key="har.entry_response_status",
                        location=where,
                    )
                )
                continue

            mime = schemas.field(content, "har.content_mime")
            if not isinstance(mime, str) or not mime.startswith(
                schemas.value("har.json_mime_prefix")
            ):
                continue

            body_text, decode_diag = _decode_body(content, where)
            if decode_diag is not None:
                diagnostics.append(decode_diag)
                continue
            if body_text is None:
                bodyless += 1
                continue

            try:
                body = json.loads(body_text)
            except json.JSONDecodeError as exc:
                diagnostics.append(
                    Diagnostic(
                        code=Code.HAR_BODY_NOT_JSON,
                        message=(
                            f"response body declared as {mime!r} but did not parse as "
                            f"JSON ({exc.msg} at line {exc.lineno}); first 60 bytes: "
                            f"{body_text[:60]!r}"
                        ),
                        severity=Severity.WARNING,
                        schema_key="har.content_text",
                        location=where,
                    )
                )
                continue

            url = schemas.dig(entry, "har.entry_request_url")
            query = _merged_params(url if isinstance(url, str) else "")
            opaque = _connection_of(query)
            if opaque is None:
                unrelated += 1
                continue

            network = _network_filter(query)
            if network != (schemas.value("enum.network_first_degree"),):
                # Rule V13. An unrestricted connectionOf capture reaches beyond the
                # mutual subset into the target's own connection list, including for
                # members who made it private. That is not what this tool collects,
                # so the record is rejected rather than kept as a bonus.
                diagnostics.append(
                    Diagnostic(
                        code=Code.VOYAGER_NETWORK_FILTER_MISSING,
                        message=(
                            f"capture for {opaque!r} was not restricted to first-degree "
                            f"connections (filter={list(network) or 'absent'}), so it may "
                            "contain the target's non-mutual connections. Record "
                            "rejected; no edges taken from it."
                        ),
                        severity=Severity.ERROR,
                        schema_key="param.network",
                        location=where,
                        remedy=(
                            "capture by clicking 'N mutual connections' on the target's "
                            "profile, which applies the restriction for you"
                        ),
                    )
                )
                continue

            people, strategy, extract_diag = _extract_people(body, where)
            if extract_diag is not None:
                diagnostics.append(extract_diag)
            if strategy is None:
                continue

            group = groups.get(opaque)
            if group is None:
                group = _CaptureGroup(opaque_id=opaque, source_url=url if isinstance(url, str) else None)
                groups[opaque] = group
                order.append(opaque)
            group.absorb(
                people=people,
                strategy=strategy,
                body=body,
                started=_started_at(entry),
                network=network,
            )

        if api_entries == 0:
            diagnostics.append(
                Diagnostic(
                    code=Code.HAR_NO_MATCHING_ENTRIES,
                    message=(
                        f"{path.name}: scanned {scanned} requests and found none to "
                        "LinkedIn's internal API. Nothing was extracted — this is not "
                        "the same as finding no mutual connections."
                    ),
                    severity=Severity.ERROR,
                    schema_key="url.voyager_api_marker",
                    location=str(path),
                    remedy=(
                        "record with DevTools → Network → 'Preserve log' on, open a "
                        "target's mutual-connections list, then export the HAR"
                    ),
                )
            )
        if bodyless:
            diagnostics.append(
                Diagnostic(
                    code=Code.HAR_NO_RESPONSE_BODY,
                    message=(
                        f"{path.name} contains {bodyless} matching internal-API requests "
                        "with no response body. Browsers do not always store XHR bodies "
                        "in a HAR, so this is a capture problem, not an empty network."
                    ),
                    severity=Severity.ERROR,
                    schema_key="har.content_text",
                    location=str(path),
                    remedy=(
                        "re-export with 'Preserve log' enabled, or right-click the "
                        "request → Copy response and save one JSON object per line"
                    ),
                )
            )
        if unrelated:
            diagnostics.append(
                Diagnostic(
                    code=Code.VOYAGER_NO_CONNECTION_OF,
                    message=(
                        f"{unrelated} internal-API responses were not shared-connections "
                        "searches (no target parameter); ignored"
                    ),
                    severity=Severity.INFO,
                    schema_key="param.connection_of",
                    location=str(path),
                )
            )

        results: list[CollectionResult] = []
        for opaque in order:
            capture = groups[opaque].to_capture(
                collector=self,
                path=path,
                digest=digest,
                creator=creator,
                resolved=self._resolve(opaque, groups[opaque]),
            )
            results.append(records_from_capture(capture))

        base = CollectionResult(
            collector=self.name,
            posture=self.compliance_posture,
            diagnostics=tuple(diagnostics),
        )
        return merge_results(
            [base, *results], collector=self.name, posture=self.compliance_posture
        )

    def _resolve(self, opaque: str, group: _CaptureGroup) -> TargetRef:
        """Attach a captured group to a known target, or say we could not."""
        known = self.targets.get(_index_key(opaque))
        if known is not None:
            return TargetRef(
                name=known.name,
                profile_url=known.profile_url,
                urn=known.urn,
                id=known.id,
                firm=known.firm,
                degree=known.degree,
                opaque_id=opaque,
            )
        if self.default_firm is not None:
            return TargetRef(
                id=ids.target_id(firm=self.default_firm.value, slug=opaque),
                firm=self.default_firm.value,
                degree=Degree.UNKNOWN,
                opaque_id=opaque,
            )
        return TargetRef(firm="other", degree=Degree.UNKNOWN, opaque_id=opaque)


# ---------------------------------------------------------------------------
# Per-target accumulation across pages
# ---------------------------------------------------------------------------


class _CaptureGroup:
    """All pages of one target's shared-connections search."""

    def __init__(self, opaque_id: str, source_url: str | None) -> None:
        self.opaque_id = opaque_id
        self.source_url = source_url
        self.people: dict[str, PersonRef] = {}
        self.strategies: set[str] = set()
        self.pages: set[int] = set()
        self.reported_total: int | None = None
        self.max_seen: int = 0
        self.started: datetime | None = None
        self.network: tuple[str, ...] = ()

    def absorb(
        self,
        *,
        people: list[PersonRef],
        strategy: str,
        body: Any,
        started: datetime | None,
        network: tuple[str, ...],
    ) -> None:
        for person in people:
            self.people.setdefault(person.key, person)
        self.strategies.add(strategy)
        self.network = network
        if started is not None and (self.started is None or started < self.started):
            self.started = started

        total = _as_int(schemas.dig(body, "json.paging_total"))
        start = _as_int(schemas.dig(body, "json.paging_start")) or 0
        count = _as_int(schemas.dig(body, "json.paging_count")) or len(people)
        if total is not None:
            self.reported_total = max(self.reported_total or 0, total)
        self.max_seen = max(self.max_seen, start + count)
        self.pages.add(start // count + 1 if count else 1)

    def to_capture(
        self,
        *,
        collector: HarCollector,
        path: Path,
        digest: str,
        creator: str,
        resolved: TargetRef,
    ) -> MutualCapture:
        people = tuple(self.people[key] for key in sorted(self.people))
        status = self._status(len(people))
        return MutualCapture(
            target=resolved,
            status=status,
            mutuals=people,
            reported_count=self.reported_total,
            pages_seen=tuple(sorted(self.pages)),
            collector=collector.name,
            collector_version=collector.version,
            posture=collector.compliance_posture,
            captured_at=self.started or datetime.now(timezone.utc),
            source_url=self.source_url,
            source_artifact=str(path),
            source_artifact_sha256=digest,
            parser_strategy="+".join(sorted(self.strategies)),
            network_filter=self.network,
            notes=creator,
        )

    def _status(self, observed: int) -> CaptureStatus:
        total = self.reported_total
        if total is not None and self.max_seen < total:
            return CaptureStatus.TRUNCATED
        if total is not None and observed < total:
            return CaptureStatus.TRUNCATED
        if observed == 0:
            return CaptureStatus.EMPTY
        return CaptureStatus.COMPLETE


# ---------------------------------------------------------------------------
# Entry selection, sanitisation, decoding
# ---------------------------------------------------------------------------


def _is_api_get(entry: Mapping[str, Any]) -> bool:
    method = schemas.dig(entry, "har.entry_request_method")
    if not isinstance(method, str) or method.upper() != schemas.value("har.expected_method"):
        return False
    url = schemas.dig(entry, "har.entry_request_url")
    if not isinstance(url, str):
        return False
    return schemas.value("url.voyager_api_marker") in url


def _sensitive_entry_indices(entries: list[Any]) -> list[int]:
    """Indices of entries carrying credentials.

    This only ever tests for *presence*: no value from a header or cookie
    container is read, decoded, logged or stored, and no credential name is
    matched by name. Generic HTTP vocabulary is enough to recognise an
    unsanitised export, and it keeps this package free of any credential
    identifier a future reader could mistake for support for one.
    """
    found: list[int] = []
    containers = schemas.values("har.sensitive_containers")
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        for side in ("request", "response"):
            block = entry.get(side)
            if not isinstance(block, Mapping):
                continue
            for container in containers:
                items = block.get(container)
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    if container == "cookies":
                        found.append(index)
                        break
                    if schemas.is_sensitive_header(_as_str(item.get("name"))):
                        found.append(index)
                        break
                if found and found[-1] == index:
                    break
            if found and found[-1] == index:
                break
    return found


def _decode_body(content: Any, where: str) -> tuple[str | None, Diagnostic | None]:
    """Return the body text, handling base64 and gzip.

    ``(None, None)`` means "no body present", which the caller counts and reports
    in aggregate — that case is common enough that one diagnostic per entry would
    bury everything else.
    """
    text = schemas.field(content, "har.content_text")
    if not isinstance(text, str) or not text.strip():
        return None, None

    encoding = schemas.field(content, "har.content_encoding")
    is_base64 = (
        isinstance(encoding, str)
        and encoding.strip().casefold() == schemas.value("har.base64_encoding")
    )
    if is_base64:
        try:
            raw = base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError) as exc:
            return None, Diagnostic(
                code=Code.HAR_BODY_NOT_DECODABLE,
                message=f"response body is marked base64 but did not decode: {exc}",
                severity=Severity.WARNING,
                schema_key="har.content_encoding",
                location=where,
            )
    else:
        raw = text.encode("utf-8", errors="surrogateescape")

    if raw[:2] == _GZIP_MAGIC:
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            return None, Diagnostic(
                code=Code.HAR_BODY_NOT_DECODABLE,
                message=f"response body looks gzipped but did not decompress: {exc}",
                severity=Severity.WARNING,
                schema_key="har.content_text",
                location=where,
            )

    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError as exc:
        return None, Diagnostic(
            code=Code.HAR_BODY_NOT_DECODABLE,
            message=f"response body is not UTF-8 text: {exc}",
            severity=Severity.WARNING,
            schema_key="har.content_text",
            location=where,
        )


def _connection_of(query: Mapping[str, list[str]]) -> str | None:
    """The opaque id the search was restricted to, or ``None``.

    The value arrives as a URL-encoded JSON array of one element. It is opaque by
    contract: never constructed, never interpreted, only carried.
    """
    raw = schemas.query_values(query, "param.connection_of")
    for candidate in raw:
        for item in _unwrap_list(candidate):
            if item:
                return item
    return None


def _network_filter(query: Mapping[str, list[str]]) -> tuple[str, ...]:
    values: list[str] = []
    for candidate in schemas.query_values(query, "param.network"):
        values.extend(item for item in _unwrap_list(candidate) if item)
    return tuple(values)


def _unwrap_list(raw: str) -> list[str]:
    """Parse ``["A","B"]`` — or a bare value — into a list of strings."""
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [part.strip().strip('"\'') for part in text[1:-1].split(",")]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed]
        return [str(parsed)]
    if text.startswith("List(") and text.endswith(")"):
        return [part.strip() for part in text[5:-1].split(",")]
    return [text]


def _started_at(entry: Mapping[str, Any]) -> datetime | None:
    raw = schemas.field(entry, "har.entry_started")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _creator(document: Any) -> str:
    name = schemas.dig(document, "har.creator_name")
    version = schemas.dig(document, "har.creator_version")
    if isinstance(name, str) and name:
        return f"{name} {version}".strip() if isinstance(version, str) else name
    return ""


# ---------------------------------------------------------------------------
# Response-shape extraction
# ---------------------------------------------------------------------------

#: Tried in order. The first three are fixed paths from the schema table; the
#: fourth is a shape assertion that survives a rename of all of them.
_FASTPATH_KEYS = ("json.search_clusters", "json.search_clusters_double_wrapped")


def _extract_people(body: Any, where: str) -> tuple[list[PersonRef], str | None, Diagnostic | None]:
    """Pull person entities out of one response body.

    Returns ``(people, strategy, diagnostic)``. A ``strategy`` of ``None`` means
    no known shape matched — and that is reported as an error naming every schema
    entry that was tried, because the alternative (returning an empty list) tells
    the user they have no network when in fact the parser is out of date.
    """
    for key in _FASTPATH_KEYS:
        elements = schemas.dig(body, key)
        if isinstance(elements, list):
            people = _people_from_clusters(elements)
            return people, f"fastpath:{key}", None

    included = schemas.field(body, "json.included")
    if isinstance(included, list):
        people = _people_from_included(included)
        if people:
            return people, "fastpath:json.included", None

    people = _people_from_walk(body)
    if people:
        return (
            people,
            "fallback:recursive",
            Diagnostic(
                code=Code.VOYAGER_FALLBACK_USED,
                message=(
                    f"no known response path matched; recovered {len(people)} people by "
                    "shape-matching the whole document. The response format has probably "
                    "changed — the extracted data is usable but the fast paths need "
                    "updating before the next release."
                ),
                severity=Severity.WARNING,
                schema_key=_FASTPATH_KEYS[0],
                location=where,
            ),
        )

    return (
        [],
        None,
        Diagnostic(
            code=Code.VOYAGER_SHAPE_UNRECOGNISED,
            message=(
                "response matched none of the known shapes and the recursive fallback "
                "found no person-shaped objects either. This means the response format "
                "changed — it does NOT mean the target has no mutual connections. "
                "Paths tried: "
                + ", ".join((*_FASTPATH_KEYS, "json.included", "token.profile_urn_prefix"))
            ),
            severity=Severity.ERROR,
            schema_key="json.search_clusters",
            location=where,
            remedy=(
                "update the shapes in collect/schemas.py, then re-run; every path this "
                "parser uses is declared there and nowhere else"
            ),
        ),
    )


def _people_from_clusters(elements: list[Any]) -> list[PersonRef]:
    people: list[PersonRef] = []
    for element in elements:
        items = schemas.field(element, "json.cluster_items")
        if not isinstance(items, list):
            continue
        for item in items:
            entity = schemas.dig(item, "json.item_entity_result")
            if entity is None:
                entity = item
            person = _person_from_entity(entity)
            if person is not None:
                people.append(person)
    return people


def _people_from_included(included: list[Any]) -> list[PersonRef]:
    markers = schemas.values("json.included_type_markers")
    people: list[PersonRef] = []
    for element in included:
        if not isinstance(element, Mapping):
            continue
        type_name = schemas.field(element, "json.included_type_field")
        if not isinstance(type_name, str):
            continue
        if not any(marker in type_name for marker in markers):
            continue
        person = _person_from_entity(element)
        if person is not None:
            people.append(person)
    return people


def _people_from_walk(body: Any) -> list[PersonRef]:
    """Recursive fallback: any object carrying a profile URN *and* a profile URL."""
    people: list[PersonRef] = []
    seen: set[str] = set()
    for node in _walk(body):
        if not _looks_like_person(node):
            continue
        person = _person_from_entity(node)
        if person is not None and person.key not in seen:
            seen.add(person.key)
            people.append(person)
    return people


def _walk(node: Any, depth: int = 0, budget: list[int] | None = None) -> Iterator[Mapping[str, Any]]:
    if budget is None:
        budget = [_MAX_WALK_NODES]
    if depth > _MAX_WALK_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1
    if isinstance(node, Mapping):
        yield node
        for child in node.values():
            yield from _walk(child, depth + 1, budget)
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child, depth + 1, budget)


def _looks_like_person(node: Mapping[str, Any]) -> bool:
    nav = schemas.field(node, "json.entity_navigation_url")
    if not isinstance(nav, str) or schemas.value("url.profile_path_prefix") not in nav:
        return False
    return _profile_urn(node) is not None


def _person_from_entity(entity: Any) -> PersonRef | None:
    if not isinstance(entity, Mapping):
        return None
    nav = schemas.field(entity, "json.entity_navigation_url")
    urn = _profile_urn(entity)
    name = _text_of(schemas.dig(entity, "json.entity_name"))
    headline = _text_of(schemas.dig(entity, "json.entity_headline"))
    distance = schemas.dig(entity, "json.entity_member_distance")
    profile_url = nav if isinstance(nav, str) else None
    if not (urn or profile_url or name):
        return None
    return PersonRef(
        name=name,
        profile_url=profile_url,
        urn=urn,
        headline=headline,
        member_distance=distance if isinstance(distance, str) else None,
    )


def _profile_urn(entity: Mapping[str, Any]) -> str | None:
    """Find a member URN on an entity, wherever the schema currently hides it."""
    for key in ("json.entity_urn", "json.entity_tracking_urn"):
        candidate = schemas.field(entity, key)
        if isinstance(candidate, str):
            match = _PROFILE_URN_RE.search(candidate)
            if match:
                return match.group(0)
    for candidate in entity.values():
        if isinstance(candidate, str):
            match = _PROFILE_URN_RE.search(candidate)
            if match:
                return match.group(0)
    return None


def _text_of(node: Any) -> str | None:
    if isinstance(node, str):
        return node.strip() or None
    return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _index_key(value: str) -> str:
    return value.strip().casefold()


def _empty(*diagnostics: Diagnostic) -> CollectionResult:
    return CollectionResult(
        collector=COLLECTOR_NAME,
        posture=CompliancePosture.MANUAL_CAPTURE,
        diagnostics=tuple(diagnostics),
    )


__all__ = ["COLLECTOR_NAME", "HarCollector"]
