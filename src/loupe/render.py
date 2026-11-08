"""Scan a page as a browser draws it, so stylesheet and script hiding is caught.

The static scan reads markup only. It misses text hidden by a CSS class, and
it flags text that starts invisible and fades in as the reader scrolls. Here
a real Chromium loads the page instead:

1. Load it, wait for the network to go quiet and for opening animations to
   finish, then record whether each element's own text is visible (S0).
2. Scroll from the top to the bottom one viewport at a time, pausing at each
   step and checking again whatever hasn't been seen yet.
3. Scroll each element that is still hidden only in ways a scroll animation
   uses (opacity, position, clipping) into the middle of the viewport, pause
   and check again, for reveals tied to one position.
4. Wait for transitions to settle and check one last time (S1).

Text never seen visible is a hidden finding. Text hidden at S0 but seen at a
later step was revealed on scroll and is reported separately. Checking at
every step, rather than only at S0 and S1, matters for content that fades
out again once scrolled past and for content-visibility:auto sections, which
a browser only draws near the viewport; both are invisible at S1.

Each element the static scan flagged for its inline style is also traced:
right after S0, and again at the end, it is paired with every live element of
the same tag and text, and reported as visible at load, revealed on scroll,
still hidden, or not matched, taking the most visible copy. Matching twice
covers both a node a framework replaces with a hidden one and a hidden node
it replaces with a visible one.

The checks run in visibility.js, inside an isolated JavaScript world, so the
page's own scripts can't tamper with them.

The browser is local Chromium unless BRIGHTDATA_BROWSER_WSS holds a CDP
websocket endpoint. That value carries credentials: it is never printed,
logged or saved, and errors from connecting to it say only that it failed.
A remote browser gets two-minute timeouts, skips images, media and fonts
(never stylesheets, which decide what shows), and has its transfer counted;
a page whose count is zero, or whose images weren't blocked, fails.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final, Generic, Literal, Protocol, TypeVar
from urllib.parse import unquote, urlsplit

from loupe.scan import Finding, Kind, ScanMode, make_finding, normalize_whitespace, scan_html

if TYPE_CHECKING:
    from playwright.sync_api import Browser, CDPSession, Page, Playwright, Request, Route

BROWSER_ENDPOINT_VARIABLE: Final[str] = "BRIGHTDATA_BROWSER_WSS"

# A browser decides directly whether these show, whatever hid them, so a
# rendered scan drops the static guesses about them and keeps its own result.
SUPERSEDED_BY_RENDERING: Final[frozenset[Kind]] = frozenset({Kind.HIDDEN_ATTRIBUTE, Kind.INLINE_STYLE})

INSTALL_HINT: Final[str] = "pip install 'loupe[render]' && playwright install chromium"
ENDPOINT_PLACEHOLDER: Final[str] = "[endpoint]"
# Bright Data's browser usernames read brd-customer-<id>-zone-<zone>, and the
# customer id or zone name can turn up on its own in an error.
_LABELLED_USERNAME_PART: Final[re.Pattern[str]] = re.compile(r"(?:^|-)(?:customer|zone)-([^-]+)")
# Pieces shorter than this are replaced only as whole tokens, so that a short
# zone name doesn't eat parts of ordinary words.
_WHOLE_TOKEN_BELOW: Final[int] = 8
_ERROR_MESSAGE_LIMIT: Final[int] = 300
_SCANNER: Final[str] = "loupeScanner"

# Playwright resource types a remote run may skip to save bandwidth. They never
# change whether text shows. Stylesheets do, so they can never be blocked.
BLOCKABLE_RESOURCE_TYPES: Final[frozenset[str]] = frozenset({"image", "media", "font"})


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
    # How many still-hidden elements to centre in the viewport after the
    # scroll pass, and how long it may take in all. 0 steps turns it off.
    max_centre_steps: int = 200
    centre_budget_ms: float = 60_000
    # Playwright resource types to refuse, from BLOCKABLE_RESOURCE_TYPES.
    blocked_resource_types: frozenset[str] = frozenset()
    # Add up the bytes received over the network for each page.
    measure_transfer: bool = False

    def __post_init__(self) -> None:
        unknown = self.blocked_resource_types - BLOCKABLE_RESOURCE_TYPES
        if unknown:
            raise ValueError(f"can't block {sorted(unknown)}; only {sorted(BLOCKABLE_RESOURCE_TYPES)} may be blocked")
        for name in ("viewport_width", "viewport_height", "max_scroll_steps"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.max_centre_steps < 0:
            raise ValueError("max_centre_steps must not be negative")
        for name in (
            "connect_timeout_ms",
            "navigation_timeout_ms",
            "load_timeout_ms",
            "network_idle_timeout_ms",
            "growth_timeout_ms",
            "scroll_pause_ms",
            "settle_timeout_ms",
            "centre_budget_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")


LOCAL_SETTINGS: Final[RenderSettings] = RenderSettings()

# A remote browser is billed by the byte and sits further away: Bright Data
# allows up to two minutes for a navigation, images, media and fonts are
# skipped, and every page's transfer is counted.
REMOTE_SETTINGS: Final[RenderSettings] = RenderSettings(
    connect_timeout_ms=120_000,
    navigation_timeout_ms=120_000,
    blocked_resource_types=BLOCKABLE_RESOURCE_TYPES,
    measure_transfer=True,
)


def settings_for(endpoint: str | None) -> RenderSettings:
    return LOCAL_SETTINGS if endpoint is None else REMOTE_SETTINGS


@dataclass(frozen=True)
class RenderedText:
    tag: str
    text: str
    # Why it couldn't be seen, such as "opacity:0" or "clipped". Empty for revealed text.
    reasons: tuple[str, ...] = ()


class Landing(str, Enum):
    """Where an element the static scan flagged for its inline style ended up once rendered."""

    VISIBLE_AT_LOAD = "visible at load"
    REVEALED_ON_SCROLL = "revealed on scroll"
    STILL_HIDDEN = "still hidden"
    NOT_MATCHED = "not matched"


# Most visible first. A page can carry several copies of one text, such as an
# invisible duplicate kept for layout, and a reader sees the text if any copy shows.
_LANDING_ORDER: Final[tuple[Landing, ...]] = (
    Landing.VISIBLE_AT_LOAD,
    Landing.REVEALED_ON_SCROLL,
    Landing.STILL_HIDDEN,
)


def most_visible(states: Iterable[Landing]) -> Landing:
    """The most visible of states, or NOT_MATCHED when there are none."""
    present = set(states)
    for landing in _LANDING_ORDER:
        if landing in present:
            return landing
    return Landing.NOT_MATCHED


@dataclass(frozen=True)
class StaticComparison:
    tag: str
    text: str
    # The static scan's reasons, such as "opacity:0".
    detail: str
    landed: Landing


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
    browser_version: str | None = None
    # Bytes sent and received over the network, or None when transfer wasn't measured.
    transfer_bytes: int | None = None
    # Requests refused because of their resource type, or None when nothing was blocked by type.
    blocked_requests: int | None = None
    # Still-hidden elements scrolled to the middle of the viewport after the scroll pass.
    centre_checks: int = 0
    # Where each of the static scan's inline-style candidates landed, in document order.
    static_comparison: tuple[StaticComparison, ...] = ()
    # The viewport the page reported (innerWidth, innerHeight), to confirm the requested size took effect.
    viewport: tuple[int, int] | None = None


class PageRenderer(Protocol):
    @property
    def source(self) -> BrowserSource: ...

    def render(self, url: str) -> RenderedPage: ...

    def redact(self, text: str) -> str:
        """text with any part of the browser endpoint replaced by ENDPOINT_PLACEHOLDER."""
        ...


class Closeable(Protocol):
    def close(self) -> None: ...


BrowserT_co = TypeVar("BrowserT_co", covariant=True)
BrowserT = TypeVar("BrowserT", bound=Closeable)


class BrowserLauncher(Protocol[BrowserT_co]):
    """The part of Playwright's BrowserType used here, so tests can pass a fake."""

    def launch(self, *, headless: bool | None = None) -> BrowserT_co: ...

    def connect_over_cdp(self, endpoint_url: str, *, timeout: float | None = None) -> BrowserT_co: ...


class CdpSender(Protocol):
    """The part of Playwright's CDPSession the isolated world uses, so tests can pass a fake."""

    def send(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]: ...


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


def _endpoint_pieces(endpoint: str) -> set[str]:
    parts = urlsplit(endpoint)
    host_and_port = parts.netloc.rpartition("@")[2]
    candidates = [endpoint, parts.netloc, host_and_port, parts.hostname, parts.username, parts.password, parts.query]
    if parts.username:
        candidates.extend(_LABELLED_USERNAME_PART.findall(unquote(parts.username)))
    pieces: set[str] = set()
    for candidate in candidates:
        if candidate:
            pieces.add(candidate)
            pieces.add(unquote(candidate))
    return pieces


def _piece_pattern(piece: str) -> str:
    escaped = re.escape(piece)
    if len(piece) >= _WHOLE_TOKEN_BELOW:
        return escaped
    return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"


class EndpointRedactor:
    """Replaces an endpoint, or any part of it, with ENDPOINT_PLACEHOLDER.

    The parts are the whole URL, the host, the username with the customer id
    and zone inside it, the password and the query, each as written and
    percent-decoded.
    """

    def __init__(self, endpoint: str | None) -> None:
        pieces = sorted(_endpoint_pieces(endpoint), key=len, reverse=True) if endpoint else []
        self._pattern = re.compile("|".join(_piece_pattern(piece) for piece in pieces)) if pieces else None

    def __repr__(self) -> str:
        return "EndpointRedactor()"

    def __call__(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(ENDPOINT_PLACEHOLDER, text)


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

    def __init__(self, session: CdpSender) -> None:
        self._session = session
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

    def alive(self) -> bool:
        """Whether the world still exists: a page that replaces its document takes its worlds with it."""
        from playwright.sync_api import Error as PlaywrightError

        try:
            return self.evaluate(f"typeof globalThis.{_SCANNER} === 'object'") is True
        except PlaywrightError:
            return False

    def call(self, method: str, *arguments: int) -> object:
        return self.evaluate(f"{_SCANNER}.{method}({', '.join(str(argument) for argument in arguments)})")

    def call_with_data(self, method: str, data: object) -> object:
        # JSON is a valid JavaScript literal, so page text passes through as data, never as code.
        return self.evaluate(f"{_SCANNER}.{method}({json.dumps(data)})")



class _PageTraffic:
    """Blocks resource types and measures transfer through Playwright's own request events.

    A remote browser may not pass Network or Fetch events to a second CDP
    session, but Playwright's own session demonstrably gets them, so both the
    byte count and the blocking hang off the page itself.
    """

    def __init__(self, page: Page, settings: RenderSettings) -> None:
        self._blocked_types = settings.blocked_resource_types
        self.blocked = 0
        self._finished: list[Request] = []
        if settings.measure_transfer:
            page.on("requestfinished", self._record)
        if self._blocked_types:
            page.route("**/*", self._route)

    def _record(self, request: Request) -> None:
        self._finished.append(request)

    def _route(self, route: Route) -> None:
        if route.request.resource_type in self._blocked_types:
            self.blocked += 1
            route.abort("blockedbyclient")
        else:
            route.fallback()

    def transfer_bytes(self) -> int:
        """Request and response headers and bodies, as sent and received, for every finished request."""
        total = 0
        for request in self._finished:
            sizes = request.sizes()
            total += sum(
                max(0, sizes[key])
                for key in ("requestHeadersSize", "requestBodySize", "responseHeadersSize", "responseBodySize")
            )
        return total


def measurement_problem(transfer_bytes: int | None, fetched_images: int, blocked: int, blocking: bool) -> str | None:
    """Why a remote page's measurements can't be trusted, or None when they look sound.

    The byte limit depends on the count, and the bandwidth on the blocking, so
    either failing silently would make a run unsafe to continue.
    """
    if transfer_bytes is not None and transfer_bytes <= 0:
        return "no bytes were measured, so the transfer limit can't be enforced"
    if blocking and fetched_images > 0 and blocked == 0:
        return f"the page has {fetched_images} images fetched over http(s) but none were blocked"
    return None


def _detach_quietly(session: CDPSession) -> None:
    try:
        session.detach()
    except Exception:
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
    viewport: tuple[int, int] | None = None
    # <img> elements whose source is an http(s) URL, so loading them is a request that blocking should refuse.
    fetched_images: int = 0


def _viewport(record: Mapping[str, object]) -> tuple[int, int] | None:
    value = record.get("viewport")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(side, int) and not isinstance(side, bool) and side >= 0 for side in value)
    ):
        raise RenderError("the scan result's viewport was not a width and height")
    return int(value[0]), int(value[1])


def parse_scan_result(value: object) -> ScanResult:
    record = _mapping(value, "the scan result")
    return ScanResult(
        text_elements=_count(record, "text_elements"),
        hidden=_texts(record, "hidden_units", with_reasons=True),
        revealed_on_scroll=_texts(record, "revealed_units", with_reasons=False),
        viewport=_viewport(record),
        fetched_images=_count(record, "fetched_images") if "fetched_images" in record else 0,
    )


def static_candidates(html: str) -> list[Finding]:
    """The static scan's inline-style findings: what it guessed was hidden by a style attribute."""
    return [finding for finding in scan_html(html) if finding.kind is Kind.INLINE_STYLE]


def parse_comparison(value: object, candidates: Sequence[Finding]) -> tuple[StaticComparison, ...]:
    """Turn compareStatic()'s states into one landing per candidate, taking the most visible copy."""
    if not isinstance(value, list) or len(value) != len(candidates):
        raise RenderError("the static comparison didn't cover every candidate")
    comparisons: list[StaticComparison] = []
    for copies, candidate in zip(value, candidates):
        if not isinstance(copies, list):
            raise RenderError("the static comparison held something other than copies")
        states: list[Landing] = []
        for members in copies:
            if not isinstance(members, list):
                raise RenderError("a copy in the static comparison wasn't a list")
            for member in members:
                try:
                    states.append(Landing(member))
                except ValueError:
                    raise RenderError(f"the static comparison had an unknown state {member!r}") from None
        landed = most_visible(states)
        comparisons.append(StaticComparison(candidate.tag, candidate.text, candidate.detail, landed))
    return tuple(comparisons)


@dataclass(frozen=True)
class WatchStatus:
    """How the watch over the static candidates went between watchStatic() and finish()."""

    samples: int
    # The scanner's own error message if the watch broke off, else None.
    error: str | None


def parse_watch_status(value: object) -> WatchStatus:
    record = _mapping(value, "the watch status")
    error = record.get("error")
    if error is not None and not isinstance(error, str):
        raise RenderError("the watch status's error was not text")
    return WatchStatus(samples=_count(record, "samples"), error=error)


def watch_notes(status: WatchStatus, candidates: int) -> list[str]:
    """Notes for a watch that couldn't do its job, so a two-moment trace isn't taken for more."""
    if candidates == 0:
        return []
    if status.error is not None:
        return [
            f"watching the inline-style candidates broke off ({status.error}), so the trace may have "
            "missed text shown only between its checks"
        ]
    if status.samples == 0:
        return [
            "the page never reported a first paint, so the trace saw the inline-style candidates "
            "only at its first and final checks"
        ]
    return []


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


def _watching_world(session: CdpSender, candidates: Sequence[Finding]) -> _IsolatedWorld:
    world = _IsolatedWorld(session)
    world.evaluate(_scanner_source())
    world.call_with_data("watchStatic", [{"tag": finding.tag, "text": finding.text} for finding in candidates])
    return world


def _early_watching_world(session: CdpSender, candidates: Sequence[Finding]) -> _IsolatedWorld | None:
    """The watching world, or None when the page replaced its document while it was being set up."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        return _watching_world(session, candidates)
    except PlaywrightError:
        # A page that replaces its document as it loads takes a half-built
        # world with it. The caller builds one again once the page has
        # loaded, and notes that the watch started late.
        return None


def _scan_loaded_page(page: Page, url: str, settings: RenderSettings, browser_version: str) -> RenderedPage:
    # One CDP session, opened before navigation so that blocking and the
    # transfer count cover every request, also hosts the isolated world.
    session = page.context.new_cdp_session(page)
    try:
        return _scan_with_session(page, session, url, settings, browser_version)
    finally:
        _detach_quietly(session)


def _scan_with_session(
    page: Page, session: CDPSession, url: str, settings: RenderSettings, browser_version: str
) -> RenderedPage:
    traffic = _PageTraffic(page, settings)

    notes: list[str] = []
    response = page.goto(url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
    if response is None:
        raise RenderError("the browser got no response")
    if response.status >= 400:
        raise RenderError(f"HTTP {response.status}")
    html = response.text()
    candidates = static_candidates(html)
    # Watching starts before the page has finished loading, because text a
    # carousel shows and takes away again may be gone by the first check.
    early_world = _early_watching_world(session, candidates)
    if not _wait_for(page, "load", settings.load_timeout_ms):
        notes.append(f"the load event hadn't fired after {_seconds(settings.load_timeout_ms)}")
    if not _wait_for(page, "networkidle", settings.network_idle_timeout_ms):
        notes.append(
            f"the network was still busy after {_seconds(settings.network_idle_timeout_ms)}, "
            "so the first check ran with requests open"
        )
    if early_world is not None and early_world.alive():
        world = early_world
    else:
        notes.append(
            "the page replaced its document while it loaded, so the inline-style candidates were "
            "watched only from then on"
        )
        world = _watching_world(session, candidates)

    if world.call("settle", int(settings.settle_timeout_ms)) is not True:
        notes.append("animations were still running when the page was first checked")
    world.call("recordLoad")
    world.call("matchStatic")
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
            "was checked only by centring it"
        )

    centre_checks = 0
    centre_started = time.monotonic()
    out_of_time = False
    while centre_checks < settings.max_centre_steps:
        if (time.monotonic() - centre_started) * 1000 >= settings.centre_budget_ms:
            out_of_time = True
            break
        if world.call("centreNext") is not True:
            break
        centre_checks += 1
        page.wait_for_timeout(settings.scroll_pause_ms)
        world.call("recheck")
    uncentred = world.call("uncentredCount")
    if isinstance(uncentred, int) and uncentred > 0:
        if out_of_time:
            limit = f"the {_seconds(settings.centre_budget_ms)} budget"
        else:
            limit = f"the limit of {settings.max_centre_steps}"
        notes.append(
            f"centred {centre_checks} still-hidden elements and stopped at {limit} with {uncentred} left, "
            "so any of those revealed only at one scroll position counts as hidden"
        )

    if world.call("settle", int(settings.settle_timeout_ms)) is not True:
        notes.append("animations were still running at the final check")
    result = parse_scan_result(world.call("finish"))
    comparison = parse_comparison(world.call("compareStatic"), candidates)
    notes.extend(watch_notes(parse_watch_status(world.call("watchStatus")), len(candidates)))

    transfer = traffic.transfer_bytes() if settings.measure_transfer else None
    problem = measurement_problem(
        transfer, result.fetched_images, traffic.blocked, blocking=bool(settings.blocked_resource_types)
    )
    if problem is not None:
        raise RenderError(problem)

    return RenderedPage(
        final_url=page.url,
        html=html,
        hidden=result.hidden,
        revealed_on_scroll=result.revealed_on_scroll,
        text_elements=result.text_elements,
        scroll_steps=steps,
        reached_bottom=reached_bottom,
        notes=tuple(notes),
        browser_version=browser_version,
        transfer_bytes=transfer,
        blocked_requests=traffic.blocked if settings.blocked_resource_types else None,
        centre_checks=centre_checks,
        static_comparison=comparison,
        viewport=result.viewport,
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
        return _scan_loaded_page(page, url, settings, browser.version)
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

    def redact(self, text: str) -> str:
        return self._redact(text)

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
        yield Renderer(playwright.chromium, endpoint, settings or settings_for(endpoint), render_page)
    finally:
        playwright.stop()
