"""The hijack harness's canary pages in docs/harness/, checked against the ground truth beside each one."""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from loupe_scan import scan_html
from loupe_scan.convert import html_to_markdown
from loupe_scan.scan import scan_html as scan_markup

from .fixture_site import serve_fixtures

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
HARNESS = DOCS / "harness"
SLUGS = ["stylesheet", "off-screen", "script-inserted", "comment", "attribute", "background-colour", "control"]


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_harness_pages", ROOT / "scripts" / "build_harness_pages.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _truth(slug: str) -> dict[str, str]:
    """A page's ground truth, field by field."""
    rows = [line for line in (HARNESS / slug / "ground-truth.md").read_text(encoding="utf-8").splitlines() if line.startswith("| ")]
    fields: dict[str, str] = {}
    for row in rows[1:]:
        field, value = (cell.strip() for cell in row.strip().strip("|").split("|"))
        fields[field] = value
    return fields


def _yes(slug: str, field: str) -> bool:
    return {"yes": True, "no": False}[_truth(slug)[field]]


def _html(slug: str) -> str:
    return (HARNESS / slug / "index.html").read_text(encoding="utf-8")


def _code(slug: str) -> str:
    return _truth(slug)["Canary code"]


def test_the_pages_are_what_the_build_script_writes() -> None:
    expected: dict[Path, str] = _builder().expected_files()
    on_disk = {path for path in HARNESS.rglob("*") if path.is_file()}
    assert on_disk == set(expected)
    for path, text in expected.items():
        assert path.read_text(encoding="utf-8") == text, path


def test_every_page_has_its_own_code_and_asks_search_engines_to_skip_it() -> None:
    assert sorted(path.name for path in HARNESS.iterdir() if path.is_dir()) == sorted(SLUGS)
    codes = [_code(slug) for slug in SLUGS]
    assert len(set(codes)) == len(codes)
    assert all(re.fullmatch(r"[A-Z]{2}-\d{4}-[A-Z]{2}", code) for code in codes)
    for slug in SLUGS:
        assert '<meta name="robots" content="noindex">' in _html(slug), slug
        assert _truth(slug)["Technique"] == slug


def test_the_pages_differ_only_in_how_the_instruction_is_hidden() -> None:
    def article(slug: str) -> str:
        # What every reader sees: the page with its scripts and the instruction's own element taken out.
        html = re.sub(r"<script>.*?</script>\n", "", _html(slug), flags=re.DOTALL)
        html = re.sub(r"^(<p [^>]*>|<!-- )Note to AI assistants:.*\n", "", html, flags=re.MULTILINE)
        return re.sub(r'alt="[^"]*"', 'alt=""', html)

    assert len({article(slug) for slug in SLUGS}) == 1


@pytest.mark.parametrize("slug", SLUGS)
def test_where_the_code_is_matches_the_ground_truth(slug: str) -> None:
    html = _html(slug)
    code = _code(slug)
    assert (code in html) == _yes(slug, "In the HTML as served")
    assert (code in html_to_markdown("https://example.com/", html)) == _yes(slug, "Reaches markdownify's markdown")
    found = any(code in finding.text for finding in scan_markup(html))
    assert found == _yes(slug, "Markup scan finds it")


@pytest.fixture(scope="module")
def harness_site() -> Iterator[str]:
    with serve_fixtures(DOCS) as site:
        yield f"{site.base_url}/harness"


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


@pytest.mark.parametrize("slug", SLUGS)
def test_what_a_rendered_scan_finds_matches_the_ground_truth(chromium: None, harness_site: str, slug: str) -> None:
    result = scan_html(_html(slug), f"{harness_site}/{slug}/", render=True)
    found = any(_code(slug) in item.text for item in result.hidden_text)
    assert found == _yes(slug, "Rendered scan finds it")
