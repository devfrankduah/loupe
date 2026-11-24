"""The rendered scan's check for text covered by another element, in Chromium, on tests/fixtures/covered.html.

Each case on the page starts with its name. The page has a fixed header,
a fixed banner and a modal over some of its text, as many real pages do,
and none of those may count as hiding it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from loupe_scan.render import RenderedPage, Where, open_renderer

from .fixture_site import serve_fixtures

REASON = "covered by another element"
COVERED: dict[str, Where] = {
    "CASE-COVERED-SOLID": Where.PAGE,
    "CASE-COVERED-BEHIND-PARENT": Where.PAGE,
    "CASE-COVERED-SHADOW": Where.SHADOW_ROOT,
    "CASE-COVERED-BELOW-FOLD": Where.PAGE,
    "CASE-COVERED-FRAME": Where.HIDDEN_FRAME,
}
SHOWING = {
    "CASE-PARTLY-COVERED",
    "CASE-UNDER-TRANSLUCENT",
    "CASE-UNDER-BLENDED",
    # Judged against its host's background while off screen too, so it is visible from the start.
    "CASE-SHADOW-WHITE-BELOW-FOLD",
    "CASE-UNDER-FADED",
    "CASE-UNDER-PICTURE",
    "CASE-TEXT-ABOVE-BOX",
    "CASE-VISIBLE-BELOW-FOLD",
    # The page scrolls under fixed and sticky elements and dialogs, so what they cover at one step shows at another.
    "CASE-UNDER-STICKY-HEADER",
    "CASE-UNDER-COOKIE-BANNER",
    "CASE-UNDER-MODAL",
    "CASE-UNDER-DIALOG-ELEMENT",
    # Hit testing skips an element the pointer passes through, so a cover like that isn't seen.
    "CASE-POINTER-EVENTS-COVER",
}


@pytest.fixture(scope="module")
def rendered() -> Iterator[RenderedPage]:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")
    with serve_fixtures() as site, open_renderer(None) as renderer:
        yield renderer.render(f"{site.base_url}/covered.html")


def _case(text: str) -> str:
    return text.split()[0]


def test_text_with_something_solid_over_it_is_hidden(rendered: RenderedPage) -> None:
    covered = {_case(item.text): item.where for item in rendered.hidden if REASON in item.reasons}
    assert covered == COVERED


def test_text_that_shows_through_or_that_the_page_scrolls_out_from_under_is_visible(rendered: RenderedPage) -> None:
    hidden = {_case(item.text) for item in rendered.hidden}
    assert hidden == set(COVERED)
    assert hidden.isdisjoint(SHOWING)
    # Nothing on the page fades in, so text first seen on scrolling was misjudged before.
    assert rendered.revealed_on_scroll == ()


def test_the_notes_count_text_under_a_picture(rendered: RenderedPage) -> None:
    notes = [note for note in rendered.notes if "shows through" in note]
    assert notes == [
        "1 text element is under an image, a picture or a gradient of several colours, so whether the text shows "
        "through wasn't judged"
    ]
