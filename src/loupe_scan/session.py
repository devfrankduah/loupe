"""Load and scan pages with one backend, for the command line and the Python API alike.

A ScanSession holds what scanning several pages shares: the backend, the
Bright Data client, the browser when rendering, and the local fetcher, whose
robots.txt cache and Crawl-delay clock are per site for the whole session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Protocol

from loupe_scan.backend import Backend, choose_backend
from loupe_scan.convert import BRIGHT_DATA_MARKDOWN, MarkdownSource, html_to_markdown, local_markdown_source
from loupe_scan.errors import BlockedError, PageFetchError, SetupError
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
    RenderedPage,
    RenderError,
    cloned_onto_page,
    open_renderer,
    read_browser_endpoint,
    removed_by_scripts,
    rendered_findings,
)
from loupe_scan.report import FailureKind, PageReport, check_findings
from loupe_scan.scan import Finding, Kind, ScanMode, scan_html, scan_markup
from loupe_scan.stylesheets import StylesheetSource

# The README section on the Bright Data backend, for sites that turn loupe away.
BRIGHT_DATA_SECTION: Final[str] = "sites-that-block-bots-and-scanning-at-scale"
BLOCKED_HINT: Final[str] = (
    "Sites that block automated visitors can be scanned through the Bright Data backend: "
    f"{REPOSITORY_URL}#{BRIGHT_DATA_SECTION}"
)


@dataclass(frozen=True)
class MarkdownMaker:
    """Gets a page's markdown, from its URL or its HTML, and says where it came from."""

    source: MarkdownSource
    make: Callable[[str, str], str]


def bright_data_markdown(client: BrightDataClient) -> MarkdownMaker:
    return MarkdownMaker(BRIGHT_DATA_MARKDOWN, lambda url, html: client.fetch_markdown(url))


def local_markdown() -> MarkdownMaker:
    return MarkdownMaker(local_markdown_source(), html_to_markdown)


def _markdown_for(
    maker: MarkdownMaker | None, url: str, html: str, page: PageReport, *, keep: bool = False
) -> str | None:
    if maker is None:
        return None
    try:
        markdown = maker.make(url, html)
    except PageFetchError as error:
        page.markdown_error = str(error)
        return None
    page.markdown_characters = len(markdown)
    page.markdown_source = maker.source
    if keep:
        page.markdown = markdown
    return markdown


class HtmlSource(Protocol):
    def fetch_html(self, url: str) -> str: ...


def fetch_with_address(source: HtmlSource, url: str) -> tuple[str, str]:
    """url's HTML from source, and where it came from: after redirects when the local fetcher followed them."""
    if isinstance(source, LocalFetcher):
        fetched = source.fetch_page(url)
        return fetched.html, fetched.final_url
    return source.fetch_html(url), url


def record_failure(page: PageReport, error: PageFetchError | RenderError, *, otherwise: FailureKind) -> None:
    """Record why page wasn't scanned, pointing to Bright Data when a site blocked the local backend."""
    page.html_error = str(error)
    if isinstance(error, BlockedError):
        page.failure = FailureKind.BLOCKED
        if page.backend is Backend.LOCAL:
            page.html_error = f"{error}. {BLOCKED_HINT}"
    elif isinstance(error, RobotsDisallowedError):
        page.failure = FailureKind.ROBOTS
    else:
        page.failure = otherwise


def scan_page(
    source: HtmlSource,
    markdown: MarkdownMaker | None,
    url: str,
    *,
    backend: Backend,
    stylesheets: StylesheetSource | None = None,
) -> PageReport:
    """Fetch url's HTML from source and scan the markup. Markdown is compared only when a maker is given.

    The stylesheet check reads linked stylesheets only when stylesheets is given to fetch them.
    """
    page = PageReport(url=url, backend=backend)
    try:
        html, address = fetch_with_address(source, url)
    except PageFetchError as error:
        record_failure(page, error, otherwise=FailureKind.FETCH)
        return page
    page.html_characters = len(html)
    scanned = scan_markup(html, page_url=address, stylesheets=stylesheets)
    page.stylesheet_check = scanned.stylesheets
    page.needs_browser = scanned.needs_browser
    page.findings = check_findings(scanned.findings, _markdown_for(markdown, url, html, page))
    return page


def _check_rendered(
    page: PageReport, rendered: RenderedPage, markdown: MarkdownMaker | None, *, keep_markdown: bool = False
) -> PageReport:
    page.html_characters = len(rendered.html)
    page.rendered = rendered
    markdown_text = _markdown_for(markdown, page.url, rendered.html, page, keep=keep_markdown)
    browser_findings = rendered_findings(rendered)
    # When a given page tried to navigate, the browser may not have read all
    # of its HTML, so the markup checks' guesses stay, except where the
    # browser reported the same text itself.
    judged = {finding.text for finding in browser_findings} if rendered.stopped_navigations else None
    markup_findings = [
        finding
        for finding in scan_html(rendered.html)
        if finding.kind not in SUPERSEDED_BY_RENDERING or (judged is not None and finding.text not in judged)
    ]
    # A template a script copied into the page shows through the copy, which the browser judged above.
    page_text = rendered.page_text
    if page_text is not None:
        markup_findings = [
            finding
            for finding in markup_findings
            if finding.kind is not Kind.TEMPLATE or not cloned_onto_page(finding, page_text)
        ]
    # Text the page's scripts took out never shows, but an agent reading
    # markdown made from the served HTML still gets it. Where the browser
    # stopped reading given HTML early, the rest was never on the page to
    # take out, so the check doesn't run.
    removed: list[Finding] = []
    if rendered.page_text is not None and not rendered.stopped_navigations:
        removed = removed_by_scripts(rendered.html, rendered.page_text)
    checked = check_findings(markup_findings + browser_findings + removed, markdown_text)
    removed_ids = {id(finding) for finding in removed}
    page.findings = [item for item in checked if id(item.finding) not in removed_ids or item.in_markdown is True]
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
        record_failure(page, error, otherwise=FailureKind.RENDER)
        return page
    return _check_rendered(page, rendered, markdown)


def scan_html_once(
    html: str,
    url: str,
    *,
    backend: Backend,
    markdown: bool,
    renderer: PageRenderer | None = None,
    supplied: bool,
    keep_markdown: bool = False,
    stylesheets: StylesheetSource | None = None,
    address: str | None = None,
) -> PageReport:
    """Scan html as the page at url without fetching url, whoever fetched the HTML.

    The markdown is always made here from that same HTML, so what's compared
    is what was scanned, and keep_markdown keeps it on the report. With a
    renderer, the browser is given the HTML as the page at url and loads its
    stylesheets and scripts from the network. Without one, the stylesheet
    check reads linked stylesheets only when stylesheets is given to fetch
    them, from the host of address, where the HTML came from (url when not
    given). backend records where the HTML came from, and supplied whether
    the caller gave it to loupe.
    """
    maker = local_markdown() if markdown else None
    if renderer is None:
        page = PageReport(url=url, backend=backend, supplied_html=supplied)
        page.html_characters = len(html)
        scanned = scan_markup(html, page_url=url if address is None else address, stylesheets=stylesheets)
        page.stylesheet_check = scanned.stylesheets
        page.needs_browser = scanned.needs_browser
        page.findings = check_findings(scanned.findings, _markdown_for(maker, url, html, page, keep=keep_markdown))
        return page
    page = PageReport(url=url, backend=backend, mode=ScanMode.RENDERED, browser=renderer.source, supplied_html=supplied)
    try:
        rendered = renderer.render_html(url, html)
    except RenderError as error:
        record_failure(page, error, otherwise=FailureKind.RENDER)
        return page
    return _check_rendered(page, rendered, maker, keep_markdown=keep_markdown)


def scan_supplied_html(
    html: str, url: str, *, backend: Backend, markdown: bool, renderer: PageRenderer | None = None
) -> PageReport:
    """Scan HTML the caller already has, as the page at url, without fetching url. See scan_html_once."""
    return scan_html_once(html, url, backend=backend, markdown=markdown, renderer=renderer, supplied=True)


def scrub_strings(value: object, scrub: Callable[[str], str]) -> object:
    """value with scrub applied to every string in it, keys included."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(str(key)): scrub_strings(item, scrub) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_strings(item, scrub) for item in value]
    return value


def endpoint_redactor(environment: Mapping[str, str]) -> Callable[[str], str]:
    """Hides any part of the Browser API endpoint, if one is set, whichever backend runs."""
    value = environment.get(BROWSER_ENDPOINT_VARIABLE, "").strip()
    try:
        return EndpointRedactor(value or None)
    except ValueError:
        # A value too malformed to split into parts can still be hidden whole.
        return lambda text: text.replace(value, ENDPOINT_PLACEHOLDER)


def bright_data_client(environment: Mapping[str, str], zone: str) -> BrightDataClient:
    try:
        return BrightDataClient(read_token_from_environment(environment), unlocker_zone=zone)
    except MissingTokenError:
        raise SetupError(
            f"the brightdata backend needs {TOKEN_ENVIRONMENT_VARIABLE} set to a Bright Data API token; "
            "set it, or use the local backend (--backend local)"
        ) from None
    except ValueError as error:
        raise SetupError(str(error)) from None


@dataclass(frozen=True)
class ScanSettings:
    render: bool = False
    # None picks the backend from the environment, as choose_backend does.
    backend: Backend | None = None
    markdown: bool = True
    # Only the local backend reads robots.txt.
    respect_robots: bool = True
    zone: str = DEFAULT_UNLOCKER_ZONE


def _ignore(message: str) -> None:
    return None


class ScanSession:
    """Scans pages one at a time with one backend.

    Use it in a with block: when rendering, the browser starts on entry and
    closes on exit. Raises SetupError when it can't scan at all, on creation
    for a missing token and on entry for a missing browser.
    """

    def __init__(
        self,
        settings: ScanSettings,
        *,
        environment: Mapping[str, str],
        on_note: Callable[[str], None] = _ignore,
    ) -> None:
        self.settings = settings
        self.backend = choose_backend(settings.backend, environment)
        self.redact = endpoint_redactor(environment)
        self._environment = environment
        # A rendered scan loads pages itself, so it needs Bright Data only for markdown.
        client: BrightDataClient | None = None
        if self.backend is Backend.BRIGHTDATA and (settings.markdown or not settings.render):
            client = bright_data_client(environment, settings.zone)
        self._client = client
        self._markdown: MarkdownMaker | None = None
        if settings.markdown:
            self._markdown = local_markdown() if client is None else bright_data_markdown(client)
        self._fetcher: LocalFetcher | None = None
        if self.backend is Backend.LOCAL:
            self._fetcher = LocalFetcher(respect_robots=settings.respect_robots, on_wait=on_note)
        self._resources = ExitStack()
        self._renderer: PageRenderer | None = None

    def __repr__(self) -> str:
        return f"ScanSession(backend={self.backend.value}, render={self.settings.render})"

    def __enter__(self) -> ScanSession:
        if self.settings.render:
            endpoint = read_browser_endpoint(self._environment) if self.backend is Backend.BRIGHTDATA else None
            self._renderer = self._resources.enter_context(open_renderer(endpoint))
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._renderer = None
        self._resources.close()

    def scan(self, url: str) -> PageReport:
        """Scan url. A page that can't be scanned comes back with html_error and failure set."""
        if self.settings.render:
            if self._renderer is None:
                raise RuntimeError("a rendering session scans pages only inside its with block")
            return scan_rendered_page(self._renderer, self._markdown, url, backend=self.backend, fetcher=self._fetcher)
        source: HtmlSource | None = self._fetcher if self._fetcher is not None else self._client
        if source is None:
            raise RuntimeError("a session needs a local fetcher or a Bright Data client")
        return scan_page(source, self._markdown, url, backend=self.backend, stylesheets=self._fetcher)
