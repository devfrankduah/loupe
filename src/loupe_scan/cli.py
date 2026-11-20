"""Command line entry points: loupe <url> [<url> ...] [--render] and loupe report <results.json>... -o <page.html>"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from loupe_scan._version import __version__
from loupe_scan.backend import Backend
from loupe_scan.errors import SetupError
from loupe_scan.fetch import DEFAULT_UNLOCKER_ZONE, TOKEN_ENVIRONMENT_VARIABLE
from loupe_scan.local_fetch import decode_html
from loupe_scan.render import BROWSER_ENDPOINT_VARIABLE, open_renderer
from loupe_scan.report import (
    DEFAULT_QUOTE_CHARACTERS,
    FailOn,
    PageReport,
    findings_counted,
    page_to_dict,
    printable,
    render_text,
    summary_markdown,
    summary_table,
)
from loupe_scan.session import ScanSession, ScanSettings, endpoint_redactor, scan_supplied_html, scrub_strings
from loupe_scan.site import PageRecord, ReportFormatError, load_results, render_report_page, run_date_from_filename


# Every page was scanned in full and none had a finding --fail-on counts.
EXIT_OK = 0
# Every page was scanned in full and at least one had a finding --fail-on counts.
EXIT_FINDINGS = 1
# A page couldn't be scanned in full (blocked, stopped by robots.txt, not
# fetched or loaded, or its markdown not compared), or loupe couldn't run.
# It wins over EXIT_FINDINGS, since the results are incomplete.
EXIT_ERROR = 2
EXIT_USAGE = EXIT_ERROR

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
            "To turn results files written with --json-output into one HTML page:\n"
            "  loupe report RESULTS [RESULTS ...] -o PATH\n"
            "To serve loupe's tools to an agent over MCP (needs the mcp extra):\n"
            "  loupe mcp\n"
            "See loupe report --help and loupe mcp --help."
        ),
    )
    parser.add_argument("urls", nargs="*", metavar="url", help="page to fetch and scan")
    parser.add_argument(
        "--urls-file",
        metavar="PATH",
        help=(
            "also scan the URLs in PATH, one per line, after any given above; blank lines and lines "
            "starting with # are skipped, and - reads standard input"
        ),
    )
    parser.add_argument(
        "--html-file",
        metavar="PATH",
        help=(
            "scan the HTML saved in PATH as the page at the one URL given, without fetching it; - reads "
            "standard input. With --render, local Chromium is given the HTML as the page at that URL"
        ),
    )
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
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the results as a JSON array instead of text, in the format described in docs/json.md",
    )
    parser.add_argument(
        "--json-output", type=Path, metavar="PATH", help="also write the results as JSON to PATH, in the same format"
    )
    parser.add_argument(
        "--markdown-summary",
        type=Path,
        metavar="PATH",
        help=(
            "also add the summary table and exit code to PATH as Markdown, appending to what's there, "
            "such as a CI job summary"
        ),
    )
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
    parser.add_argument(
        "--fail-on",
        type=FailOn,
        choices=list(FailOn),
        default=FailOn.INSTRUCTIONS,
        metavar="{instructions,hidden,never}",
        help=(
            "which findings make loupe exit 1: instructions (hidden text worded like an instruction to an AI), "
            "hidden (any hidden text) or never (default: instructions). A page that couldn't be scanned "
            "always exits 2"
        ),
    )
    parser.add_argument("--version", action="version", version=f"loupe {__version__}")
    return parser


def _build_report_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loupe report",
        description=(
            "Turn results JSON files into one self-contained HTML page. Give a markup-only run and a "
            "--render run of the same pages to see them side by side."
        ),
    )
    parser.add_argument("results", type=Path, nargs="+", help="JSON written by loupe --json or --json-output")
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


def _note(message: str) -> None:
    print(f"loupe: {message}", file=sys.stderr, flush=True)


def _read_urls_file(name: str) -> list[str]:
    """The URLs listed in the file called name, or on standard input for -. Raises OSError or UnicodeDecodeError."""
    if name == "-":
        text = sys.stdin.read()
    else:
        # utf-8-sig, so a file saved with a byte order mark reads the same as one without.
        text = Path(name).read_text(encoding="utf-8-sig")
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _urls_to_scan(arguments: argparse.Namespace) -> list[str] | None:
    """Every URL to scan, each once, in the order given, or None after explaining why there are none."""
    urls: list[str] = list(arguments.urls)
    if arguments.urls_file is not None:
        try:
            urls.extend(_read_urls_file(arguments.urls_file))
        except OSError as error:
            _note(f"could not read {arguments.urls_file}: {error.strerror or error}")
            return None
        except UnicodeDecodeError:
            _note(f"could not read {arguments.urls_file}: it isn't UTF-8 text")
            return None
    if not urls:
        _note("give at least one URL to scan, or a file of them with --urls-file")
        return None
    unique = list(dict.fromkeys(urls))
    if len(unique) < len(urls):
        repeats = len(urls) - len(unique)
        _note(f"scanning each URL once: {repeats:,} {'repeat was' if repeats == 1 else 'repeats were'} left out")
    return unique


def _html_file_url(arguments: argparse.Namespace) -> list[str] | None:
    """The one URL a saved HTML file is scanned as, or None after explaining why the arguments don't give one."""
    if arguments.urls_file is not None or len(arguments.urls) != 1:
        _note("--html-file scans one page: give the one URL its HTML came from, and no --urls-file")
        return None
    (url,) = arguments.urls
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = urlsplit("")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        _note(f"--html-file needs the http(s) URL its HTML came from, not {url!r}")
        return None
    for given, option in ((arguments.backend is not None, "--backend"), (arguments.ignore_robots, "--ignore-robots")):
        if given:
            _note(f"{option} has no effect with --html-file, which fetches nothing")
    return [url]


def _read_html_file(name: str) -> str | None:
    """The HTML in the file called name, or on standard input for -, or None after explaining why it can't be read."""
    try:
        body = sys.stdin.buffer.read() if name == "-" else Path(name).read_bytes()
    except OSError as error:
        _note(f"could not read {name}: {error.strerror or error}")
        return None
    # A saved page has no Content-Type header, so its charset comes from a byte order mark or the page itself.
    return decode_html(body, "")


def _scan_html_file(html: str, url: str, arguments: argparse.Namespace) -> PageReport:
    markdown = not arguments.skip_markdown
    if not arguments.render:
        return scan_supplied_html(html, url, backend=Backend.LOCAL, markdown=markdown)
    with open_renderer(None) as renderer:
        return scan_supplied_html(html, url, backend=Backend.LOCAL, markdown=markdown, renderer=renderer)


MCP_INSTALL_HINT = "from the loupe folder, run pip install -e '.[mcp]'"


def mcp_main(argv: Sequence[str]) -> int:
    """loupe mcp, which needs the MCP SDK from the mcp extra."""
    if importlib.util.find_spec("mcp") is None:
        _note(f"loupe mcp needs the MCP Python SDK: {MCP_INSTALL_HINT}")
        return EXIT_USAGE
    from loupe_scan.mcp_server import main as serve

    return serve(argv)


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    # A page URL always starts with a scheme, so a bare "report" or "mcp" can only be the subcommand.
    if raw_arguments[:1] == ["report"]:
        return report_main(raw_arguments[1:])
    if raw_arguments[:1] == ["mcp"]:
        return mcp_main(raw_arguments[1:])
    arguments = _build_parser().parse_args(raw_arguments)
    html: str | None = None
    if arguments.html_file is not None:
        urls = _html_file_url(arguments)
        if urls is not None:
            html = _read_html_file(arguments.html_file)
            if html is None:
                return EXIT_USAGE
    else:
        urls = _urls_to_scan(arguments)
    if urls is None:
        return EXIT_USAGE
    # Everything printed or saved passes through this, a second guard after
    # the one around each error, so no part of the endpoint gets out.
    scrub = endpoint_redactor(os.environ)
    settings = ScanSettings(
        render=arguments.render,
        backend=arguments.backend,
        markdown=not arguments.skip_markdown,
        respect_robots=not arguments.ignore_robots,
        zone=arguments.zone,
    )
    pages: list[PageReport] = []

    def report(page: PageReport) -> None:
        pages.append(page)
        if not arguments.json:
            print(scrub(render_text(page, arguments.quote_chars)), flush=True)
            print()

    try:
        if html is not None:
            report(_scan_html_file(html, urls[0], arguments))
        else:
            session = ScanSession(settings, environment=os.environ, on_note=_note)
            if session.backend is Backend.BRIGHTDATA and arguments.ignore_robots:
                _note("--ignore-robots only changes the local backend, so it has no effect here")
            with session:
                for url in urls:
                    report(session.scan(url))
    except SetupError as error:
        message = scrub(f"loupe couldn't scan: {error}")
        print(scrub(f"loupe: {error}"), file=sys.stderr)
        _append_markdown(arguments.markdown_summary, f"### loupe\n\n{printable(message)}\n")
        return EXIT_USAGE
    if len(pages) > 1 and not arguments.json:
        print(scrub(summary_table(pages)), flush=True)

    results = scrub_strings([page_to_dict(page, arguments.quote_chars) for page in pages], scrub)
    if arguments.json:
        # Escaped to ASCII, so text from a page can't reach the terminal as a
        # control or direction-changing character; any JSON reader decodes it.
        print(json.dumps(results, indent=2, ensure_ascii=True), flush=True)
    if arguments.json_output is not None:
        try:
            text = json.dumps(results, indent=2, ensure_ascii=False) + "\n"
            arguments.json_output.write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"loupe: could not write {arguments.json_output}: {error.strerror}", file=sys.stderr)
            return EXIT_USAGE

    code, reason = _outcome(pages, arguments.fail_on)
    if code != EXIT_OK:
        _note(f"exit {code}: {reason}")
    if not _append_markdown(arguments.markdown_summary, scrub(summary_markdown(pages, f"Exit {code}: {reason}."))):
        return EXIT_USAGE
    return code


def _append_markdown(path: Path | None, markdown: str) -> bool:
    """Add markdown to the end of path, if one was given. False, after saying why, when it couldn't be written."""
    if path is None:
        return True
    try:
        with path.open("a", encoding="utf-8") as summary:
            summary.write(markdown)
    except OSError as error:
        _note(f"could not write {path}: {error.strerror}")
        return False
    return True


def _count_pages(number: int) -> str:
    return "1 page" if number == 1 else f"{number:,} pages"


def _outcome(pages: Sequence[PageReport], fail_on: FailOn) -> tuple[int, str]:
    """The exit code for the run, and why, in words that follow "exit N: "."""
    incomplete = [page for page in pages if page.html_error is not None or page.markdown_error is not None]
    if incomplete:
        return EXIT_ERROR, f"{_count_pages(len(incomplete))} couldn't be scanned in full"
    counted = [page for page in pages if findings_counted(page, fail_on)]
    if counted:
        what = "hidden text" if fail_on is FailOn.HIDDEN else "hidden text worded like instructions to an AI"
        return EXIT_FINDINGS, f"{_count_pages(len(counted))} with {what} (--fail-on {fail_on.value})"
    return EXIT_OK, f"{_count_pages(len(pages))} scanned in full, with nothing --fail-on {fail_on.value} counts"


if __name__ == "__main__":
    sys.exit(main())
