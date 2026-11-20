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

from loupe_scan import cli  # noqa: E402

from .fixture_site import FixtureSite, serve_fixtures  # noqa: E402
from . import test_animated_page as animated  # noqa: E402
from .local_results import findings_of, scan_locally, traced_of  # noqa: E402
from .results_schema import check_results  # noqa: E402
from .test_test_page import check_scan  # noqa: E402

MARKER = re.compile(r"CASE-[A-Z-]+")


@pytest.fixture(scope="module")
def chromium_installed() -> None:
    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")


@pytest.fixture(scope="module")
def fixture_server() -> Iterator[FixtureSite]:
    with serve_fixtures() as site:
        yield site


@pytest.fixture
def fixture_site(fixture_server: FixtureSite) -> str:
    fixture_server.requested.clear()
    return fixture_server.base_url


def test_render_flag_end_to_end(
    chromium_installed: None, fixture_site: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "rendered.json"
    url = f"{fixture_site}/rendered.html"

    # No Bright Data token is set: with --skip-markdown a rendered scan needs none.
    assert cli.main([url, "--render", "--skip-markdown", "--json-output", str(output_path)]) == cli.EXIT_OK

    (page,) = json.loads(output_path.read_text(encoding="utf-8"))
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


def test_the_blender_page_matches_its_ground_truth_rendered_on_the_local_backend(
    chromium_installed: None, fixture_site: str, tmp_path: Path
) -> None:
    page = scan_locally(f"{fixture_site}/test-page/index.html", tmp_path / "local.json", "--render")
    assert page["rendering"]["browser"] == "local-chromium"
    check_scan(findings_of(page), "rendered")


def test_the_animated_page_matches_its_ground_truth_rendered_on_the_local_backend(
    chromium_installed: None, fixture_site: str, tmp_path: Path
) -> None:
    page = scan_locally(f"{fixture_site}/animated-page/index.html", tmp_path / "local.json", "--render")
    animated.check_rendered_scan(findings_of(page), traced_of(page))


def test_rendered_results_fit_the_published_schema(
    chromium_installed: None, fixture_site: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([f"{fixture_site}/test-page/index.html", "--render", "--json"]) == cli.EXIT_OK

    results = json.loads(capsys.readouterr().out)
    check_results(results)
    (page,) = results
    assert page["scan_mode"] == "rendered" and page["rendering"]["static_comparison"]
    assert {finding["mode"] for finding in page["findings"]} == {"static", "rendered"}
