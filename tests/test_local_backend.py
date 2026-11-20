"""Both test pages, scanned end to end on the local backend: fetched from 127.0.0.1, markdown made here.

The rendered half is in test_cli_render.py, which runs the browser.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from . import test_animated_page as animated
from .fixture_site import FixtureSite, serve_fixtures
from .local_results import findings_of, scan_locally
from .test_test_page import check_scan


@pytest.fixture(scope="module")
def fixture_site() -> Iterator[FixtureSite]:
    with serve_fixtures() as site:
        yield site


def test_the_blender_page_matches_its_ground_truth_on_the_local_backend(fixture_site: FixtureSite, tmp_path: Path) -> None:
    page = scan_locally(f"{fixture_site.base_url}/test-page/index.html", tmp_path / "local.json")
    check_scan(findings_of(page), "markup")
    assert page["survival"]["survived"] + page["survival"]["dropped"] > 0


def test_the_animated_page_matches_its_ground_truth_on_the_local_backend(fixture_site: FixtureSite, tmp_path: Path) -> None:
    page = scan_locally(f"{fixture_site.base_url}/animated-page/index.html", tmp_path / "local.json")
    animated.check_markup_scan(findings_of(page))
