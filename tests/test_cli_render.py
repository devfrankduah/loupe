"""loupe --render end to end, in real Chromium against a local fixture page.

This lives apart from test_render_browser.py because Playwright's sync API
allows one running instance per thread, and the CLI starts its own.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from loupe import cli  # noqa: E402

from .fixture_site import serve_fixtures  # noqa: E402

MARKER = re.compile(r"CASE-[A-Z-]+")


@pytest.fixture(scope="module")
def chromium_installed() -> None:
    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")


@pytest.fixture(scope="module")
def fixture_site() -> Iterator[str]:
    with serve_fixtures() as base_url:
        yield base_url


def test_render_flag_end_to_end(
    chromium_installed: None, fixture_site: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "rendered.json"
    url = f"{fixture_site}/rendered.html"

    # No Bright Data token is set: with --skip-markdown a rendered scan needs none.
    assert cli.main([url, "--render", "--skip-markdown", "--json", str(output_path)]) == cli.EXIT_OK

    (page,) = json.loads(output_path.read_text(encoding="utf-8"))["pages"]
    assert page["scan_mode"] == "rendered"
    assert page["rendering"]["browser"] == "local-chromium"
    assert page["rendering"]["reached_bottom"] is True
    by_marker = {
        MARKER.findall(finding["text"])[0]: (finding["mode"], finding["kind"])
        for finding in page["findings"]
        if "CASE-" in finding["text"]
    }
    assert by_marker["CASE-COMMENT"] == ("static", "comment")
    assert by_marker["CASE-STYLESHEET"] == ("rendered", "rendered-hidden")
    assert sum("CASE-HIDDEN-ATTRIBUTE" in finding["text"] for finding in page["findings"]) == 1
    assert {finding["kind"] for finding in page["findings"]}.isdisjoint({"hidden-attribute", "inline-style"})
    revealed = {MARKER.findall(item["text"])[0] for item in page["rendering"]["revealed_on_scroll"]}
    assert revealed == {"CASE-REVEAL", "CASE-REVEAL-WHILE-IN-VIEW", "CASE-CONTENT-VISIBILITY"}

    printed = capsys.readouterr().out
    assert "rendered in local Chromium" in printed
    assert "revealed on scroll: 3 (not counted as hidden)" in printed
