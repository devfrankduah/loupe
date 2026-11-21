"""The rendered scan's check for text coloured like what's behind it, in Chromium, on tests/fixtures/contrast.html."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from loupe_scan import ScanResult, scan_html

from .conftest import FIXTURES
from .fixture_site import serve_fixtures

REASON = "coloured like its background"


@pytest.fixture(scope="module")
def rendered() -> Iterator[ScanResult]:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")
    html = (FIXTURES / "contrast.html").read_text(encoding="utf-8")
    with serve_fixtures() as site:
        yield scan_html(html, f"{site.base_url}/contrast.html", render=True)


def _cases(result: ScanResult) -> set[str]:
    return {item.text.split()[0] for item in result.hidden_text if item.detail == REASON}


def test_text_too_close_in_colour_to_a_solid_background_is_hidden(rendered: ScanResult) -> None:
    assert _cases(rendered) == {"CASE-SAME-COLOUR", "CASE-NEAR-WHITE", "CASE-LIGHT", "CASE-FLAT-GRADIENT", "CASE-TRANSPARENT", "CASE-WASH", "CASE-PANEL"}


def test_readable_text_and_text_it_cant_judge_count_as_visible(rendered: ScanResult) -> None:
    hidden = {item.text.split()[0] for item in rendered.hidden_text}
    assert hidden.isdisjoint({"CASE-VISIBLE", "CASE-FAINT", "CASE-MANY-COLOURS", "CASE-OVER-PICTURE", "CASE-SHADOW", "CASE-DARK"})


def test_the_notes_say_how_many_elements_couldnt_be_judged(rendered: ScanResult) -> None:
    rendering = rendered.to_dict()["rendering"]
    assert isinstance(rendering, dict)
    notes = [note for note in rendering["notes"] if "wasn't judged" in note]
    # The gradient of several colours and the text over a picture.
    assert notes == ["2 text elements sit on an image or a gradient of several colours, so whether the text stands out from it wasn't judged"]
