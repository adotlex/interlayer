"""The shipped documentation must say the things it is required to say.

The runbook is the only place a user learns how to acquire edges at all, and
the only place the risk of doing so is stated. A runbook that quietly loses its
risk section, or starts claiming a path is "compliant", is a worse defect than a
failing renderer — so the load-bearing claims are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
RUNBOOK = REPO / "docs" / "02-operator-runbook.md"


def test_the_documents_exist_and_are_substantial() -> None:
    for doc in (README, RUNBOOK):
        assert doc.exists(), f"{doc} is missing"
        assert len(doc.read_text(encoding="utf-8").strip()) > 1000, f"{doc} is a stub"


@pytest.mark.parametrize(
    "phrase",
    [
        "Connections of",  # the inversion filter
        "Current company",  # the server-side company filter
        "1st-degree",  # why the inversion works
        "server-side",  # why it minimises data
        "mutual connections",  # the backfill
        "3rd degree",  # degree pruning
        "Commercial Use Limit",  # the quota
        "150",  # targets per month, free tier
        "119.99",  # Sales Navigator Core
        "30-day",  # the trial
        "Advanced",  # the tier that is explicitly not recommended
        "TeamLink",  # why Advanced is useless to a solo user
        "multi-seat",  # what TeamLink actually requires
        "6,200",  # extension fingerprinting
        "8.2",  # the User Agreement clause
        "2026-08-17",  # the UI-drift date
    ],
)
def test_runbook_covers_the_mandated_ground(phrase: str) -> None:
    assert phrase in RUNBOOK.read_text(encoding="utf-8"), f"runbook never mentions {phrase!r}"


def test_runbook_presents_a_gradient_rather_than_a_compliance_claim() -> None:
    """§8.2(3) reaches even manual copying, so no path may be sold as clean."""
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "risk gradient" in text.lower()
    assert "reaches manual copying" in text
    # Every rung of the gradient is named.
    for rung in ("data export", "Manual browsing", "HAR", "extension", "automation", "Headless"):
        assert rung in text, f"the gradient omits {rung!r}"
    # Account risk and legal risk are distinguished.
    assert "Account risk" in text
    assert "Legal risk" in text


def test_runbook_never_claims_compliance_affirmatively() -> None:
    """Every mention of compliance must be a denial of one.

    R4 established that §8.2(3) reaches even manual copying, so there is no
    path here to describe as clean. This checks the whole document rather than
    a blocklist of phrasings: each occurrence of the word has to sit next to a
    negation, which is only true if the sentence is disclaiming compliance.
    """
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    negations = ("not ", "no ", "nothing", "cannot", "never", "otherwise")

    occurrences = [i for i in range(len(text)) if text.startswith("complian", i)]
    assert occurrences, "the runbook never addresses compliance at all"

    for index in occurrences:
        window = text[max(0, index - 160) : index + 160]
        assert any(n in window for n in negations), (
            f"an affirmative compliance claim near: ...{text[index - 80 : index + 80]}..."
        )

    for claim in ("perfectly legal", "completely legal", "permitted by linkedin", "tos-compliant"):
        assert claim not in text, f"runbook claims {claim!r}"

    assert "does not tell you that anything here is compliant" in text


def test_runbook_warns_that_the_ui_may_have_changed() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "UI drift" in text
    assert "could not" in text or "no agent" in text.lower()
    assert "verify" in text


def test_readme_states_plainly_what_the_tool_cannot_do() -> None:
    text = README.read_text(encoding="utf-8")
    assert "What it cannot do" in text
    assert "cannot buy" in text
    assert "scrape" in text
    assert "authenticated session" in text
    assert "Commercial Use Limit" in text or "quota" in text
    assert "same rate" in text or "same ceiling" in text


def test_readme_documents_every_command() -> None:
    from interlayer.cli import app

    text = README.read_text(encoding="utf-8")
    for command in app.registered_commands:
        name = command.name or (command.callback.__name__ if command.callback else "")
        assert name and f"`{name}`" in text, f"README never mentions the {name!r} command"
