"""The ``normalize`` stage end to end: orgs, affiliations and the review queue.

Everything the matcher decides only matters once it reaches an artifact, so this
file asserts the artifact-level consequences: that a bucket veto produces two
separate employers rather than one fabricated shared one, that a punctuation-only
employer field produces no employer at all, that the fund and the market maker land
in two Orgs with two ``target_firm`` values, and that every REVIEW is visible in
``review_queue.jsonl`` -- because the alternative to a review queue is a silent
accept, and a silent accept of ``The Citadel`` in an employer field puts a military
college alumnus into a hedge-fund cluster.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from interlayer.config import Settings
from interlayer.errors import StageInputMissingError
from interlayer.io import write_jsonl
from interlayer.models import (
    Affiliation,
    AffiliationKind,
    ApproxDate,
    Education,
    Org,
    OrgKind,
    Person,
    Position,
    Seniority,
    TargetFirm,
)
from interlayer.normalize.stage import ReviewItem, run
from interlayer.normalize.titles import RoleFamily, extract_role, extract_seniority, role_weight

REPO_ROOT = Path(__file__).resolve().parents[2]
GAZETTEER_PATH = REPO_ROOT / "data" / "gazetteer" / "firms.yaml"

ARTIFACTS = ("orgs.jsonl", "affiliations.jsonl", "review_queue.jsonl")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings pointed at a throwaway artifact dir and the real gazetteer.

    The gazetteer path is absolute so the tests do not depend on the process cwd.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    fields: dict[str, Any] = {"gazetteer": GAZETTEER_PATH, **overrides}
    return Settings(artifact_dir=artifacts, **fields)


def employee(slug: str, company: str, title: str = "", **kw: Any) -> Person:
    return Person.make(
        slug.capitalize(),
        "Person",
        linkedin_slug=slug,
        positions=(Position(company_raw=company, title_raw=title, **kw),),
    )


def run_with(cfg: Settings, people: list[Person]) -> None:
    write_jsonl(cfg.people_path, people)
    run(cfg)


def read_orgs(cfg: Settings) -> list[Org]:
    return [Org.model_validate_json(line) for line in _lines(cfg.orgs_path)]


def read_affiliations(cfg: Settings) -> list[Affiliation]:
    return [Affiliation.model_validate_json(line) for line in _lines(cfg.affiliations_path)]


def read_reviews(cfg: Settings) -> list[ReviewItem]:
    return [ReviewItem.model_validate_json(line) for line in _lines(cfg.review_queue_path)]


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# running out of order
# ---------------------------------------------------------------------------


def test_run_before_ingest_raises_stage_input_missing(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    with pytest.raises(StageInputMissingError) as excinfo:
        run(cfg)
    assert excinfo.value.produced_by == "ingest"
    assert excinfo.value.path == str(cfg.people_path)
    assert "interlayer ingest" in str(excinfo.value)


def test_run_before_ingest_writes_nothing(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    with pytest.raises(StageInputMissingError):
        run(cfg)
    for name in ARTIFACTS:
        assert not cfg.artifact(name).exists()


def test_a_missing_gazetteer_is_a_resolution_error_not_a_silent_empty_run(
    tmp_path: Path,
) -> None:
    from interlayer.errors import ResolutionError

    cfg = make_settings(tmp_path, gazetteer=tmp_path / "absent.yaml")
    write_jsonl(cfg.people_path, [employee("a", "Jane Street")])
    with pytest.raises(ResolutionError):
        run(cfg)


# ---------------------------------------------------------------------------
# artifact permissions
# ---------------------------------------------------------------------------


def test_artifacts_are_written_0600(tmp_path: Path) -> None:
    """These files hold identifiable data about third parties."""
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Jane Street"), employee("b", "Citadal")])
    for name in ARTIFACTS:
        path = cfg.artifact(name)
        assert path.is_file(), name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name


def test_artifact_directory_is_0700_even_if_it_started_wrong(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    cfg.artifact_dir.chmod(0o777)
    run_with(cfg, [employee("a", "Jane Street")])
    assert stat.S_IMODE(cfg.artifact_dir.stat().st_mode) == 0o700


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Jane Street")])
    assert [p.name for p in cfg.artifact_dir.glob(".tmp-*")] == []


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

_DETERMINISM_SCRIPT = textwrap.dedent(
    """
    import hashlib, json, sys
    from pathlib import Path
    from interlayer.config import Settings
    from interlayer.io import write_jsonl
    from interlayer.models import ApproxDate, Education, Person, Position
    from interlayer.normalize.stage import run

    artifacts = Path(sys.argv[1])
    gazetteer = Path(sys.argv[2])
    cfg = Settings(artifact_dir=artifacts, gazetteer=gazetteer)

    rows = [
        ("Jane Street", "Trader"), ("Citadel", "Portfolio Manager"),
        ("Citadel Securities", "Software Engineer"), ("The Citadel", "Professor"),
        ("Jane Street Coffee", ""), ("Jane Street Dental", ""),
        ("Citadel Broadcasting", ""), ("Optivar", ""), ("Citadal", "Managing Director"),
        ("Two Sigma", "Quantitative Researcher"), ("Acme Widgets", "CTO"),
    ]
    people = [
        Person.make(f"P{i}", "X", linkedin_slug=f"p{i}",
                    positions=(Position(company_raw=c, title_raw=t,
                                        start=ApproxDate(year=2015 + i)),))
        for i, (c, t) in enumerate(rows)
    ]
    people.append(Person.make("Ed", "U", linkedin_slug="ed",
                              educations=(Education(school_raw="The Citadel", degree="BS"),)))
    write_jsonl(cfg.people_path, people)
    run(cfg)
    print(json.dumps({
        name: hashlib.sha256(cfg.artifact(name).read_bytes()).hexdigest()
        for name in ("orgs.jsonl", "affiliations.jsonl", "review_queue.jsonl")
    }))
    """
)


@pytest.mark.parametrize("hashseed", ["0", "1", "12345", "999"])
def test_artifacts_are_byte_identical_across_pythonhashseed(hashseed: str, tmp_path: Path) -> None:
    """``PYTHONHASHSEED`` produced four distinct set-iteration orders during Wave 1.

    Same input plus same seed must produce byte-identical artifacts, so every set
    boundary in the stage has to be sorted. A subprocess is required: the hash seed
    is fixed at interpreter start.
    """
    script = tmp_path / "run_stage.py"
    script.write_text(_DETERMINISM_SCRIPT, encoding="utf-8")

    digests: list[dict[str, str]] = []
    for run_index, seed in enumerate(("0", hashseed)):
        artifacts = tmp_path / f"run{run_index}"
        artifacts.mkdir(mode=0o700)
        env = {**os.environ, "PYTHONHASHSEED": seed}
        completed = subprocess.run(
            [sys.executable, str(script), str(artifacts), str(GAZETTEER_PATH)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
            cwd=str(REPO_ROOT),
        )
        digests.append(json.loads(completed.stdout.strip().splitlines()[-1]))

    assert digests[0] == digests[1]


def test_two_runs_in_one_process_produce_identical_bytes(tmp_path: Path) -> None:
    cfg_a = make_settings(tmp_path / "a")
    cfg_b = make_settings(tmp_path / "b")
    people = [
        employee("a", "Jane Street", "Trader"),
        employee("b", "Citadel", "Portfolio Manager"),
        employee("c", "Citadel Securities", "SWE"),
        employee("d", "The Citadel", "Professor"),
    ]
    run_with(cfg_a, people)
    run_with(cfg_b, list(reversed(people)))
    for name in ARTIFACTS:
        digest_a = hashlib.sha256(cfg_a.artifact(name).read_bytes()).hexdigest()
        digest_b = hashlib.sha256(cfg_b.artifact(name).read_bytes()).hexdigest()
        assert digest_a == digest_b, f"{name} depends on input ordering"


def test_orgs_and_affiliations_are_written_sorted(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", "Two Sigma"),
            employee("b", "Jane Street"),
            employee("c", "Citadel"),
            employee("d", "Acme Widgets"),
        ],
    )
    orgs = read_orgs(cfg)
    assert [o.org_id for o in orgs] == sorted(o.org_id for o in orgs)
    affiliations = read_affiliations(cfg)
    keys = [(a.person_id, a.org_id, str(a.kind)) for a in affiliations]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# unusable employer strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placeholder", ["---", "...", "-", "!!!", "|", "()", "   ", "***"])
def test_punctuation_only_employers_are_skipped_entirely(placeholder: str, tmp_path: Path) -> None:
    """These previously created a shared Org.

    Everybody who typed the same placeholder got a shared-employer edge to everybody
    else who typed it -- a fabricated relationship between strangers.
    """
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", placeholder),
            employee("b", placeholder),
            employee("c", "Jane Street"),
        ],
    )
    orgs = read_orgs(cfg)
    assert [o.name for o in orgs] == ["Jane Street"]
    affiliations = read_affiliations(cfg)
    assert len(affiliations) == 1


def test_blank_employers_never_reach_the_resolver(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Blank",
        "Person",
        linkedin_slug="blank",
        positions=(Position(company_raw="   ", title_raw="Trader"),),
    )
    run_with(cfg, [person, employee("a", "Jane Street")])
    assert len(read_affiliations(cfg)) == 1
    assert len(read_orgs(cfg)) == 1


def test_two_different_placeholders_do_not_merge_anybody(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "---"), employee("b", "...")])
    assert read_orgs(cfg) == []
    assert read_affiliations(cfg) == []


# ---------------------------------------------------------------------------
# buckets never name an org
# ---------------------------------------------------------------------------


def test_a_coffee_shop_and_a_dental_practice_stay_separate_employers(tmp_path: Path) -> None:
    """Naming an org after a bucket merged these two and fabricated a co-employment edge.

    ``jane_street_generic`` stands for every business on a road called Jane Street.
    It may veto a string; it may never name one.
    """
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", "Jane Street Coffee"),
            employee("b", "Jane Street Dental"),
            employee("c", "Jane Street Entertainment"),
        ],
    )
    orgs = read_orgs(cfg)
    names = sorted(o.name for o in orgs)
    assert names == ["Jane Street Coffee", "Jane Street Dental", "Jane Street Entertainment"]
    assert len({o.org_id for o in orgs}) == 3
    assert all(o.is_target is False for o in orgs)
    assert "Businesses on a road named Jane Street" not in names

    # Three people, three different employers, no shared org between any two.
    affiliations = read_affiliations(cfg)
    assert len({a.org_id for a in affiliations}) == 3


def test_the_security_bucket_also_never_names_an_org(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Citadel Protection"), employee("b", "Citadel Risk")])
    names = sorted(o.name for o in read_orgs(cfg))
    assert names == ["Citadel Protection", "Citadel Risk"]


def test_a_named_negative_entity_does_collapse_its_spellings(tmp_path: Path) -> None:
    """The contrast: Citadel Broadcasting is one real company, so one Org."""
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", "Citadel Broadcasting"),
            employee("b", "Citadel Broadcasting Corporation"),
            employee("c", "Citadel Broadcasting Co"),
        ],
    )
    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert orgs[0].name == "Citadel Broadcasting Corporation"
    assert orgs[0].is_target is False
    assert sorted(orgs[0].aliases) == [
        "Citadel Broadcasting",
        "Citadel Broadcasting Co",
        "Citadel Broadcasting Corporation",
    ]


# ---------------------------------------------------------------------------
# the two Citadels, at artifact level
# ---------------------------------------------------------------------------


def test_the_fund_and_the_market_maker_become_two_orgs(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", "Citadel", "Portfolio Manager"),
            employee("b", "Citadel LLC", "Analyst"),
            employee("c", "Citadel Securities", "Trader"),
            employee("d", "Citadel Securities LLC", "Quant"),
        ],
    )
    orgs = {o.target_firm: o for o in read_orgs(cfg)}
    assert set(orgs) == {TargetFirm.CITADEL_LLC, TargetFirm.CITADEL_SECURITIES}
    fund = orgs[TargetFirm.CITADEL_LLC]
    maker = orgs[TargetFirm.CITADEL_SECURITIES]
    assert fund.org_id != maker.org_id
    assert fund.name == "Citadel"
    assert maker.name == "Citadel Securities"
    assert fund.is_target and maker.is_target

    # No person is affiliated with both, and no org holds both firms' people.
    affiliations = read_affiliations(cfg)
    by_org: dict[str, set[str]] = {}
    for aff in affiliations:
        by_org.setdefault(aff.org_id, set()).add(aff.person_id)
    assert len(by_org[fund.org_id]) == 2
    assert len(by_org[maker.org_id]) == 2
    assert by_org[fund.org_id].isdisjoint(by_org[maker.org_id])


def test_the_college_and_the_fund_never_share_an_org(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    alum = Person.make(
        "Alum",
        "Person",
        linkedin_slug="alum",
        educations=(Education(school_raw="The Citadel", degree="BS"),),
    )
    run_with(cfg, [alum, employee("b", "Citadel", "Portfolio Manager")])
    orgs = read_orgs(cfg)
    assert len(orgs) == 2
    college = next(o for o in orgs if o.kind is OrgKind.SCHOOL)
    fund = next(o for o in orgs if o.kind is OrgKind.COMPANY)
    assert college.name == "The Citadel, The Military College of South Carolina"
    assert college.is_target is False
    assert college.domain == "citadel.edu"
    assert fund.target_firm is TargetFirm.CITADEL_LLC
    assert college.org_id != fund.org_id


def test_citadel_in_an_education_field_is_the_college(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Cadet",
        "Person",
        linkedin_slug="cadet",
        educations=(Education(school_raw="Citadel", degree="BSc"),),
    )
    run_with(cfg, [person])
    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert orgs[0].kind is OrgKind.SCHOOL
    assert orgs[0].is_target is False


# ---------------------------------------------------------------------------
# the review queue
# ---------------------------------------------------------------------------


def test_the_ambiguous_citadel_reaches_the_review_queue(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "The Citadel", "Professor")])
    reviews = read_reviews(cfg)
    assert len(reviews) == 1
    item = reviews[0]
    assert item.raw_text == "The Citadel"
    assert item.field_kind is AffiliationKind.EMPLOYMENT
    assert item.reason.startswith("ambiguous:")
    assert item.score == 100.0
    assert set(item.candidate_names) == {
        "The Citadel, The Military College of South Carolina",
        "Citadel",
    }
    assert item.suggested_target_firm is None


def test_the_ambiguous_citadel_is_not_filed_under_either_candidate(tmp_path: Path) -> None:
    """An ambiguous string must not become a Citadel employee while it is ambiguous."""
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "The Citadel", "Professor")])
    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert orgs[0].is_target is False
    assert orgs[0].target_firm is None
    assert orgs[0].name == "The Citadel"


def test_a_guard_rejection_near_the_threshold_is_surfaced_as_curation_backlog(
    tmp_path: Path,
) -> None:
    """Something scored like a firm and was blocked by its extra tokens.

    That set is the alias-curation backlog; ordinary sub-threshold rejects are every
    unremarkable employer in the export and would drown it.
    """
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Jane Street Bagels"), employee("b", "Acme Widgets")])
    reasons = [item.reason for item in read_reviews(cfg)]
    assert any(r.startswith("guard_reject") for r in reasons)
    assert not any("Acme" in r for r in reasons)


def test_ordinary_rejects_do_not_reach_the_review_queue(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Acme Widgets"), employee("b", "Bellweather Foods")])
    assert read_reviews(cfg) == []
    assert cfg.review_queue_path.is_file(), "an empty queue is still written"


def test_review_items_are_sorted(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("z", "The Citadel", "Professor"),
            employee("a", "Optivar"),
            employee("m", "Citadal", "Analyst"),
        ],
    )
    reviews = read_reviews(cfg)
    assert [r.sort_key for r in reviews] == sorted(r.sort_key for r in reviews)


def test_the_review_queue_is_this_stages_own_path(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "The Citadel", "Professor")])
    assert cfg.review_queue_path == cfg.artifact("review_queue.jsonl")
    assert cfg.review_queue_path.is_file()


# ---------------------------------------------------------------------------
# thresholds are configuration, not constants
# ---------------------------------------------------------------------------


def test_raising_match_accept_moves_a_match_into_review(tmp_path: Path) -> None:
    """``Citadel Chicago`` scores exactly 90.0 -- the default accept boundary."""
    default = make_settings(tmp_path / "default")
    run_with(default, [employee("a", "Citadel Chicago")])
    assert read_reviews(default) == []
    assert read_orgs(default)[0].is_target is True

    strict = make_settings(tmp_path / "strict", match_accept=95.0)
    run_with(strict, [employee("a", "Citadel Chicago")])
    assert [r.reason for r in read_reviews(strict)] == ["fuzzy:citadel_llc"]


def test_raising_match_review_turns_a_review_into_a_silent_reject(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path, match_review=95.0, match_accept=99.0)
    run_with(cfg, [employee("a", "Citadal")])
    assert read_reviews(cfg) == []
    orgs = read_orgs(cfg)
    assert [o.name for o in orgs] == ["Citadal"]
    assert orgs[0].is_target is False


# ---------------------------------------------------------------------------
# affiliations
# ---------------------------------------------------------------------------


def test_dates_are_carried_through_unchanged(tmp_path: Path) -> None:
    """The co-tenure filter downstream removes ~62% of candidate edges, and it can
    only do that if the dates survive this stage."""
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Dated",
        "Person",
        linkedin_slug="dated",
        positions=(
            Position(
                company_raw="Jane Street",
                title_raw="Trader",
                start=ApproxDate(year=2019, month=6),
                end=ApproxDate(year=2023, month=1, day=15),
            ),
        ),
    )
    run_with(cfg, [person])
    aff = read_affiliations(cfg)[0]
    assert aff.start == ApproxDate(year=2019, month=6)
    assert aff.end == ApproxDate(year=2023, month=1, day=15)
    assert aff.title == "Trader"
    assert aff.kind is AffiliationKind.EMPLOYMENT


def test_educations_become_education_affiliations(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Both",
        "Person",
        linkedin_slug="both",
        positions=(Position(company_raw="Jane Street", title_raw="Trader"),),
        educations=(Education(school_raw="The Citadel", degree="BS", field_of_study="EE"),),
    )
    run_with(cfg, [person])
    kinds = sorted(str(a.kind) for a in read_affiliations(cfg))
    assert kinds == ["education", "employment"]


def test_duplicate_positions_collapse_to_one_affiliation(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Dup",
        "Person",
        linkedin_slug="dup",
        positions=(
            Position(company_raw="Jane Street", title_raw="Trader"),
            Position(company_raw="Jane Street Capital", title_raw="Trader"),
        ),
    )
    run_with(cfg, [person])
    affiliations = read_affiliations(cfg)
    assert len(affiliations) == 1
    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert sorted(orgs[0].aliases) == ["Jane Street", "Jane Street Capital"]


def test_a_review_band_match_is_confidence_damped(tmp_path: Path) -> None:
    """Only a gazetteer identification can be *wrong*, so only it is damped."""
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Optivar"), employee("b", "Acme Widgets")])
    by_person = {a.person_id: a for a in read_affiliations(cfg)}
    damped = next(a for a in by_person.values() if a.weight < 1.0)
    assert damped.weight == pytest.approx(0.857143, abs=1e-6)
    unresolved = next(a for a in by_person.values() if a.weight == 1.0)
    assert unresolved.weight == 1.0


def test_a_recruiter_affiliation_is_weighted_down(tmp_path: Path) -> None:
    """A recruiter shares an employer with everyone they hired.

    A shared-employer edge through one carries far less evidence of a real working
    relationship than the same edge through a trader.
    """
    cfg = make_settings(tmp_path)
    run_with(
        cfg,
        [
            employee("a", "Jane Street", "Campus Recruiter"),
            employee("b", "Jane Street", "Quantitative Trader"),
        ],
    )
    weights = sorted(a.weight for a in read_affiliations(cfg))
    assert weights == [pytest.approx(0.35), pytest.approx(1.0)]


def test_an_unresolved_employer_still_groups_people_together(tmp_path: Path) -> None:
    """Two people at an employer the gazetteer has never heard of still share an Org."""
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Acme Widgets Ltd"), employee("b", "Acme Widgets, Ltd.")])
    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert len({a.org_id for a in read_affiliations(cfg)}) == 1


# ---------------------------------------------------------------------------
# weak aliases need a second signal on the same profile
# ---------------------------------------------------------------------------


def test_js_resolves_only_when_the_profile_already_names_jane_street(
    tmp_path: Path,
) -> None:
    """``JS`` on its own is JavaScript far more often than it is Jane Street."""
    corroborated = make_settings(tmp_path / "yes")
    person = Person.make(
        "Two",
        "Roles",
        linkedin_slug="tworoles",
        positions=(
            Position(company_raw="Jane Street", title_raw="Trader", start=ApproxDate(year=2020)),
            Position(company_raw="JS", title_raw="Intern", start=ApproxDate(year=2018)),
        ),
    )
    run_with(corroborated, [person])
    orgs = read_orgs(corroborated)
    assert len(orgs) == 1
    assert orgs[0].target_firm is TargetFirm.JANE_STREET
    assert sorted(orgs[0].aliases) == ["JS", "Jane Street"]

    alone = make_settings(tmp_path / "no")
    run_with(alone, [employee("solo", "JS", "Developer")])
    orgs_alone = read_orgs(alone)
    assert [o.name for o in orgs_alone] == ["JS"]
    assert orgs_alone[0].is_target is False


# ---------------------------------------------------------------------------
# seniority and role extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Summer Analyst", Seniority.INTERN),
        ("Intern", Seniority.INTERN),
        ("Analyst", Seniority.JUNIOR),
        ("Senior Analyst", Seniority.MID),
        ("Associate", Seniority.MID),
        ("Vice President", Seniority.SENIOR),
        ("Senior Software Engineer", Seniority.SENIOR),
        ("Principal Engineer", Seniority.LEAD),
        ("Managing Director", Seniority.EXECUTIVE),
        ("Head of Trading", Seniority.EXECUTIVE),
        ("Chief Technology Officer", Seniority.EXECUTIVE),
        ("Partner", Seniority.EXECUTIVE),
        ("Co-Founder", Seniority.FOUNDER),
        ("Professor", Seniority.UNKNOWN),
        ("", Seniority.UNKNOWN),
    ],
)
def test_seniority_extraction(title: str, expected: Seniority) -> None:
    """Ordered regexes, most specific first. ``Summer Analyst`` is an internship,
    not an analyst rung; ``Senior Analyst`` is a rung below ``Senior <anything>``."""
    assert extract_seniority(title) is expected


def test_md_is_a_physician_until_the_employer_is_a_finance_entity() -> None:
    assert extract_seniority("MD") is Seniority.UNKNOWN
    assert extract_seniority("MD", finance_context=True) is Seniority.EXECUTIVE


def test_pm_is_a_portfolio_manager_only_in_a_finance_context() -> None:
    assert extract_role("PM") is RoleFamily.OTHER
    assert extract_role("PM", finance_context=True) is RoleFamily.PORTFOLIO_MANAGER


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Quantitative Trader", RoleFamily.QUANT_TRADER),
        ("Quantitative Developer", RoleFamily.QUANT_DEV),
        ("Quant Researcher", RoleFamily.QUANT_RESEARCHER),
        ("Portfolio Manager", RoleFamily.PORTFOLIO_MANAGER),
        ("Software Engineer", RoleFamily.SWE),
        ("Campus Recruiter", RoleFamily.RECRUITER),
        ("Sales Trader", RoleFamily.SALES),
        ("Compliance Officer", RoleFamily.RISK),
        ("Trader", RoleFamily.TRADER),
        ("Professor", RoleFamily.OTHER),
    ],
)
def test_role_extraction(title: str, expected: RoleFamily) -> None:
    assert extract_role(title) is expected


def test_role_weight_only_ever_damps() -> None:
    """A recruiter is high-reach and low-signal; a trader is the opposite."""
    assert role_weight(RoleFamily.RECRUITER) == 0.35
    assert role_weight(RoleFamily.SALES) == 0.5
    for role in RoleFamily:
        assert 0.0 < role_weight(role) <= 1.0


def test_seniority_reaches_the_review_queue_with_finance_context(tmp_path: Path) -> None:
    """The reviewer sees the title's reading, so they can judge the match on it.

    ``MD`` only reads as Managing Director because the candidate entity is a fund.
    """
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Citadal", "MD")])
    item = read_reviews(cfg)[0]
    assert item.title_raw == "MD"
    assert item.seniority is Seniority.EXECUTIVE
    assert item.candidate_key == "citadel_llc"
    assert item.suggested_target_firm is TargetFirm.CITADEL_LLC


def test_education_rows_carry_no_seniority(tmp_path: Path) -> None:
    cfg = make_settings(tmp_path)
    person = Person.make(
        "Cadet",
        "Person",
        linkedin_slug="cadet2",
        educations=(Education(school_raw="Citadel Chicago University", degree="MD"),),
    )
    run_with(cfg, [person])
    for item in read_reviews(cfg):
        assert item.seniority is Seniority.UNKNOWN


# ---------------------------------------------------------------------------
# a REVIEW verdict must not be applied on its own
# ---------------------------------------------------------------------------


def test_a_review_verdict_is_not_auto_applied_as_a_target_affiliation(
    tmp_path: Path,
) -> None:
    """``Settings.match_review``: "surfaced for human review, never auto-applied".

    ``Citadal`` (a typo, or an unrelated firm nobody has curated) scores 85.7 against
    ``citadel`` -- inside the review band. The stage queues it *and* builds the
    canonical Citadel Org from it, marked ``is_target=True`` with
    ``target_firm=CITADEL_LLC``, and attaches the person to it. Downstream scoring
    reads that as direct employment at a target firm, weighted 6.0, before any human
    has looked at the queue. The confidence damping (0.857) does not undo the
    is_target flag.

    ``The Citadel`` is handled correctly -- ``entity_key`` stays None and a
    surface-form Org is built instead -- so the fix is to treat every REVIEW the same
    way that one already is.
    """
    cfg = make_settings(tmp_path)
    run_with(cfg, [employee("a", "Citadal", "Managing Director")])

    assert len(read_reviews(cfg)) == 1, "precondition: the string is queued for review"

    orgs = read_orgs(cfg)
    assert len(orgs) == 1
    assert orgs[0].is_target is False, (
        f"a REVIEW verdict produced target Org {orgs[0].name!r} "
        f"({orgs[0].target_firm}) with no human confirmation"
    )
    assert orgs[0].target_firm is None
