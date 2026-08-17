"""The offline HAR parser.

Every failure mode gets its own test, because the whole point of this parser is
that a shape change, an empty body and a genuinely empty result must be three
visibly different outcomes rather than one silent zero.
"""

from __future__ import annotations

import base64
import gzip
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from interlayer.collect import schemas
from interlayer.collect.har import HarCollector
from interlayer.collect.interface import CaptureStatus, Code, Severity, TargetRef
from interlayer.core.models import CompliancePosture, Firm

TARGET_OPAQUE = "ACoAAAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

#: The internal-API request the browser makes behind the rendered results page,
#: with the facets as ordinary query parameters.
SEARCH_URL = (
    "https://www.linkedin.com/voyager/api/search/dash/clusters"
    f"?connectionOf=%5B%22{TARGET_OPAQUE}%22%5D&network=%5B%22F%22%5D"
    "&origin=MEMBER_PROFILE_CANNED_SEARCH"
)
#: The GraphQL form, where the same facets are packed into one Rest.li blob.
GRAPHQL_URL = (
    "https://www.linkedin.com/voyager/api/graphql"
    "?variables=(start:0,origin:FACETED_SEARCH,query:(flagshipSearchIntent:SEARCH_SRP,"
    "queryParameters:List((key:resultType,value:List(PEOPLE)),"
    f"(key:connectionOf,value:List({TARGET_OPAQUE})),(key:network,value:List(F)))))"
    "&queryId=voyagerSearchDashClusters.994bf4e7d2173b92ccdb5935710c3c5d"
)
#: A page the user simply browsed. Not an API response; must be ignored.
PAGE_URL = "https://www.linkedin.com/search/results/people/?keywords=quant"


def entity(slug: str, name: str, headline: str = "Quant") -> dict[str, Any]:
    return {
        "entityUrn": f"urn:li:fsd_entityResultViewModel:(urn:li:fsd_profile:{slug}-urn,SEARCH)",
        "trackingUrn": f"urn:li:fsd_profile:{slug}-urn",
        "navigationUrl": f"https://www.linkedin.com/in/{slug}?miniProfileUrn=x",
        "title": {"text": name},
        "primarySubtitle": {"text": headline},
        "entityCustomTrackingInfo": {"memberDistance": "DISTANCE_1"},
    }


def clusters_body(entities: list[dict[str, Any]], **paging: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "data": {
            "searchDashClustersByAll": {
                "elements": [
                    {"items": [{"itemUnion": {"entityResult": item}} for item in entities]}
                ]
            }
        }
    }
    if paging:
        body["paging"] = paging
    return body


def har_entry(
    url: str,
    body: Any,
    *,
    status: int = 200,
    mime: str = "application/json",
    started: str = "2026-08-17T10:04:11Z",
    encoding: str | None = None,
    text: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"mimeType": mime}
    if text is not None:
        content["text"] = text
    elif body is not None:
        content["text"] = json.dumps(body)
    if encoding is not None:
        content["encoding"] = encoding
    entry: dict[str, Any] = {
        "startedDateTime": started,
        "request": {"method": "GET", "url": url},
        "response": {"status": status, "content": content},
    }
    if extra:
        entry.update(extra)
    return entry


def write_har(tmp_path: Path, entries: list[dict[str, Any]], name: str = "session.har") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(
            {"log": {"version": "1.2", "creator": {"name": "Chrome", "version": "140"}, "entries": entries}}
        ),
        encoding="utf-8",
    )
    return path


def resolver() -> dict[str, TargetRef]:
    return {
        TARGET_OPAQUE: TargetRef(
            name="Jordan Smith",
            profile_url="https://www.linkedin.com/in/jsmith-quant-9a1/",
            id="jsmith-quant-9a1",
            firm=Firm.JANE_STREET.value,
        )
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_parses_search_page_capture(tmp_path: Path) -> None:
    path = write_har(
        tmp_path,
        [har_entry(SEARCH_URL, clusters_body([entity("alexrivera", "Alex Rivera")]))],
    )
    result = HarCollector(targets=resolver()).collect(path)

    assert result.ok
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert edge.target_id == "jsmith-quant-9a1"
    assert edge.provenance is not None
    assert edge.provenance.source == "har"
    assert edge.provenance.posture is CompliancePosture.MANUAL_CAPTURE
    assert edge.provenance.truncated is False
    assert edge.provenance.collected_at.isoformat().startswith("2026-08-17T10:04:11")
    assert result.members[0].linkedin_slug == "alexrivera"


def test_parses_graphql_variables_blob(tmp_path: Path) -> None:
    """On the GraphQL endpoint the facets are packed inside one Rest.li blob."""
    path = write_har(
        tmp_path, [har_entry(GRAPHQL_URL, clusters_body([entity("danawu", "Dana Wu")]))]
    )
    result = HarCollector(targets=resolver()).collect(path)
    assert len(result.edges) == 1
    assert result.captures[0].network_filter == ("F",)


def test_legacy_facet_parameter_spelling_still_parses(tmp_path: Path) -> None:
    legacy = (
        "https://www.linkedin.com/voyager/api/search"
        f'?facetConnectionOf=%5B%22{TARGET_OPAQUE}%22%5D&facetNetwork=%5B%22F%22%5D'
    )
    path = write_har(tmp_path, [har_entry(legacy, clusters_body([entity("a", "A B")]))])
    assert len(HarCollector(targets=resolver()).collect(path).edges) == 1


def test_base64_and_gzip_bodies(tmp_path: Path) -> None:
    payload = json.dumps(clusters_body([entity("alexrivera", "Alex Rivera")]))
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    gz64 = base64.b64encode(gzip.compress(payload.encode("utf-8"))).decode("ascii")

    for text in (b64, gz64):
        path = write_har(
            tmp_path,
            [har_entry(SEARCH_URL, None, encoding="base64", text=text)],
            name=f"{len(text)}.har",
        )
        result = HarCollector(targets=resolver()).collect(path)
        assert len(result.edges) == 1, result.diagnostics


def test_pages_merge_and_report_truncation(tmp_path: Path) -> None:
    page1 = har_entry(
        SEARCH_URL,
        clusters_body([entity("a", "A One"), entity("b", "B Two")], start=0, count=2, total=5),
    )
    page2 = har_entry(
        SEARCH_URL,
        clusters_body([entity("c", "C Three")], start=2, count=2, total=5),
        started="2026-08-17T10:05:11Z",
    )
    result = HarCollector(targets=resolver()).collect(write_har(tmp_path, [page1, page2]))

    assert len(result.edges) == 3
    capture = result.captures[0]
    assert capture.status is CaptureStatus.TRUNCATED
    assert capture.reported_count == 5
    assert capture.pages_seen == (1, 2)
    assert all(edge.provenance is not None and edge.provenance.truncated for edge in result.edges)
    assert result.truncated_target_ids == ("jsmith-quant-9a1",)


def test_included_array_fallback(tmp_path: Path) -> None:
    body = {
        "included": [
            {
                "$type": "com.linkedin.voyager.dash.search.EntityResultViewModel",
                **entity("alexrivera", "Alex Rivera"),
            },
            {"$type": "com.linkedin.voyager.dash.common.Paging", "total": 1},
        ]
    }
    result = HarCollector(targets=resolver()).collect(
        write_har(tmp_path, [har_entry(SEARCH_URL, body)])
    )
    assert len(result.edges) == 1
    assert result.captures[0].parser_strategy == "fastpath:json.included"


def test_recursive_fallback_recovers_and_warns(tmp_path: Path) -> None:
    """A renamed wrapper must degrade to shape-matching, loudly."""
    body = {"data": {"searchDashSomethingRenamed": {"stuff": [entity("alexrivera", "Alex Rivera")]}}}
    result = HarCollector(targets=resolver()).collect(
        write_har(tmp_path, [har_entry(SEARCH_URL, body)])
    )
    assert len(result.edges) == 1
    assert result.captures[0].parser_strategy == "fallback:recursive"
    warned = [d for d in result.diagnostics if d.code is Code.VOYAGER_FALLBACK_USED]
    assert warned and warned[0].schema_key == "json.search_clusters"


def test_target_is_dropped_from_its_own_mutual_list(tmp_path: Path) -> None:
    body = clusters_body(
        [entity("alexrivera", "Alex Rivera"), entity("jsmith-quant-9a1", "Jordan Smith")]
    )
    result = HarCollector(targets=resolver()).collect(
        write_har(tmp_path, [har_entry(SEARCH_URL, body)])
    )
    assert len(result.edges) == 1
    assert any(d.code is Code.TARGET_IN_OWN_MUTUALS for d in result.diagnostics)


# ---------------------------------------------------------------------------
# Failure modes — each must be distinguishable from "no mutual connections"
# ---------------------------------------------------------------------------


def test_unrecognised_shape_is_an_error_naming_the_schema_entry(tmp_path: Path) -> None:
    result = HarCollector(targets=resolver()).collect(
        write_har(tmp_path, [har_entry(SEARCH_URL, {"data": {"somethingElse": {}}})])
    )
    assert result.edges == ()
    problems = [d for d in result.diagnostics if d.code is Code.VOYAGER_SHAPE_UNRECOGNISED]
    assert problems, [d.code for d in result.diagnostics]
    problem = problems[0]
    assert problem.severity is Severity.ERROR
    assert problem.schema_key == "json.search_clusters"
    assert "does NOT mean the target has no mutual connections" in problem.message
    assert "collect/schemas.py" in (problem.remedy or "")


def test_missing_response_body_is_reported_as_a_capture_problem(tmp_path: Path) -> None:
    path = write_har(tmp_path, [har_entry(SEARCH_URL, None)])
    result = HarCollector(targets=resolver()).collect(path)
    diag = [d for d in result.diagnostics if d.code is Code.HAR_NO_RESPONSE_BODY]
    assert diag and diag[0].severity is Severity.ERROR
    assert "Preserve log" in (diag[0].remedy or "")


def test_non_json_body_is_its_own_diagnostic(tmp_path: Path) -> None:
    path = write_har(tmp_path, [har_entry(SEARCH_URL, None, text="<html>nope</html>")])
    result = HarCollector(targets=resolver()).collect(path)
    assert any(d.code is Code.HAR_BODY_NOT_JSON for d in result.diagnostics)


def test_bad_base64_body_is_its_own_diagnostic(tmp_path: Path) -> None:
    path = write_har(
        tmp_path, [har_entry(SEARCH_URL, None, encoding="base64", text="!!!not base64!!!")]
    )
    result = HarCollector(targets=resolver()).collect(path)
    codes = {d.code for d in result.diagnostics}
    assert Code.HAR_BODY_NOT_DECODABLE in codes or Code.HAR_BODY_NOT_JSON in codes


def test_unrestricted_capture_is_rejected(tmp_path: Path) -> None:
    """connectionOf without the first-degree restriction reaches past the mutuals."""
    url = f"https://www.linkedin.com/voyager/api/search?connectionOf=%5B%22{TARGET_OPAQUE}%22%5D"
    result = HarCollector(targets=resolver()).collect(
        write_har(tmp_path, [har_entry(url, clusters_body([entity("a", "A B")]))])
    )
    assert result.edges == ()
    diag = [d for d in result.diagnostics if d.code is Code.VOYAGER_NETWORK_FILTER_MISSING]
    assert diag and diag[0].schema_key == "param.network"


def test_unsanitised_har_is_refused_before_parsing(tmp_path: Path) -> None:
    good = har_entry(SEARCH_URL, clusters_body([entity("alexrivera", "Alex Rivera")]))
    dirty = har_entry(
        SEARCH_URL,
        clusters_body([entity("danawu", "Dana Wu")]),
        extra={"request": {"method": "GET", "url": SEARCH_URL, "cookies": [{"name": "x", "value": "y"}]}},
    )
    result = HarCollector(targets=resolver()).collect(write_har(tmp_path, [good, dirty]))

    assert not result.ok
    assert result.edges == ()
    assert result.members == ()
    fatal = result.of_severity(Severity.FATAL)[0]
    assert fatal.code is Code.HAR_UNSANITISED
    assert "re-export" in (fatal.remedy or "")


def test_sensitive_headers_also_refuse_the_file(tmp_path: Path) -> None:
    dirty = har_entry(SEARCH_URL, clusters_body([entity("a", "A B")]))
    dirty["request"]["headers"] = [{"name": "Cookie", "value": "redacted"}]
    result = HarCollector(targets=resolver()).collect(write_har(tmp_path, [dirty]))
    assert result.of_severity(Severity.FATAL)[0].code is Code.HAR_UNSANITISED


def test_har_with_no_api_traffic_says_so(tmp_path: Path) -> None:
    entry = har_entry("https://example.test/page", None, mime="text/html")
    result = HarCollector(targets=resolver()).collect(write_har(tmp_path, [entry]))
    diag = [d for d in result.diagnostics if d.code is Code.HAR_NO_MATCHING_ENTRIES]
    assert diag and "not the same as finding no mutual connections" in diag[0].message


def test_not_json_at_all_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "broken.har"
    path.write_text("this is not json", encoding="utf-8")
    result = HarCollector().collect(path)
    assert result.of_severity(Severity.FATAL)[0].code is Code.INPUT_NOT_JSON


def test_json_without_entries_is_fatal(tmp_path: Path) -> None:
    path = tmp_path / "other.har"
    path.write_text(json.dumps({"not": "a har"}), encoding="utf-8")
    result = HarCollector().collect(path)
    fatal = result.of_severity(Severity.FATAL)[0]
    assert fatal.code is Code.HAR_MISSING_ENTRIES
    assert fatal.schema_key == "har.entries"


def test_missing_file_is_fatal(tmp_path: Path) -> None:
    result = HarCollector().collect(tmp_path / "absent.har")
    assert result.of_severity(Severity.FATAL)[0].code is Code.INPUT_MISSING


# ---------------------------------------------------------------------------
# Target resolution and posture
# ---------------------------------------------------------------------------


def test_unresolved_target_reports_rather_than_inventing_one(tmp_path: Path) -> None:
    path = write_har(tmp_path, [har_entry(SEARCH_URL, clusters_body([entity("a", "A B")]))])
    result = HarCollector().collect(path)
    assert result.edges == ()
    assert result.targets == ()
    assert any(d.code is Code.TARGET_UNRESOLVED for d in result.diagnostics)


def test_default_firm_lets_a_bare_har_produce_edges(tmp_path: Path) -> None:
    path = write_har(tmp_path, [har_entry(SEARCH_URL, clusters_body([entity("a", "A B")]))])
    result = HarCollector(default_firm=Firm.CITADEL_SECURITIES).collect(path)
    assert len(result.edges) == 1
    assert result.targets[0].firm is Firm.CITADEL_SECURITIES
    assert result.targets[0].harvested is True


def test_accepts_sniffs_without_parsing(tmp_path: Path) -> None:
    collector = HarCollector()
    good = write_har(tmp_path, [har_entry(SEARCH_URL, clusters_body([]))])
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    assert collector.accepts(good)
    assert not collector.accepts(bad)


def test_empty_result_is_recorded_as_observed_empty(tmp_path: Path) -> None:
    path = write_har(tmp_path, [har_entry(SEARCH_URL, clusters_body([], total=0, start=0, count=10))])
    result = HarCollector(targets=resolver()).collect(path)
    assert result.captures[0].status is CaptureStatus.EMPTY
    assert result.coverage_states()["observed_empty"] == 1
    assert result.edges == ()


def test_parser_makes_no_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file boundary is the architecture. Prove it with the socket removed."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the HAR collector must never open a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)

    path = write_har(
        tmp_path, [har_entry(SEARCH_URL, clusters_body([entity("alexrivera", "Alex Rivera")]))]
    )
    assert len(HarCollector(targets=resolver()).collect(path).edges) == 1


def test_collector_declares_manual_capture() -> None:
    assert HarCollector().compliance_posture is CompliancePosture.MANUAL_CAPTURE
    assert HarCollector().version


def test_only_schema_declared_urls_are_used() -> None:
    """Sanity: the fixtures above exercise the shapes the table declares."""
    assert schemas.value("url.voyager_api_marker") in SEARCH_URL
    assert schemas.value("url.voyager_graphql_path") in GRAPHQL_URL
    assert schemas.value("url.people_search_path") in PAGE_URL
