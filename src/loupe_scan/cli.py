"""Command line entry points: loupe <url> [<url> ...] [--render] and loupe report <results.json>... -o <page.html>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Mapping
from typing import Final, Protocol, Sequence

from loupe_scan import __version__
from loupe_scan.backend import Backend, choose_backend
from loupe_scan.convert import BRIGHT_DATA_MARKDOWN, MarkdownSource, html_to_markdown, local_markdown_source
from loupe_scan.errors import BlockedError, PageFetchError
from loupe_scan.fetch import (
    DEFAULT_UNLOCKER_ZONE,
    TOKEN_ENVIRONMENT_VARIABLE,
    BrightDataClient,
    MissingTokenError,
    read_token_from_environment,
)
from loupe_scan.local_fetch import REPOSITORY_URL, LocalFetcher, RobotsDisallowedError
from loupe_scan.render import (
    BROWSER_ENDPOINT_VARIABLE,
    ENDPOINT_PLACEHOLDER,
    SUPERSEDED_BY_RENDERING,
    EndpointRedactor,
    PageRenderer,
    RenderError,
    RenderSetupError,
    open_renderer,
    read_browser_endpoint,
    rendered_findings,
)
from loupe_scan.report import DEFAULT_QUOTE_CHARACTERS, PageReport, check_findings, page_to_dict, render_text
from loupe_scan.scan import ScanMode, scan_html
from loupe_scan.site import PageRecord, ReportFormatError, load_results, render_report_page, run_date_from_filename

EXIT_OK = 0
EXIT_FETCH_FAILED = 1
EXIT_USAGE = 2

# The README section on the Bright Data backend, for sites that turn loupe away.
BRIGHT_DATA_SECTION: Final[str] = "sites-that-block-bots-and-scanning-at-scale"
BLOCKED_HINT: Final[str] = (
    "Sites that block automated visitors can be scanned through the Bright Data backend: "
    f"{REPOSITORY_URL}#{BRIGHT_DATA_SECTION}"
)


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loupe",
        description="Flag text that readers can't see but an AI agent reading the page would.",
        # Raw, so the report command stays on one line however wide the terminal is.
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "To turn results files written with --json into one HTML page:\n"
            "  loupe report RESULTS [RESULTS ...] -o PATH\n"
            "See loupe report --help."
        ),
    )
    parser.add_argument("urls", nargs="+", metavar="url", help="page to fetch and scan")
    parser.add_argument(
        "--backend",
        type=Backend,
        choices=list(Backend),
        metavar="{local,brightdata}",
        help=(
            "where pages are loaded from: local (this machine, no account needed) or brightdata "
            f"(Bright Data's APIs). Default: brightdata when {TOKEN_ENVIRONMENT_VARIABLE} or "
            f"{BROWSER_ENDPOINT_VARIABLE} is set, local otherwise"
        ),
    )
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the full report as JSON")
    parser.add_argument(
        "--zone",
        default=DEFAULT_UNLOCKER_ZONE,
        help=f"Bright Data Web Unlocker zone for HTML (default: {DEFAULT_UNLOCKER_ZONE})",
    )
    parser.add_argument(
        "--skip-markdown", action="store_true", help="scan the HTML only and skip the markdown comparison"
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help=(
            "load each page in Chromium and check what actually shows, scrolling to the bottom; the "
            f"brightdata backend uses the Browser API endpoint in {BROWSER_ENDPOINT_VARIABLE} if it's set, "
            "and local Chromium otherwise"
        ),
    )
    parser.add_argument(
        "--ignore-robots",
        action="store_true",
        help=(
            "fetch pages even where robots.txt asks loupe not to, for pages you own or have permission "
            "to scan (local backend only)"
        ),
    )
    parser.add_argument(
        "--quote-chars",
        type=_non_negative_int,
        default=DEFAULT_QUOTE_CHARACTERS,
        metavar="N",
        help=f"cut quoted hidden text to N characters, 0 for no limit (default: {DEFAULT_QUOTE_CHARACTERS})",
    )
    parser.add_argument("--version", action="version", version=f"loupe {__version__}")
    return parser


@dataclass(frozen=True)
class MarkdownMaker:
    """Gets a page's markdown, from its URL or its HTML, and says where it came from."""

    source: MarkdownSource
    make: Callable[[str, str], str]


def bright_data_markdown(client: BrightDataClient) -> MarkdownMaker:
    return MarkdownMaker(BRIGHT_DATA_MARKDOWN, lambda url, html: client.fetch_markdown(url))


def local_markdown() -> MarkdownMaker:
    return MarkdownMaker(local_markdown_source(), html_to_markdown)


def _markdown_for(maker: MarkdownMaker | None, url: str, html: str, page: PageReport) -> str | None:
    if maker is None:
        return None
    try:
        markdown = maker.make(url, html)
    except PageFetchError as error:
        page.markdown_error = str(error)
        return None
    page.markdown_characters = len(markdown)
    page.markdown_source = maker.source
    return markdown


class HtmlSource(Protocol):
    def fetch_html(self, url: str) -> str: ...


def _record_failure(page: PageReport, error: PageFetchError | RenderError, *, loading: str) -> None:
    """Record why page wasn't scanned, pointing to Bright Data when a site blocked the local backend."""
    page.html_error = str(error)
    if isinstance(error, BlockedError):
        page.failure = "the site turned loupe away"
        if page.backend is Backend.LOCAL:
            page.html_error = f"{error}. {BLOCKED_HINT}"
    elif isinstance(error, RobotsDisallowedError):
        page.failure = "the site's robots.txt stopped loupe"
    else:
        page.failure = f"the page couldn't be {loading}"


def scan_page(source: HtmlSource, markdown: MarkdownMaker | None, url: str, *, backend: Backend) -> PageReport:
    """Fetch url's HTML from source and scan the markup. Markdown is compared only when a maker is given."""
    page = PageReport(url=url, backend=backend)
    try:
        html = source.fetch_html(url)
    except PageFetchError as error:
        _record_failure(page, error, loading="fetched")
        return page
    page.html_characters = len(html)
    page.findings = check_findings(scan_html(html), _markdown_for(markdown, url, html, page))
    return page


def scan_rendered_page(
    renderer: PageRenderer,
    markdown: MarkdownMaker | None,
    url: str,
    *,
    backend: Backend,
    fetcher: LocalFetcher | None = None,
) -> PageReport:
    """Render url, then check what never showed. Markdown is compared only when a maker is given.

    The local backend's markdown is made from the HTML the site served, the same HTML the markup checks read.

    A local fetcher, when given, checks robots.txt and waits out any Crawl-delay before the browser loads url.
    """
    page = PageReport(url=url, backend=backend, mode=ScanMode.RENDERED, browser=renderer.source)
    try:
        if fetcher is not None:
            fetcher.admit(url)
        rendered = renderer.render(url)
    except (PageFetchError, RenderError) as error:
        _record_failure(page, error, loading="loaded in the browser")
        return page
    page.html_characters = len(rendered.html)
    page.rendered = rendered
    markdown_text = _markdown_for(markdown, url, rendered.html, page)
    markup_findings = [finding for finding in scan_html(rendered.html) if finding.kind not in SUPERSEDED_BY_RENDERING]
    page.findings = check_findings(markup_findings + rendered_findings(rendered), markdown_text)
    return page


def _build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loupe report",
        description=(
            "Turn results JSON files into one self-contained HTML page. Give a markup-only run and a "
            "--render run of the same pages to see them side by side."
        ),
    )
    parser.add_argument("results", type=Path, nargs="+", help="JSON written by loupe --json")
    parser.add_argument("-o", "--output", type=Path, required=True, metavar="PATH", help="HTML file to write")
    return parser


def report_main(argv: Sequence[str]) -> int:
    arguments = _build_report_parser().parse_args(argv)
    pages: list[PageRecord] = []
    for results_path in arguments.results:
        try:
            pages.extend(load_results(results_path.read_text(encoding="utf-8")))
        except OSError as error:
            print(f"loupe report: could not read {results_path}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE
        except ReportFormatError as error:
            print(f"loupe report: {results_path}: {error}", file=sys.stderr)
            return EXIT_USAGE

    run_dates = [date for date in map(run_date_from_filename, arguments.results) if date is not None]
    page_html = render_report_page(tuple(pages), max(run_dates, default=None))
    try:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(page_html, encoding="utf-8")
    except OSError as error:
        print(f"loupe report: could not write {arguments.output}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE
    print(f"wrote {arguments.output}")
    return EXIT_OK


def _scrub_strings(value: object, scrub: Callable[[str], str]) -> object:
    """value with scrub applied to every string in it, keys included."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(key)): _scrub_strings(item, scrub) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_strings(item, scrub) for item in value]
    return value


def _client_from_environment(zone: str) -> BrightDataClient | None:
    """The Bright Data client, or None after explaining on stderr why there isn't one."""
    try:
        return BrightDataClient(read_token_from_environment(), unlocker_zone=zone)
    except MissingTokenError:
        print(
            f"loupe: the brightdata backend needs {TOKEN_ENVIRONMENT_VARIABLE} set to a Bright Data API token; "
            "set it, or use --backend local",
            file=sys.stderr,
        )
    except ValueError as error:
        print(f"loupe: {error}", file=sys.stderr)
    return None


def _note(message: str) -> None:
    print(f"loupe: {message}", file=sys.stderr, flush=True)


def _local_fetcher(ignore_robots: bool) -> LocalFetcher:
    return LocalFetcher(respect_robots=not ignore_robots, on_wait=_note)


def _endpoint_redactor(environment: Mapping[str, str]) -> Callable[[str], str]:
    """Hides any part of the Browser API endpoint, if one is set, whichever backend runs."""
    value = environment.get(BROWSER_ENDPOINT_VARIABLE, "").strip()
    try:
        return EndpointRedactor(value or None)
    except ValueError:
        # A value too malformed to split into parts can still be hidden whole.
        return lambda text: text.replace(value, ENDPOINT_PLACEHOLDER)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # A page URL always starts with a scheme, so a bare "report" can only be the subcommand.
    if raw_arguments[:1] == ["report"]:
        return report_main(raw_arguments[1:])
    arguments = _build_parser().parse_args(raw_arguments)
    backend = choose_backend(arguments.backend, os.environ)
    # Everything printed or saved passes through this, a second guard after
    # the one around each error, so no part of the endpoint gets out.
    scrub = _endpoint_redactor(os.environ)

    # A rendered scan loads pages itself, so it needs Bright Data only for markdown.
    client: BrightDataClient | None = None
    if backend is Backend.BRIGHTDATA and not (arguments.render and arguments.skip_markdown):
        client = _client_from_environment(arguments.zone)
        if client is None:
            return EXIT_USAGE
    markdown: MarkdownMaker | None = None
    if not arguments.skip_markdown and backend is Backend.LOCAL:
        markdown = local_markdown()
    elif not arguments.skip_markdown and client is not None:
        markdown = bright_data_markdown(client)
    if backend is Backend.BRIGHTDATA and arguments.ignore_robots:
        print("loupe: --ignore-robots only changes the local backend, so it has no effect here", file=sys.stderr)

    pages: list[PageReport] = []

    def record(page: PageReport) -> None:
        pages.append(page)
        print(scrub(render_text(page, arguments.quote_chars)), flush=True)
        print()

    if arguments.render:
        try:
            endpoint = read_browser_endpoint(os.environ) if backend is Backend.BRIGHTDATA else None
            fetcher = _local_fetcher(arguments.ignore_robots) if backend is Backend.LOCAL else None
            with open_renderer(endpoint) as renderer:
                for url in arguments.urls:
                    record(scan_rendered_page(renderer, markdown, url, backend=backend, fetcher=fetcher))
        except RenderSetupError as error:
            print(scrub(f"loupe: {error}"), file=sys.stderr)
            return EXIT_USAGE
    elif backend is Backend.LOCAL:
        local_fetcher = _local_fetcher(arguments.ignore_robots)
        for url in arguments.urls:
            record(scan_page(local_fetcher, markdown, url, backend=backend))
    elif client is not None:
        for url in arguments.urls:
            record(scan_page(client, markdown, url, backend=backend))

    if arguments.json is not None:
        report = {
            "loupe_version": __version__,
            "backend": backend.value,
            "pages": [page_to_dict(page, arguments.quote_chars) for page in pages],
        }
        try:
            text = json.dumps(_scrub_strings(report, scrub), indent=2, ensure_ascii=False) + "\n"
            arguments.json.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"loupe: could not write {arguments.json}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE

    failed = any(page.html_error is not None or page.markdown_error is not None for page in pages)
    return EXIT_FETCH_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
