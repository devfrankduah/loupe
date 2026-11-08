"""Command line entry points: loupe <url> [<url> ...] [--render] and loupe report <results.json>... -o <page.html>"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Sequence

from loupe import __version__
from loupe.fetch import (
    DEFAULT_UNLOCKER_ZONE,
    TOKEN_ENVIRONMENT_VARIABLE,
    BrightDataClient,
    BrightDataError,
    MissingTokenError,
    read_token_from_environment,
)
from loupe.render import (
    BROWSER_ENDPOINT_VARIABLE,
    SUPERSEDED_BY_RENDERING,
    PageRenderer,
    RenderError,
    RenderSetupError,
    open_renderer,
    rendered_findings,
)
from loupe.report import DEFAULT_QUOTE_CHARACTERS, PageReport, check_findings, page_to_dict, render_text
from loupe.scan import ScanMode, scan_html
from loupe.site import PageRecord, ReportFormatError, load_results, render_report_page, run_date_from_filename

EXIT_OK = 0
EXIT_FETCH_FAILED = 1
EXIT_USAGE = 2


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
            "load each page in Chromium and check what actually shows, scrolling to the bottom; "
            f"uses the CDP endpoint in {BROWSER_ENDPOINT_VARIABLE} if set, local Chromium otherwise"
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


def _fetch_markdown(client: BrightDataClient | None, url: str, page: PageReport) -> str | None:
    if client is None:
        return None
    try:
        markdown = client.fetch_markdown(url)
    except BrightDataError as error:
        page.markdown_error = str(error)
        return None
    page.markdown_characters = len(markdown)
    return markdown


def scan_page(client: BrightDataClient, url: str, *, fetch_markdown: bool) -> PageReport:
    page = PageReport(url=url)
    try:
        html = client.fetch_html(url)
    except BrightDataError as error:
        page.html_error = str(error)
        return page
    page.html_characters = len(html)
    markdown = _fetch_markdown(client if fetch_markdown else None, url, page)
    page.findings = check_findings(scan_html(html), markdown)
    return page


def scan_rendered_page(renderer: PageRenderer, markdown_client: BrightDataClient | None, url: str) -> PageReport:
    """Render url, then check what never showed. Markdown is compared only when a client is given."""
    page = PageReport(url=url, mode=ScanMode.RENDERED, browser=renderer.source)
    try:
        rendered = renderer.render(url)
    except RenderError as error:
        page.html_error = str(error)
        return page
    page.html_characters = len(rendered.html)
    page.rendered = rendered
    markdown = _fetch_markdown(markdown_client, url, page)
    markup_findings = [finding for finding in scan_html(rendered.html) if finding.kind not in SUPERSEDED_BY_RENDERING]
    page.findings = check_findings(markup_findings + rendered_findings(rendered), markdown)
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


def _unchanged(text: str) -> str:
    return text


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
        print(f"loupe: set {TOKEN_ENVIRONMENT_VARIABLE} to a Bright Data API token", file=sys.stderr)
    except ValueError as error:
        print(f"loupe: {error}", file=sys.stderr)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # A page URL always starts with a scheme, so a bare "report" can only be the subcommand.
    if raw_arguments[:1] == ["report"]:
        return report_main(raw_arguments[1:])
    arguments = _build_parser().parse_args(raw_arguments)

    # A rendered scan loads pages itself, so it needs Bright Data only for markdown.
    client: BrightDataClient | None = None
    if not arguments.render or not arguments.skip_markdown:
        client = _client_from_environment(arguments.zone)
        if client is None:
            return EXIT_USAGE

    pages: list[PageReport] = []
    # A rendered scan passes everything it prints or saves through the
    # renderer's redactor, a second guard after the one around each error.
    scrub: Callable[[str], str] = _unchanged

    def record(page: PageReport) -> None:
        pages.append(page)
        print(scrub(render_text(page, arguments.quote_chars)), flush=True)
        print()

    if arguments.render:
        try:
            with open_renderer(os.environ) as renderer:
                scrub = renderer.redact
                for url in arguments.urls:
                    record(scan_rendered_page(renderer, client, url))
        except RenderSetupError as error:
            print(scrub(f"loupe: {error}"), file=sys.stderr)
            return EXIT_USAGE
    elif client is not None:
        for url in arguments.urls:
            record(scan_page(client, url, fetch_markdown=not arguments.skip_markdown))

    if arguments.json is not None:
        report = {"loupe_version": __version__, "pages": [page_to_dict(page, arguments.quote_chars) for page in pages]}
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
