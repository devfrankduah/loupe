"""Rendered scans of local fixture pages in real Chromium.

Every test here is skipped when Playwright or its Chromium isn't installed.
The pages are served from tests/fixtures on 127.0.0.1, so nothing leaves the
machine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, sync_playwright  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402

from loupe.render import RenderedPage, RenderError, RenderSettings, render_page, rendered_findings  # noqa: E402
from loupe.scan import scan_html  # noqa: E402

from .fixture_site import serve_fixtures  # noqa: E402

FAST = RenderSettings(scroll_pause_ms=150, network_idle_timeout_ms=5_000, settle_timeout_ms=2_000)
MARKER = re.compile(r"CASE-[A-Z-]+")


@pytest.fixture(scope="module")
def fixture_site() -> Iterator[str]:
    with serve_fixtures() as base_url:
        yield base_url


@pytest.fixture(scope="module")
def chromium() -> Iterator[Browser]:
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
    except PlaywrightError as error:
        playwright.stop()
        pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")
    try:
        yield browser
    finally:
        browser.close()
        playwright.stop()


@pytest.fixture(scope="module")
def recipe(chromium: Browser, fixture_site: str) -> RenderedPage:
    return render_page(chromium, f"{fixture_site}/rendered.html", FAST)


def _buckets(page: RenderedPage) -> dict[str, str]:
    buckets: dict[str, str] = {}
    for bucket, items in (("hidden", page.hidden), ("revealed", page.revealed_on_scroll)):
        for item in items:
            markers = MARKER.findall(item.text)
            assert len(markers) == 1, f"one case per finding, got {markers} in {item.text!r}"
            buckets[markers[0]] = bucket
    return buckets


def _reasons(page: RenderedPage) -> dict[str, tuple[str, ...]]:
    return {MARKER.findall(item.text)[0]: item.reasons for item in page.hidden}


def test_each_case_lands_in_its_bucket(recipe: RenderedPage) -> None:
    assert _buckets(recipe) == {
        "CASE-SR-ONLY": "hidden",
        "CASE-STYLESHEET": "hidden",
        "CASE-HIDDEN-ATTRIBUTE": "hidden",
        "CASE-DETAILS": "hidden",
        "CASE-COLLAPSED": "hidden",
        "CASE-CLIP": "hidden",
        "CASE-CLIP-PATH": "hidden",
        "CASE-VIS-HIDDEN": "hidden",
        "CASE-FONT-SIZE": "hidden",
        "CASE-OFFSCREEN": "hidden",
        "CASE-INJECTED": "hidden",
        "CASE-REVEAL": "revealed",
        "CASE-REVEAL-WHILE-IN-VIEW": "revealed",
        "CASE-CONTENT-VISIBILITY": "revealed",
    }
    assert recipe.reached_bottom
    assert recipe.notes == ()


def test_hidden_text_says_how_it_was_hidden(recipe: RenderedPage) -> None:
    assert _reasons(recipe) == {
        "CASE-SR-ONLY": ("clipped",),
        "CASE-STYLESHEET": ("display:none",),
        "CASE-HIDDEN-ATTRIBUTE": ("hidden attribute",),
        "CASE-DETAILS": ("inside a closed <details>",),
        "CASE-COLLAPSED": ("clipped",),
        "CASE-CLIP": ("clipped",),
        "CASE-CLIP-PATH": ("clipped",),
        "CASE-VIS-HIDDEN": ("visibility:hidden",),
        "CASE-FONT-SIZE": ("font-size:0", "too small to see"),
        "CASE-OFFSCREEN": ("outside the page",),
        "CASE-INJECTED": ("opacity:0",),
    }


def test_hidden_text_goes_through_the_instruction_phrase_check(recipe: RenderedPage) -> None:
    phrases = {MARKER.findall(finding.text)[0]: finding.instruction_phrases for finding in rendered_findings(recipe)}
    assert phrases["CASE-STYLESHEET"] == ("ignore previous", "previous instructions")
    assert phrases["CASE-INJECTED"] == ("you are", "assistant")
    assert phrases["CASE-DETAILS"] == ()


def test_the_served_html_is_kept_for_the_markup_checks(recipe: RenderedPage) -> None:
    static_texts = [finding.text for finding in scan_html(recipe.html)]
    assert any("CASE-COMMENT" in text for text in static_texts)
    # The injected line only exists once scripts run, so only rendering sees it.
    assert not any("CASE-INJECTED" in text for text in static_texts)


def test_a_page_cannot_fool_the_scan_by_patching_the_dom(chromium: Browser, fixture_site: str) -> None:
    page = render_page(chromium, f"{fixture_site}/tampered.html", FAST)
    assert _buckets(page) == {"CASE-TAMPERED": "hidden"}


def test_a_scroll_limit_is_reported_with_what_it_costs(chromium: Browser, fixture_site: str) -> None:
    page = render_page(chromium, f"{fixture_site}/rendered.html", replace(FAST, max_scroll_steps=1))
    assert not page.reached_bottom
    assert page.scroll_steps == 1
    assert any("counts as hidden" in note for note in page.notes)
    assert _buckets(page)["CASE-CONTENT-VISIBILITY"] == "hidden"


def test_an_error_status_fails_the_page(chromium: Browser, fixture_site: str) -> None:
    with pytest.raises(RenderError, match="HTTP 404"):
        render_page(chromium, f"{fixture_site}/missing.html", FAST)
