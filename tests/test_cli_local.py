"""loupe on the local backend, end to end against a server on 127.0.0.1."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from loupe import cli
from loupe.convert import html_to_markdown, local_markdown_source
from loupe.scan import scan_html

from .route_site import Route, html_route, serve_routes

ROOT = Path(__file__).parent.parent


def _robots(text: str) -> Route:
    return Route(body=text.encode("utf-8"), headers={"content-type": "text/plain"})


def test_with_no_credentials_a_markup_scan_runs_on_this_machine(
    planted_html: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "local.json"
    with serve_routes({"/cake": html_route(planted_html)}) as site:
        assert cli.main([site.url("/cake"), "--skip-markdown", "--json", str(output_path)]) == cli.EXIT_OK

    report = json.loads(output_path.read_text(encoding="utf-8"))
    (page,) = report["pages"]
    assert (report["backend"], page["backend"], page["scan_mode"]) == ("local", "local", "static")
    assert len(page["findings"]) == len(scan_html(planted_html))
    assert page["instruction_like_count"] == 13
    assert "  backend: local" in capsys.readouterr().out.splitlines()


def test_a_page_that_turns_loupe_away_points_to_the_bright_data_backend(capsys: pytest.CaptureFixture[str]) -> None:
    with serve_routes({"/cake": html_route("<p>Forbidden</p>", status=403)}) as site:
        assert cli.main([site.url("/cake"), "--skip-markdown"]) == cli.EXIT_FETCH_FAILED

    printed = capsys.readouterr().out
    assert "the site turned loupe away (HTTP 403, forbidden). Sites that block automated visitors" in printed
    assert f"https://github.com/devfrankduah/loupe#{cli.BRIGHT_DATA_SECTION}" in printed


def _github_anchor(heading: str) -> str:
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def test_the_bright_data_hint_links_to_a_real_readme_section() -> None:
    headings = re.findall(r"^#{1,6} (.+)$", (ROOT / "README.md").read_text(encoding="utf-8"), re.MULTILINE)
    assert cli.BRIGHT_DATA_SECTION in {_github_anchor(heading) for heading in headings}


def test_robots_txt_is_obeyed_unless_the_user_says_the_page_is_theirs(capsys: pytest.CaptureFixture[str]) -> None:
    routes = {"/robots.txt": _robots("User-agent: *\nDisallow: /\n"), "/cake": html_route("<p>Cake</p>")}
    with serve_routes(routes) as site:
        assert cli.main([site.url("/cake"), "--skip-markdown"]) == cli.EXIT_FETCH_FAILED
        assert "its robots.txt asks loupe not to" in capsys.readouterr().out
        assert cli.main([site.url("/cake"), "--skip-markdown", "--ignore-robots"]) == cli.EXIT_OK
    assert site.requested() == ["/robots.txt", "/cake"]


def test_the_local_markdown_is_made_from_the_html_and_labelled_an_approximation(
    planted_html: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "local.json"
    with serve_routes({"/cake": html_route(planted_html)}) as site:
        assert cli.main([site.url("/cake"), "--json", str(output_path)]) == cli.EXIT_OK
    assert site.requested() == ["/robots.txt", "/cake"]

    (page,) = json.loads(output_path.read_text(encoding="utf-8"))["pages"]
    source = page["markdown_source"]
    assert (source["name"], source["license"], source["approximation"]) == ("markdownify", "MIT", True)
    assert source["version"] == local_markdown_source().version
    assert page["markdown_characters"] == len(html_to_markdown(site.url("/cake"), planted_html))
    assert page["survival"]["survived"] > 0 and page["survival"]["dropped"] > 0
    by_kind = {finding["kind"]: finding["in_markdown"] for finding in page["findings"] if finding["kind"] == "comment"}
    assert by_kind == {"comment": False}
    printed = capsys.readouterr().out
    expected = f"chars (converted here by markdownify {source['version']}, an approximation of what agent tools produce)"
    assert expected in printed


@pytest.mark.parametrize(
    ("routes", "reason"),
    [
        ({"/cake": html_route("<p>Forbidden</p>", status=403)}, "the site turned loupe away"),
        ({"/robots.txt": _robots("User-agent: *\nDisallow: /\n"), "/cake": html_route("<p>x</p>")}, "the site's robots.txt stopped loupe"),
        ({"/cake": html_route("<p>Broken</p>", status=500)}, "the page couldn't be fetched"),
    ],
)
def test_a_page_that_wasnt_scanned_says_why_in_its_first_line(
    routes: dict[str, Route], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_routes(routes) as site:
        cli.main([site.url("/cake")])
    assert capsys.readouterr().out.splitlines()[0] == f"Not scanned: {reason}. The details are below."
