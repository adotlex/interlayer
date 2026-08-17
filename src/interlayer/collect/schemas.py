"""The single declarative table of every external shape the collectors rely on.

**Why this file exists.** No agent that built this project could reach
``linkedin.com``: the research session's egress policy blocked it (verified — the
host returned ``000``). Every claim in ``docs/research/`` about a URL parameter
name, a query-string vocabulary or a Voyager JSON path is therefore *second-hand*,
quoted from third parties who were themselves quoting primary material, some of it
years old. Any one of these shapes may be wrong today or wrong tomorrow.

Two rules follow, and they are enforced by tests:

1. **No parser may hard-code a LinkedIn URL shape, query-parameter name, or
   DOM/JSON path as a bare literal.** Every such string lives here, once, with a
   version stamp, a ``verified`` flag, a note and a citation. ``tests/`` greps the
   ``collect`` and ``enrichment`` packages to keep it that way.
2. **An unrecognised shape must produce a clear diagnostic naming the entry that
   failed** — never a crash, and never a silent empty result. The difference
   matters enormously to the user: "LinkedIn's response shape changed, entry
   ``json.search_clusters`` no longer matches" is actionable; "0 mutual
   connections found" reads as "you have no network" and is a lie.

**What ``verified`` means here.** It is *not* "we think this is right". It is:
"this project has confirmed the shape against an artefact it can actually see".
That is true of the HAR container format (a published, LinkedIn-independent
format, exercised by our own fixtures) and of the CSV column specs (this project
owns them outright). It is false for **every LinkedIn-specific entry**, without
exception, because nobody here could open a LinkedIn page. Flipping one to
``True`` is a claim that a human compared it against a real captured artefact.
Do not flip one because the parser happened to work once.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

#: Date these shapes were last reviewed. Stamped into every entry that does not
#: override it, and reported by the CLI so a user can see how stale the table is.
SCHEMA_VERSION = "2026-08-17"

#: Plain-language warning that belongs in any document or diagnostic quoting this
#: table. LinkedIn's UI and internal API change without notice or changelog.
STALENESS_WARNING = (
    "LinkedIn's interface and internal API may have changed since "
    f"{SCHEMA_VERSION}; these shapes were recorded from second-hand sources and "
    "were never verified against linkedin.com by this project."
)


class SchemaKind(str, Enum):
    """What sort of shape an entry describes."""

    URL = "url"
    """A path fragment, host, or full URL template."""

    QUERY_PARAM = "query_param"
    """A query-string parameter name. ``values`` lists current name first, then
    historical aliases, so a parser accepts both generations."""

    ENUM_VALUE = "enum_value"
    """A closed vocabulary of values a field may take."""

    JSON_PATH = "json_path"
    """A path through a decoded JSON document. ``values`` are the keys in order."""

    JSON_FIELD = "json_field"
    """A single key name looked up at whatever depth the caller is at."""

    TOKEN = "token"
    """A literal substring used for shape sniffing (e.g. a URN prefix)."""

    COLUMN_SET = "column_set"
    """An ordered set of CSV column names."""


class Confidence(str, Enum):
    """How much the source behind an entry is worth."""

    HIGH = "high"
    """Owned by this project, or an open published spec."""

    MEDIUM = "medium"
    """Quoted verbatim from source code or an issue thread that had evidently
    run against the real service."""

    LOW = "low"
    """Prose description in a blog post or vendor page; plausible, unconfirmed."""


class UnknownSchemaKey(KeyError):
    """Raised when code asks for a table entry that does not exist.

    This is a programming error, not a data error: a typo in a schema key must
    fail loudly at the call site rather than degrade into a diagnostic about the
    user's file.
    """


@dataclass(frozen=True, slots=True)
class SchemaEntry:
    """One externally-owned shape."""

    key: str
    kind: SchemaKind
    values: tuple[str, ...]
    verified: bool
    note: str
    source: str = ""
    confidence: Confidence = Confidence.LOW
    version: str = SCHEMA_VERSION

    @property
    def value(self) -> str:
        """The primary (current) value."""
        return self.values[0]

    @property
    def path(self) -> tuple[str, ...]:
        """The values read as an ordered JSON path."""
        return self.values

    def describe(self) -> str:
        """One-line, user-facing description naming this entry.

        Diagnostics quote this so the person reading the error knows exactly
        which line of which file to go and check.
        """
        shown = ".".join(self.values) if self.kind is SchemaKind.JSON_PATH else "/".join(self.values)
        state = "verified" if self.verified else "UNVERIFIED"
        return (
            f"schemas.py entry '{self.key}' ({self.kind.value}, {state}, "
            f"stamped {self.version}): {shown} — {self.note}"
        )


def _e(
    key: str,
    kind: SchemaKind,
    *values: str,
    verified: bool,
    note: str,
    source: str = "",
    confidence: Confidence = Confidence.LOW,
) -> SchemaEntry:
    return SchemaEntry(
        key=key,
        kind=kind,
        values=values,
        verified=verified,
        note=note,
        source=source,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# A. LinkedIn URL shapes — every one UNVERIFIED
# ---------------------------------------------------------------------------

_LINKEDIN_URLS: tuple[SchemaEntry, ...] = (
    _e(
        "url.host",
        SchemaKind.URL,
        "www.linkedin.com",
        verified=False,
        note=(
            "Canonical host. Locale subdomains (uk., de., …) are equivalent and "
            "must be folded to this one before comparison (R4 rule V2)."
        ),
        source="R4 §Proposed input schemas / V2",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.host_suffix",
        SchemaKind.URL,
        "linkedin.com",
        verified=False,
        note="Registrable suffix used to recognise any locale subdomain variant.",
        source="R4 §V2",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.profile_path_prefix",
        SchemaKind.URL,
        "/in/",
        verified=False,
        note=(
            "Public profile path. The segment after it is the publicIdentifier "
            "('slug'), the only near-stable per-person identifier available."
        ),
        source="R4 §B, §V2",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.people_search_path",
        SchemaKind.URL,
        "/search/results/people/",
        verified=False,
        note=(
            "The people-search results page. The mutual-connections surface is "
            "literally this page with a 'Connections of' facet applied, which is "
            "why it consumes Commercial Use Limit quota."
        ),
        source="R4 §A.2",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.voyager_api_marker",
        SchemaKind.URL,
        "/voyager/api/",
        verified=False,
        note=(
            "Substring identifying an internal-API request inside a HAR. Used only "
            "to SELECT already-captured entries — this project never issues one."
        ),
        source="R4 §B, HAR entry-selection predicate",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.voyager_graphql_path",
        SchemaKind.URL,
        "/voyager/api/graphql",
        verified=False,
        note="GraphQL endpoint the people-search surface is driven by.",
        source="R4 §B (quoting dchrastil/ScrapedIn)",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "url.locale_query_param",
        SchemaKind.QUERY_PARAM,
        "originalSubdomain",
        verified=False,
        note="Locale marker appended to profile URLs; stripped during normalisation.",
        source="R4 §V2",
        confidence=Confidence.LOW,
    ),
)

# ---------------------------------------------------------------------------
# B. Query-parameter names — churned once already, hence the alias lists
# ---------------------------------------------------------------------------

_QUERY_PARAMS: tuple[SchemaEntry, ...] = (
    _e(
        "param.connection_of",
        SchemaKind.QUERY_PARAM,
        "connectionOf",
        "facetConnectionOf",
        verified=False,
        note=(
            "Identifies whose connections the search is restricted to. Value is a "
            "URL-encoded JSON array of ONE opaque id. The id is opaque — usually a "
            "member URN fragment (ACoAA…), sometimes reported as a publicIdentifier. "
            "Never construct or interpret it; carry it through as a key. The "
            "'facet' prefix was dropped around 2019-2020; both spellings are in the "
            "wild, so both are accepted here."
        ),
        source="R4 §A.2 (linkedtales/scrapedin issue #120, fetched)",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "param.network",
        SchemaKind.QUERY_PARAM,
        "network",
        "facetNetwork",
        verified=False,
        note=(
            "Degree restriction, URL-encoded JSON array. MUST be ['F'] for a mutual-"
            "connections capture: connectionOf alone reportedly returns the target's "
            "connections beyond the mutual subset, including for members who set "
            "their connections to private. Capturing that is out of scope and any "
            "record lacking this restriction is rejected (R4 rule V13)."
        ),
        source="R4 §A.2 coverage note; rule V13",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "param.origin",
        SchemaKind.QUERY_PARAM,
        "origin",
        verified=False,
        note="Telemetry only, not access control. Recorded, never required.",
        source="R4 §A.2",
        confidence=Confidence.LOW,
    ),
    _e(
        "param.page",
        SchemaKind.QUERY_PARAM,
        "page",
        verified=False,
        note="1-based page number on the rendered search URL.",
        source="R4 §A.2",
        confidence=Confidence.LOW,
    ),
    _e(
        "param.start",
        SchemaKind.QUERY_PARAM,
        "start",
        verified=False,
        note="0-based offset on the internal API. Pairs with param.count.",
        source="R4 §B",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "param.count",
        SchemaKind.QUERY_PARAM,
        "count",
        verified=False,
        note="Page size on the internal API.",
        source="R4 §B",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "param.variables",
        SchemaKind.QUERY_PARAM,
        "variables",
        verified=False,
        note=(
            "On the GraphQL endpoint the search facets are not top-level query "
            "parameters: they are packed into this one Rest.li-encoded blob as a "
            "queryParameters list. A parser that only reads top-level parameters "
            "silently finds nothing on exactly the requests that matter most."
        ),
        source="R4 §B (dchrastil/ScrapedIn, fetched)",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "token.restli_query_pair",
        SchemaKind.TOKEN,
        "key:",
        ",value:",
        "List(",
        verified=False,
        note=(
            "Rest.li encoding of one key/value pair inside param.variables, e.g. "
            "'(key:connectionOf,value:List(ACoAA…))'. The three fragments are "
            "assembled into the extraction pattern; the parameter names themselves "
            "still come from param.connection_of and param.network."
        ),
        source="R4 §B",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "param.query_id",
        SchemaKind.QUERY_PARAM,
        "queryId",
        verified=False,
        note=(
            "Rotating hash of a registered GraphQL query. Recorded for diagnostics "
            "only. Any code that CONSTRUCTS one breaks on the next deploy; code that "
            "reads what the browser already fetched is immune. That asymmetry is a "
            "large part of why this tool parses files instead of making requests."
        ),
        source="R4 §B stability warning 1",
        confidence=Confidence.MEDIUM,
    ),
)

# ---------------------------------------------------------------------------
# C. Closed vocabularies
# ---------------------------------------------------------------------------

_ENUMS: tuple[SchemaEntry, ...] = (
    _e(
        "enum.network_first_degree",
        SchemaKind.ENUM_VALUE,
        "F",
        verified=False,
        note="'First degree'. The only acceptable network filter value (V13).",
        source="R4 §A.2",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "enum.network_values",
        SchemaKind.ENUM_VALUE,
        "F",
        "S",
        "O",
        verified=False,
        note="First / second / out-of-network. Same vocabulary the linkedin-api wrapper documents.",
        source="R4 §A.2",
        confidence=Confidence.LOW,
    ),
    _e(
        "enum.member_distance_first",
        SchemaKind.ENUM_VALUE,
        "DISTANCE_1",
        verified=False,
        note=(
            "Per-entity degree badge in the API response. A mutual connection must "
            "be DISTANCE_1 by definition; anything else means the capture was not "
            "restricted to mutuals."
        ),
        source="R4 §B",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "enum.member_distance_values",
        SchemaKind.ENUM_VALUE,
        "DISTANCE_1",
        "DISTANCE_2",
        "DISTANCE_3",
        "OUT_OF_NETWORK",
        verified=False,
        note="Full degree vocabulary carried on search entities.",
        source="R4 §B",
        confidence=Confidence.LOW,
    ),
)

# ---------------------------------------------------------------------------
# D. Voyager JSON paths — the shapes most likely to break first
# ---------------------------------------------------------------------------

_VOYAGER_JSON: tuple[SchemaEntry, ...] = (
    _e(
        "json.search_clusters",
        SchemaKind.JSON_PATH,
        "data",
        "searchDashClustersByAll",
        "elements",
        verified=False,
        note="Primary fast path to the search result clusters.",
        source="R4 §B, §Voyager body extraction step 1",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.search_clusters_double_wrapped",
        SchemaKind.JSON_PATH,
        "data",
        "data",
        "searchDashClustersByAll",
        "elements",
        verified=False,
        note="Some responses double-wrap under 'data'. Second fast path.",
        source="R4 §Voyager body extraction step 2",
        confidence=Confidence.LOW,
    ),
    _e(
        "json.cluster_items",
        SchemaKind.JSON_FIELD,
        "items",
        verified=False,
        note="Per-cluster list of result items.",
        source="R4 §Voyager body extraction step 1",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.item_entity_result",
        SchemaKind.JSON_PATH,
        "itemUnion",
        "entityResult",
        verified=False,
        note="Unwraps one search item to the person entity.",
        source="R4 §Voyager body extraction step 1",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.included",
        SchemaKind.JSON_FIELD,
        "included",
        verified=False,
        note=(
            "Rest.li sibling array of normalised entities. Third extraction path, "
            "used when the clusters path is absent."
        ),
        source="R4 §B (linkedin-to-jsonresume dev notes, fetched)",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.included_type_field",
        SchemaKind.JSON_FIELD,
        "$type",
        verified=False,
        note="Rest.li type discriminator on entries of the included[] array.",
        source="R4 §B",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.included_type_markers",
        SchemaKind.TOKEN,
        "EntityResultViewModel",
        "identity.profile.Profile",
        verified=False,
        note=(
            "Substrings marking an included[] entry as a person. The decoration "
            "namespace is undocumented and renames without notice, so these are "
            "matched as substrings, not equality."
        ),
        source="R4 §Voyager body extraction step 3",
        confidence=Confidence.LOW,
    ),
    _e(
        "json.entity_urn",
        SchemaKind.JSON_FIELD,
        "entityUrn",
        verified=False,
        note="Strongest per-person identifier available in a capture.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.entity_tracking_urn",
        SchemaKind.JSON_FIELD,
        "trackingUrn",
        verified=False,
        note="Alternative URN carrier; used when entityUrn is absent.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.entity_navigation_url",
        SchemaKind.JSON_FIELD,
        "navigationUrl",
        verified=False,
        note="Profile URL for the entity; the /in/<slug> source.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.entity_name",
        SchemaKind.JSON_PATH,
        "title",
        "text",
        verified=False,
        note="Rendered display name. May carry a degree suffix in some locales.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.entity_headline",
        SchemaKind.JSON_PATH,
        "primarySubtitle",
        "text",
        verified=False,
        note="Headline text; aids disambiguation, never used as an identifier.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.entity_member_distance",
        SchemaKind.JSON_PATH,
        "entityCustomTrackingInfo",
        "memberDistance",
        verified=False,
        note="Per-entity degree badge; lets the parser cross-check degree for free.",
        source="R4 §B per-entity field map",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.paging_total",
        SchemaKind.JSON_PATH,
        "paging",
        "total",
        verified=False,
        note="Server-reported total. Drives truncation detection when present.",
        source="R4 §Truncation from HAR",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.paging_start",
        SchemaKind.JSON_PATH,
        "paging",
        "start",
        verified=False,
        note="Offset of this page.",
        source="R4 §Truncation from HAR",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "json.paging_count",
        SchemaKind.JSON_PATH,
        "paging",
        "count",
        verified=False,
        note="Size of this page. start+count < total means more pages exist.",
        source="R4 §Truncation from HAR",
        confidence=Confidence.MEDIUM,
    ),
    _e(
        "token.profile_urn_prefix",
        SchemaKind.TOKEN,
        "urn:li:fsd_profile:",
        verified=False,
        note=(
            "Prefix of a member URN. The recursive fallback recognises a person as "
            "'an object carrying a string with this prefix AND a navigationUrl "
            "containing url.profile_path_prefix' — a shape assertion rather than a "
            "path, so it survives a schema rename."
        ),
        source="R4 §Voyager body extraction step 4",
        confidence=Confidence.MEDIUM,
    ),
)

# ---------------------------------------------------------------------------
# E. HAR container — VERIFIED: an open format, independent of LinkedIn
# ---------------------------------------------------------------------------

_HAR: tuple[SchemaEntry, ...] = (
    _e(
        "har.entries",
        SchemaKind.JSON_PATH,
        "log",
        "entries",
        verified=True,
        note="Top-level list of captured requests in the HAR 1.2 archive format.",
        source="HAR 1.2 spec; exercised by this project's fixtures",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.creator_name",
        SchemaKind.JSON_PATH,
        "log",
        "creator",
        "name",
        verified=True,
        note="Exporting tool; recorded in capture notes for diagnostics only.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.creator_version",
        SchemaKind.JSON_PATH,
        "log",
        "creator",
        "version",
        verified=True,
        note="Exporting tool version; diagnostics only.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.entry_started",
        SchemaKind.JSON_FIELD,
        "startedDateTime",
        verified=True,
        note="ISO-8601 timestamp of the request; becomes Provenance.collected_at.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.entry_request_method",
        SchemaKind.JSON_PATH,
        "request",
        "method",
        verified=True,
        note="Only GET entries are considered.",
        source="HAR 1.2 spec; R4 entry-selection predicate",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.entry_request_url",
        SchemaKind.JSON_PATH,
        "request",
        "url",
        verified=True,
        note="Request URL; source of the connectionOf id and the network filter.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.entry_response_status",
        SchemaKind.JSON_PATH,
        "response",
        "status",
        verified=True,
        note="Only 200 responses are considered.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.entry_response_content",
        SchemaKind.JSON_PATH,
        "response",
        "content",
        verified=True,
        note="Body container: mimeType, text, encoding, size.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.content_text",
        SchemaKind.JSON_FIELD,
        "text",
        verified=True,
        note=(
            "Response body. Frequently ABSENT for XHR depending on browser and "
            "export settings — presence must be checked, never assumed."
        ),
        source="HAR 1.2 spec; R4 §B HAR mechanics",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.content_encoding",
        SchemaKind.JSON_FIELD,
        "encoding",
        verified=True,
        note="Present and equal to har.base64_encoding when the body is base64.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.content_mime",
        SchemaKind.JSON_FIELD,
        "mimeType",
        verified=True,
        note="Filtered on har.json_mime_prefix.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.base64_encoding",
        SchemaKind.ENUM_VALUE,
        "base64",
        verified=True,
        note="Body must be base64-decoded before parsing when this is set.",
        source="HAR 1.2 spec",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.expected_method",
        SchemaKind.ENUM_VALUE,
        "GET",
        verified=True,
        note="Entry-selection predicate, part 1.",
        source="R4 §HAR entry-selection predicate",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.expected_status",
        SchemaKind.ENUM_VALUE,
        "200",
        verified=True,
        note="Entry-selection predicate, part 3.",
        source="R4 §HAR entry-selection predicate",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.json_mime_prefix",
        SchemaKind.TOKEN,
        "application/",
        verified=True,
        note="Entry-selection predicate, part 4.",
        source="R4 §HAR entry-selection predicate",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.sensitive_containers",
        SchemaKind.JSON_FIELD,
        "cookies",
        "headers",
        verified=True,
        note=(
            "Fields this parser must NEVER read for content — only test for "
            "presence, in order to refuse an unsanitised export. A HAR carrying "
            "populated cookies is a credential file; the correct response is to "
            "stop and tell the user to re-export with sanitisation enabled."
        ),
        source="R4 rule V8; Chrome >=130 sanitises by default",
        confidence=Confidence.HIGH,
    ),
    _e(
        "har.sensitive_header_names",
        SchemaKind.ENUM_VALUE,
        "cookie",
        "set-cookie",
        "authorization",
        "csrf-token",
        verified=True,
        note=(
            "Generic HTTP header names whose presence marks an export as "
            "unsanitised. Deliberately generic: this package does not name, match "
            "on, read, store or transmit any LinkedIn session-cookie key, and a "
            "test asserts that no such literal appears anywhere in the source."
        ),
        source="R4 rule V8",
        confidence=Confidence.HIGH,
    ),
)

# ---------------------------------------------------------------------------
# F. Hand-fillable CSV column specs — VERIFIED: this project owns them
# ---------------------------------------------------------------------------

_CSV: tuple[SchemaEntry, ...] = (
    _e(
        "csv.mutuals_columns",
        SchemaKind.COLUMN_SET,
        "target_id",
        "mutual_name",
        "mutual_profile_url",
        "mutual_headline",
        "capture_status",
        "reported_count",
        "captured_at",
        "source_url",
        "collector",
        "notes",
        verified=True,
        note=(
            "R4 §Proposed input schemas 3, verbatim. Long/tidy: one row per "
            "(target, mutual) edge. A target with zero mutuals gets ONE row with an "
            "empty mutual_name and capture_status=empty — that is how the format "
            "distinguishes 'observed, none' from 'not yet looked at'."
        ),
        source="R4 §Proposed input schemas, table 3",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.mutuals_required_columns",
        SchemaKind.COLUMN_SET,
        "target_id",
        "capture_status",
        "captured_at",
        "collector",
        verified=True,
        note="Columns without which a mutuals row cannot be interpreted at all.",
        source="R4 §Proposed input schemas, table 3",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.mutuals_target_level_columns",
        SchemaKind.COLUMN_SET,
        "capture_status",
        "reported_count",
        "captured_at",
        "source_url",
        "collector",
        verified=True,
        note=(
            "Repeat on every row of a target. On conflict take the first non-empty "
            "value and warn — never fail a hand-edited file over this (rule V14)."
        ),
        source="R4 §Proposed input schemas, table 3; rule V14",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.targets_columns",
        SchemaKind.COLUMN_SET,
        "target_id",
        "name",
        "profile_url",
        "firm",
        "degree",
        "degree_observed_at",
        "enumeration_source",
        "enumeration_truncated",
        "notes",
        verified=True,
        note=(
            "R4 §Proposed input schemas 2, verbatim. 'degree' is the load-bearing "
            "column: it decides whether a target is visited at all."
        ),
        source="R4 §Proposed input schemas, table 2",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.targets_required_columns",
        SchemaKind.COLUMN_SET,
        "target_id",
        "name",
        "firm",
        "degree",
        verified=True,
        note="Minimum for a usable target row.",
        source="R4 §Proposed input schemas, table 2",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.capture_status_values",
        SchemaKind.ENUM_VALUE,
        "complete",
        "truncated",
        "empty",
        "unavailable",
        "skipped_by_degree",
        verified=True,
        note=(
            "The five states coverage accounting must distinguish. Only the status "
            "field — never len(mutuals) — separates empty from unavailable from "
            "pruned."
        ),
        source="R4 §Proposed input schemas, table 3",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.degree_values",
        SchemaKind.ENUM_VALUE,
        "1",
        "2",
        "3",
        "out",
        "unknown",
        verified=True,
        note="3rd degree and out-of-network have zero shared 1st-degree connections "
        "by the definition of shortest-path degree. That is the pruning oracle.",
        source="R4 §Proposed input schemas, table 2",
        confidence=Confidence.HIGH,
    ),
    _e(
        "csv.firm_values",
        SchemaKind.ENUM_VALUE,
        "jane_street",
        "citadel",
        "citadel_securities",
        "other",
        verified=True,
        note=(
            "Vocabulary owned by the target registry. Citadel and Citadel Securities "
            "are legally distinct firms with separate pages; conflating them "
            "silently corrupts results. 'other' has no core.models.Firm member and "
            "yields a diagnostic rather than a fabricated Target."
        ),
        source="R4 §Proposed input schemas, table 2; core.models.Firm",
        confidence=Confidence.HIGH,
    ),
)


SCHEMA: dict[str, SchemaEntry] = {
    entry.key: entry
    for entry in (*_LINKEDIN_URLS, *_QUERY_PARAMS, *_ENUMS, *_VOYAGER_JSON, *_HAR, *_CSV)
}


# ---------------------------------------------------------------------------
# Accessors — the ONLY way parsers are allowed to reach an external shape
# ---------------------------------------------------------------------------


def entry(key: str) -> SchemaEntry:
    """Look up one entry, or fail loudly."""
    try:
        return SCHEMA[key]
    except KeyError:
        raise UnknownSchemaKey(
            f"no schema entry {key!r}; known keys: {', '.join(sorted(SCHEMA))}"
        ) from None


def value(key: str) -> str:
    """Primary value of an entry."""
    return entry(key).value


def values(key: str) -> tuple[str, ...]:
    """All values of an entry (current name first, then historical aliases)."""
    return entry(key).values


def path(key: str) -> tuple[str, ...]:
    """An entry read as an ordered JSON path."""
    return entry(key).path


def describe(key: str) -> str:
    """User-facing description of an entry, for diagnostics."""
    return entry(key).describe()


def keys() -> tuple[str, ...]:
    """Every key in the table, sorted."""
    return tuple(sorted(SCHEMA))


def unverified_keys() -> tuple[str, ...]:
    """Keys whose shape this project could never confirm against the real service."""
    return tuple(sorted(k for k, e in SCHEMA.items() if not e.verified))


def verified_keys() -> tuple[str, ...]:
    """Keys confirmed against an artefact this project can actually see."""
    return tuple(sorted(k for k, e in SCHEMA.items() if e.verified))


def dig(document: Any, key: str) -> Any:
    """Walk the JSON path named by ``key``; return ``None`` if it does not exist.

    Returning ``None`` rather than raising is deliberate: a missing path is
    expected input variation and the caller turns it into a diagnostic naming
    ``key``. Only an unknown *schema key* is an exception.
    """
    node = document
    for segment in path(key):
        if not isinstance(node, Mapping):
            return None
        if segment not in node:
            return None
        node = node[segment]
    return node


def field(document: Any, key: str) -> Any:
    """Read the single field named by ``key`` from a mapping.

    Tries every alias in the entry, in order, and returns the first present.
    """
    if not isinstance(document, Mapping):
        return None
    for name in values(key):
        if name in document:
            return document[name]
    return None


def query_values(query: Mapping[str, Sequence[str]], key: str) -> tuple[str, ...]:
    """Values of a query parameter, trying every alias the table records.

    ``query`` is the output of :func:`urllib.parse.parse_qs`. Returns an empty
    tuple when no alias is present — callers turn that into a diagnostic.
    """
    for name in values(key):
        if name in query:
            return tuple(query[name])
    return ()


def profile_url(slug: str) -> str:
    """Canonical public profile URL for a slug, assembled from the table.

    Nothing else in the package may build one; that is what keeps the host and
    path in a single reviewable place.
    """
    return f"https://{value('url.host')}{value('url.profile_path_prefix')}{slug}"


def is_linkedin_host(host: str | None) -> bool:
    """True for the canonical host and any locale subdomain of it."""
    if not host:
        return False
    normalised = host.strip().casefold()
    suffix = value("url.host_suffix")
    return normalised == suffix or normalised.endswith("." + suffix)


def is_sensitive_header(name: str | None) -> bool:
    """True when a HAR header name marks the export as unsanitised."""
    if not name:
        return False
    return name.strip().casefold() in {v.casefold() for v in values("har.sensitive_header_names")}


def table_report() -> tuple[str, ...]:
    """One description line per entry, sorted. Used by the CLI and by docs."""
    return tuple(SCHEMA[k].describe() for k in keys())


def iter_entries() -> Iterable[SchemaEntry]:
    """Every entry, in sorted key order."""
    return (SCHEMA[k] for k in keys())


__all__ = [
    "SCHEMA",
    "SCHEMA_VERSION",
    "STALENESS_WARNING",
    "Confidence",
    "SchemaEntry",
    "SchemaKind",
    "UnknownSchemaKey",
    "describe",
    "dig",
    "entry",
    "field",
    "is_linkedin_host",
    "is_sensitive_header",
    "iter_entries",
    "keys",
    "path",
    "profile_url",
    "query_values",
    "table_report",
    "unverified_keys",
    "value",
    "values",
    "verified_keys",
]
