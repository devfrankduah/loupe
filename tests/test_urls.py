"""A URL someone gave loupe that it can't fetch is refused with the reason, before any request, wherever it came in."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from loupe_scan import FailureKind, ScanError, cli, scan, scan_html
from loupe_scan.urls import url_problem

from .route_site import Route, RouteSite, html_route, serve_routes

SPACE = "it has a space or a control character in it. Write a space in a path as %20, and give each URL on its own"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://127.0.0.1:8080/menu?day=friday#top",
        "https://bücher.example/kuchen",
        "https://example.com/opening%20hours",
        "HTTPS://EXAMPLE.COM/",
    ],
)
def test_an_http_url_with_a_host_is_fine(url: str) -> None:
    assert url_problem(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://exa mple.com/",
        "https://example.com/opening hours",
        "https://bakery.example https://cafe.example/menu",
        "https://example.com/\tmenu",
        "https://example.com/menu\n",
        " https://example.com/",
        "https://example.com/ menu",
        "https://example.com/​menu",
        "https://example.com/\x7f",
    ],
    ids=["host", "path", "two-urls", "tab", "newline", "leading", "no-break-space", "zero-width-space", "delete"],
)
def test_a_space_or_a_control_character_makes_a_url_invalid(url: str) -> None:
    assert url_problem(url) == f"{url!r} isn't a valid URL: {SPACE}"


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("ftp://example.com/", "isn't an http(s) URL with a host"),
        ("file:///etc/passwd", "isn't an http(s) URL with a host"),
        ("bakery.example/cake", "isn't an http(s) URL with a host"),
        ("https://", "isn't an http(s) URL with a host"),
        ("http://[::1", "isn't a valid URL"),
        ("https://example.com:99999/", "isn't a valid URL"),
        ("https://example.com:port/", "isn't a valid URL"),
    ],
)
def test_other_urls_it_cant_fetch_say_why(url: str, problem: str) -> None:
    assert url_problem(url) == f"{url!r} {problem}"


@pytest.fixture
def site() -> Iterator[RouteSite]:
    routes = {
        "/robots.txt": Route(body=b"User-agent: *\nAllow: /\n", headers={"content-type": "text/plain"}),
        "/menu": html_route("<p>Bread and cake.</p>"),
    }
    with serve_routes(routes) as served:
        yield served


def test_two_urls_run_together_are_refused_with_the_reason_and_nothing_is_requested(
    site: RouteSite, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    joined = f"{site.url('/menu')} {site.url('/menu')}"
    output = tmp_path / "results.json"
    assert cli.main([joined, "--skip-markdown", "--json-output", str(output)]) == cli.EXIT_ERROR
    printed = capsys.readouterr().out
    assert f"error: {joined!r} isn't a valid URL: {SPACE}" in printed
    assert "robots.txt" not in printed
    (page,) = json.loads(output.read_text(encoding="utf-8"))
    assert page["error"] == {"kind": "fetch", "message": url_problem(joined)}
    assert site.requested() == []


def test_a_space_in_the_host_is_refused_rather_than_blamed_on_robots_txt(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["https://exa mple.com/", "--skip-markdown"]) == cli.EXIT_ERROR
    printed = capsys.readouterr().out
    assert "isn't a valid URL" in printed
    assert "robots.txt" not in printed


def test_a_urls_file_line_holding_two_urls_is_refused_and_the_others_are_scanned(
    site: RouteSite, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = tmp_path / "urls.txt"
    listed.write_text(f"{site.url('/menu')} {site.url('/menu')}\n{site.url('/menu')}\n", encoding="utf-8")
    assert cli.main(["--urls-file", str(listed), "--skip-markdown"]) == cli.EXIT_ERROR
    assert "isn't a valid URL" in capsys.readouterr().out
    assert site.requested() == ["/robots.txt", "/menu"]


def test_the_python_api_refuses_an_invalid_url_as_a_page_it_couldnt_scan(site: RouteSite) -> None:
    joined = f"{site.url('/menu')} {site.url('/menu')}"
    with pytest.raises(ScanError) as caught:
        scan(joined)
    assert (caught.value.url, caught.value.kind) == (joined, FailureKind.FETCH)
    assert "isn't a valid URL" in str(caught.value)
    with pytest.raises(ValueError, match="isn't a valid URL"):
        scan_html("<p>Bread.</p>", "https://exa mple.com/")
    assert site.requested() == []


def test_html_file_says_why_it_cant_use_the_url(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved = tmp_path / "page.html"
    saved.write_text("<p>Plain.</p>", encoding="utf-8")
    assert cli.main(["--html-file", str(saved), "https://example.com/opening hours"]) == cli.EXIT_USAGE
    assert f"needs the http(s) URL its HTML came from, and {url_problem('https://example.com/opening hours')}" in (
        capsys.readouterr().err
    )
