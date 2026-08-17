"""Hard constraints on the acquisition layer. These are the tests that must never
be quietly relaxed.

Two of them encode findings rather than preferences:

* **No session credential, anywhere.** The third-party tools that genuinely do
  return mutual connections achieve it by taking the user's LinkedIn session
  cookie and acting as them from a cloud server. That is credential surrender.
  This package must not contain the cookie's name, let alone a code path that
  reads it, and the name is assembled from fragments below so that this guard
  file is not itself a match.
* **One declarative table.** No LinkedIn URL shape, query-parameter name or JSON
  path may appear as a bare literal outside ``collect/schemas.py``. Nobody on this
  project could reach ``linkedin.com``, so all of these shapes are second-hand and
  may be stale; keeping them in one reviewable table is the only thing that makes
  a future breakage a one-line fix instead of an archaeology exercise.
"""

from __future__ import annotations

import ast
import socket
from pathlib import Path

import pytest

from interlayer.collect import registry, schemas
from interlayer.collect.har import HarCollector
from interlayer.collect.manual_csv import ManualCsvCollector

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "interlayer"
SCHEMA_FILE = PACKAGE_ROOT / "collect" / "schemas.py"

#: LinkedIn's session cookie key, assembled from fragments. Written this way so a
#: repository-wide grep for the token finds the package clean and finds this file
#: only as the guard, not as a use.
SESSION_COOKIE_TOKEN = "li" + "_" + "at"
OTHER_CREDENTIAL_TOKENS = ("JSESSIONID",)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_code_path_anywhere_references_the_session_cookie() -> None:
    """No adapter may accept, read, store or transmit a LinkedIn session cookie.

    Not "must not by default" — must not be possible. The name does not appear in
    the package at all, so there is nothing to enable, misconfigure or accidentally
    log.
    """
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT):
        text = path.read_text(encoding="utf-8").casefold()
        if SESSION_COOKIE_TOKEN in text:
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
        for token in OTHER_CREDENTIAL_TOKENS:
            if token.casefold() in text:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)} ({token})")
    assert offenders == [], (
        "credential identifiers found in the package: "
        + ", ".join(offenders)
        + ". Adapters that take the user's session are out of scope permanently."
    )


def test_unsanitised_input_is_refused_generically() -> None:
    """The refusal works on generic HTTP vocabulary, not on a credential name."""
    assert schemas.is_sensitive_header("Cookie")
    assert schemas.is_sensitive_header("Authorization")
    for value in schemas.values("har.sensitive_header_names"):
        assert SESSION_COOKIE_TOKEN not in value.casefold()


def _code_string_constants(path: Path) -> list[str]:
    """String literals in real code — docstrings and comments excluded.

    Comments never reach the AST; docstrings are dropped explicitly. What is left
    is exactly the set of literals a parser could be depending on.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
        # A bare string expression statement is an attribute docstring.
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            docstrings.add(id(node.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


#: Shapes that belong to LinkedIn and therefore belong in exactly one file.
FORBIDDEN_LITERALS = (
    "linkedin.com",
    "/voyager/",
    "/search/results/",
    "connectionOf",
    "facetNetwork",
    "searchDashClustersByAll",
    "navigationUrl",
    "entityUrn",
    "trackingUrn",
    "primarySubtitle",
    "memberDistance",
    "urn:li:",
    "DISTANCE_1",
    "queryId",
)


@pytest.mark.parametrize(
    "package", ["collect", "enrichment"], ids=["collect", "enrichment"]
)
def test_no_external_shape_is_hard_coded_outside_the_table(package: str) -> None:
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT / package):
        if path == SCHEMA_FILE:
            continue
        for literal in _code_string_constants(path):
            for forbidden in FORBIDDEN_LITERALS:
                if forbidden.casefold() in literal.casefold():
                    offenders.append(
                        f"{path.relative_to(PACKAGE_ROOT)}: {literal!r} contains "
                        f"{forbidden!r}"
                    )
    assert offenders == [], (
        "external shapes must live only in collect/schemas.py:\n" + "\n".join(offenders)
    )


def test_the_table_actually_contains_those_shapes() -> None:
    """The previous test is only meaningful if the shapes are declared somewhere."""
    declared = " ".join(
        value for entry in schemas.iter_entries() for value in entry.values
    )
    for forbidden in ("linkedin.com", "connectionOf", "searchDashClustersByAll", "urn:li:"):
        assert forbidden in declared


def test_no_collector_module_imports_a_network_library() -> None:
    """The file boundary is structural: there is no HTTP client to misuse."""
    banned = {"httpx", "requests", "urllib.request", "socket", "http.client", "aiohttp"}
    offenders: list[str] = []
    for path in _python_files(PACKAGE_ROOT / "collect"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {node.module or ""}
            else:
                continue
            hit = names & banned
            if hit:
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {sorted(hit)}")
    assert offenders == []


def test_collectors_run_with_the_socket_removed(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("no collector may open a socket")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    for spec in registry.specs():
        collector = spec.create()
        assert collector.compliance_posture is spec.posture


def test_shipped_collectors_are_the_two_offline_file_parsers() -> None:
    assert isinstance(registry.create("manual_csv"), ManualCsvCollector)
    assert isinstance(registry.create("har"), HarCollector)
