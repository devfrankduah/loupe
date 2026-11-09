"""Keyboard use of the report page in a real browser."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Browser, Page, sync_playwright  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402

from loupe.site import load_results, render_report_page  # noqa: E402

from .test_site import _page, _results  # noqa: E402


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


@pytest.fixture
def report(chromium: Browser) -> Iterator[Page]:
    context = chromium.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    page.set_content(render_report_page(load_results(_results(_page("https://a.test/", [])))))
    try:
        yield page
    finally:
        context.close()


def _focused(page: Page) -> str:
    focused: str = page.evaluate(
        "() => { const el = document.activeElement;"
        " return el.id || el.getAttribute('href') || el.tagName.toLowerCase(); }"
    )
    return focused


def test_tab_starts_at_the_skip_link_and_reaches_the_toggle_after_the_header_links(report: Page) -> None:
    stops: list[str] = []
    for _ in range(4):
        report.keyboard.press("Tab")
        stops.append(_focused(report))
    assert stops[0] == "#main"
    assert stops[3] == "theme"
    assert report.evaluate("getComputedStyle(document.querySelector('label[for=theme]')).outlineStyle") == "solid"


def test_the_switch_keeps_its_name_and_flips_its_state_from_the_keyboard(report: Page) -> None:
    switch = report.get_by_role("switch", name="Dark mode", exact=True)
    label = report.locator("label[for=theme]")
    assert switch.count() == 1
    assert report.locator("#theme").aria_snapshot() == '- switch "Dark mode"'
    assert label.inner_text().strip() == "Dark mode"

    report.focus("#theme")
    report.keyboard.press("Space")
    assert report.evaluate("getComputedStyle(document.documentElement).colorScheme") == "dark"
    assert switch.count() == 1 and switch.is_checked()
    assert report.locator("#theme").aria_snapshot() == '- switch "Dark mode" [checked]'
    assert label.inner_text().strip() == "Dark mode"

    report.keyboard.press("Space")
    assert report.evaluate("getComputedStyle(document.documentElement).colorScheme") == "light"
    assert not switch.is_checked()
