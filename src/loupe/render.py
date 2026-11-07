"""Scan a page as a browser draws it, so stylesheet and script hiding is caught.

The static scan reads markup only. It misses text hidden by a CSS class, and
it flags text that starts invisible and fades in as the reader scrolls. Here
a real Chromium loads the page instead:

1. Load it, wait for the network to go quiet and for opening animations to
   finish, then record whether each element's own text is visible (S0).
2. Scroll from the top to the bottom one viewport at a time, pausing at each
   step and checking again whatever hasn't been seen yet.
3. Wait for transitions to settle and check one last time (S1).

Text never seen visible is a hidden finding. Text hidden at S0 but seen at a
later step was revealed on scroll and is reported separately. Checking at
every step, rather than only at S0 and S1, matters for content that fades
out again once scrolled past and for content-visibility:auto sections, which
a browser only draws near the viewport; both are invisible at S1.

The checks run in visibility.js, inside an isolated JavaScript world, so the
page's own scripts can't tamper with them.

The browser is local Chromium unless BRIGHTDATA_BROWSER_WSS holds a CDP
websocket endpoint. That value carries credentials: it is never printed,
logged or saved, and errors from connecting to it say only that it failed.
"""

from __future__ import annotations

import functools
import importlib.resources
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, Generic, Literal, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from loupe.scan import Finding, Kind, ScanMode, make_finding, normalize_whitespace

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright

BROWSER_ENDPOINT_VARIABLE: Final[str] = "BRIGHTDATA_BROWSER_WSS"

# A browser decides directly whether these show, whatever hid them, so a
# rendered scan drops the static guesses about them and keeps its own result.
SUPERSEDED_BY_RENDERING: Final[frozenset[Kind]] = frozenset({Kind.HIDDEN_ATTRIBUTE, Kind.INLINE_STYLE})

INSTALL_HINT: Final[str] = "pip install 'loupe[render]' && playwright install chromium"
_REDACTED: Final[str] = "[redacted]"
_ERROR_MESSAGE_LIMIT: Final[int] = 300
_SCANNER: Final[str] = "loupeScanner"


class RenderError(RuntimeError):
    """Rendering one page failed. The message is safe to print and to save."""


class RenderSetupError(RuntimeError):
    """Rendering can't run at all here: Playwright or Chromium is missing, or the endpoint is malformed."""


class BrowserSource(str, Enum):
    LOCAL = "local-chromium"
    REMOTE = "remote-cdp"

    @property
    def description(self) -> str:
        return "local Chromium" if self is BrowserSource.LOCAL else f"the browser at {BROWSER_ENDPOINT_VARIABLE}"


@dataclass(frozen=True)
class RenderSettings:
    viewport_width: int = 1280
    viewport_height: int = 800
    connect_timeout_ms: float = 60_000
    navigation_timeout_ms: float = 60_000
    load_timeout_ms: float = 15_000
    network_idle_timeout_ms: float = 10_000
    # After the last step, how long to let lazily loaded content arrive
    # before deciding the page really ends there.
    growth_timeout_ms: float = 3_000
    scroll_pause_ms: float = 250
    settle_timeout_ms: float = 3_000
    max_scroll_steps: int = 60

    def __post_init__(self) -> None:
        for name in ("viewport_width", "viewport_height", "max_scroll_steps"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        for name in (
            "connect_timeout_ms",
            "navigation_timeout_ms",
            "load_timeout_ms",
            "network_idle_timeout_ms",
            "growth_timeout_ms",
            "scroll_pause_ms",
            "settle_timeout_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True)
class RenderedText:
    tag: str
    text: str
    # Why it couldn't be seen, such as "opacity:0" or "clipped". Empty for revealed text.
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedPage:
    final_url: str
    # The HTML as served, before scripts ran, for the static markup checks.
    html: str
    hidden: tuple[RenderedText, ...]
    revealed_on_scroll: tuple[RenderedText, ...]
    text_elements: int
    scroll_steps: int
    reached_bottom: bool
    notes: tuple[str, ...]


class PageRenderer(Protocol):
    @property
    def source(self) -> BrowserSource: ...

    def render(self, url: str) -> RenderedPage: ...


class Closeable(Protocol):
    def close(self) -> None: ...


BrowserT_co = TypeVar("BrowserT_co", covariant=True)
BrowserT = TypeVar("BrowserT", bound=Closeable)


class BrowserLauncher(Protocol[BrowserT_co]):
    """The part of Playwright's BrowserType used here, so tests can pass a fake."""

    def launch(self, *, headless: bool | None = None) -> BrowserT_co: ...

    def connect_over_cdp(self, endpoint_url: str, *, timeout: float | None = None) -> BrowserT_co: ...


def rendered_findings(page: RenderedPage) -> list[Finding]:
    return [
        make_finding(Kind.RENDERED_HIDDEN, ", ".join(item.reasons), item.tag, item.text, ScanMode.RENDERED)
        for item in page.hidden
    ]


# The endpoint


def read_browser_endpoint(environment: Mapping[str, str]) -> str | None:
    """The CDP endpoint to connect to, or None to launch local Chromium."""
    value = environment.get(BROWSER_ENDPOINT_VARIABLE, "").strip()
    if not value:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        raise RenderSetupError(f"{BROWSER_ENDPOINT_VARIABLE} is not a valid URL (its value is not shown)") from None
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise RenderSetupError(f"{BROWSER_ENDPOINT_VARIABLE} must be a ws:// or wss:// URL (its value is not shown)")
    return value


class EndpointRedactor:
    """Removes an endpoint, and the credentials and query inside it, from text."""

    def __init__(self, endpoint: str | None) -> None:
        pieces: set[str] = set()
        if endpoint:
            pieces.add(endpoint)
            parts = urlsplit(endpoint)
            for piece in (parts.username, parts.password, parts.query):
                if piece:
                    pieces.add(piece)
                    pieces.add(unquote(piece))
        self._pieces = tuple(sorted(pieces, key=len, reverse=True))

    def __repr__(self) -> str:
        return "EndpointRedactor()"

    def __call__(self, text: str) -> str:
        for piece in self._pieces:
            text = text.replace(piece, _REDACTED)
        return text


def _first_line(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    line = lines[0].strip() if lines else type(error).__name__
    return line[:_ERROR_MESSAGE_LIMIT]


def acquire_browser(launcher: BrowserLauncher[BrowserT], endpoint: str | None, settings: RenderSettings) -> BrowserT:
    if endpoint is None:
        try:
            return launcher.launch(headless=True)
        except Exception as error:
            message = _first_line(error)
            if "Executable doesn't exist" in message:
                raise RenderSetupError(f"Chromium for Playwright isn't installed. Run: {INSTALL_HINT}") from None
            raise RenderSetupError(f"Chromium didn't start: {message}") from None
    try:
        return launcher.connect_over_cdp(endpoint, timeout=settings.connect_timeout_ms)
    except Exception as error:
        # Playwright's message and call log can repeat the endpoint with its
        # credentials, so none of the original text is passed on.
        raise RenderError(
            f"Couldn't connect to the browser at {BROWSER_ENDPOINT_VARIABLE} ({type(error).__name__})"
        ) from None


# Running the scanner inside the page


@functools.cache
def _scanner_source() -> str:
    return (importlib.resources.files("loupe") / "visibility.js").read_text(encoding="utf-8")


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RenderError(f"{what} was not an object")
    return value


class _IsolatedWorld:
    """A JavaScript world on the page's main frame that the page's own scripts can't reach."""

    def __init__(self, page: Page) -> None:
        self._session = page.context.new_cdp_session(page)
        tree = _mapping(self._session.send("Page.getFrameTree"), "the frame tree")
        frame = _mapping(_mapping(tree.get("frameTree"), "the frame tree").get("frame"), "the main frame")
        frame_id = frame.get("id")
        if not isinstance(frame_id, str):
            raise RenderError("the browser didn't report a main frame")
        world = _mapping(
            self._session.send("Page.createIsolatedWorld", {"frameId": frame_id, "worldName": "loupe"}),
            "the isolated world",
        )
        context_id = world.get("executionContextId")
        if not isinstance(context_id, int) or isinstance(context_id, bool):
            raise RenderError("the browser didn't create an isolated world")
        self._context_id = context_id

    def evaluate(self, expression: str) -> object:
        reply = _mapping(
            self._session.send(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "contextId": self._context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
            ),
            "the evaluation result",
        )
        details = reply.get("exceptionDetails")
        if details is not None:
            raise RenderError(f"the scan script failed: {_exception_text(details)}")
        result = reply.get("result")
        return result.get("value") if isinstance(result, dict) else None

    def call(self, method: str, *arguments: int) -> object:
        return self.evaluate(f"{_SCANNER}.{method}({', '.join(str(argument) for argument in arguments)})")

    def detach(self) -> None:
        from playwright.sync_api import Error as PlaywrightError

        try:
            self._session.detach()
        except PlaywrightError:
            # The page is closed right after this, which ends the session anyway.
            pass


def _exception_text(details: object) -> str:
    if isinstance(details, dict):
        exception = details.get("exception")
        if isinstance(exception, dict) and isinstance(exception.get("description"), str):
            lines = exception["description"].splitlines()
            return (lines[0] if lines else "unknown error")[:_ERROR_MESSAGE_LIMIT]
        if isinstance(details.get("text"), str):
            return str(details["text"])[:_ERROR_MESSAGE_LIMIT]
    return "unknown error"


# Reading the scanner's result


def _count(record: Mapping[str, object], name: str) -> int:
    value = record.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RenderError(f"the scan result's {name} was not a count")
    return value


def _texts(record: Mapping[str, object], name: str, *, with_reasons: bool) -> tuple[RenderedText, ...]:
    items = record.get(name)
    if not isinstance(items, list):
        raise RenderError(f"the scan result's {name} was not a list")
    texts: list[RenderedText] = []
    for item in items:
        unit = _mapping(item, f"an entry in {name}")
        tag = unit.get("tag")
        text = unit.get("text")
        if not isinstance(tag, str) or not isinstance(text, str):
            raise RenderError(f"an entry in {name} had no tag or text")
        reasons: tuple[str, ...] = ()
        if with_reasons:
            raw_reasons = unit.get("reasons")
            if not isinstance(raw_reasons, list) or not all(isinstance(reason, str) for reason in raw_reasons):
                raise RenderError(f"an entry in {name} had no reasons")
            reasons = tuple(str(reason) for reason in raw_reasons)
        normalized = normalize_whitespace(text)
        if normalized:
            texts.append(RenderedText(tag=tag, text=normalized, reasons=reasons))
    return tuple(texts)


@dataclass(frozen=True)
class ScanResult:
    text_elements: int
    hidden: tuple[RenderedText, ...]
    revealed_on_scroll: tuple[RenderedText, ...]


def parse_scan_result(value: object) -> ScanResult:
    record = _mapping(value, "the scan result")
    return ScanResult(
        text_elements=_count(record, "text_elements"),
        hidden=_texts(record, "hidden_units", with_reasons=True),
        revealed_on_scroll=_texts(record, "revealed_units", with_reasons=False),
    )


# Rendering one page


def _require_http_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise RenderError(f"Rendering {url!r} failed: not an http(s) URL")


def _seconds(milliseconds: float) -> str:
    return f"{milliseconds / 1000:g} s"


def _wait_for(page: Page, state: Literal["load", "networkidle"], timeout_ms: float) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    try:
        page.wait_for_load_state(state, timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    return True


def _scan_loaded_page(page: Page, url: str, settings: RenderSettings) -> RenderedPage:
    notes: list[str] = []
    response = page.goto(url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
    if response is None:
        raise RenderError("the browser got no response")
    if response.status >= 400:
        raise RenderError(f"HTTP {response.status}")
    html = response.text()
    if not _wait_for(page, "load", settings.load_timeout_ms):
        notes.append(f"the load event hadn't fired after {_seconds(settings.load_timeout_ms)}")
    if not _wait_for(page, "networkidle", settings.network_idle_timeout_ms):
        notes.append(
            f"the network was still busy after {_seconds(settings.network_idle_timeout_ms)}, "
            "so the first check ran with requests open"
        )

    world = _IsolatedWorld(page)
    try:
        world.evaluate(_scanner_source())
        if world.call("settle", int(settings.settle_timeout_ms)) is not True:
            notes.append("animations were still running when the page was first checked")
        world.call("recordLoad")
        world.call("scrollToTop")
        page.wait_for_timeout(settings.scroll_pause_ms)
        world.call("recheck")

        steps = 0
        reached_bottom = False
        while steps < settings.max_scroll_steps:
            at_bottom = world.call("scrollOneViewport") is True
            steps += 1
            page.wait_for_timeout(settings.scroll_pause_ms)
            world.call("recheck")
            if at_bottom:
                _wait_for(page, "networkidle", settings.growth_timeout_ms)
                if world.call("atBottom") is True:
                    reached_bottom = True
                    break
        if not reached_bottom:
            notes.append(
                f"stopped after {steps} scroll steps without reaching the bottom, so text further down "
                "that only appears on scroll counts as hidden"
            )

        if world.call("settle", int(settings.settle_timeout_ms)) is not True:
            notes.append("animations were still running at the final check")
        result = parse_scan_result(world.call("finish"))
    finally:
        world.detach()

    return RenderedPage(
        final_url=page.url,
        html=html,
        hidden=result.hidden,
        revealed_on_scroll=result.revealed_on_scroll,
        text_elements=result.text_elements,
        scroll_steps=steps,
        reached_bottom=reached_bottom,
        notes=tuple(notes),
    )


def _close_quietly(closeable: Closeable) -> None:
    try:
        closeable.close()
    except Exception:
        # Cleanup is best effort: a tab or browser that is already closed or
        # disconnected has nothing left to release, and an error here must
        # not replace the page's own result or error.
        pass


def render_page(
    browser: Browser,
    url: str,
    settings: RenderSettings,
    redact: Callable[[str], str] = EndpointRedactor(None),
) -> RenderedPage:
    from playwright.sync_api import Error as PlaywrightError

    _require_http_url(url)
    try:
        page = browser.new_page(viewport={"width": settings.viewport_width, "height": settings.viewport_height})
    except PlaywrightError as error:
        raise RenderError(redact(f"Rendering {url} failed: couldn't open a tab: {_first_line(error)}")) from None
    try:
        return _scan_loaded_page(page, url, settings)
    except (PlaywrightError, RenderError) as error:
        raise RenderError(redact(f"Rendering {url} failed: {_first_line(error)}")) from None
    finally:
        _close_quietly(page)


class Renderer(Generic[BrowserT]):
    """Renders pages one at a time, each in a browser of its own."""

    def __init__(
        self,
        launcher: BrowserLauncher[BrowserT],
        endpoint: str | None,
        settings: RenderSettings,
        render_in: Callable[[BrowserT, str, RenderSettings, Callable[[str], str]], RenderedPage],
    ) -> None:
        self._launcher = launcher
        self._endpoint = endpoint
        self._settings = settings
        self._render_in = render_in
        self._redact = EndpointRedactor(endpoint)

    def __repr__(self) -> str:
        return f"Renderer(source={self.source.value!r})"

    @property
    def source(self) -> BrowserSource:
        return BrowserSource.LOCAL if self._endpoint is None else BrowserSource.REMOTE

    def render(self, url: str) -> RenderedPage:
        _require_http_url(url)
        # A fresh browser per page keeps one site's state from reaching the
        # next, and doesn't rely on a remote session allowing more than one
        # navigation.
        browser = acquire_browser(self._launcher, self._endpoint, self._settings)
        try:
            return self._render_in(browser, url, self._settings, self._redact)
        finally:
            _close_quietly(browser)


def _start_playwright() -> Playwright:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RenderSetupError(f"Rendered scans need Playwright. Install it with: {INSTALL_HINT}") from None
    try:
        return sync_playwright().start()
    except Exception as error:
        raise RenderSetupError(f"Playwright didn't start: {_first_line(error)}") from None


@contextmanager
def open_renderer(
    environment: Mapping[str, str], settings: RenderSettings | None = None
) -> Iterator[Renderer[Browser]]:
    endpoint = read_browser_endpoint(environment)
    playwright = _start_playwright()
    try:
        yield Renderer(playwright.chromium, endpoint, settings or RenderSettings(), render_page)
    finally:
        playwright.stop()
