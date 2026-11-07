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

from loupe.render import (  # noqa: E402
    BLOCKABLE_RESOURCE_TYPES,
    RenderedPage,
    RenderError,
    RenderSettings,
    render_page,
    rendered_findings,
)
from loupe.scan import scan_html  # noqa: E402

from .conftest import FIXTURES  # noqa: E402
from .fixture_site import FixtureSite, serve_fixtures  # noqa: E402

FAST = RenderSettings(scroll_pause_ms=150, network_idle_timeout_ms=5_000, settle_timeout_ms=2_000)
MARKER = re.compile(r"CASE-[A-Z-]+")


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[FixtureSite]:
    with serve_fixtures() as site:
        yield site


@pytest.fixture
def fixture_site(fixture_server: FixtureSite) -> str:
    fixture_server.requested.clear()
    return fixture_server.base_url


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
def recipe(chromium: Browser, fixture_server: FixtureSite) -> RenderedPage:
    return render_page(chromium, f"{fixture_server.base_url}/rendered.html", FAST)


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
        "CASE-OPACITY-LOW": "hidden",
        "CASE-OPACITY-NESTED": "hidden",
        "CASE-OFFSCREEN": "hidden",
        "CASE-INJECTED": "hidden",
        "CASE-REVEAL": "revealed",
        "CASE-REVEAL-WHILE-IN-VIEW": "revealed",
        "CASE-CONTENT-VISIBILITY": "revealed",
    }
    assert recipe.reached_bottom
    assert recipe.notes == ()
    assert recipe.viewport == (FAST.viewport_width, FAST.viewport_height)


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
        "CASE-OPACITY-LOW": ("near-zero opacity",),
        "CASE-OPACITY-NESTED": ("near-zero opacity",),
        "CASE-OFFSCREEN": ("outside the page",),
        "CASE-INJECTED": ("opacity:0",),
    }


def test_hidden_text_goes_through_the_instruction_phrase_check(recipe: RenderedPage) -> None:
    phrases = {MARKER.findall(finding.text)[0]: finding.instruction_phrases for finding in rendered_findings(recipe)}
    assert phrases["CASE-STYLESHEET"] == ("ignore previous", "previous instructions")
    assert phrases["CASE-INJECTED"] == ("you are",)
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
    page = render_page(chromium, f"{fixture_site}/rendered.html", replace(FAST, max_scroll_steps=1, max_centre_steps=0))
    assert not page.reached_bottom
    assert page.scroll_steps == 1
    assert any("checked only by centring" in note for note in page.notes)
    assert _buckets(page)["CASE-CONTENT-VISIBILITY"] == "hidden"


def test_centring_recovers_what_a_scroll_limit_missed(chromium: Browser, fixture_site: str) -> None:
    page = render_page(chromium, f"{fixture_site}/rendered.html", replace(FAST, max_scroll_steps=1))
    assert _buckets(page)["CASE-CONTENT-VISIBILITY"] == "revealed"


def test_an_error_status_fails_the_page(chromium: Browser, fixture_site: str) -> None:
    with pytest.raises(RenderError, match="HTTP 404"):
        render_page(chromium, f"{fixture_site}/missing.html", FAST)


BLOCKED_ASSETS = {"/assets/cake.png", "/assets/icing.webm", "/assets/fixture-serif.woff2"}


def test_blocking_skips_images_media_and_fonts_but_never_stylesheets(
    chromium: Browser, fixture_server: FixtureSite, fixture_site: str
) -> None:
    remote_like = replace(FAST, blocked_resource_types=BLOCKABLE_RESOURCE_TYPES, measure_transfer=True)
    page = render_page(chromium, f"{fixture_site}/assets.html", remote_like)

    assert "/assets/style.css" in fixture_server.requested
    assert BLOCKED_ASSETS.isdisjoint(fixture_server.requested)
    # The stylesheet still applied: the line it hides is found hidden.
    assert _buckets(page) == {"CASE-ASSETS-STYLESHEET": "hidden"}
    served = (FIXTURES / "assets.html").stat().st_size + (FIXTURES / "assets" / "style.css").stat().st_size
    assert page.transfer_bytes is not None and page.transfer_bytes >= served
    # The image, the video and the font, each refused at least once.
    assert page.blocked_requests is not None and page.blocked_requests >= 3


def test_without_blocking_every_asset_is_requested_and_nothing_is_counted(
    chromium: Browser, fixture_server: FixtureSite, fixture_site: str
) -> None:
    page = render_page(chromium, f"{fixture_site}/assets.html", FAST)
    assert BLOCKED_ASSETS <= set(fixture_server.requested)
    assert page.transfer_bytes is None
    assert page.blocked_requests is None
    assert page.browser_version == chromium.version



def test_a_reveal_tied_to_the_middle_of_the_screen_is_found_by_centring(
    chromium: Browser, fixture_site: str
) -> None:
    page = render_page(chromium, f"{fixture_site}/centre.html", FAST)
    assert _buckets(page) == {"CASE-CENTRE": "revealed", "CASE-CENTRE-SECOND": "revealed"}
    assert page.centre_checks == 2


def test_screen_sized_steps_alone_skip_the_middle_band(chromium: Browser, fixture_site: str) -> None:
    # Without the centring pass the fixture line is never seen, which shows the
    # line really sits between the positions the scroll steps stop at.
    page = render_page(chromium, f"{fixture_site}/centre.html", replace(FAST, max_centre_steps=0))
    assert _buckets(page) == {"CASE-CENTRE": "hidden", "CASE-CENTRE-SECOND": "hidden"}
    assert any("stopped at the limit of 0 with 2 left" in note for note in page.notes)


def test_each_static_inline_style_candidate_is_traced_to_where_it_landed(
    chromium: Browser, fixture_site: str
) -> None:
    page = render_page(chromium, f"{fixture_site}/inline.html", FAST)
    landed = {(item.tag, MARKER.findall(item.text)[0]): item.landed.value for item in page.static_comparison}
    assert landed == {
        ("p", "CASE-INLINE-INTRO"): "visible at load",
        # The invisible layout copy is hidden, but the span a reader sees is not.
        ("div", "CASE-INLINE-TWICE"): "still hidden",
        ("span", "CASE-INLINE-TWICE"): "visible at load",
        ("p", "CASE-INLINE-HIDDEN"): "still hidden",
        ("p", "CASE-INLINE-REWRITTEN"): "not matched",
        # Hidden at load and replaced: only the final page holds the copy that showed.
        ("span", "CASE-INLINE-SWAPPED"): "revealed on scroll",
        ("p", "CASE-INLINE-REVEAL"): "revealed on scroll",
    }


def test_a_page_whose_images_slip_past_blocking_fails(chromium: Browser, fixture_site: str) -> None:
    # Blocking is on, but only for media, so the photo still loads: the run
    # must refuse to trust its bandwidth rather than carry on quietly.
    settings = replace(FAST, blocked_resource_types=frozenset({"media"}), measure_transfer=True)
    with pytest.raises(RenderError, match="1 images fetched over http"):
        render_page(chromium, f"{fixture_site}/picture.html", settings)


def test_only_text_hidden_the_way_scroll_animations_hide_it_is_centred(
    chromium: Browser, fixture_site: str
) -> None:
    page = render_page(chromium, f"{fixture_site}/centre-eligibility.html", FAST)
    # The faded line and the slid-away line, not visibility, display:none or a closed details.
    assert page.centre_checks == 2
    assert not any("centred" in note for note in page.notes)


def test_the_centring_cap_is_noted(chromium: Browser, fixture_site: str) -> None:
    page = render_page(chromium, f"{fixture_site}/centre.html", replace(FAST, max_centre_steps=1))
    assert page.centre_checks == 1
    assert any("stopped at the limit of 1 with 1 left" in note for note in page.notes)


def test_the_centring_time_budget_is_noted(chromium: Browser, fixture_site: str) -> None:
    page = render_page(chromium, f"{fixture_site}/centre.html", replace(FAST, centre_budget_ms=0))
    assert page.centre_checks == 0
    assert any("stopped at the 0 s budget with 2 left" in note for note in page.notes)

