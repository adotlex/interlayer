"""The HTML report must be self-contained, well-formed and actually readable."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import ClassVar

from interlayer.report import render_html
from test_report_fixtures import make_ctx, make_result


class _Balance(HTMLParser):
    VOID: ClassVar[set[str]] = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
    }

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.mismatched: list[str] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.mismatched.append(tag)


def _render(**kwargs) -> str:
    return render_html(make_result(**kwargs), make_ctx(names={"m_alpha": "Dana Wu"}))


def test_document_is_well_formed() -> None:
    parser = _Balance()
    parser.feed(_render())
    assert parser.mismatched == []
    assert parser.stack == []


def test_document_is_entirely_self_contained() -> None:
    """No stylesheet, font, script, image or fetch. Opening it is offline."""
    html = _render()
    external = re.findall(r'(?:src|href)\s*=\s*"([^"#][^"]*)"', html)
    assert external == [], f"external references: {external}"
    assert "<script" not in html.lower()
    assert "@import" not in html
    assert "http://" not in html
    assert "https://" not in html


def test_stylesheet_is_inline_and_handles_both_themes() -> None:
    html = _render()
    assert "<style>" in html
    assert "color-scheme: light dark" in html
    assert "@media (prefers-color-scheme: dark)" in html
    # Both palettes must define the same tokens, or one theme renders blind.
    light = html.split("@media (prefers-color-scheme: dark)")[0]
    dark = html.split("@media (prefers-color-scheme: dark)")[1]
    for token in ("--bg", "--surface", "--ink", "--line", "--warn-bg", "--warn-ink"):
        assert f"{token}:" in light, f"{token} missing from the light palette"
        assert f"{token}:" in dark, f"{token} missing from the dark palette"


def test_it_contains_the_three_things_a_reader_came_for() -> None:
    html = _render()
    assert "Bridge ranking" in html
    assert 'class="card cluster"' in html  # cluster cards
    assert "Who to look at next" in html
    assert 'class="next"' in html


def test_cluster_cards_name_their_members_and_targets() -> None:
    html = _render()
    assert "options | market making" in html
    assert "infrastructure | systems" in html
    assert "Dana Wu" in html
    assert "Reaches into" in html


def test_names_are_escaped_not_interpreted() -> None:
    """Display names are third-party free text from a web page."""
    html = render_html(
        make_result(),
        make_ctx(names={"m_alpha": '<script>alert(1)</script>', "m_beta": 'a"b&c'}),
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;c" in html


def test_wide_tables_scroll_inside_their_own_container() -> None:
    html = _render()
    assert 'class="scroll"' in html
    assert "overflow-x: auto" in html


def test_empty_result_renders_without_blank_sections() -> None:
    html = render_html(make_result(empty=True), make_ctx())
    assert "No bridges" in html
    assert "No clusters" in html
    assert 'class="empty"' in html
