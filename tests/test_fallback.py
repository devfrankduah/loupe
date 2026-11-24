"""Text inside elements whose content browsers never show, on tests/fixtures/fallback.html, in both scan modes.

Each case on the page starts with its name. markdownify keeps all of it, so
an agent reading the markdown gets it while a person sees none of it.
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

HTML = (FIXTURES / "fallback.html").read_text(encoding="utf-8")
URL = "https://fallback.test/"
FALLBACK: dict[str, str] = {
    "CASE-IFRAME-CONTENT": "<iframe>",
    # The browser keeps this one's tags as text, so none of its words are on the page as served, and it mustn't
    # also count as text a script removed. Its name is short so that no tag holds enough text to compare alone.
    "CASE-TAGGED": "<iframe>",
    "CASE-VIDEO-FALLBACK": "<video>",
    "CASE-AUDIO-FALLBACK": "<audio>",
    "CASE-CANVAS-FALLBACK": "<canvas>",
    "CASE-NOFRAMES": "<noframes>",
    "CASE-NOEMBED": "<noembed>",
    "CASE-IFRAME-IN-HIDDEN": "<iframe>",
    "CASE-NESTED-FALLBACK": "<video>",
}
SHOWN = {"CASE-VISIBLE", "CASE-FRAME-DOCUMENT"}


def _case(text: str) -> str:
    return text.split()[0]


def _by_case(report: PageReport) -> dict[str, tuple[Kind, str, bool | None]]:
    found: dict[str, tuple[Kind, str, bool | None]] = {}
    for item in report.findings:
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


def test_markup_finds_fallback_content_and_it_reaches_the_markdown() -> None:
    report = scan_html_once(HTML, URL, backend=Backend.LOCAL, markdown=True, supplied=True)
    found = _by_case(report)
    expected = {case: (Kind.FALLBACK_CONTENT, detail, True) for case, detail in FALLBACK.items()}
    expected["CASE-HIDDEN-AROUND"] = (Kind.STYLESHEET, "display:none", True)
    assert found == expected


def test_a_rendered_scan_keeps_the_markup_finding_for_fallback_content(rendered: PageReport) -> None:
    assert rendered.mode is ScanMode.RENDERED and rendered.html_error is None
    found = _by_case(rendered)
    expected = {case: (Kind.FALLBACK_CONTENT, detail, True) for case, detail in FALLBACK.items()}
    # The browser judges the hidden element's own text and drops the stylesheet guess, but not the frame inside it.
    expected["CASE-HIDDEN-AROUND"] = (Kind.RENDERED_HIDDEN, "display:none", True)
    assert found == expected
    assert SHOWN.isdisjoint(found)
    # Each <iframe> is a frame too: the shown one's document has nothing hidden, and the others are empty.
    assert rendered.rendered is not None and rendered.rendered.frames_checked == 4
