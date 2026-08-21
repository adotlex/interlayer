"""The report must make ZERO outbound requests when it is opened.

This is the failure with the sharpest human consequence in the whole tool. The
operator's report lists people who never consented to being analysed; if the
document fetches one avatar, one webfont or one analytics pixel, the fetch tells
the host exactly which people were analysed, from which IP, at what time. A
``licdn.com`` avatar hands that list straight back to LinkedIn.

So the checks here are structural rather than cosmetic:

* attributes are read from a real HTML parse, not from a regex over the source,
  because ``src = "http://…"`` and ``src="http://…"`` are the same document and
  only one of them survives a naive pattern;
* the CSP meta tag is compared byte for byte against the literal in the build
  contract, not against the constant in the module under test, so a drifting
  constant fails instead of redefining the requirement;
* everything is checked at two scales, because a large graph is where an
  implementation is tempted to reach for a CDN.

Research reference: ``docs/research/05-privacy-compliance.md`` P-6 and its §6
tests ``test_report_has_no_external_references`` / ``test_report_has_csp_meta``.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

from interlayer.config import Settings
from interlayer.errors import InterlayerError
from interlayer.io import write_jsonl
from interlayer.models import (
    AffiliationKind,
    ApproxDate,
    Cluster,
    EvidenceItem,
    GraphEdge,
    Org,
    OrgKind,
    Person,
    Position,
    Provenance,
    ScoreComponents,
    ScoredCluster,
    ScoredPerson,
    TargetFirm,
)
from interlayer.report import assert_self_contained
from interlayer.report import run as render_report

#: P-6, byte for byte. Written out here rather than imported from the module
#: under test: the point of the requirement is that this exact string reaches the
#: document, and a test that imports the constant would agree with any drift.
EXPECTED_CSP = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; img-src data:; font-src data:\">"
)

#: Substrings that must not occur anywhere in the rendered document, at any scale,
#: in either mode. Each one is a distinct way to cause a fetch.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "http://",
    "https://",
    "//cdn",
    "licdn",
    "linkedin.com",
    "<script",
    "<iframe",
    "<embed",
    "<object",
    "<base",
    "@font-face",
    "@import",
    "srcset",
    "integrity=",
    "crossorigin",
)

#: Attribute values that can pull a sub-resource or navigate off-document.
FETCHING_ATTRS = frozenset({"src", "href", "action", "data", "poster", "formaction", "cite"})


# ---------------------------------------------------------------------------
# HTML inspection
# ---------------------------------------------------------------------------


@dataclass
class Parsed:
    """A structural view of the rendered report."""

    tags: list[str] = field(default_factory=list)
    attrs: list[tuple[str, str, str]] = field(default_factory=list)
    styles: list[str] = field(default_factory=list)
    metas: list[dict[str, str]] = field(default_factory=list)

    def values_of(self, attr: str) -> list[tuple[str, str]]:
        return [(tag, value) for tag, name, value in self.attrs if name == attr]


class _Reader(HTMLParser):
    """Collect tags, attributes and stylesheet text from a document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = Parsed()
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.out.tags.append(tag)
        mapping = {name: (value or "") for name, value in attrs}
        for name, value in mapping.items():
            self.out.attrs.append((tag, name, value))
        if tag == "meta":
            self.out.metas.append(mapping)
        if tag == "style":
            self._in_style = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.out.styles.append(data)


def parse(html: str) -> Parsed:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return reader.out


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _write_scenario(art: Path, *, n_people: int, redact: bool, seed: int = 7) -> Settings:
    """Write a full artifact set and render it. Returns the settings used.

    Structure over realism: what matters here is that every rendering path is
    exercised -- observed rows, inferred rows, speculative rows, clusters,
    per-firm columns and the SVG graph -- because an external reference can hide
    in any one of them.
    """
    rng = random.Random(seed)
    jane = Org.make(
        "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
    )
    citadel = Org.make(
        "Citadel LLC", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.CITADEL_LLC
    )
    acme = Org.make("Acme Widgets", OrgKind.COMPANY)
    school = Org.make("Test Polytechnic", OrgKind.SCHOOL)
    orgs = [jane, citadel, acme, school]

    people = [
        Person.make(
            f"Given{i}",
            f"Family{i}",
            linkedin_url=f"https://www.linkedin.com/in/given{i}-family{i}",
            headline=f"Engineer at {'Jane Street' if i % 5 == 0 else 'Acme Widgets'}",
            positions=(
                Position(
                    company_raw="Jane Street" if i % 5 == 0 else "Acme Widgets",
                    title_raw="Trader" if i % 5 == 0 else "Engineer",
                    start=ApproxDate(year=2015 + (i % 8), month=1 + (i % 12), day=1 + (i % 27)),
                    is_current=i % 3 == 0,
                    end=None if i % 3 == 0 else ApproxDate(year=2024, month=6, day=30),
                ),
            ),
        )
        for i in range(n_people)
    ]
    ids = [p.person_id for p in people]

    edges: list[GraphEdge] = []
    seen: set[tuple[str, str]] = set()
    for i in range(n_people):
        for _ in range(2):
            j = rng.randrange(n_people)
            if i == j:
                continue
            key = tuple(sorted((ids[i], ids[j])))
            if key in seen:
                continue
            seen.add(key)  # type: ignore[arg-type]
            observed = (i + j) % 11 == 0
            edges.append(
                GraphEdge(
                    source=ids[i],
                    target=ids[j],
                    weight=round(rng.uniform(0.05, 1.0), 4),
                    shared_org_ids=() if observed else (acme.org_id,),
                    kinds=() if observed else (AffiliationKind.EMPLOYMENT,),
                    provenance=Provenance.OBSERVED if observed else Provenance.INFERRED,
                )
            )

    n_clusters = max(1, n_people // 8)
    clusters: list[Cluster] = []
    scored_clusters: list[ScoredCluster] = []
    for c in range(n_clusters):
        members = tuple(sorted(ids[c::n_clusters]))
        clusters.append(
            Cluster(
                cluster_id=f"cl_{c:03d}",
                label=f"Jane Street and Acme Widgets, group {c}",
                member_ids=members,
                top_orgs=((jane.org_id, 4), (acme.org_id, 3), (school.org_id, 1)),
            )
        )
        scored_clusters.append(
            ScoredCluster(
                cluster_id=f"cl_{c:03d}",
                label="Jane Street",
                score=round(1.0 / (c + 1), 4),
                rank=c + 1,
                size=len(members),
                target_density=round(rng.uniform(0.0, 1.0), 4),
                per_firm={TargetFirm.JANE_STREET: 0.5, TargetFirm.CITADEL_LLC: 0.25},
                top_person_ids=members[:5],
                rationale=f"{len(members)} people, mostly at Jane Street",
            )
        )

    scored_people: list[ScoredPerson] = []
    for i, person in enumerate(people):
        kinds = [
            ("direct_employment", 6.0),
            ("shared_school", 1.5),
            ("cluster_membership", 1.0),
            ("title_signal", 0.5),
            ("path_proximity", 2.0),
        ]
        chosen = kinds[i % len(kinds) :][:2] or kinds[:1]
        evidence = [
            EvidenceItem(
                kind=kind,  # type: ignore[arg-type]
                detail=f"{kind} fired for row {i}",
                contribution=weight,
                org_id=jane.org_id if i % 2 else acme.org_id,
                hops=(i % 4) or None,
            )
            for kind, weight in chosen
        ]
        if i % 9 == 0:
            evidence.append(
                EvidenceItem(
                    kind="observed_mutual",
                    detail="a human read this off the mutual-connections list",
                    contribution=10.0,
                    org_id=jane.org_id,
                    via_person_id=ids[(i + 1) % n_people],
                    hops=1,
                    provenance=Provenance.OBSERVED,
                )
            )
        scored_people.append(
            ScoredPerson(
                person_id=person.person_id,
                full_name=person.full_name,
                score=round(float(n_people - i) / 3.0, 4),
                rank=i + 1,
                components=ScoreComponents(direct=1.0, alumni=0.5, proximity=0.25, cluster=0.1),
                per_firm={TargetFirm.JANE_STREET: 1.0, TargetFirm.CITADEL_LLC: 0.5},
                cluster_id=f"cl_{i % n_clusters:03d}",
                evidence=tuple(evidence),
                hops_to_target=(i % 4) or None,
            )
        )

    cfg = Settings(artifact_dir=art, redact=redact, seed=seed, consensus_runs=3)
    write_jsonl(cfg.people_path, people)
    write_jsonl(cfg.orgs_path, orgs)
    write_jsonl(cfg.edges_path, edges)
    write_jsonl(cfg.clusters_path, clusters)
    write_jsonl(cfg.scored_people_path, scored_people)
    write_jsonl(cfg.scored_clusters_path, scored_clusters)
    render_report(cfg)
    return cfg


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Four rendered documents: {small, large} x {identified, redacted}.

    Module-scoped because rendering 500 nodes four times per test would dominate
    the suite; the documents are immutable strings once written.
    """
    root = tmp_path_factory.mktemp("selfcontained")
    out: dict[str, str] = {}
    for scale, n in (("small", 12), ("large", 500)):
        for mode, redact in (("identified", False), ("redacted", True)):
            cfg = _write_scenario(root / f"{scale}-{mode}", n_people=n, redact=redact)
            out[f"{scale}-{mode}"] = cfg.report_path.read_text(encoding="utf-8")
    return out


ALL_REPORTS = ("small-identified", "small-redacted", "large-identified", "large-redacted")


# ===========================================================================
# zero outbound references
# ===========================================================================


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_has_no_external_references(reports: dict[str, str], key: str) -> None:
    """P-6: nothing in the document can cause a fetch when it is opened."""
    html = reports[key]
    lowered = html.lower()

    found = {token: lowered.count(token) for token in FORBIDDEN_SUBSTRINGS if token in lowered}

    assert not found, f"{key}: the report contains fetchable references {found}"


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_src_and_href_attributes_are_local_only(reports: dict[str, str], key: str) -> None:
    """Every fetching attribute points at a ``data:`` URI or an in-document fragment.

    Parsed from the document rather than pattern-matched over its text, so quoting
    style, whitespace and attribute order cannot hide a remote reference.
    """
    parsed = parse(reports[key])

    offenders: list[str] = []
    for tag, name, value in parsed.attrs:
        if name not in FETCHING_ATTRS:
            continue
        target = value.strip()
        if target.startswith("#") or target.startswith("data:"):
            continue
        offenders.append(f"<{tag} {name}={target!r}>")

    assert not offenders, f"{key}: non-local references {offenders}"
    assert parsed.values_of("href"), f"{key}: no href found at all -- is the parse working?"


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_renders_no_profile_avatars(reports: dict[str, str], key: str) -> None:
    """P-6: a page of ``licdn.com`` avatars would hand the whole list to LinkedIn.

    The report ships no ``<img>`` at all, which is the strongest available form of
    this guarantee: there is no element for a remote URL to be attached to later.
    """
    parsed = parse(reports[key])

    assert "img" not in parsed.tags, f"{key}: the report renders an <img> element"
    assert "picture" not in parsed.tags
    assert "licdn" not in reports[key].lower()


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_has_no_script_or_frame_elements(reports: dict[str, str], key: str) -> None:
    """No script, no frame, no plugin: nothing that can execute or embed."""
    parsed = parse(reports[key])

    banned = {"script", "iframe", "frame", "embed", "object", "applet", "portal"}
    present = banned & set(parsed.tags)

    assert not present, f"{key}: executable/embedding elements present: {present}"


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_stylesheet_fetches_nothing(reports: dict[str, str], key: str) -> None:
    """Inline CSS only: no ``@import``, no webfont, no ``url()`` to a remote asset."""
    parsed = parse(reports[key])
    css = "\n".join(parsed.styles)

    assert css.strip(), f"{key}: no inline stylesheet found -- the parse is wrong"
    assert "@import" not in css
    assert "@font-face" not in css
    remote = [m.group(0) for m in re.finditer(r"url\(\s*['\"]?(?!data:)[^)]*\)", css)]
    assert not remote, f"{key}: stylesheet fetches remote assets {remote}"
    assert "link" not in parsed.tags, f"{key}: an external stylesheet <link> is present"


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_has_no_form_that_can_submit(reports: dict[str, str], key: str) -> None:
    """The CSS-only controls are inputs, not a form: nothing can POST anywhere."""
    parsed = parse(reports[key])

    assert "form" not in parsed.tags, f"{key}: the report contains a <form>"
    assert not parsed.values_of("action"), f"{key}: an action attribute is present"


# ===========================================================================
# the Content-Security-Policy
# ===========================================================================


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_has_csp_meta(reports: dict[str, str], key: str) -> None:
    """P-6: present, byte-exact, and exactly once."""
    html = reports[key]

    assert EXPECTED_CSP in html, f"{key}: the mandated CSP meta tag is missing or has drifted"
    assert html.count(EXPECTED_CSP) == 1, f"{key}: the CSP tag appears {html.count(EXPECTED_CSP)}x"
    assert html.lower().count("content-security-policy") == 1, (
        f"{key}: more than one CSP declaration -- a second, weaker policy can override"
    )


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_csp_directives_permit_nothing_remote(reports: dict[str, str], key: str) -> None:
    """Read the policy as a policy: every source list must be local or ``'none'``."""
    parsed = parse(reports[key])
    policies = [
        m["content"]
        for m in parsed.metas
        if m.get("http-equiv", "").lower() == "content-security-policy"
    ]

    assert len(policies) == 1, f"{key}: expected one CSP meta, got {len(policies)}"
    directives = {
        part.split()[0]: part.split()[1:] for part in policies[0].split(";") if part.strip()
    }
    assert directives["default-src"] == ["'none'"]
    assert directives["img-src"] == ["data:"]
    assert directives["font-src"] == ["data:"]
    assert directives["style-src"] == ["'unsafe-inline'"]
    assert "script-src" not in directives, (
        "no script-src is declared, so `default-src 'none'` blocks all script; "
        "declaring one could only widen that"
    )
    flat = " ".join(v for values in directives.values() for v in values)
    assert "http" not in flat and "*" not in flat


@pytest.mark.parametrize("key", ALL_REPORTS)
def test_report_sends_no_referrer(reports: dict[str, str], key: str) -> None:
    """Belt and braces: even a user-initiated click must not name this document."""
    parsed = parse(reports[key])
    referrer = [m for m in parsed.metas if m.get("name", "").lower() == "referrer"]

    assert referrer, f"{key}: no referrer policy declared"
    assert referrer[0]["content"] == "no-referrer"


# ===========================================================================
# the guard itself, and scale
# ===========================================================================


def test_self_contained_guard_rejects_planted_references() -> None:
    """CONTROL: the egress guard must actually fire, or every test above is theatre."""
    plants = (
        '<script src="https://cdn.example.com/a.js"></script>',
        '<img src="https://media.licdn.com/dms/image/x">',
        '<iframe src="data:text/html,x"></iframe>',
        '<link rel="stylesheet" href="/style.css">',
        "<style>@font-face{src:url(https://fonts.gstatic.com/a.woff2)}</style>",
        "<style>body{background:url(//cdn.example.com/bg.png)}</style>",
        '<a href="https://www.linkedin.com/in/somebody">profile</a>',
    )

    for plant in plants:
        with pytest.raises(InterlayerError, match="outbound requests"):
            assert_self_contained(f"<html><body>{plant}</body></html>")

    assert_self_contained('<html><body><a href="#p-1">local</a></body></html>')


def test_report_refuses_to_write_when_free_text_carries_a_profile_url(tmp_path: Path) -> None:
    """Fail closed: a URL smuggled in through an employer string blocks the write.

    An export's company field is free text an operator does not control. If one
    contains a profile URL, the guard must refuse the document rather than emit a
    page that links to LinkedIn -- and it must leave no file behind.
    """
    art = tmp_path / "artifacts"
    org = Org.make("Acme Widgets", OrgKind.COMPANY)
    person = Person.make(
        "Ada",
        "Lovelace",
        linkedin_url="https://www.linkedin.com/in/ada-lovelace",
        positions=(
            Position(
                company_raw="Acme Widgets (https://www.linkedin.com/in/ada-lovelace)",
                title_raw="Engineer",
                is_current=True,
            ),
        ),
    )
    scored = ScoredPerson(
        person_id=person.person_id,
        full_name=person.full_name,
        score=1.0,
        rank=1,
        evidence=(EvidenceItem(kind="direct_employment", detail="employer names a target"),),
    )
    cfg = Settings(artifact_dir=art, consensus_runs=3)
    write_jsonl(cfg.people_path, [person])
    write_jsonl(cfg.orgs_path, [org])
    write_jsonl(cfg.edges_path, [])
    write_jsonl(cfg.clusters_path, [])
    write_jsonl(cfg.scored_people_path, [scored])
    write_jsonl(cfg.scored_clusters_path, [])

    with pytest.raises(InterlayerError, match="outbound requests"):
        render_report(cfg)

    assert not cfg.report_path.exists(), "a report that would phone home was written to disk"


def test_large_report_stays_a_single_local_file(reports: dict[str, str]) -> None:
    """~500 nodes: still one self-contained document, still no CDN, still not enormous."""
    html = reports["large-redacted"]
    parsed = parse(html)

    assert html.count("<circle") > 100, "the large fixture did not actually draw a graph"
    assert "svg" in parsed.tags, "the network panel is missing at scale"
    assert not [v for _tag, v in parsed.values_of("src")], "a src attribute appeared at scale"
    assert len(html.encode("utf-8")) < 2_000_000, (
        "the report ballooned; the research budget is 279 KB at 2 800 nodes"
    )
