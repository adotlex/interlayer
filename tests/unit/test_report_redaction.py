"""Redaction must fail closed: no name from the input may survive into the output.

The people in this data never agreed to be analysed, and a redacted report is the
artifact an operator forwards. So these tests are written to break it rather than
to confirm it:

* the leak detector is **differential**. Counting occurrences of a name in the
  redacted document alone is worthless -- ``.tablewrap{overflow-x:auto}`` contains
  a standalone "x", and the boilerplate contains the words "may" and "will". Each
  scenario is therefore rendered twice, once with the real names and once with
  neutral placeholders, and only an *excess* over the placeholder render counts as
  a leak. False positives cancel; real ones do not.
* the strongest assertion is stronger still: with redaction on, changing every
  name in the input must not change a single byte of the document apart from the
  run id, which is a digest over the input artifacts. If redaction is complete,
  the names simply are not information the document carries.
* there is a **control**. A planted leak must be caught by the same detector, so a
  green test cannot mean "the detector stopped detecting".

Research reference: ``docs/research/05-privacy-compliance.md`` §3.3 (P-12 … P-17)
and the redaction block of its §6 test list.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Iterable, Sequence
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
from interlayer.report import run as render_report
from interlayer.report.redact import RedactionLeakError, Redactor, pseudonym_key_path

#: The cast. Every entry is adversarial in a different way:
#:   * ``Will May``          -- both halves are ordinary English words that occur
#:                              in the report's own fixed copy ("may be wrong").
#:   * ``Jane Street``       -- identical to a curated target firm name.
#:   * ``Zoë Bäcker``        -- non-ASCII, so casefolding and word boundaries have
#:                              to behave outside the Latin-1 comfort zone.
#:   * ``X Y``               -- single characters, below the scrubber's minimum
#:                              token length; nothing may emit them regardless.
#:   * ``A+B (Kim)``         -- regex metacharacters; an unescaped alternation
#:                              would either crash or silently match nothing.
#:   * ``Art Mann``          -- substrings of common words ("part", "management").
#:   * ``李 雷``              -- single-codepoint CJK names.
#:   * ``Grace Hopper``      -- an ordinary control case.
ADVERSARIAL_NAMES: tuple[tuple[str, str], ...] = (
    ("Will", "May"),
    ("Jane", "Street"),
    ("Zoë", "Bäcker"),
    ("X", "Y"),
    ("A+B", "(Kim)"),
    ("Art", "Mann"),
    ("李", "雷"),
    ("Grace", "Hopper"),
)

PLAIN_NAMES: tuple[tuple[str, str], ...] = (
    ("Ada", "Lovelace"),
    ("Alan", "Turing"),
    ("Barbara", "Liskov"),
    ("Edsger", "Dijkstra"),
    ("Frances", "Allen"),
    ("Donald", "Knuth"),
    ("Katherine", "Johnson"),
    ("Margaret", "Hamilton"),
)

#: Placeholders with no overlap with the cast, the fixed copy, or CSS.
NEUTRAL_NAMES: tuple[tuple[str, str], ...] = tuple(
    (f"Qqaa{i}", f"Zzbb{i}") for i in range(len(PLAIN_NAMES))
)

RUN_ID = re.compile(r"run_[0-9a-f]{16}")
EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PSEUDONYM = re.compile(r"PC-[0-9A-HJKMNP-TV-Z]{12}")
DAY_PRECISION_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


# ---------------------------------------------------------------------------
# leak detection
# ---------------------------------------------------------------------------


def occurrences(text: str, token: str) -> int:
    """Case-insensitive, word-bounded occurrences of ``token`` in ``text``."""
    if not token.strip():
        return 0
    return len(re.findall(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.IGNORECASE))


def name_tokens(names: Sequence[tuple[str, str]], slugs: Sequence[str] = ()) -> list[str]:
    """Every first name, last name, full name and slug fragment from an input."""
    tokens: set[str] = set()
    for first, last in names:
        tokens.update({first, last, f"{first} {last}", f"{last}, {first}"})
    for slug in slugs:
        tokens.update(part for part in re.split(r"[^\w]+", slug) if part)
    return sorted(t for t in tokens if t.strip())


def leaks(document: str, baseline: str, tokens: Iterable[str]) -> dict[str, int]:
    """Tokens occurring more often in ``document`` than in a name-free ``baseline``.

    The baseline is the same report rendered from the same structure with
    placeholder names, so every occurrence explained by CSS, boilerplate or a
    curated firm name cancels out and only genuine leakage is left.
    """
    found: dict[str, int] = {}
    for token in tokens:
        excess = occurrences(document, token) - occurrences(baseline, token)
        if excess > 0:
            found[token] = excess
    return found


# ---------------------------------------------------------------------------
# fixture construction
# ---------------------------------------------------------------------------


def build_artifacts(
    art: Path,
    names: Sequence[tuple[str, str]],
    *,
    redact: bool,
    small_cluster: bool = True,
    seed: int = 20240101,
) -> Settings:
    """Write a full artifact set whose every free-text field names somebody.

    Names are planted everywhere an implementation could forget to scrub: job
    titles, headlines, employer strings, evidence details, cluster labels and
    cluster rationales. Person ids are derived from fixed slugs rather than from
    the names, so two casts differing only in name produce identical ids -- which
    is what makes the byte-identity test below meaningful.
    """
    jane = Org.make(
        "Jane Street", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.JANE_STREET
    )
    citadel = Org.make(
        "Citadel LLC", OrgKind.COMPANY, is_target=True, target_firm=TargetFirm.CITADEL_LLC
    )
    boutique = Org.make(f"{names[0][1]} Advisory Partners", OrgKind.COMPANY)
    school = Org.make("Test Polytechnic", OrgKind.SCHOOL)
    orgs = [jane, citadel, boutique, school]

    people: list[Person] = []
    for i, (first, last) in enumerate(names):
        principal = f"{names[0][0]} {names[0][1]}"
        people.append(
            Person.make(
                first,
                last,
                linkedin_url=f"https://www.linkedin.com/in/interlayer-fixture-{i:02d}",
                headline=f"EA to {principal} at {names[0][1]} Advisory Partners",
                email=f"fixture{i}@example.com",
                positions=(
                    Position(
                        company_raw="Jane Street"
                        if i % 3 == 0
                        else f"{names[0][1]} Advisory Partners",
                        title_raw=f"Chief of staff to {principal}",
                        start=ApproxDate(year=2015 + i, month=1 + (i % 12), day=1 + (i % 27)),
                        is_current=i % 2 == 0,
                        end=None if i % 2 == 0 else ApproxDate(year=2024, month=6, day=30),
                    ),
                ),
            )
        )
    ids = [p.person_id for p in people]

    edges = [
        GraphEdge(
            source=ids[i],
            target=ids[i + 1],
            weight=0.4 + i / 100,
            shared_org_ids=(boutique.org_id,),
            kinds=(AffiliationKind.EMPLOYMENT,),
        )
        for i in range(len(ids) - 1)
    ]
    edges.append(
        GraphEdge(source=ids[0], target=ids[-1], weight=1.0, provenance=Provenance.OBSERVED)
    )

    big_members = tuple(sorted(ids[: max(3, len(ids) - 2)]))
    clusters = [
        Cluster(
            cluster_id="cl_big",
            label=f"{names[1][0]} {names[1][1]} and colleagues",
            member_ids=big_members,
            top_orgs=((jane.org_id, 4), (boutique.org_id, 3), (school.org_id, 1)),
        )
    ]
    scored_clusters = [
        ScoredCluster(
            cluster_id="cl_big",
            # Deliberately empty so the *cluster's* own label is the one rendered:
            # both are agent-authored prose and both are leak surfaces.
            label="",
            score=2.5,
            rank=1,
            size=len(big_members),
            target_density=0.42,
            per_firm={TargetFirm.JANE_STREET: 1.0, TargetFirm.CITADEL_LLC: 0.5},
            top_person_ids=big_members[:5],
            rationale=f"clusters around {names[1][0]} {names[1][1]} and two Jane Street traders",
        )
    ]
    if small_cluster:
        pair = tuple(sorted(ids[-2:]))
        clusters.append(
            Cluster(
                cluster_id="cl_pair",
                label=f"{names[-1][0]} {names[-1][1]} and one other",
                member_ids=pair,
                top_orgs=((boutique.org_id, 2),),
            )
        )
        scored_clusters.append(
            ScoredCluster(
                cluster_id="cl_pair",
                label=f"{names[-1][1]} Advisory Partners",
                score=0.9,
                rank=2,
                size=len(pair),
                target_density=0.1,
                per_firm={TargetFirm.JANE_STREET: 0.1},
                top_person_ids=pair,
                rationale=f"two people around {names[-1][0]} {names[-1][1]}",
            )
        )

    scored_people: list[ScoredPerson] = []
    for i, person in enumerate(names):
        other = names[(i + 1) % len(names)]
        evidence = [
            EvidenceItem(
                kind="shared_employer",
                detail=f"shared employer with {other[0]} {other[1]}",
                contribution=1.0,
                org_id=boutique.org_id,
                via_person_id=ids[(i + 1) % len(ids)],
                hops=1,
            ),
            EvidenceItem(
                kind="direct_employment",
                # A target-firm employee who is NOT in people.jsonl: the scrubber
                # has never seen this name and cannot match it, so the only safe
                # handling is to drop the whole field.
                detail="employer field reads Jane Street, alongside Priya Raman",
                contribution=6.0,
                org_id=jane.org_id,
            ),
        ]
        if i == 0:
            evidence.append(
                EvidenceItem(
                    kind="observed_mutual",
                    detail=f"mutual connection with {other[0]} {other[1]} (read 2026-08-21)",
                    contribution=10.0,
                    org_id=jane.org_id,
                    via_person_id=ids[(i + 1) % len(ids)],
                    hops=1,
                    provenance=Provenance.OBSERVED,
                )
            )
        scored_people.append(
            ScoredPerson(
                person_id=ids[i],
                full_name=f"{person[0]} {person[1]}",
                score=round(10.0 - i, 4),
                rank=i + 1,
                components=ScoreComponents(direct=6.0, alumni=1.5, proximity=0.5, cluster=1.0),
                per_firm={TargetFirm.JANE_STREET: 6.0, TargetFirm.CITADEL_LLC: 1.0},
                cluster_id="cl_pair" if i >= len(ids) - 2 else "cl_big",
                evidence=tuple(evidence),
                hops_to_target=1 + (i % 3),
            )
        )

    cfg = Settings(artifact_dir=art, redact=redact, seed=seed, consensus_runs=3)
    write_jsonl(cfg.people_path, people)
    write_jsonl(cfg.orgs_path, orgs)
    write_jsonl(cfg.edges_path, edges)
    write_jsonl(cfg.clusters_path, clusters)
    write_jsonl(cfg.scored_people_path, scored_people)
    write_jsonl(cfg.scored_clusters_path, scored_clusters)
    return cfg


def render(
    root: Path,
    tag: str,
    names: Sequence[tuple[str, str]],
    *,
    redact: bool,
    **kwargs: object,
) -> tuple[Settings, str]:
    cfg = build_artifacts(root / tag, names, redact=redact, **kwargs)  # type: ignore[arg-type]
    render_report(cfg)
    return cfg, cfg.report_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Redacted, identified and placeholder renders of one structure.

    All three share a parent directory so they also share one pseudonym key,
    which is what makes the identifiers comparable across them.
    """
    root = tmp_path_factory.mktemp("redaction")
    out: dict[str, str] = {}
    _cfg, out["redacted"] = render(root, "redacted", PLAIN_NAMES, redact=True)
    _cfg, out["identified"] = render(root, "identified", PLAIN_NAMES, redact=False)
    _cfg, out["baseline"] = render(root, "baseline", NEUTRAL_NAMES, redact=True)
    _cfg, out["baseline_identified"] = render(root, "baseline-id", NEUTRAL_NAMES, redact=False)
    _cfg, out["adversarial"] = render(root, "adversarial", ADVERSARIAL_NAMES, redact=True)
    _cfg, out["adversarial_baseline"] = render(
        root, "adversarial-base", NEUTRAL_NAMES[: len(ADVERSARIAL_NAMES)], redact=True
    )
    return out


def make_redactor(tmp_path: Path, names: Sequence[tuple[str, str]] = PLAIN_NAMES) -> Redactor:
    cfg = Settings(artifact_dir=tmp_path / "artifacts", redact=True)
    people = [
        Person.make(first, last, linkedin_url=f"https://www.linkedin.com/in/fixture-{i}")
        for i, (first, last) in enumerate(names)
    ]
    return Redactor(cfg, people, safe_phrases=["Jane Street", "Citadel LLC"])


# ===========================================================================
# the detector itself
# ===========================================================================


def test_leak_detector_detects_a_planted_leak(rendered: dict[str, str]) -> None:
    """CONTROL: a name spliced into a clean document must be reported.

    Without this, ``test_redact_mode_removes_all_names`` passing would be
    consistent with a detector that reports nothing at all.
    """
    clean = rendered["redacted"]
    tokens = name_tokens(PLAIN_NAMES)

    assert leaks(clean, rendered["baseline"], tokens) == {}

    planted = clean.replace("<footer>", '<span class="who">Ada Lovelace</span><footer>', 1)
    caught = leaks(planted, rendered["baseline"], tokens)

    assert caught, "the leak detector did not notice a name spliced into the document"
    assert "Ada Lovelace" in caught
    assert "Lovelace" in caught


def test_identified_mode_shows_the_names_redaction_must_remove(rendered: dict[str, str]) -> None:
    """CONTROL: the fixture really does carry names, so removing them means something."""
    found = leaks(rendered["identified"], rendered["baseline_identified"], name_tokens(PLAIN_NAMES))

    assert len(found) >= 8, f"the identified report barely names anyone: {found}"
    assert "Ada Lovelace" in found


# ===========================================================================
# no name survives  (P-12, P-15)
# ===========================================================================


def test_redact_mode_removes_all_names(rendered: dict[str, str]) -> None:
    """P-12: no first name, last name or full name appears anywhere in the output."""
    found = leaks(rendered["redacted"], rendered["baseline"], name_tokens(PLAIN_NAMES))

    assert found == {}, f"names survived redaction: {found}"


def test_redact_mode_removes_adversarial_names(rendered: dict[str, str]) -> None:
    """Unicode, single characters, regex metacharacters and firm-name collisions."""
    tokens = name_tokens(ADVERSARIAL_NAMES)
    found = leaks(rendered["adversarial"], rendered["adversarial_baseline"], tokens)

    # "Jane"/"Street" are expected to appear: they are the curated firm name, and
    # they appear identically whether or not anyone in the input is called that,
    # which is why the differential baseline cancels them. Anything else is a leak.
    assert found == {}, f"adversarial names survived redaction: {found}"


def test_redact_pseudonymises_people_absent_from_people_jsonl(tmp_path: Path) -> None:
    """The hole a name-based scrubber cannot see: somebody it has never been shown.

    ``scored_people.jsonl`` may name a person with no ``Person`` record -- a
    target-firm employee reached through an observation, say. Their name is not in
    the scrubber's alternation, so a design that scrubbed by name would emit it
    verbatim. Only pseudonymising at the label chokepoint closes this.
    """
    cfg = build_artifacts(tmp_path / "artifacts", PLAIN_NAMES, redact=True)
    stranger = ScoredPerson(
        person_id="p_exact_deadbeefdeadbeef",
        full_name="Unmatched Stranger",
        score=7.0,
        rank=99,
        evidence=(
            EvidenceItem(
                kind="direct_employment", detail="employer names a target", contribution=6.0
            ),
        ),
    )
    existing = [
        ScoredPerson.model_validate_json(line)
        for line in cfg.scored_people_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    write_jsonl(cfg.scored_people_path, [*existing, stranger])

    render_report(cfg)
    html = cfg.report_path.read_text(encoding="utf-8")

    assert "Unmatched" not in html, "a person absent from people.jsonl was named in full"
    assert "Stranger" not in html
    assert "p_exact_deadbeefdeadbeef" not in html, "the raw person id leaked instead"
    assert len(PSEUDONYM.findall(html)) >= len(PLAIN_NAMES)


def test_redact_applies_to_the_report_not_the_intermediate_artifacts(tmp_path: Path) -> None:
    """A boundary worth knowing: ``--redact`` makes the *report* shareable, not the run.

    ``people.jsonl`` still holds names in redacted mode, by design -- the pipeline
    needs them. The consequence for an operator is concrete: share ``report.html``,
    never the artifact directory. Asserted so the boundary is documented in code
    rather than assumed.
    """
    cfg = build_artifacts(tmp_path / "artifacts", PLAIN_NAMES, redact=True)
    render_report(cfg)

    people_blob = cfg.people_path.read_text(encoding="utf-8")
    report_blob = cfg.report_path.read_text(encoding="utf-8")

    assert "Lovelace" in people_blob, "the fixture never wrote a name to people.jsonl"
    assert "Lovelace" not in report_blob
    assert leaks(report_blob, "", ["Lovelace", "Ada Lovelace"]) == {}


def test_redact_output_does_not_depend_on_the_names_in_the_input(tmp_path: Path) -> None:
    """The strongest form of the requirement, and the one hardest to fake.

    Two inputs identical except for every person's name must render to the same
    bytes. The run id is excepted and only the run id: it is a digest over the
    input artifacts, kept for the audit trail P-20 requires.
    """
    _cfg_a, first = render(tmp_path, "a", PLAIN_NAMES, redact=True)
    _cfg_b, second = render(tmp_path, "b", NEUTRAL_NAMES, redact=True)

    masked_first = RUN_ID.sub("run_<masked>", first)
    masked_second = RUN_ID.sub("run_<masked>", second)

    assert masked_first == masked_second, (
        "the redacted report changed when only the names changed, so it still "
        "carries name-derived information"
    )
    assert first != second, "the run id should differ; it digests the input artifacts"


def test_redact_scrubs_names_in_freetext_fields(tmp_path: Path) -> None:
    """P-15: ``"EA to Jane Doe"`` in a job title defeats field-level redaction."""
    red = make_redactor(tmp_path)

    scrubbed = red.scrub("EA to Ada Lovelace, reporting to Alan Turing")

    assert "Ada" not in scrubbed
    assert "Lovelace" not in scrubbed
    assert "Turing" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_redact_drops_freetext_written_by_other_stages(rendered: dict[str, str]) -> None:
    """A name the scrubber has never seen cannot be scrubbed, so the field is dropped.

    Evidence details routinely name target-firm employees, who by construction are
    not in ``people.jsonl``. Scrubbing by known name would leave those names in
    place; dropping the field is the only handling that fails closed.
    """
    html = rendered["redacted"]
    control = rendered["identified"]
    assert "Priya" in control and "alongside" in control, "the fixture plants no such name"

    assert "Priya" not in html, "an unknown third party's name survived in evidence detail"
    assert "Raman" not in html
    assert "alongside" not in html, "stage-authored evidence prose reached a redacted report"


def test_redact_removes_names_from_cluster_labels(rendered: dict[str, str]) -> None:
    """Cluster labels and rationales are agent-authored prose and may name people."""
    html = rendered["redacted"]
    control = rendered["identified"]
    for token in ("colleagues", "clusters around", "Advisory Partners"):
        assert token in control, f"the fixture never renders {token!r}, so hiding it proves nothing"

    assert "colleagues" not in html, "a cluster label written upstream survived"
    assert "clusters around" not in html, "a cluster rationale written upstream survived"
    assert "Advisory Partners" not in html, "a non-target employer was named in redacted mode"


def test_redacted_report_contains_no_email_addresses(rendered: dict[str, str]) -> None:
    """P-12: an address in any form is a direct identifier."""
    addresses = EMAIL_IN_TEXT.findall(rendered["redacted"])

    assert addresses == [], f"e-mail addresses in a redacted report: {addresses}"


# ===========================================================================
# identifiers  (P-12, P-13)
# ===========================================================================


def test_pseudonyms_have_the_mandated_shape(rendered: dict[str, str]) -> None:
    """P-12: ``PC-`` plus 12 Crockford base32 characters -- no I, L, O or U."""
    ids = set(PSEUDONYM.findall(rendered["redacted"]))

    assert ids, "a redacted report with no pseudonyms is not a redacted report"
    assert all(not set(pid[3:]) & set("ILOU") for pid in ids)
    loose = set(re.findall(r"PC-[0-9A-Za-z]{6,}", rendered["redacted"]))
    assert loose == ids, f"malformed pseudonyms present: {sorted(loose - ids)}"


def test_redact_ids_are_stable_across_runs(tmp_path: Path) -> None:
    """P-13: same key, same input -> same ids, so an updated report can be re-shared."""
    _cfg_a, first = render(tmp_path, "run-a", PLAIN_NAMES, redact=True)
    _cfg_b, second = render(tmp_path, "run-b", PLAIN_NAMES, redact=True)

    assert set(PSEUDONYM.findall(first)) == set(PSEUDONYM.findall(second))
    assert PSEUDONYM.findall(first)[:20] == PSEUDONYM.findall(second)[:20], (
        "pseudonyms are stable but their order is not, which breaks byte-identity"
    )


def test_redact_ids_differ_across_installs(tmp_path: Path) -> None:
    """P-13: two installs must produce unlinkable ids for the same people."""
    one = tmp_path / "install-one"
    two = tmp_path / "install-two"
    one.mkdir()
    two.mkdir()

    _cfg_a, first = render(one, "artifacts", PLAIN_NAMES, redact=True)
    _cfg_b, second = render(two, "artifacts", PLAIN_NAMES, redact=True)

    ids_one = set(PSEUDONYM.findall(first))
    ids_two = set(PSEUDONYM.findall(second))

    assert ids_one and ids_two
    assert ids_one.isdisjoint(ids_two), "two installs produced joinable pseudonyms"


def test_pseudonym_key_never_appears_in_any_artifact(tmp_path: Path) -> None:
    """P-13: the key unmasks every id in every report the operator ever shared."""
    cfg, html = render(tmp_path, "artifacts", PLAIN_NAMES, redact=True)
    key_path = pseudonym_key_path(cfg)
    key_hex = key_path.read_text(encoding="utf-8").strip()
    raw = bytes.fromhex(key_hex)
    forms = {
        key_hex,
        key_hex.upper(),
        base64.b64encode(raw).decode("ascii"),
        base64.b32encode(raw).decode("ascii"),
        base64.b16encode(raw).decode("ascii"),
    }

    assert key_path.parent not in cfg.artifact_dir.parents
    for artifact in sorted(cfg.artifact_dir.rglob("*")):
        if not artifact.is_file():
            continue
        blob = artifact.read_bytes()
        assert raw not in blob, f"the raw key bytes are in {artifact.name}"
        for form in forms:
            assert form.encode("ascii") not in blob, f"the key ({form[:8]}…) is in {artifact.name}"
    assert key_hex not in html


# ===========================================================================
# profile URLs, dates, small clusters  (P-12, P-16, P-17)
# ===========================================================================


@pytest.mark.parametrize("key", ["redacted", "identified"])
def test_profile_urls_are_never_emitted_in_either_mode(rendered: dict[str, str], key: str) -> None:
    """A profile URL is a direct identifier and a referrer leak in one string."""
    html = rendered[key]

    assert "linkedin.com" not in html.lower()
    assert "/in/" not in html
    assert "interlayer-fixture" not in html, "a profile slug reached the document"


def test_redact_quantises_connection_dates(rendered: dict[str, str]) -> None:
    """P-16: an exact date re-identifies anyone holding a similar export."""
    redacted = rendered["redacted"]
    identified = rendered["identified"]

    assert DAY_PRECISION_DATE.findall(redacted) == [], (
        f"day-precision dates in redacted output: {DAY_PRECISION_DATE.findall(redacted)[:5]}"
    )
    assert re.findall(r"\b\d{4}-\d{2}\b", redacted) == [], "month precision also re-identifies"
    assert DAY_PRECISION_DATE.findall(identified), (
        "the identified report carries no dates either, so the contrast proves nothing"
    )


def test_redact_suppresses_small_clusters(rendered: dict[str, str]) -> None:
    """P-17: a cluster of two with an employer label is a re-identification."""
    redacted = rendered["redacted"]
    identified = rendered["identified"]

    assert "c-cl_pair" not in redacted, "a two-person cluster was rendered in redacted mode"
    assert "c-cl_pair" in identified, "the fixture has no small cluster to suppress"
    assert "cluster(s) with fewer than three members" in redacted, (
        "suppression happened silently; the operator is not told anything was withheld"
    )


def test_suppressed_cluster_leaves_no_dangling_reference(rendered: dict[str, str]) -> None:
    """A suppressed cluster must not survive as a label or a dead anchor on a row."""
    redacted = rendered["redacted"]

    assert redacted.count('href="#c-cl_pair"') == 0
    assert "cl_pair" not in redacted


# ===========================================================================
# the chokepoint  (P-14)
# ===========================================================================


def test_redact_fails_closed_on_unknown_fields(tmp_path: Path) -> None:
    """P-14: a field added to the model later is dropped, not leaked."""
    red = make_redactor(tmp_path)

    emitted = red.emit(
        "person",
        {
            "id": "PC-000000000000",
            "label": "PC-000000000000",
            "score": 1.0,
            "newly_added_field": "Ada Lovelace",
            "another_new_one": {"nested": "Alan Turing"},
        },
    )

    assert "newly_added_field" not in emitted, "an un-allowlisted field survived redaction"
    assert "another_new_one" not in emitted
    assert emitted["label"] == "PC-000000000000"


def test_redact_refuses_a_record_kind_with_no_allowlist(tmp_path: Path) -> None:
    """A new record kind must stop the render rather than default to emitting."""
    red = make_redactor(tmp_path)

    with pytest.raises(InterlayerError, match="allowlist"):
        red.emit("brand_new_kind", {"label": "Ada Lovelace"})


def test_redaction_audit_raises_rather_than_writing_a_leaked_report(tmp_path: Path) -> None:
    """Gate 3: a leak is a crash, never a file somebody has already forwarded."""
    red = make_redactor(tmp_path)

    with pytest.raises(RedactionLeakError, match="redaction leak"):
        red.audit({"rows": [{"label": "PC-1", "note": "introduced by Ada Lovelace"}]})

    red.audit({"rows": [{"label": "PC-1", "note": "introduced by a mutual connection"}]})


def test_audit_leaves_curated_firm_names_alone(tmp_path: Path) -> None:
    """ "Possible Jane Street cluster" is the finding; redacting it redacts the point."""
    red = make_redactor(tmp_path, names=(("Jane", "Street"), ("Ada", "Lovelace")))

    red.audit({"label": "Jane Street"})
    assert red.scrub("Possible Jane Street cluster") == "Possible Jane Street cluster"
    assert "Lovelace" not in red.scrub("Jane Street, via Ada Lovelace")


def test_identified_mode_does_not_pseudonymise(tmp_path: Path) -> None:
    """The two modes must be genuinely different, or "redacted" means nothing."""
    _cfg, html = render(tmp_path, "identified", PLAIN_NAMES, redact=False)

    assert PSEUDONYM.findall(html) == [], "identified mode emitted pseudonyms"
    assert "Ada Lovelace" in html
    assert "Redacted mode." not in html


def test_redaction_note_states_what_was_done(rendered: dict[str, str]) -> None:
    """P-11-adjacent: a forwarded report must say what it does and does not contain."""
    html = rendered["redacted"]

    assert "Redacted mode." in html
    assert "pseudonyms" in html
    assert "allowlist" in html
    assert "<b>mode</b> redacted" in html
