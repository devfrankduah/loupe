"""Command line entry points: loupe <url> [<url> ...] and loupe report <results.json> -o <page.html>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
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
from loupe.report import DEFAULT_QUOTE_CHARACTERS, PageReport, check_findings, page_to_dict, render_text
from loupe.scan import scan_html
from loupe.site import ReportFormatError, load_results, render_report_page, run_date_from_filename

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
        "--quote-chars",
        type=_non_negative_int,
        default=DEFAULT_QUOTE_CHARACTERS,
        metavar="N",
        help=f"cut quoted hidden text to N characters, 0 for no limit (default: {DEFAULT_QUOTE_CHARACTERS})",
    )
    parser.add_argument("--version", action="version", version=f"loupe {__version__}")
    return parser


def scan_page(client: BrightDataClient, url: str, *, fetch_markdown: bool) -> PageReport:
    page = PageReport(url=url)
    try:
        html = client.fetch_html(url)
    except BrightDataError as error:
        page.html_error = str(error)
        return page
    page.html_characters = len(html)

    markdown: str | None = None
    if fetch_markdown:
        try:
            markdown = client.fetch_markdown(url)
            page.markdown_characters = len(markdown)
        except BrightDataError as error:
            page.markdown_error = str(error)

    page.findings = check_findings(scan_html(html), markdown)
    return page


def _build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loupe report",
        description="Turn a results JSON file into one self-contained HTML page.",
    )
    parser.add_argument("results", type=Path, help="JSON written by loupe --json")
    parser.add_argument("-o", "--output", type=Path, required=True, metavar="PATH", help="HTML file to write")
    return parser


def report_main(argv: Sequence[str]) -> int:
    arguments = _build_report_parser().parse_args(argv)
    try:
        pages = load_results(arguments.results.read_text(encoding="utf-8"))
    except OSError as error:
        print(f"loupe report: could not read {arguments.results}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE
    except ReportFormatError as error:
        print(f"loupe report: {arguments.results}: {error}", file=sys.stderr)
        return EXIT_USAGE

    page_html = render_report_page(pages, run_date_from_filename(arguments.results))
    try:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(page_html, encoding="utf-8")
    except OSError as error:
        print(f"loupe report: could not write {arguments.output}: {error.strerror}", file=sys.stderr)
        return EXIT_USAGE
    print(f"wrote {arguments.output}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # A page URL always starts with a scheme, so a bare "report" can only be the subcommand.
    if raw_arguments[:1] == ["report"]:
        return report_main(raw_arguments[1:])
    arguments = _build_parser().parse_args(raw_arguments)

    try:
        token = read_token_from_environment()
        client = BrightDataClient(token, unlocker_zone=arguments.zone)
    except MissingTokenError:
        print(f"loupe: set {TOKEN_ENVIRONMENT_VARIABLE} to a Bright Data API token", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as error:
        print(f"loupe: {error}", file=sys.stderr)
        return EXIT_USAGE

    pages: list[PageReport] = []
    for url in arguments.urls:
        page = scan_page(client, url, fetch_markdown=not arguments.skip_markdown)
        pages.append(page)
        print(render_text(page, arguments.quote_chars), flush=True)
        print()

    if arguments.json is not None:
        report = {"loupe_version": __version__, "pages": [page_to_dict(page, arguments.quote_chars) for page in pages]}
        try:
            arguments.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"loupe: could not write {arguments.json}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE

    failed = any(page.html_error is not None or page.markdown_error is not None for page in pages)
    return EXIT_FETCH_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
