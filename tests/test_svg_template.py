"""An SVG's title, desc and metadata, and ordinary templates, on tests/fixtures/svg-template.html, in both modes.

Each case on the page starts with its name. markdownify keeps all of their
text, though a browser draws none of it, and a script copies one template
onto the page, where the copy shows.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from loupe_scan.backend import Backend
from loupe_scan.render import open_renderer
from loupe_scan.report import PageReport
from loupe_scan.scan import Kind, ScanMode
from loupe_scan.session import scan_html_once

from .conftest import FIXTURES

HTML = (FIXTURES / "svg-template.html").read_text(encoding="utf-8")
URL = "https://svg-template.test/"
NEVER_DRAWN: dict[str, tuple[Kind, str]] = {
    "CASE-SVG-TITLE": (Kind.SVG_LABEL, "<title> in <svg>"),
    "CASE-SVG-DESC": (Kind.SVG_LABEL, "<desc> in <svg>"),
    "CASE-SVG-METADATA": (Kind.SVG_LABEL, "<metadata> in <svg>"),
    "CASE-TEMPLATE-UNUSED": (Kind.TEMPLATE, "<template>"),
    "CASE-SVG-IN-HIDDEN": (Kind.SVG_LABEL, "<title> in <svg>"),
    "CASE-TEMPLATE-IN-HIDDEN": (Kind.TEMPLATE, "<template>"),
}
SHOWN = {"CASE-VISIBLE", "CASE-SVG-DRAWN", "CASE-FOREIGN"}


def _case(text: str) -> str:
    return text.split()[0]


def _by_case(report: PageReport) -> dict[str, tuple[Kind, str, bool | None]]:
    found: dict[str, tuple[Kind, str, bool | None]] = {}
    for item in report.findings:
        if item.finding.kind in (Kind.META, Kind.LABEL_ATTRIBUTE):
            continue
        case = _case(item.finding.text)
        assert case not in found, f"{case} was reported twice"
        found[case] = (item.finding.kind, item.finding.detail, item.in_markdown)
    return found


@pytest.fixture(scope="module")
def rendered() -> Iterator[PageReport]:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")
    with open_renderer(None) as renderer:
        yield scan_html_once(HTML, URL, backend=Backend.LOCAL, markdown=True, renderer=renderer, supplied=True)


def test_markup_finds_svg_labels_and_templates_and_they_reach_the_markdown() -> None:
    found = _by_case(scan_html_once(HTML, URL, backend=Backend.LOCAL, markdown=True, supplied=True))
    expected = {case: (kind, detail, True) for case, (kind, detail) in NEVER_DRAWN.items()}
    # The markup can't tell that a script copies these templates onto the page.
    expected["CASE-TEMPLATE-CLONED"] = (Kind.TEMPLATE, "<template>", True)
    expected["CASE-TEMPLATE-FILLED"] = (Kind.TEMPLATE, "<template>", True)
    # The hidden element's own finding leaves out the picture's title and the template, reported on their own.
    expected["CASE-HIDDEN-AROUND"] = (Kind.STYLESHEET, "display:none", True)
    assert found == expected
    assert SHOWN.isdisjoint(found)


def test_a_rendered_scan_keeps_them_but_drops_a_template_a_script_copied_onto_the_page(rendered: PageReport) -> None:
    assert rendered.mode is ScanMode.RENDERED and rendered.html_error is None
    found = _by_case(rendered)
    expected = {case: (kind, detail, True) for case, (kind, detail) in NEVER_DRAWN.items()}
    expected["CASE-HIDDEN-AROUND"] = (Kind.RENDERED_HIDDEN, "display:none", True)
    assert found == expected
    # One copied word for word, and one with a word filled in.
    assert SHOWN.isdisjoint(found) and {"CASE-TEMPLATE-CLONED", "CASE-TEMPLATE-FILLED"}.isdisjoint(found)
