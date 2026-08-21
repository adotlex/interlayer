"""Honest presentation, degenerate inputs, and byte-for-byte reproducibility.

Three separate obligations meet in the rendered document:

* **Provenance survives the render.** An inference that reads as a fact is the
  failure that hurts a real person: they get approached about a firm they have no
  connection to, or skipped because a guess said so. So "observed" and "inferred"
  are checked as *structure* -- distinct classes, distinct text badges, distinct
  non-colour cues -- and the arithmetic invariant behind them (inference is capped
  at 0.79, observation is 1.00, so the Strong band is unreachable by guessing) is
  checked over a real rendered report rather than only at the unit level.
* **Degenerate input produces a page, not a traceback.** An operator whose export
  yields nothing must be told that, in the document, rather than shown a stack
  trace they cannot interpret.
* **Determinism.** ``PYTHONHASHSEED`` produced four distinct set-iteration orders
  in Wave 1, so reproducibility is tested by actually varying it in subprocesses.

Research reference: ``docs/research/05-privacy-compliance.md`` §3.7-§3.8 (P-29 …
P-38) and the provenance block of its §6 test list.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from interlayer.config import Settings
from interlayer.errors import StageInputMissingError
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
from interlayer.report import run as render_report
from interlayer.report.confidence import INFERRED_CEILING, band_for, confidence_for

REPO_ROOT = Path(__file__).resolve().parents[2]

OBSERVED_BADGE = "OBSERVED"
INFERRED_BADGE = "INFERRED"

#: Every band label from P-35. None of them is "confirmed", and that is the point.
BAND_LABELS = {
    "strong": "Strong signal",
    "moderate": "Moderate signal",
    "weak": "Weak signal",
    "speculative": "Speculative",
}


# ---------------------------------------------------------------------------
# a structural reading of the rendered document
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """One person row plus the evidence row that follows it."""

    classes: list[str]
    anchor: str
    prov: list[str] = field(default_factory=list)
    band: str = ""
    confidence: float | None = None
    score: float | None = None
    evidence_items: int = 0
    evidence_prov: list[str] = field(default_factory=list)
    evidence_note: str = ""
    has_details: bool = False

    @property
    def observed(self) -> bool:
        return "obs" in self.classes


class _RowReader(HTMLParser):
    """Extract ranked-table rows without regexing over raw markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[Row] = []
        self._row: Row | None = None
        self._in_table = False
        self._in_ev = False
        self._buf: list[str] | None = None
        self._kind = ""
        self._depth = 0

    def _start(self, kind: str) -> None:
        self._buf = []
        self._kind = kind
        self._depth = 0

    def _flush(self) -> None:
        if self._buf is None or self._row is None:
            self._buf = None
            return
        text = "".join(self._buf).strip()
        if self._kind == "prov":
            (self._row.evidence_prov if self._in_ev else self._row.prov).append(text)
        elif self._kind == "pill":
            self._row.confidence = float(text.rsplit(" ", 1)[-1])
        elif self._kind == "score":
            self._row.score = float(text)
        elif self._kind == "ew" and self._in_ev:
            self._row.evidence_note += text + " "
        self._buf = None
        self._kind = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {k: (v or "") for k, v in attrs}
        classes = mapping.get("class", "").split()
        if self._buf is not None:
            self._depth += 1
            return
        if tag == "table" and "rank" in classes:
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "tr" and "row" in classes:
            self._row = Row(classes=classes, anchor=mapping.get("id", ""))
            self.rows.append(self._row)
            self._in_ev = False
            return
        if tag == "tr" and "ev" in classes:
            self._in_ev = True
            return
        if self._row is None:
            return
        if tag == "span" and "prov" in classes:
            self._start("prov")
        elif tag == "span" and "pill" in classes:
            self._row.band = next((c[2:] for c in classes if c.startswith("p-")), "")
            self._start("pill")
        elif tag == "td" and "num" in classes and not self._in_ev and self._row.score is None:
            self._start("score")
        elif tag == "li" and self._in_ev:
            self._row.evidence_items += 1
        elif tag == "details" and self._in_ev:
            self._row.has_details = True
        elif tag == "span" and "ew" in classes and self._in_ev:
            self._start("ew")

    def handle_endtag(self, tag: str) -> None:
        if self._buf is not None:
            if self._depth:
                self._depth -= 1
            else:
                self._flush()
            return
        if tag == "table":
            self._in_table = False
            self._row = None
            self._in_ev = False

    def handle_data(self, data: str) -> None:
        if self._buf is not None:
            self._buf.append(data)


def read_rows(html: str) -> list[Row]:
    reader = _RowReader()
    reader.feed(html)
    reader.close()
    return reader.rows


def css_rules(html: str) -> dict[str, str]:
    """Selector -> declaration block, for the inline stylesheet."""
    styles = "\n".join(re.findall(r"<style>(.*?)</style>", html, flags=re.S))
    return {
        selector.strip(): body.strip()
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", styles)
    }


def declarations_for(rules: dict[str, str], needle: str) -> str:
    """Every declaration block whose selector mentions ``needle``.

    Selectors are matched by substring because the class is what carries the
    meaning: ``.lg-observed`` is styled as ``.legend .lg-observed``, and a test
    that demanded the exact selector would be testing the stylesheet's nesting
    rather than whether observation and inference look different.
    """
    return "\n".join(body for selector, body in rules.items() if needle in selector)


def visible_text(html: str) -> str:
    """The document with its stylesheet and markup removed."""
    without_style = re.sub(r"<style>.*?</style>", " ", html, flags=re.S)
    return re.sub(r"<[^>]+>", " ", without_style)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _blank(cfg: Settings) -> None:
    for path in (
        cfg.people_path,
        cfg.orgs_path,
        cfg.edges_path,
        cfg.clusters_path,
        cfg.scored_people_path,
        cfg.scored_clusters_path,
    ):
        write_jsonl(path, [])


def write_case(
    art: Path,
    *,
    people: list[Person],
    orgs: list[Org],
    edges: list[GraphEdge],
    clusters: list[Cluster],
    scored_people: list[ScoredPerson],
    scored_clusters: list[ScoredCluster],
    redact: bool = False,
) -> Settings:
    cfg = Settings(artifact_dir=art, redact=redact, consensus_runs=3)
    write_jsonl(cfg.people_path, people)
    write_jsonl(cfg.orgs_path, orgs)
    write_jsonl(cfg.edges_path, edges)
    write_jsonl(cfg.clusters_path, clusters)
    write_jsonl(cfg.scored_people_path, scored_people)
    write_jsonl(cfg.scored_clusters_path, scored_clusters)
    return cfg


def build_mixed(art: Path, *, redact: bool = False) -> Settings:
    """A cast spanning every provenance and every confidence band.

    Person 0 is genuinely observed (confidence 1.00). Person 1 is the adversarial
    case: an ``observed_mutual`` evidence item marked INFERRED, stacked with five
    other kinds. If the ceiling is anywhere but in ``confidence_for``, that row is
    the one that reaches "Strong signal" while being a guess.
    """
    jane = Org.make(
        "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
    )
    citadel = Org.make(
        "Citadel Securities",
        OrgKind.COMPANY,
        is_target=True,
        target_firm=TargetFirm.CITADEL_SECURITIES,
    )
    acme = Org.make("Acme Widgets", OrgKind.COMPANY)
    school = Org.make("Test Polytechnic", OrgKind.SCHOOL)
    orgs = [jane, citadel, acme, school]

    people = [
        Person.make(
            f"Given{i}",
            f"Family{i}",
            linkedin_url=f"https://www.linkedin.com/in/render-fixture-{i:02d}",
            positions=(
                Position(
                    company_raw="Jane Street" if i % 2 else "Acme Widgets",
                    title_raw="Trader",
                    start=ApproxDate(year=2016 + (i % 6), month=3, day=9),
                    is_current=i % 2 == 0,
                    end=None if i % 2 == 0 else ApproxDate(year=2023, month=5, day=1),
                ),
            ),
        )
        for i in range(12)
    ]
    ids = [p.person_id for p in people]

    edges = [
        GraphEdge(
            source=ids[i],
            target=ids[i + 1],
            weight=0.3 + i / 50,
            shared_org_ids=(acme.org_id,),
            kinds=(AffiliationKind.EMPLOYMENT,),
        )
        for i in range(len(ids) - 1)
    ]
    edges.append(
        GraphEdge(source=ids[0], target=ids[3], weight=1.0, provenance=Provenance.OBSERVED)
    )

    clusters = [
        Cluster(
            cluster_id="cl_a",
            label="Jane Street cluster",
            member_ids=tuple(sorted(ids[:8])),
            top_orgs=((jane.org_id, 5), (acme.org_id, 2)),
        )
    ]
    scored_clusters = [
        ScoredCluster(
            cluster_id="cl_a",
            label="Jane Street",
            score=1.4,
            rank=1,
            size=8,
            target_density=0.5,
            per_firm={TargetFirm.JANE_STREET: 1.0},
            top_person_ids=tuple(sorted(ids[:5])),
            rationale="eight people, five of whom name a target firm",
        )
    ]

    def scored(index: int, evidence: list[EvidenceItem], score: float) -> ScoredPerson:
        return ScoredPerson(
            person_id=ids[index],
            full_name=people[index].full_name,
            score=score,
            rank=index + 1,
            components=ScoreComponents(direct=score / 2, cluster=0.4),
            per_firm={TargetFirm.JANE_STREET: score / 2, TargetFirm.CITADEL_SECURITIES: 0.25},
            cluster_id="cl_a" if index < 8 else None,
            evidence=tuple(evidence),
            hops_to_target=1 + (index % 3),
        )

    every_kind: list[tuple[str, float]] = [
        ("observed_mutual", 4.0),
        ("direct_employment", 6.0),
        ("past_employment", 3.0),
        ("shared_employer", 1.0),
        ("shared_school", 1.5),
        ("cluster_membership", 1.0),
        ("path_proximity", 2.0),
    ]
    scored_people = [
        # 0: a real observed reading.
        scored(
            0,
            [
                EvidenceItem(
                    kind="observed_mutual",
                    detail="a human read this off the mutual-connections list",
                    contribution=10.0,
                    org_id=jane.org_id,
                    via_person_id=ids[3],
                    hops=1,
                    provenance=Provenance.OBSERVED,
                )
            ],
            10.0,
        ),
        # 1: the adversarial pile-up -- every inferred kind at once, including an
        # `observed_mutual` item that is NOT marked observed.
        scored(
            1,
            [
                EvidenceItem(
                    kind=kind,  # type: ignore[arg-type]
                    detail=f"{kind} fired",
                    contribution=weight,
                    org_id=jane.org_id,
                    hops=1,
                )
                for kind, weight in every_kind
            ],
            17.5,
        ),
        scored(
            2,
            [
                EvidenceItem(
                    kind="direct_employment", detail="employer names a target", contribution=6.0
                )
            ],
            6.0,
        ),
        scored(
            3,
            [
                EvidenceItem(
                    kind="shared_school", detail="same school as a target", contribution=1.5
                )
            ],
            1.5,
        ),
        # 4: title signal alone -> confidence 0.26 -> speculative.
        scored(
            4,
            [
                EvidenceItem(
                    kind="title_signal", detail="title resembles a target role", contribution=0.5
                )
            ],
            0.5,
        ),
        # 5: no evidence at all, so the score must be zero.
        ScoredPerson(person_id=ids[5], full_name=people[5].full_name, score=0.0, rank=6),
    ]
    return write_case(
        art,
        people=people,
        orgs=orgs,
        edges=edges,
        clusters=clusters,
        scored_people=scored_people,
        scored_clusters=scored_clusters,
        redact=redact,
    )


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("render")
    out: dict[str, Any] = {}
    for mode, redact in (("identified", False), ("redacted", True)):
        cfg = build_mixed(root / mode, redact=redact)
        render_report(cfg)
        out[mode] = cfg.report_path.read_text(encoding="utf-8")
        out[f"{mode}_cfg"] = cfg
    return out


# ===========================================================================
# provenance is visible and structural  (P-36, §3.8)
# ===========================================================================


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_observed_and_inferred_are_visually_distinct(mixed: dict[str, Any], mode: str) -> None:
    """P-36: distinct classes, distinct text, and a cue that survives greyscale."""
    html = mixed[mode]
    rules = css_rules(html)
    marks = {
        needle: declarations_for(rules, needle)
        for needle in (
            ".prov-obs",
            ".prov-inf",
            "tr.obs td:first-child",
            "tr.inf td:first-child",
            ".lg-observed",
            ".lg-inferred",
        )
    }

    missing = [needle for needle, body in marks.items() if not body]
    assert not missing, f"{mode}: no styling rule for {missing}"
    assert marks[".prov-obs"] != marks[".prov-inf"], "the two badges are styled identically"
    assert marks["tr.obs td:first-child"] != marks["tr.inf td:first-child"]
    assert marks[".lg-observed"] != marks[".lg-inferred"], "the legend keys are identical"
    assert "dashed" in marks[".prov-inf"] + marks["tr.inf td:first-child"], (
        "inference is distinguished by colour alone; that is lost in greyscale"
    )
    assert OBSERVED_BADGE in html and INFERRED_BADGE in html


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_every_row_carries_exactly_one_provenance_badge(mixed: dict[str, Any], mode: str) -> None:
    """No row may be silent about where its claim came from."""
    rows = read_rows(mixed[mode])

    assert rows, f"{mode}: no ranked rows were parsed out of the report"
    for row in rows:
        assert len(row.prov) == 1, f"{row.anchor}: {len(row.prov)} provenance badges"
        expected = OBSERVED_BADGE if row.observed else INFERRED_BADGE
        assert row.prov[0].startswith(expected), f"{row.anchor}: badge says {row.prov[0]!r}"


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_inferred_rows_are_labelled_inferred(mixed: dict[str, Any], mode: str) -> None:
    """P-36: "Inferred - not observed", adjacent to the claim, never in a footer."""
    rows = read_rows(mixed[mode])
    inferred = [r for r in rows if not r.observed]

    assert inferred, f"{mode}: the fixture produced no inferred rows"
    for row in inferred:
        assert row.prov[0].startswith(INFERRED_BADGE)
        assert "not observed" in row.prov[0]


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_no_inferred_row_reaches_the_top_confidence_band(mixed: dict[str, Any], mode: str) -> None:
    """The invariant that makes provenance arithmetic, not decoration.

    Confidence 1.00 means "a human read this off LinkedIn". Everything else is
    capped at 0.79, which leaves the 0.80 boundary permanently empty for
    inference -- so no amount of piled-up guessing can present as a strong signal.
    """
    rows = read_rows(mixed[mode])
    offenders = [
        (r.anchor, r.band, r.confidence)
        for r in rows
        if not r.observed and (r.band == "strong" or (r.confidence or 0.0) > INFERRED_CEILING)
    ]

    assert not offenders, f"{mode}: inferred rows in the top band: {offenders}"
    observed = [r for r in rows if r.observed]
    assert observed, f"{mode}: no observed row, so the invariant is untested"
    assert all(r.confidence == 1.0 and r.band == "strong" for r in observed)
    assert any((r.confidence or 0.0) >= 0.75 for r in rows if not r.observed), (
        "the fixture never pushes inference near the ceiling, so this proves little"
    )


def test_confidence_ceiling_holds_for_every_evidence_combination() -> None:
    """Unit-level: nothing short of an OBSERVED item can produce 1.00."""
    kinds = (
        "observed_mutual",
        "direct_employment",
        "past_employment",
        "shared_employer",
        "shared_school",
        "cluster_membership",
        "title_signal",
        "path_proximity",
    )
    inferred = [
        EvidenceItem(kind=k, detail=f"{k} fired", contribution=9.9)  # type: ignore[arg-type]
        for k in kinds
    ]

    assert confidence_for(inferred) <= INFERRED_CEILING
    assert band_for(confidence_for(inferred))[0] != "strong"
    for size in range(1, len(inferred) + 1):
        assert confidence_for(inferred[:size]) <= INFERRED_CEILING

    observed = [
        EvidenceItem(
            kind="observed_mutual",
            detail="human read",
            contribution=10.0,
            provenance=Provenance.OBSERVED,
        )
    ]
    assert confidence_for(observed) == 1.0
    assert confidence_for([]) == 0.0


def test_confidence_band_boundaries() -> None:
    """P-35: the boundaries are fixed, and none of the bands is "confirmed"."""
    assert band_for(0.80)[0] == "strong"
    assert band_for(0.79)[0] == "moderate"
    assert band_for(0.55)[0] == "moderate"
    assert band_for(0.54)[0] == "weak"
    assert band_for(0.30)[0] == "weak"
    assert band_for(0.29)[0] == "speculative"
    assert band_for(0.0)[0] == "speculative"
    assert "confirmed" not in " ".join(
        label for _slug, label in map(band_for, (0.0, 0.4, 0.6, 1.0))
    )


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_every_row_shows_its_confidence_and_band(mixed: dict[str, Any], mode: str) -> None:
    """P-34: no surface may show a link without both a number and a band label."""
    html = mixed[mode]
    rows = read_rows(html)

    for row in rows:
        assert row.band in BAND_LABELS, f"{row.anchor}: unknown band {row.band!r}"
        assert row.confidence is not None
        assert 0.0 <= row.confidence <= 1.0
        assert BAND_LABELS[row.band] in html, "the band label is not spelled out in words"


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_every_scored_row_displays_its_evidence(mixed: dict[str, Any], mode: str) -> None:
    """P-30/P-31: "why is this person here" must be answerable inside the document."""
    rows = read_rows(mixed[mode])

    for row in rows:
        if row.score == 0.0:
            assert "no evidence recorded" in row.evidence_note, (
                f"{row.anchor}: a zero-score row says nothing about why it is empty"
            )
            continue
        assert row.evidence_items >= 1, f"{row.anchor}: a scored row shows no evidence"
        assert row.has_details, f"{row.anchor}: evidence is not disclosable"
        assert len(row.evidence_prov) == row.evidence_items, (
            f"{row.anchor}: {row.evidence_items} evidence items, "
            f"{len(row.evidence_prov)} provenance badges"
        )
        for badge in row.evidence_prov:
            assert badge in {OBSERVED_BADGE, INFERRED_BADGE}


def test_claim_without_evidence_raises() -> None:
    """P-30: a score nobody can audit is a score nobody can act on."""
    with pytest.raises(ValueError, match="evidence"):
        ScoredPerson(person_id="p_1", full_name="Somebody", score=4.2)

    ScoredPerson(person_id="p_1", full_name="Somebody", score=0.0)


def test_observed_provenance_is_refused_for_inferred_kinds() -> None:
    """The type system, not a convention: only a human reading may claim OBSERVED."""
    with pytest.raises(ValueError, match="observed_mutual"):
        EvidenceItem(
            kind="direct_employment",
            detail="employer field names a target",
            provenance=Provenance.OBSERVED,
        )


# ===========================================================================
# honest framing  (P-37, P-38, §3.8)
# ===========================================================================


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_report_contains_limitations_block(mixed: dict[str, Any], mode: str) -> None:
    """P-38: the HTML file is the artifact that gets forwarded, README or not."""
    html = mixed[mode]

    for phrase in (
        "The data is stale by construction.",
        "is a guess, not a relationship",
        "Name and employer matching is fuzzy and fallible.",
        "Absence means nothing.",
        "Your export is not your network.",
        "The gazetteer is hand-curated and incomplete.",
        "hypothesis to check, never a fact to forward",
    ):
        assert phrase in html, f"{mode}: the limitations block is missing {phrase!r}"


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_report_carries_the_persistent_header_line(mixed: dict[str, Any], mode: str) -> None:
    """§3.8 item 7: the caveat travels with the file, at the top of every page."""
    html = mixed[mode]

    assert "Not verified. Not observed. Check before acting." in html
    assert 'class="persist"' in html
    assert "no scripts, no fonts, no images, no network" in html


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_no_unqualified_relational_assertions(mixed: dict[str, Any], mode: str) -> None:
    """P-37: not "Alex works at Jane Street" but "the employer field reads …"."""
    text = visible_text(mixed[mode])

    for pattern in (r"\bworks at\b", r"\bworked at\b", r"\bknows\b", r"\bis connected to\b"):
        assert not re.search(pattern, text, re.IGNORECASE), (
            f"{mode}: unqualified relational claim matching {pattern}"
        )
    # The employer string itself is dropped in redacted mode, so the hedge that
    # survives there is the staleness note rather than "employer field reads X".
    hedge = "employer field reads" if mode == "identified" else "employer field"
    assert hedge in text, "the hedged phrasing is missing entirely"
    assert "No relationship has been observed and none is implied." in text


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_report_never_claims_a_result_is_confirmed(mixed: dict[str, Any], mode: str) -> None:
    """P-35: none of the bands is "confirmed", and the copy must not say it either."""
    text = visible_text(mixed[mode]).lower()

    for word in ("confirmed", "verified relationship", "definitely", "guaranteed"):
        assert word not in text, f"{mode}: the report claims {word!r}"


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_cluster_headers_are_hedged_and_counted(mixed: dict[str, Any], mode: str) -> None:
    """§3.8 item 5: "Possible X cluster (n people, mean confidence …)"."""
    html = mixed[mode]

    assert "Possible" in html, f"{mode}: a cluster is named without a hedge"
    assert "people</span>" in html, f"{mode}: cluster cards show no member count"
    assert "mean</span>" in html, f"{mode}: cluster cards show no mean confidence"
    assert "Citadel LLC (the hedge fund) and Citadel Securities" in html, (
        "the two Citadels are presented without the note that they are different firms"
    )


@pytest.mark.parametrize("mode", ["identified", "redacted"])
def test_staleness_is_stated_for_every_row(mixed: dict[str, Any], mode: str) -> None:
    """§3.8 item 4: a stale employer field is the commonest source of a wrong claim."""
    html = mixed[mode]

    assert "LinkedIn profiles lag reality by months or years" in html
    assert "may be out of date" in html


def test_speculative_rows_are_hidden_unless_requested(mixed: dict[str, Any]) -> None:
    """P-35: below 0.30 a row must be absent, not merely folded away.

    The implementation collapses these into a ``<details>`` block instead, and no
    ``--include-speculative`` flag exists, so a person whose only signal is a
    job-title keyword still has their name in a file the operator may forward.
    """
    html = mixed["identified"]
    rows = read_rows(html)
    speculative = [r for r in rows if r.band == "speculative"]

    assert not speculative, (
        "speculative rows are rendered by default; P-35 requires them to be absent "
        f"unless --include-speculative is given (found {len(speculative)})"
    )


# ===========================================================================
# manifest and header disclosure  (P-11, P-20)
# ===========================================================================


def test_manifest_records_run_parameters(tmp_path: Path) -> None:
    """P-20: the audit trail that makes "why was this person flagged" answerable."""
    cfg = build_mixed(tmp_path / "artifacts")
    render_report(cfg)
    manifest = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))

    assert manifest["tool_version"]
    assert manifest["created_at"].endswith("Z") or "+00:00" in manifest["created_at"]
    assert manifest["schema_version"]
    assert manifest["seed"] == cfg.seed
    assert manifest["config_sha256"]
    assert manifest["stage_versions"]["report.mode"] == "identified"
    assert len(manifest["stage_versions"]["report.inputs_sha256"]) == 64
    assert manifest["counts"]["people"] > 0
    assert manifest["counts"]["scored_people"] > 0


def test_manifest_records_the_email_mode_and_gazetteer_hash(tmp_path: Path) -> None:
    """P-20 lists both explicitly, and neither is recoverable from what is written.

    ``config_sha256`` covers them cryptographically but not legibly: an auditor
    reading the manifest months later cannot tell whether hashed addresses are
    sitting in ``people.jsonl``, nor which gazetteer produced the firm matches.
    """
    cfg = build_mixed(tmp_path / "artifacts")
    render_report(cfg)
    manifest = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    flat = json.dumps(manifest)

    assert re.search(r"emails?[\"']?\s*[:=]", flat, re.IGNORECASE), (
        "the manifest does not record the e-mail mode (P-20)"
    )
    assert "gazetteer" in flat.lower(), "the manifest does not record the gazetteer hash (P-20)"


def test_report_header_states_the_email_mode(mixed: dict[str, Any]) -> None:
    """P-11: a shared report must never be ambiguous about what it contains.

    The header states the redaction mode, the seed, the tool and schema versions
    -- but not whether e-mail addresses were dropped or retained as digests.
    """
    header = re.search(r'<p class="runmeta">.*?</p>', mixed["identified"], re.S)

    assert header is not None
    assert re.search(r"e-?mail", header.group(0), re.IGNORECASE), (
        "the report header does not state the e-mail handling mode (P-11)"
    )


# ===========================================================================
# degenerate inputs must render, not raise
# ===========================================================================


@pytest.mark.parametrize("redact", [False, True])
def test_renders_with_completely_empty_artifacts(tmp_path: Path, redact: bool) -> None:
    """An operator whose export yields nothing gets a page that says so."""
    cfg = Settings(artifact_dir=tmp_path / f"artifacts-{int(redact)}", redact=redact)
    _blank(cfg)

    render_report(cfg)

    html = cfg.report_path.read_text(encoding="utf-8")
    assert "No graph to draw." in html
    assert "No observed bridges in this run." in html
    assert "No clusters." in html
    assert read_rows(html) == []


def test_renders_with_people_but_no_scores(tmp_path: Path) -> None:
    """People without scores are not an error; they are an empty result."""
    person = Person.make("Ada", "Lovelace", linkedin_url="https://www.linkedin.com/in/ada")
    cfg = write_case(
        tmp_path / "artifacts",
        people=[person],
        orgs=[],
        edges=[],
        clusters=[],
        scored_people=[],
        scored_clusters=[],
    )

    render_report(cfg)

    assert "No graph to draw." in cfg.report_path.read_text(encoding="utf-8")


def test_renders_with_all_zero_scores(tmp_path: Path) -> None:
    """Every score zero: no division by a total, no empty ranking crash."""
    people = [
        Person.make(f"Given{i}", f"Family{i}", linkedin_url=f"https://www.linkedin.com/in/z{i}")
        for i in range(5)
    ]
    scored = [
        ScoredPerson(person_id=p.person_id, full_name=p.full_name, score=0.0, rank=0)
        for p in people
    ]
    cfg = write_case(
        tmp_path / "artifacts",
        people=people,
        orgs=[],
        edges=[],
        clusters=[],
        scored_people=scored,
        scored_clusters=[],
    )

    render_report(cfg)

    html = cfg.report_path.read_text(encoding="utf-8")
    for row in read_rows(html):
        assert row.score == 0.0
        assert "no evidence recorded" in row.evidence_note


def test_renders_with_edges_but_no_clusters(tmp_path: Path) -> None:
    """Clustering can legitimately find nothing; the graph still draws."""
    people = [
        Person.make(f"Given{i}", f"Family{i}", linkedin_url=f"https://www.linkedin.com/in/e{i}")
        for i in range(6)
    ]
    ids = [p.person_id for p in people]
    edges = [
        GraphEdge(source=ids[i], target=ids[i + 1], weight=0.5, shared_org_ids=("org_x",))
        for i in range(5)
    ]
    cfg = write_case(
        tmp_path / "artifacts",
        people=people,
        orgs=[],
        edges=edges,
        clusters=[],
        scored_people=[],
        scored_clusters=[],
    )

    render_report(cfg)

    html = cfg.report_path.read_text(encoding="utf-8")
    assert "No clusters." in html
    assert "<circle" in html, "edges exist but nothing was drawn"


@pytest.mark.parametrize("redact", [False, True])
def test_renders_one_enormous_cluster(tmp_path: Path, redact: bool) -> None:
    """The 65%-of-all-edges failure mode: one cluster holding everybody."""
    people = [
        Person.make(f"Given{i}", f"Family{i}", linkedin_url=f"https://www.linkedin.com/in/big{i}")
        for i in range(300)
    ]
    ids = tuple(sorted(p.person_id for p in people))
    cluster = Cluster(cluster_id="cl_all", label="everyone", member_ids=ids)
    scored_cluster = ScoredCluster(
        cluster_id="cl_all",
        label="everyone",
        score=1.0,
        rank=1,
        size=len(ids),
        top_person_ids=ids[:5],
        rationale="every person in the export",
    )
    scored = [
        ScoredPerson(
            person_id=p.person_id,
            full_name=p.full_name,
            score=1.0,
            rank=1,
            cluster_id="cl_all",
            evidence=(EvidenceItem(kind="cluster_membership", detail="in the one cluster"),),
        )
        for p in people
    ]
    cfg = write_case(
        tmp_path / f"artifacts-{int(redact)}",
        people=people,
        orgs=[],
        edges=[],
        clusters=[cluster],
        scored_people=scored,
        scored_clusters=[scored_cluster],
        redact=redact,
    )

    render_report(cfg)

    html = cfg.report_path.read_text(encoding="utf-8")
    assert "300 people" in html
    assert len(read_rows(html)) <= 2 * cfg.top_n, "top_n is not bounding the rendered rows"


def test_missing_input_artifact_is_reported_not_swallowed(tmp_path: Path) -> None:
    """Running the report before the pipeline must name the stage that owes the file."""
    cfg = Settings(artifact_dir=tmp_path / "artifacts")

    with pytest.raises(StageInputMissingError):
        render_report(cfg)


# ===========================================================================
# determinism  (non-negotiable 5)
# ===========================================================================


@pytest.mark.parametrize("redact", [False, True])
def test_report_is_byte_identical_across_runs(tmp_path: Path, redact: bool) -> None:
    """Same input, same seed, same bytes -- so a diff is a diff in the data."""
    cfg = build_mixed(tmp_path / "artifacts", redact=redact)

    render_report(cfg)
    first = cfg.report_path.read_bytes()
    render_report(cfg)
    second = cfg.report_path.read_bytes()

    assert first == second
    assert b"20" in first  # sanity: the document is not empty


@pytest.mark.parametrize("redact", [False, True])
def test_report_is_byte_identical_across_artifact_directories(tmp_path: Path, redact: bool) -> None:
    """No absolute path, no directory name and no clock may reach the document."""
    one = build_mixed(tmp_path / f"one-{int(redact)}", redact=redact)
    two = build_mixed(tmp_path / f"two-{int(redact)}", redact=redact)

    render_report(one)
    render_report(two)

    assert one.report_path.read_bytes() == two.report_path.read_bytes()


_DRIVER = """
import hashlib, sys
from pathlib import Path
from interlayer.config import Settings
from interlayer.report import run
root = Path(sys.argv[1])
for mode in ("identified", "redacted"):
    cfg = Settings(artifact_dir=root / mode, redact=mode == "redacted")
    run(cfg)
    sys.stdout.write(hashlib.sha256((cfg.artifact_dir / "report.html").read_bytes()).hexdigest())
    sys.stdout.write("\\n")
"""


def test_report_is_byte_identical_across_pythonhashseed(tmp_path: Path) -> None:
    """Wave 1 saw four distinct set-iteration orders across four seed values.

    Run in subprocesses because ``PYTHONHASHSEED`` is fixed at interpreter start;
    setting it inside the test process would change nothing.
    """
    root = tmp_path / "artifacts"
    for mode, redact in (("identified", False), ("redacted", True)):
        build_mixed(root / mode, redact=redact)

    digests: dict[str, list[str]] = {}
    for seed in ("0", "1", "12345", "999"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run(
            [sys.executable, "-c", _DRIVER, str(root)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"PYTHONHASHSEED={seed} failed:\n{result.stderr}"
        digests[seed] = result.stdout.split()

    values = list(digests.values())
    assert all(len(v) == 2 for v in values), f"driver output was malformed: {digests}"
    assert all(v == values[0] for v in values), f"report varies with PYTHONHASHSEED: {digests}"


def test_manifest_is_stable_apart_from_the_clock(tmp_path: Path) -> None:
    """The manifest is the only artifact that carries a timestamp, deliberately."""
    cfg = build_mixed(tmp_path / "artifacts")

    render_report(cfg)
    first = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    first_report = hashlib.sha256(cfg.report_path.read_bytes()).hexdigest()
    render_report(cfg)
    second = json.loads(cfg.manifest_path.read_text(encoding="utf-8"))
    second_report = hashlib.sha256(cfg.report_path.read_bytes()).hexdigest()

    assert first.pop("created_at") and second.pop("created_at")
    assert first == second, "the manifest varies between identical runs"
    assert first_report == second_report
    assert first["run_id"] == second["run_id"]
