"""PRIV-17 — contact details are stripped from every rendered output.

Unconditionally. ``retain_emails`` decides whether an address is kept in the
local store; it has no bearing on whether one may appear in a document that
leaves the machine.
"""

from __future__ import annotations

import pytest

from interlayer.report import render_html, render_json, render_markdown
from interlayer.report.redact import (
    EMAIL_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    has_contact_details,
    redact_text,
    redact_value,
)
from test_report_fixtures import LEAKY_NAMES, make_ctx, make_result

RENDERERS = (render_markdown, render_html, render_json)

EMAILS = [
    "dana.wu@example.com",
    "priya+work@sub.example.co.uk",
    "j.doe@janestreet.example",
    "mailto:alex@example.org",
    "UPPER.CASE@Example.COM",
]

PHONES = [
    "+1 (212) 555-0142",
    "212-555-0142",
    "+44 20 7123 4567",
    "+442071234567",
    "2125550142",
    "tel:+1-212-555-0142",
    "020 7123 4567",
]

#: Numbers this tool legitimately prints. Redacting any of these would be a
#: visible defect, so they are pinned as negatives.
NOT_PHONES = [
    "2026-08-17",
    "3.14159265",
    "0.35",
    "150-250",
    "1,000",
    "networkx==3.6.1",
    "scipy 1.17.1",
    "119.99",
    "9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f",
    "m_alpha",
    "coverage 47.0%",
]


@pytest.mark.parametrize("email", EMAILS)
def test_emails_are_removed(email: str) -> None:
    out = redact_text(f"contact: {email} (preferred)")
    assert email not in out
    assert EMAIL_PLACEHOLDER in out
    assert not has_contact_details(out)


@pytest.mark.parametrize("phone", PHONES)
def test_phone_numbers_are_removed(phone: str) -> None:
    out = redact_text(f"call {phone} any time")
    assert phone not in out
    assert PHONE_PLACEHOLDER in out
    assert not has_contact_details(out)


@pytest.mark.parametrize("value", NOT_PHONES)
def test_legitimate_numbers_survive(value: str) -> None:
    assert redact_text(f"value {value} here") == f"value {value} here"


def test_redaction_is_idempotent() -> None:
    once = redact_text("mail me at dana@example.com or +1 212-555-0142")
    assert redact_text(once) == once


def test_redact_value_walks_nested_structures() -> None:
    payload = {
        "dana@example.com": ["+1 212-555-0142", {"deep": "x@y.example"}],
        "keep": 42,
        "flag": True,
    }
    out = redact_value(payload)
    assert EMAIL_PLACEHOLDER in out
    assert out[EMAIL_PLACEHOLDER][0] == PHONE_PLACEHOLDER
    assert out[EMAIL_PLACEHOLDER][1]["deep"] == EMAIL_PLACEHOLDER
    assert out["keep"] == 42
    assert out["flag"] is True


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_no_contact_details_survive_any_renderer(renderer) -> None:
    """PRIV-17 proper: every format, with contact details in the input."""
    result = make_result(used_inferred_edges=True, unadjudicated_count=2)
    ctx = make_ctx(names=LEAKY_NAMES)
    rendered = renderer(result, ctx)

    for email in ("dana.wu@example.com", "priya@sub.example.co.uk", "j.doe@janestreet.example"):
        assert email not in rendered
    for phone in ("212) 555-0142", "7123 4567", "212-555-9987"):
        assert phone not in rendered
    assert not has_contact_details(rendered)


@pytest.mark.parametrize("renderer", RENDERERS, ids=lambda f: f.__name__)
def test_redaction_ignores_retain_emails(renderer, monkeypatch) -> None:
    """Config cannot switch this off; there is no switch to find.

    The renderers take no config object at all — the retention period is the
    only config value that reaches them — so retaining emails in the store
    cannot change what a report contains.
    """
    monkeypatch.setenv("INTERLAYER_RETAIN_EMAILS", "1")
    rendered = renderer(make_result(), make_ctx(names=LEAKY_NAMES))
    assert not has_contact_details(rendered)


def test_still_renders_the_rest_of_the_name() -> None:
    """Redaction removes the contact detail, not the person."""
    rendered = render_markdown(make_result(), make_ctx(names=LEAKY_NAMES))
    assert "Dana Wu" in rendered
    assert "Alex Rivera" in rendered
    assert "Priya Nair" in rendered
