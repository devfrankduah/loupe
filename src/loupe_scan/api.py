"""loupe from Python: scan a page and get back the text it hides from people.

    from loupe_scan import scan

    result = scan("https://example.com/")
    print(result.verdict)

docs/python.md has the whole API and an example of checking a page before
handing it to a model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal
from urllib.parse import urlsplit

from loupe_scan.backend import Backend
from loupe_scan.report import (
    DEFAULT_QUOTE_CHARACTERS,
    CheckedFinding,
    FailOn,
    FailureKind,
    PageReport,
    findings_counted,
    page_to_dict,
    verdict,
)
from loupe_scan.render import open_renderer
from loupe_scan.scan import Kind, ScanMode
from loupe_scan.session import ScanSession, ScanSettings, endpoint_redactor, scan_supplied_html, scrub_strings

BackendName = Literal["local", "brightdata"]
FailOnName = Literal["instructions", "hidden", "never"]


class ScanError(RuntimeError):
    """The page couldn't be scanned. kind says why, as error.kind does in the JSON. The message is safe to print."""

    def __init__(self, message: str, *, url: str, kind: FailureKind) -> None:
        super().__init__(message)
        self.url = url
        self.kind = kind


@dataclass(frozen=True)
class HiddenText:
    """One piece of text the page hides from people."""

    # The whole text. Per-request meta values such as nonces are replaced with a placeholder.
    text: str
    kind: Kind
    # How it was hidden, such as "display:none" or "alt".
    detail: str
    tag: str
    # STATIC when the markup scan found it, RENDERED when the browser did.
    mode: ScanMode
    # The phrases that make it read like an instruction to an AI. Empty when there are none.
    instruction_phrases: tuple[str, ...]
    # Whether the text reaches the markdown. None when it wasn't compared.
    in_markdown: bool | None

    @property
    def is_instruction_like(self) -> bool:
        return bool(self.instruction_phrases)


def _hidden_text(item: CheckedFinding, redact: Callable[[str], str]) -> HiddenText:
    finding = item.finding
    return HiddenText(
        text=redact(finding.text),
        kind=finding.kind,
        detail=redact(finding.detail),
        tag=finding.tag,
        mode=finding.mode,
        instruction_phrases=finding.instruction_phrases,
        in_markdown=item.in_markdown,
    )


@dataclass(frozen=True)
class ScanResult:
    """What loupe found on one page. scan() and Scanner.scan() make these."""

    url: str
    backend: Backend
    mode: ScanMode
    # The plain-English summary the command line prints first. Its wording can change between releases.
    verdict: str
    hidden_text: tuple[HiddenText, ...]
    # The markdown's length, or None when no markdown was compared.
    markdown_characters: int | None
    # Why the markdown couldn't be compared. The page itself was still scanned.
    markdown_error: str | None
    _report: PageReport = field(repr=False, compare=False)
    _redact: Callable[[str], str] = field(repr=False, compare=False)

    @property
    def instruction_like(self) -> tuple[HiddenText, ...]:
        """The hidden text worded like an instruction to an AI."""
        return tuple(item for item in self.hidden_text if item.is_instruction_like)

    def fails(self, fail_on: FailOn | FailOnName = FailOn.INSTRUCTIONS) -> bool:
        """True when the page has a finding fail_on counts, as loupe --fail-on does for exit code 1."""
        return bool(findings_counted(self._report, FailOn(fail_on)))

    def to_dict(self, *, quote_characters: int = DEFAULT_QUOTE_CHARACTERS) -> dict[str, object]:
        """The page as one object of loupe --json's array, with text cut to quote_characters (0 keeps it whole)."""
        if quote_characters < 0:
            raise ValueError("quote_characters must be 0 or more")
        document = scrub_strings(page_to_dict(self._report, quote_characters), self._redact)
        if not isinstance(document, dict):
            raise TypeError("a page should turn into a JSON object")
        return document

    def to_json(self, *, quote_characters: int = DEFAULT_QUOTE_CHARACTERS, indent: int | None = 2) -> str:
        """to_dict() as JSON text, in the format docs/json.md describes."""
        return json.dumps(self.to_dict(quote_characters=quote_characters), indent=indent, ensure_ascii=False)


def _result(report: PageReport, redact: Callable[[str], str]) -> ScanResult:
    if report.html_error is not None:
        kind = FailureKind.FETCH if report.failure is None else report.failure
        raise ScanError(redact(report.html_error), url=report.url, kind=kind)
    return ScanResult(
        url=report.url,
        backend=report.backend,
        mode=report.mode,
        verdict=redact(verdict(report)),
        hidden_text=tuple(_hidden_text(item, redact) for item in report.findings),
        markdown_characters=report.markdown_characters,
        markdown_error=None if report.markdown_error is None else redact(report.markdown_error),
        _report=report,
        _redact=redact,
    )


class Scanner:
    """Scans several pages with one browser, and one robots.txt and Crawl-delay per site.

        with Scanner(render=True) as scanner:
            for url in urls:
                result = scanner.scan(url)

    backend is "local", "brightdata", or None to use Bright Data when its
    environment variables are set and this machine otherwise. Raises
    SetupError when loupe can't scan at all: when created for a missing
    Bright Data token, and on entering the with block for a missing browser.
    """

    def __init__(
        self,
        *,
        render: bool = False,
        backend: Backend | BackendName | None = None,
        markdown: bool = True,
        respect_robots: bool = True,
    ) -> None:
        settings = ScanSettings(
            render=render,
            backend=None if backend is None else Backend(backend),
            markdown=markdown,
            respect_robots=respect_robots,
        )
        self._session = ScanSession(settings, environment=os.environ)
        self.backend = self._session.backend

    def __repr__(self) -> str:
        return f"Scanner(backend={self.backend.value!r}, render={self._session.settings.render})"

    def __enter__(self) -> Scanner:
        self._session.__enter__()
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._session.__exit__(error_type, error, traceback)

    def scan(self, url: str) -> ScanResult:
        """Scan url. Raises ScanError when the page couldn't be scanned, so a page is never passed on unchecked."""
        return _result(self._session.scan(url), self._session.redact)


def scan(
    url: str,
    *,
    render: bool = False,
    backend: Backend | BackendName | None = None,
    markdown: bool = True,
    respect_robots: bool = True,
) -> ScanResult:
    """Scan one page. For several, use a Scanner, which reads each site's robots.txt once and keeps its Crawl-delay.

    render loads the page in Chromium (it needs the render extra and
    playwright install chromium). markdown compares the hidden text with a
    markdown version of the page. respect_robots obeys robots.txt on the
    local backend; turn it off only for pages you own or may scan.

    Raises ScanError when the page couldn't be scanned (blocked, kept out by
    robots.txt, or not fetched or loaded) and SetupError when loupe can't
    scan at all.
    """
    with Scanner(render=render, backend=backend, markdown=markdown, respect_robots=respect_robots) as scanner:
        return scanner.scan(url)


def scan_html(html: str, url: str, *, render: bool = False, markdown: bool = True) -> ScanResult:
    """Scan HTML you already have, as the page at url, without fetching anything.

    Scan the HTML you're about to hand to a model, and a site that serves
    agents something different from what loupe would have fetched can't slip
    past. The markdown compared is made here from the same HTML.

    url must be an http(s) URL. Without render it only names the page. With
    render, local Chromium is given the HTML as the page at url, so relative
    links resolve against it, and its stylesheets and scripts load from the
    network; docs/python.md says how that can differ from a live load. It
    needs the render extra and playwright install chromium.

    Raises ScanError when the browser couldn't load it, SetupError when
    rendering can't run here, and ValueError for a url that isn't http(s).
    """
    if not isinstance(html, str):
        raise TypeError("html must be a str; decode bytes first, for example with the page's charset")
    _require_web_url(url)
    redact = endpoint_redactor(os.environ)
    if not render:
        return _result(scan_supplied_html(html, url, backend=Backend.LOCAL, markdown=markdown), redact)
    with open_renderer(None) as renderer:
        report = scan_supplied_html(html, url, backend=Backend.LOCAL, markdown=markdown, renderer=renderer)
    return _result(report, redact)


def _require_web_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError(f"not a valid URL: {url!r}") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"url must be an http(s) URL with a host, not {url!r}")
