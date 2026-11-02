"""Command line entry point: loupe <url> [<url> ...]"""

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
from loupe.report import PageReport, check_findings, page_to_dict, render_text
from loupe.scan import scan_html

EXIT_OK = 0
EXIT_FETCH_FAILED = 1
EXIT_USAGE = 2


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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)

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
        print(render_text(page), flush=True)
        print()

    if arguments.json is not None:
        report = {"loupe_version": __version__, "pages": [page_to_dict(page) for page in pages]}
        try:
            arguments.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as error:
            print(f"loupe: could not write {arguments.json}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE

    failed = any(page.html_error is not None or page.markdown_error is not None for page in pages)
    return EXIT_FETCH_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
