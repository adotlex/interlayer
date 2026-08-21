"""Privacy guardrails: egress, e-mail handling, artifact permissions, retention.

This stage handles identifiable data about real third parties who never agreed to
be analysed, so the tests here are written to *try to make it leak* rather than to
confirm that it does not. Three properties are load-bearing and each is checked
in a way that fails loudly rather than vacuously:

* **Offline by construction.** The socket guard in ``conftest`` is asserted to be
  armed *before* the pipeline is run, so a green ``test_no_network_egress`` cannot
  mean "the guard silently stopped working".
* **Imports, not grep.** The ban on network libraries is enforced by walking the
  AST of every module under ``src/interlayer``. A commented-out import is
  harmless; ``if False: import requests`` is not, and only a parser sees the
  difference.
* **Permissions asserted, never assumed.** Every permission test runs under
  ``os.umask(0)``, which is the only way to tell an explicit ``chmod`` apart from
  a mode inherited from a conveniently strict ambient umask.

Research references are ``docs/research/05-privacy-compliance.md`` §3.1-§3.5
(P-1 … P-28) and its §6 test list.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from interlayer import io as interlayer_io
from interlayer.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "interlayer"

STAGES: tuple[str, ...] = (
    "ingest",
    "normalize",
    "enrich",
    "graph",
    "cluster",
    "score",
    "report",
)

#: P-2. ``urllib`` is on the list as a whole: ``urllib.parse`` is inert, but the
#: rule is deliberately absolute so that nobody has to adjudicate submodules in a
#: code review, and ``urllib.request`` is one character away from ``urllib.parse``.
BANNED_IMPORTS: frozenset[str] = frozenset(
    {
        "aiohttp",
        "http",
        "httpx",
        "playwright",
        "requests",
        "selenium",
        "socket",
        "socketserver",
        "ssl",
        "telnetlib",
        "urllib",
        "urllib3",
        "webbrowser",
        "xmlrpc",
    }
)

#: The single module P-2 permits to hold network code. It does not exist today,
#: which makes the rule absolute; the constant is here so the exemption is
#: explicit rather than a thing somebody has to remember.
QUARANTINED_MODULE = SRC_ROOT / "net.py"

#: P-3: telemetry, analytics, crash reporting and update checks must be *absent*,
#: not merely off. Vendor names and idioms only -- deliberately no generic words
#: like "segment" or "track", which collide with ordinary local variables.
TELEMETRY_MARKERS: tuple[str, ...] = (
    "amplitude",
    "analytics",
    "appcenter",
    "bugsnag",
    "crashlytics",
    "datadog",
    "google-analytics",
    "gtag(",
    "honeycomb.io",
    "mixpanel",
    "newrelic",
    "opentelemetry",
    "phone_home",
    "phonehome",
    "posthog",
    "rollbar",
    "sentry_sdk",
    "sentry-sdk",
    "statsd",
    "telemetry",
    "usage_stats",
)

#: P-33: no code that drives a browser or handles a LinkedIn session.
SCRAPER_MARKERS: tuple[str, ...] = (
    "chromedriver",
    "geckodriver",
    "headless_browser",
    "li_at",
    "playwright",
    "puppeteer",
    "selenium",
    "session_cookie",
    "webdriver",
)

#: Hostname-shaped tokens allowed to appear in a source string literal. The
#: pipeline canonicalises LinkedIn profile URLs, so it must know that hostname;
#: anything else in a literal is an endpoint and endpoints are what P-3 forbids.
_HOST_TOKEN = re.compile(r"\b(?:[\w-]+\.)+(?:com|io|net|org|dev|ai|co|cn|app|cloud)\b", re.I)
_ALLOWED_HOSTS = frozenset({"linkedin.com", "linkedin.cn", "www.linkedin.com"})

FIRST_NAMES = ("Ada", "Alan", "Grace", "Edsger", "Barbara", "Donald", "Ken", "Frances")
LAST_NAMES = ("Lovelace", "Turing", "Hopper", "Dijkstra", "Liskov", "Knuth", "Allen", "Bartik")
COMPANIES = ("Jane Street", "Citadel Securities", "Citadel LLC", "Acme Widgets", "Globex Trading")

CSV_HEADER = "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _person(index: int) -> tuple[str, str, str, str, str]:
    """One synthetic connection: first, last, slug, e-mail, company.

    Every value is invented. ``example.com`` is reserved by RFC 2606 precisely so
    that a fixture cannot accidentally address a real person (P-27).
    """
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = f"{LAST_NAMES[(index * 3) % len(LAST_NAMES)]}{index}"
    slug = f"{first.lower()}-{last.lower()}"
    return first, last, slug, f"{slug}@example.com", COMPANIES[index % len(COMPANIES)]


def write_export(path: Path, count: int = 24, *, preamble: bool = True) -> Path:
    """Write a synthetic ``Connections.csv`` with an e-mail on every row."""
    head = '"Notes:"\n"Some connections have no e-mail address."\n\n' if preamble else ""
    rows = []
    for i in range(count):
        first, last, slug, email, company = _person(i)
        rows.append(
            f"{first},{last},https://www.linkedin.com/in/{slug},{email},"
            f"{company},Engineer,{(i % 28) + 1:02d} Mar 2021"
        )
    path.write_text(head + CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def export_identifiers(count: int = 24) -> tuple[list[str], list[str]]:
    """``(names, emails)`` matching :func:`write_export` for leak scanning."""
    names: list[str] = []
    emails: list[str] = []
    for i in range(count):
        first, last, slug, email, _company = _person(i)
        names += [first, last, f"{first} {last}", slug]
        emails.append(email)
    return names, emails


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    """Settings pointed at a throwaway tree, with an absolute gazetteer path.

    Absolute on purpose: several tests ``chdir`` away from the repository to prove
    that nothing is written relative to the working directory, and a relative
    gazetteer would turn that into an unrelated failure.
    """
    return Settings(
        artifact_dir=tmp_path / "artifacts",
        gazetteer=REPO_ROOT / "data" / "gazetteer" / "firms.yaml",
        consensus_runs=3,
        **overrides,  # type: ignore[arg-type]
    )


def run_pipeline(cfg: Settings) -> None:
    """Run every stage in order, exactly as ``interlayer run`` does."""
    for name in STAGES:
        importlib.import_module(f"interlayer.{name}").run(cfg)


def source_modules() -> list[Path]:
    """Every Python module under ``src/interlayer``, excluding the quarantine."""
    return [p for p in sorted(SRC_ROOT.rglob("*.py")) if p != QUARANTINED_MODULE]


def imported_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Top-level module names an import statement pulls in."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if node.level:  # relative import: cannot reach a stdlib/third-party name
        return []
    return [(node.module or "").split(".")[0]]


def module_scope_imports(tree: ast.Module) -> Iterator[ast.Import | ast.ImportFrom]:
    """Imports executed when the module is imported (module body, incl. ``if``/``try``)."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node
        elif isinstance(node, ast.If | ast.Try | ast.With):
            stack.extend(ast.iter_child_nodes(node))


def docstring_nodes(tree: ast.Module) -> set[int]:
    """Identity of every docstring constant, so prose is exempt from literal scans."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def code_text(path: Path) -> str:
    """Everything in a module that *executes*, lowercased, with prose removed.

    Docstrings and comments are excluded deliberately. ``enrich/__init__.py``
    promises in prose that it never imports ``selenium`` or ``playwright``, and a
    marker scan that cannot tell a promise from a call would read that sentence
    as the very violation it rules out.
    """
    if path.suffix == ".j2":
        stripped = re.sub(r"\{#.*?#\}", " ", path.read_text(encoding="utf-8"), flags=re.S)
        return re.sub(r"<!--.*?-->", " ", stripped, flags=re.S).lower()

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs = docstring_nodes(tree)
    chunks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docs:
                chunks.append(node.value)
        elif isinstance(node, ast.Name):
            chunks.append(node.id)
        elif isinstance(node, ast.Attribute):
            chunks.append(node.attr)
        elif isinstance(node, ast.alias):
            chunks.append(node.name)
            chunks.append(node.asname or "")
        elif isinstance(node, ast.ImportFrom):
            chunks.append(node.module or "")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            chunks.append(node.name)
        elif isinstance(node, ast.keyword):
            chunks.append(node.arg or "")
    return "\n".join(chunks).lower()


def artifact_files(root: Path) -> list[Path]:
    return [p for p in sorted(root.rglob("*")) if p.is_file()]


@pytest.fixture
def permissive_umask() -> Iterator[int]:
    """Run the body under ``umask(0)``.

    Appendix A of the research doc: a plain ``open()`` produces ``0o666`` under a
    permissive umask, and ``mkdir(mode=…, exist_ok=True)`` never repairs an
    existing directory. Both failures are invisible at the default ``022``, so a
    permission test that does not set the umask proves nothing.
    """
    previous = os.umask(0)
    try:
        yield previous
    finally:
        os.umask(previous)


# ===========================================================================
# network egress  (P-1 … P-6)
# ===========================================================================


def test_socket_guard_is_actually_armed() -> None:
    """The autouse guard must be live, or every egress test below is vacuous."""
    import socket

    with pytest.raises(Exception, match="network access attempted"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(Exception, match="network access attempted"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.01)


def test_no_network_egress(tmp_path: Path) -> None:
    """P-1: the full pipeline runs to completion with the socket layer disabled."""
    write_export(tmp_path / "Connections.csv")
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")

    run_pipeline(cfg)

    assert cfg.report_path.is_file(), "the pipeline produced no report"
    assert cfg.manifest_path.is_file(), "the pipeline produced no manifest"
    assert cfg.report_path.stat().st_size > 0


def test_no_network_egress_in_redacted_mode(tmp_path: Path) -> None:
    """Redacted mode adds a key file and a scrubber; neither may reach for a socket."""
    write_export(tmp_path / "Connections.csv")
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv", redact=True)

    run_pipeline(cfg)

    assert cfg.report_path.is_file()


def test_core_imports_no_network_libs() -> None:
    """P-2: no module under ``src/interlayer`` imports a network library at module scope.

    AST-based rather than textual: a name inside a docstring or a comment is
    inert, and the only thing that matters is whether the import statement runs.
    """
    offences: list[str] = []
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in module_scope_imports(tree):
            for name in imported_names(node):
                if name in BANNED_IMPORTS:
                    rel = path.relative_to(REPO_ROOT)
                    offences.append(f"{rel}:{node.lineno} imports {name!r}")

    assert not offences, (
        "network libraries imported at module scope in the core pipeline (P-2):\n  "
        + "\n  ".join(sorted(offences))
    )


def test_no_network_libs_imported_in_nested_scopes() -> None:
    """A deferred import is still an import. Function-level imports are scanned too."""
    offences: list[str] = []
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_level = {id(n) for n in module_scope_imports(tree)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import | ast.ImportFrom) or id(node) in module_level:
                continue
            for name in imported_names(node):
                if name in BANNED_IMPORTS:
                    rel = path.relative_to(REPO_ROOT)
                    offences.append(f"{rel}:{node.lineno} imports {name!r} at nested scope")

    assert not offences, "\n  ".join(["deferred network imports found:", *sorted(offences)])


def test_no_dynamic_import_of_network_libs() -> None:
    """``importlib.import_module("requests")`` would defeat a naive import scan."""
    offences: list[str] = []
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name not in {"import_module", "__import__", "find_spec", "load_module"}:
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and arg.value.split(".")[0] in BANNED_IMPORTS
                ):
                    rel = path.relative_to(REPO_ROOT)
                    offences.append(f"{rel}:{node.lineno} dynamically imports {arg.value!r}")

    assert not offences, "\n  ".join(["dynamic network imports found:", *sorted(offences)])


def test_no_telemetry_endpoints() -> None:
    """P-3: no telemetry, analytics, crash reporting or update check exists at all."""
    offences: list[str] = []
    for path in [*source_modules(), *sorted(SRC_ROOT.rglob("*.j2"))]:
        blob = code_text(path)
        for marker in TELEMETRY_MARKERS:
            if marker in blob:
                offences.append(f"{path.relative_to(REPO_ROOT)} mentions {marker!r}")

    assert not offences, "\n  ".join(["telemetry markers found in the package:", *offences])


def test_no_hardcoded_endpoints_in_string_literals() -> None:
    """The only hostname a literal may name is LinkedIn's, for URL canonicalisation.

    Docstrings are exempt: they are prose about the domain, not an address the
    code can dial. Everything else that looks like a host is treated as an
    endpoint, which is exactly what P-3 says must not exist.
    """
    offences: list[str] = []
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docs = docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in docs:
                continue
            if not isinstance(node.value, str):
                continue
            for host in _HOST_TOKEN.findall(node.value):
                if host.lower().removeprefix("www.") not in _ALLOWED_HOSTS:
                    rel = path.relative_to(REPO_ROOT)
                    offences.append(f"{rel}:{node.lineno} names host {host!r}")

    assert not offences, "\n  ".join(["hardcoded endpoints found:", *sorted(offences)])


def test_no_scraper_or_browser_automation_in_tree() -> None:
    """P-33: no browser driver, no LinkedIn session cookie handling, anywhere."""
    offences: list[str] = []
    for path in [*source_modules(), *sorted(SRC_ROOT.rglob("*.j2"))]:
        blob = code_text(path)
        for marker in SCRAPER_MARKERS:
            if marker in blob:
                offences.append(f"{path.relative_to(REPO_ROOT)} mentions {marker!r}")

    assert not offences, "\n  ".join(["scraper markers found in the package:", *offences])


def test_adapters_declare_no_network(tmp_path: Path) -> None:
    """P-32: the adapter boundary runs to completion with the socket layer down.

    There is no ``DataSourceAdapter`` ABC in the tree yet, so the boundary is the
    ``enrich`` stage: it is the single documented doorway that hand-collected
    Tier 2 data comes through. Any class named ``…Adapter`` that appears later is
    held to the same import rule automatically.
    """
    for path in source_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        adapters = [
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name.endswith("Adapter")
        ]
        if not adapters:
            continue
        banned = {
            name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            for name in imported_names(node)
            if name in BANNED_IMPORTS
        }
        assert not banned, f"{path.name} defines {adapters} beside network imports {banned}"

    cfg = settings_for(tmp_path)
    importlib.import_module("interlayer.enrich").run(cfg)

    assert cfg.observations_path.is_file(), "the adapter boundary produced no artifact"
    assert cfg.targets_path.is_file()


# ===========================================================================
# e-mail handling  (P-7 … P-11)
# ===========================================================================


def test_emails_dropped_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """P-7: with the default settings no artifact byte stream contains an address."""
    write_export(tmp_path / "Connections.csv")
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")
    assert cfg.keep_emails is False, "the default must be drop"

    run_pipeline(cfg)

    _names, emails = export_identifiers()
    leaked: list[str] = []
    for artifact in artifact_files(cfg.artifact_dir):
        blob = artifact.read_bytes()
        if b"@" in blob:
            for email in emails:
                if email.encode("utf-8") in blob:
                    leaked.append(f"{artifact.name} contains {email}")
            local_part = emails[0].split("@")[0].encode("utf-8")
            if local_part + b"@" in blob:
                leaked.append(f"{artifact.name} contains a bare local part")
    assert not leaked, "\n  ".join(["e-mail addresses reached an artifact:", *leaked])

    logged = capsys.readouterr()
    assert "@example.com" not in logged.err, "an address was echoed to the log (P-24)"
    assert "@example.com" not in logged.out


def test_emails_never_stored_in_plaintext(tmp_path: Path) -> None:
    """P-8: retention means a keyed digest. There is no mode that stores an address.

    ``Settings`` offers ``keep_emails`` + ``email_hmac_key`` and refuses the first
    without the second, so the research doc's ``--emails=keep`` (plaintext) mode
    simply does not exist. That is stricter than the requirement, and the property
    worth testing is the one that matters: plaintext never lands on disk.
    """
    write_export(tmp_path / "Connections.csv", count=6)
    cfg = settings_for(
        tmp_path,
        input_csv=tmp_path / "Connections.csv",
        keep_emails=True,
        email_hmac_key="a-local-secret",
    )

    importlib.import_module("interlayer.ingest").run(cfg)

    _names, emails = export_identifiers(6)
    blob = cfg.people_path.read_bytes()
    for email in emails:
        assert email.encode("utf-8") not in blob, f"plaintext {email} written to people.jsonl"
    assert b"@example.com" not in blob


def test_keep_emails_requires_a_key() -> None:
    """A hash mode with no key would silently degrade to a bare digest."""
    with pytest.raises(Exception, match="email_hmac_key"):
        Settings(keep_emails=True)


def test_email_hash_is_keyed_not_bare_sha256() -> None:
    """P-8: a bare digest is reversible by dictionary attack over a known corpus."""
    address = "alice@example.com"
    keyed = interlayer_io.hash_email(address, "a-local-secret")
    bare = hashlib.sha256(address.encode("utf-8")).hexdigest()

    assert keyed != bare
    assert len(keyed) == 64


def test_emails_hashed_when_requested_differ_across_installs() -> None:
    """P-8/P-13: two installs must not be able to join their datasets on a digest."""
    address = "alice@example.com"

    assert interlayer_io.hash_email(address, "key-one") != interlayer_io.hash_email(
        address, "key-two"
    )
    assert interlayer_io.hash_email(address, "key-one") == interlayer_io.hash_email(
        address, "key-one"
    ), "the digest must be stable for a fixed key"


def test_email_normalisation_does_not_strip_plus_tags() -> None:
    """P-9: folding ``a+x@`` into ``a@`` is deliberate cross-identity linkage."""
    key = "a-local-secret"

    assert interlayer_io.hash_email("a+x@example.com", key) != interlayer_io.hash_email(
        "a@example.com", key
    )
    assert interlayer_io.hash_email("a.b@example.com", key) != interlayer_io.hash_email(
        "ab@example.com", key
    ), "Gmail dot-stripping links two identities the operator was never given"


def test_email_normalisation_folds_case_and_whitespace_only() -> None:
    """P-9: ``strip()`` + ``casefold()`` is the whole normalisation, and it is enough."""
    key = "a-local-secret"
    canonical = interlayer_io.hash_email("alice@example.com", key)

    assert interlayer_io.hash_email("  Alice@Example.COM  ", key) == canonical


# ===========================================================================
# storage, permissions, retention  (P-18 … P-23)
# ===========================================================================


def test_artifact_dir_permissions_0700(tmp_path: Path, permissive_umask: int) -> None:
    """P-19, under ``umask(0)`` so an inherited mode cannot pass for an explicit one."""
    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")

    run_pipeline(cfg)

    mode = stat.S_IMODE(cfg.artifact_dir.stat().st_mode)
    assert mode == 0o700, f"artifact dir is {mode:o}, expected 700"


def test_artifact_files_permissions_0600(tmp_path: Path, permissive_umask: int) -> None:
    """P-19: every file the pipeline writes is owner-only, report and manifest included."""
    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv", redact=True)

    run_pipeline(cfg)

    wrong = {
        str(p.relative_to(cfg.artifact_dir)): oct(stat.S_IMODE(p.stat().st_mode))
        for p in artifact_files(cfg.artifact_dir)
        if stat.S_IMODE(p.stat().st_mode) != 0o600
    }
    assert not wrong, f"artifact files with the wrong mode: {wrong}"
    assert len(artifact_files(cfg.artifact_dir)) >= 5, "suspiciously few artifacts to check"


def test_permissions_enforced_under_permissive_umask(tmp_path: Path) -> None:
    """The named §6 test, with the umask set inside the test rather than by a fixture.

    ``umask(0)`` is what separates "the code called chmod" from "the ambient umask
    happened to be strict". Restored in a ``finally`` because the umask is
    process-global and leaking it would silently loosen every later test.
    """
    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv", redact=True)

    previous = os.umask(0)
    try:
        run_pipeline(cfg)
        probe = tmp_path / "control.txt"
        probe.write_text("x", encoding="utf-8")
        control = stat.S_IMODE(probe.stat().st_mode)
    finally:
        os.umask(previous)

    assert control == 0o666, (
        f"the umask was not permissive during the run (a plain write gave {control:o}), "
        "so this test proves nothing about explicit chmod calls"
    )
    assert stat.S_IMODE(cfg.artifact_dir.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in artifact_files(cfg.artifact_dir))


def test_permissions_fixed_on_preexisting_0755_dir(tmp_path: Path, permissive_umask: int) -> None:
    """Appendix A: ``mkdir(mode=…, exist_ok=True)`` silently leaves a wrong mode alone."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o755)
    artifacts.chmod(0o755)
    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o755

    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")
    run_pipeline(cfg)

    assert stat.S_IMODE(artifacts.stat().st_mode) == 0o700, "a pre-existing 0755 dir was left alone"


def test_permissions_fixed_on_preexisting_0644_file(tmp_path: Path, permissive_umask: int) -> None:
    """Appendix A: ``touch(mode=…)`` does not repair an existing file either."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    stale = artifacts / "people.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    stale.chmod(0o644)

    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")
    run_pipeline(cfg)

    mode = stat.S_IMODE(stale.stat().st_mode)
    assert mode == 0o600, f"a pre-existing 0644 artifact stayed {mode:o}"


def test_state_key_file_permissions_0600(tmp_path: Path, permissive_umask: int) -> None:
    """P-13: the pseudonym key is the one file whose disclosure unmasks every id."""
    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv", redact=True)

    run_pipeline(cfg)

    key_path = cfg.artifact_dir.parent / ".interlayer" / "pseudonym.key"
    assert key_path.is_file(), "redacted mode did not create a pseudonym key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700


def test_atomic_write_never_leaves_a_broadly_readable_file(
    tmp_path: Path, permissive_umask: int
) -> None:
    """P-19: 0600 *at every instant*, not merely once the write has finished.

    The window that matters is between creating the temporary file and renaming
    it into place, so the modes of everything in the directory are sampled at
    both boundaries rather than only after the fact.
    """
    target = tmp_path / "artifacts" / "people.jsonl"
    observed: list[tuple[str, str, int]] = []

    def snapshot(where: str) -> None:
        parent = target.parent
        if not parent.is_dir():
            return
        for child in sorted(parent.iterdir()):
            if child.is_file():
                observed.append((where, child.name, stat.S_IMODE(child.stat().st_mode)))

    real_chmod = Path.chmod
    real_replace = Path.replace

    def chmod(self: Path, mode: int, **kwargs: object) -> None:
        snapshot("before-chmod")
        real_chmod(self, mode, **kwargs)  # type: ignore[arg-type]

    def replace(self: Path, other: str | Path) -> Path:
        snapshot("before-replace")
        return real_replace(self, other)

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(Path, "chmod", chmod)
        monkey.setattr(Path, "replace", replace)
        interlayer_io.secure_write(target, "x" * 8192)
    finally:
        monkey.undo()

    assert observed, "the write produced no intermediate state to inspect"
    broad = [entry for entry in observed if entry[2] != 0o600]
    assert not broad, f"an intermediate file was readable by others: {broad}"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_nothing_personal_is_written_outside_the_artifact_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-18: one documented root. A stray relative write would land in the cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(home)
    write_export(home / "Connections.csv")
    cfg = Settings(
        artifact_dir=home / "artifacts",
        gazetteer=REPO_ROOT / "data" / "gazetteer" / "firms.yaml",
        input_csv=home / "Connections.csv",
        consensus_runs=3,
        redact=True,
    )

    run_pipeline(cfg)

    names, emails = export_identifiers()
    outside = [
        p
        for p in artifact_files(home)
        if cfg.artifact_dir not in p.parents and p.name != "Connections.csv"
    ]
    assert {p.relative_to(home).as_posix() for p in outside} <= {".interlayer/pseudonym.key"}, (
        f"unexpected files written outside the artifact dir: {outside}"
    )
    for path in outside:
        blob = path.read_bytes()
        for token in [*names, *emails]:
            assert token.encode("utf-8") not in blob, f"{path} contains {token!r}"


def test_logs_contain_no_personal_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """P-24: diagnostics carry counts and line numbers, never row contents.

    A log file outlives the artifact directory and survives ``purge``. An
    exception handler that interpolates a row into its message writes a name
    somewhere the erasure command will never look.
    """
    write_export(tmp_path / "Connections.csv")
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")

    run_pipeline(cfg)

    captured = capsys.readouterr()
    stream = captured.out + captured.err
    assert stream.strip(), "the run logged nothing at all, so this proves nothing"

    names, emails = export_identifiers()
    leaked = [token for token in [*names, *emails] if token in stream]
    assert not leaked, f"the run log echoed personal data: {sorted(set(leaked))[:5]}"
    assert "linkedin.com/in/" not in stream, "a profile URL was logged"


def test_purge_removes_all_artifacts(tmp_path: Path) -> None:
    """P-21: ``purge`` is the implementable core of the right to erasure."""
    from typer.testing import CliRunner

    from interlayer.cli import app

    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")
    run_pipeline(cfg)
    assert artifact_files(cfg.artifact_dir), "nothing to purge -- the fixture is broken"

    result = CliRunner().invoke(app, ["purge", "--artifact-dir", str(cfg.artifact_dir), "--yes"])

    assert result.exit_code == 0, result.output
    assert not cfg.artifact_dir.exists(), "purge left the artifact directory behind"
    assert artifact_files(tmp_path) == [tmp_path / "Connections.csv"], (
        "purge left personal data somewhere else in the tree"
    )


def test_purge_does_not_touch_the_gazetteer(tmp_path: Path) -> None:
    """P-21: the curated firm list is not personal data and must survive a purge."""
    from typer.testing import CliRunner

    from interlayer.cli import app

    gazetteer = REPO_ROOT / "data" / "gazetteer" / "firms.yaml"
    before = gazetteer.read_bytes()
    write_export(tmp_path / "Connections.csv", count=8)
    cfg = settings_for(tmp_path, input_csv=tmp_path / "Connections.csv")
    run_pipeline(cfg)

    CliRunner().invoke(app, ["purge", "--artifact-dir", str(cfg.artifact_dir), "--yes"])

    assert gazetteer.read_bytes() == before


# ===========================================================================
# repository hygiene  (P-25 … P-28)
# ===========================================================================


def test_no_personal_data_files_tracked() -> None:
    """P-26: a tracked export is a leak that survives every later `purge`."""
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    denied = re.compile(
        r"(?:^|/)(?:Connections\.csv|Contacts\.csv|Invitations\.csv|messages\.csv)$"
        r"|LinkedInDataExport",
        re.IGNORECASE,
    )
    offenders = [p for p in tracked if denied.search(p)]
    stray_csv = [
        p
        for p in tracked
        if p.endswith((".csv", ".tsv")) and not p.startswith(("tests/fixtures/", "data/gazetteer/"))
    ]

    assert not offenders, f"export files are tracked in git: {offenders}"
    assert not stray_csv, f"spreadsheets tracked outside fixtures/gazetteer: {stray_csv}"


def test_gitignore_covers_export_filenames() -> None:
    """P-25: the ignore rules must already cover the files before anyone downloads one."""
    candidates = [
        "Connections.csv",
        "Contacts.csv",
        "Invitations.csv",
        "messages.csv",
        "Basic_LinkedInDataExport_2026-01-01.zip",
        "data/observations.yaml",
        "artifacts/report.html",
        ".interlayer/pseudonym.key",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(candidates) + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    ignored = set(result.stdout.split())

    missing = [c for c in candidates if c not in ignored]
    assert not missing, f".gitignore does not cover: {missing}"


def test_fixtures_contain_no_real_domains() -> None:
    """P-27: every fixture identity must be synthetic, per RFC 2606."""
    reserved = ("@example.com", "@example.org", "@example.net", "@example.edu")
    surfaces = [REPO_ROOT / "tests" / "conftest.py", *sorted((REPO_ROOT / "tests").rglob("*.csv"))]
    email_re = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

    offenders: list[str] = []
    for path in surfaces:
        if not path.is_file():
            continue
        for address in email_re.findall(path.read_text(encoding="utf-8")):
            if not address.lower().endswith(reserved):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {address}")

    assert not offenders, "\n  ".join(["non-reserved e-mail domains in fixtures:", *offenders])
