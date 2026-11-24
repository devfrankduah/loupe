"""The rendered scan's reach into shadow roots and frames, in Chromium, on tests/fixtures/frames.html.

Each case on the page starts with its name. The page loads from 127.0.0.1
and builds its cross-origin frame from the same server under the name
localhost, which is another origin and another site.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from loupe_scan.public_proxy import REFUSED_MESSAGE
from loupe_scan.render import LOCAL_SETTINGS, RenderedPage, Renderer, RenderSettings, Where, render_page

from .conftest import FIXTURES
from .fixture_site import FixtureSite, serve_fixtures

HIDDEN: dict[str, tuple[str, Where]] = {
    "CASE-OPEN-SHADOW-HIDDEN": ("outside the page", Where.SHADOW_ROOT),
    "CASE-CLOSED-SHADOW-HIDDEN": ("display:none", Where.SHADOW_ROOT),
    "CASE-HOST-TEXT-HIDDEN": ("outside the page", Where.SHADOW_ROOT),
    "CASE-SLOTTED-INTO-HIDDEN": ("display:none", Where.PAGE),
    "CASE-UNSLOTTED": ("not rendered", Where.PAGE),
    "CASE-SCRIPTED-HIDDEN": ("display:none", Where.SHADOW_ROOT),
    "CASE-SCRIPTED-CLOSED-HIDDEN": ("opacity:0", Where.SHADOW_ROOT),
    "CASE-FRAME-HIDDEN": ("outside the page", Where.FRAME),
    "CASE-SHADOW-IN-FRAME-HIDDEN": ("font-size:0, too small to see", Where.SHADOW_ROOT_IN_FRAME),
    "CASE-CROSS-HIDDEN": ("display:none", Where.FRAME),
    "CASE-SRCDOC-HIDDEN": ("too small to see", Where.HIDDEN_FRAME),
    "CASE-DISPLAY-NONE-FRAME": ("display:none", Where.HIDDEN_FRAME),
    "CASE-INSIDE-HIDDEN-FRAME": ("display:none", Where.HIDDEN_FRAME),
    "CASE-CLOSED-IN-FRAME-HIDDEN": ("visibility:hidden", Where.SHADOW_ROOT_IN_FRAME),
    "CASE-NESTED-HIDDEN": ("outside the page", Where.FRAME),
}
VISIBLE = {
    "CASE-OPEN-SHADOW-VISIBLE",
    "CASE-CLOSED-SHADOW-VISIBLE",
    "CASE-SLOTTED-VISIBLE",
    "CASE-SHADOW-WHITE-ON-DARK",
    "CASE-SCRIPTED-VISIBLE",
    "CASE-FRAME-VISIBLE",
    "CASE-CROSS-VISIBLE",
    "CASE-NESTED-VISIBLE",
    "CASE-PAGE-VISIBLE",
}
# The frame on a port nothing listens on, and the one showing only loupe's refusal.
UNCHECKED_NOTE = (
    "2 frames couldn't be checked, such as one that didn't load or that loupe refused, so whether the text in them "
    "shows wasn't judged"
)


@pytest.fixture(scope="module")
def chromium() -> None:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")


@pytest.fixture(scope="module")
def site() -> Iterator[FixtureSite]:
    with serve_fixtures() as served:
        yield served


def _render(site: FixtureSite, settings: RenderSettings = LOCAL_SETTINGS, *, isolate_sites: bool = False) -> RenderedPage:
    from playwright.sync_api import Browser, ProxySettings, sync_playwright

    with sync_playwright() as playwright:

        class Launcher:
            """Local Chromium, optionally running each site's frames in a process of their own as a remote browser may."""

            def launch(self, *, headless: bool | None = None, proxy: ProxySettings | None = None) -> Browser:
                return playwright.chromium.launch(headless=True, args=["--site-per-process"] if isolate_sites else [])

            def connect_over_cdp(self, endpoint_url: str, *, timeout: float | None = None) -> Browser:
                raise AssertionError("these tests render locally")

        return Renderer(Launcher(), None, settings, render_page).render(f"{site.base_url}/frames.html")


@pytest.fixture(scope="module")
def rendered(chromium: None, site: FixtureSite) -> RenderedPage:
    return _render(site)


def _cases(page: RenderedPage) -> dict[str, tuple[str, Where]]:
    return {item.text.split()[0]: (", ".join(item.reasons), item.where) for item in page.hidden if item.text.startswith("CASE-")}


def test_hidden_text_in_shadow_roots_and_frames_is_found_and_says_where_it_is(rendered: RenderedPage) -> None:
    assert _cases(rendered) == HIDDEN


def test_visible_text_in_shadow_roots_and_frames_stays_visible(rendered: RenderedPage) -> None:
    hidden_words = " ".join(item.text for item in rendered.hidden)
    assert not [case for case in VISIBLE if case in hidden_words]


def test_a_finding_names_where_it_was_found(rendered: RenderedPage) -> None:
    details = {item.text.split()[0]: item.detail for item in rendered.hidden if item.text.startswith("CASE-")}
    assert details["CASE-OPEN-SHADOW-HIDDEN"] == "outside the page (in a shadow root)"
    assert details["CASE-DISPLAY-NONE-FRAME"] == "display:none (in a hidden frame)"
    assert details["CASE-UNSLOTTED"] == "not rendered"


def test_frames_that_cant_be_checked_are_counted_never_scanned(rendered: RenderedPage) -> None:
    assert (FIXTURES / "frames" / "refused.txt").read_text(encoding="utf-8") == f"{REFUSED_MESSAGE}\n"
    assert UNCHECKED_NOTE in rendered.notes
    assert not any("loupe refused" in item.text for item in rendered.hidden)
    # The same-origin, cross-origin, zero-size and display:none frames, and the frame inside each of two of them.
    assert rendered.frames_checked == 6


def test_a_frame_in_a_process_of_its_own_is_checked_too(chromium: None, site: FixtureSite) -> None:
    page = _render(site, isolate_sites=True)
    assert _cases(page) == HIDDEN
    assert UNCHECKED_NOTE in page.notes


def test_frames_past_the_limit_are_counted_not_checked(chromium: None, site: FixtureSite) -> None:
    page = _render(site, RenderSettings(stop_at_challenges=True, max_frames=1))
    # Only the first frame in the page, the same-origin one, is checked; the frame inside it is past the limit.
    assert {case for case, (_, where) in _cases(page).items() if where is not Where.PAGE and where is not Where.SHADOW_ROOT} == {
        "CASE-FRAME-HIDDEN",
        "CASE-SHADOW-IN-FRAME-HIDDEN",
        "CASE-CLOSED-IN-FRAME-HIDDEN",
    }
    assert page.frames_checked == 1
    # Every other frame, loaded or not, and the frames inside them, are past the limit, not unchecked.
    assert "7 frames past the limit of 1 frame or 30 s weren't checked" in page.notes
    assert not any("couldn't be checked" in note for note in page.notes)
