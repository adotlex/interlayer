"""The review queue — PRIV-20's human loop."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from interlayer.core.models import Firm, MatchStatus
from interlayer.ingest import ReviewError
from interlayer.ingest.company_match import (
    CompanyMatcher,
    is_admissible,
    require_admissible,
)
from interlayer.ingest.person_match import PersonRecord, dedupe
from interlayer.ingest.review import (
    DECISIONS_FILENAME,
    NO_FIRM,
    ReviewQueue,
    default_decisions_path,
    load_decisions,
)
from interlayer.ingest.targets import load_registry

STAMP = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)

#: A slice of a plausible Connections.csv "Company" column.
CORPUS = [
    "Jane Street",
    "Jane Street Capital",
    "Citadel Securities",
    "Citadel Technology",
    "Citadel Technology",
    "Citadel Technology",
    "Citadel Capital",
    "JS",
    "Goldman Sachs",
    "",
]


@pytest.fixture
def matcher() -> CompanyMatcher:
    return CompanyMatcher(load_registry())


@pytest.fixture
def queue(matcher: CompanyMatcher) -> ReviewQueue:
    q = ReviewQueue()
    q.observe_many((matcher.explain(raw) for raw in CORPUS), seen_at=STAMP)
    return q


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_only_needs_review_items_are_queued(queue: ReviewQueue) -> None:
    assert {item.key for item in queue.pending()} == {
        "citadel technology",
        "citadel capital",
        "js",
    }


def test_pending_is_ordered_commonest_first_then_alphabetically(queue: ReviewQueue) -> None:
    keys = [item.key for item in queue.pending()]
    assert keys == ["citadel technology", "citadel capital", "js"]
    assert queue.pending()[0].occurrences == 3


def test_items_carry_the_suggestion_the_rule_and_the_raw_spellings(queue: ReviewQueue) -> None:
    item = queue.item("citadel technology")
    assert item.suggested_firm is Firm.CITADEL
    assert "unexpected_tokens" in item.rule
    assert item.raw_examples == ("Citadel Technology",)
    assert item.first_seen == STAMP
    assert item.raw == "Citadel Technology"


def test_distinct_spellings_of_one_key_collapse_to_one_item(matcher: CompanyMatcher) -> None:
    """Review volume is bounded by distinct *normalised* strings, not rows."""
    q = ReviewQueue()
    q.observe_many(matcher.explain(r) for r in ("Citadel Technology", "CITADEL  TECHNOLOGY"))
    assert len(q.pending()) == 1
    assert q.pending()[0].occurrences == 2
    assert len(q.pending()[0].raw_examples) == 2


def test_the_unadjudicated_count_is_what_the_report_states(queue: ReviewQueue) -> None:
    assert queue.unadjudicated_count == 3


# ---------------------------------------------------------------------------
# Adjudicating
# ---------------------------------------------------------------------------


def test_adjudicate_one_item(queue: ReviewQueue) -> None:
    decision = queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    assert decision.firm is Firm.CITADEL
    assert decision.decided_by == "user"
    assert queue.unadjudicated_count == 2
    assert "citadel technology" not in {i.key for i in queue.pending()}


def test_adjudicating_to_none_rejects_the_string(queue: ReviewQueue) -> None:
    queue.adjudicate("citadel capital", None, decided_at=STAMP)
    assert queue.decision_map()["citadel capital"] is None
    assert queue.unadjudicated_count == 2


def test_a_firm_may_be_named_as_a_string_or_the_none_sentinel(queue: ReviewQueue) -> None:
    assert queue.adjudicate("js", "jane_street", decided_at=STAMP).firm is Firm.JANE_STREET
    assert queue.adjudicate("citadel capital", NO_FIRM, decided_at=STAMP).firm is None


def test_adjudicating_an_unknown_key_is_an_error_not_a_silent_no_op(queue: ReviewQueue) -> None:
    with pytest.raises(ReviewError, match="not in the review queue"):
        queue.adjudicate("citadel tecnology", Firm.CITADEL)


def test_adjudicating_to_an_unknown_firm_is_an_error(queue: ReviewQueue) -> None:
    with pytest.raises(ReviewError, match="not a known firm"):
        queue.adjudicate("citadel technology", "two_sigma")


def test_a_registry_without_a_firm_refuses_a_decision_for_it(matcher: CompanyMatcher) -> None:
    q = ReviewQueue(known_firms=(Firm.JANE_STREET,))
    q.observe(matcher.explain("Citadel Technology"))
    with pytest.raises(ReviewError, match="not in this registry"):
        q.adjudicate("citadel technology", Firm.CITADEL)


# ---------------------------------------------------------------------------
# The barrier, before and after adjudication
# ---------------------------------------------------------------------------


def test_a_review_item_cannot_reach_the_graph_until_it_is_decided(
    matcher: CompanyMatcher,
) -> None:
    queue = ReviewQueue()
    before = queue.observe(matcher.explain("Citadel Technology"))
    assert before.status is MatchStatus.NEEDS_REVIEW
    assert is_admissible(before) is False

    queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    after = queue.observe(matcher.explain("Citadel Technology"))
    assert after.status is MatchStatus.CONFIDENT
    assert require_admissible(after) is Firm.CITADEL
    assert after.rule == "cached_decision"


def test_a_rejection_keeps_it_out_permanently(matcher: CompanyMatcher) -> None:
    queue = ReviewQueue()
    queue.observe(matcher.explain("Citadel Capital"))
    queue.adjudicate("citadel capital", None, decided_at=STAMP)
    decided = queue.observe(matcher.explain("Citadel Capital"))
    assert decided.status is MatchStatus.NO_MATCH
    assert is_admissible(decided) is False


def test_decisions_feed_straight_back_into_the_matcher(queue: ReviewQueue) -> None:
    queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    warm = CompanyMatcher(load_registry(), decisions=queue.decision_map())
    assert warm.classify("Citadel Technology").firm is Firm.CITADEL
    assert warm.classify("Citadel Technology").rule == "cached_decision"


def test_confident_and_no_match_are_never_queued(matcher: CompanyMatcher) -> None:
    queue = ReviewQueue()
    queue.observe(matcher.explain("Jane Street"))
    queue.observe(matcher.explain("Goldman Sachs"))
    assert queue.pending() == ()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_decisions_round_trip_through_yaml(queue: ReviewQueue, tmp_path: Path) -> None:
    queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    queue.adjudicate("citadel capital", None, decided_at=STAMP)
    path = queue.save(tmp_path / DECISIONS_FILENAME)

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert payload["decisions"]["citadel technology"]["entity_id"] == "citadel"
    assert payload["decisions"]["citadel capital"]["entity_id"] == NO_FIRM
    assert payload["decisions"]["citadel technology"]["decided_by"] == "user"

    fresh = ReviewQueue()
    assert fresh.load(path) == 2
    assert fresh.decision_map() == {"citadel technology": Firm.CITADEL, "citadel capital": None}


def test_loading_a_missing_cache_is_not_an_error(tmp_path: Path) -> None:
    assert ReviewQueue().load(tmp_path / "absent.yaml") == 0


def test_a_corrupt_cache_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / DECISIONS_FILENAME
    path.write_text("decisions:\n  'x': {entity_id: two_sigma, decided_at: '2026-01-01'}\n")
    with pytest.raises(ReviewError, match="unknown entity_id"):
        load_decisions(path)

    path.write_text("decisions:\n  'x': {entity_id: citadel, decided_at: 'whenever'}\n")
    with pytest.raises(ReviewError, match="unparseable decided_at"):
        load_decisions(path)

    path.write_text("decisions: [not, a, mapping]\n")
    with pytest.raises(ReviewError, match="must be a mapping"):
        load_decisions(path)


def test_an_empty_cache_file_loads_as_no_decisions(tmp_path: Path) -> None:
    path = tmp_path / DECISIONS_FILENAME
    path.write_text("")
    assert load_decisions(path) == ()


def test_default_path_follows_the_state_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INTERLAYER_STATE_ROOT", "/tmp/interlayer-test")
    assert default_decisions_path() == Path("/tmp/interlayer-test") / DECISIONS_FILENAME
    monkeypatch.delenv("INTERLAYER_STATE_ROOT")
    assert default_decisions_path() == Path.home() / ".interlayer" / DECISIONS_FILENAME


# ---------------------------------------------------------------------------
# Audit (PRIV-15)
# ---------------------------------------------------------------------------


def test_audit_events_carry_identifiers_and_counts_but_no_free_text(
    queue: ReviewQueue,
) -> None:
    queue.adjudicate("citadel technology", Firm.CITADEL, decided_at=STAMP)
    event = queue.audit_event("review_adjudicate", "citadel technology")
    assert event["entity_id"] == "citadel"
    assert event["pending"] == 2
    assert "citadel technology" not in str(event)
    assert isinstance(event["key_hash"], str)


# ---------------------------------------------------------------------------
# Person pairs are counted separately: they never block an edge
# ---------------------------------------------------------------------------


def test_person_pairs_are_tracked_apart_from_the_company_barrier(queue: ReviewQueue) -> None:
    pairs = dedupe(
        [
            PersonRecord(record_id="m1", first_name="Robert", last_name="Smith", slug="a"),
            PersonRecord(record_id="m2", first_name="Robert", last_name="Smith", slug="b"),
        ]
    ).review_pairs
    queue.add_person_pairs(pairs)
    assert queue.person_pending_count == 1
    assert queue.unadjudicated_count == 3  # unchanged: PRIV-20 is company-side
    assert queue.pending_person_pairs() == pairs
