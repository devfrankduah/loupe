"""Fetch a page's HTML from this machine, the way a polite crawler does.

This is the local backend's fetch, and it needs no account. Every request:

* names loupe and its repository in the User-Agent, so a site can see who
  is asking;
* gives up after 10 seconds connecting, 30 seconds without data, or about
  60 seconds in all;
* follows at most 5 redirects, checking each new address as it goes;
* stops reading at 10 MB.

robots.txt is read once per site and obeyed unless the caller turns that off
for pages the user owns. As RFC 9309 says, a robots.txt that's missing or
answers 4xx allows everything, while one that can't be read (a 5xx, a 429 or
a network error) disallows everything. A Crawl-delay is waited out between
requests to the same site, up to MAX_CRAWL_DELAY_SECONDS.

A 403 or 429, or a challenge page, raises BlockedError, so the caller can
point to the Bright Data backend.
"""

from __future__ import annotations

import codecs
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit

import requests
import urllib3
from bs4.dammit import EncodingDetector
from protego import Protego
from urllib3.response import BaseHTTPResponse

from loupe_scan._version import __version__
from loupe_scan.blocking import blocked_by_answer, blocked_by_content
from loupe_scan.errors import BlockedError, PageFetchError

REPOSITORY_URL: Final[str] = "https://github.com/devfrankduah/loupe"
USER_AGENT: Final[str] = f"loupe/{__version__} (+{REPOSITORY_URL})"
# The product token robots.txt groups are matched against.
ROBOTS_USER_AGENT: Final[str] = "loupe"
MAX_CRAWL_DELAY_SECONDS: Final[float] = 30.0
_CHUNK_BYTES: Final[int] = 65_536
_CHARSET: Final[re.Pattern[str]] = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_IGNORE_ROBOTS_HINT: Final[str] = "Use --ignore-robots only for pages you own or have permission to scan."


@dataclass(frozen=True)
class FetchLimits:
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    total_seconds: float = 60.0
    max_redirects: int = 5
    max_page_bytes: int = 10_000_000
    # RFC 9309 asks parsers to read at least 500 KiB of a robots.txt.
    max_robots_bytes: int = 500 * 1024
    max_crawl_delay_seconds: float = MAX_CRAWL_DELAY_SECONDS

    def __post_init__(self) -> None:
        for name in ("connect_timeout_seconds", "read_timeout_seconds", "total_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be more than 0")
        for name in ("max_page_bytes", "max_robots_bytes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if self.max_redirects < 0 or self.max_crawl_delay_seconds < 0:
            raise ValueError("max_redirects and max_crawl_delay_seconds must not be negative")


class FetchError(PageFetchError):
    """A local fetch failed. The message is safe to print."""


class RobotsDisallowedError(FetchError):
    """robots.txt asks loupe to leave the page alone, or couldn't be read."""


class _ReadProblem(Exception):
    """Why a body couldn't be read in full, worded to follow "failed: "."""


class GetSession(Protocol):
    """The part of requests.Session the fetcher uses, so tests can pass a fake."""

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: tuple[float, float],
        stream: bool,
        allow_redirects: bool,
    ) -> requests.Response: ...


@dataclass(frozen=True)
class _RobotsRules:
    # None allows everything: robots.txt was missing or unavailable.
    parser: Protego | None = None
    # Set when robots.txt couldn't be read, which disallows everything.
    unreadable_because: str | None = None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _require_http_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError(f"not an http(s) URL: {url!r}")


def _codec(name: str | None) -> str | None:
    if not name:
        return None
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def decode_html(body: bytes, content_type: str) -> str:
    """The page's text, using the charset from the header, a byte order mark or the page itself, else UTF-8."""
    header = _CHARSET.search(content_type)
    body_without_mark, marked = EncodingDetector.strip_byte_order_mark(body)
    encoding = (
        _codec(marked)
        or _codec(header.group(1) if header else None)
        or _codec(EncodingDetector.find_declared_encoding(body_without_mark[:4096], is_html=True))
        or "utf-8"
    )
    return body_without_mark.decode(encoding, errors="replace")


def _is_html(content_type: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return not media_type or "html" in media_type or "xml" in media_type


class LocalFetcher:
    """Fetches pages one at a time, keeping each site's robots.txt for the rest of the run."""

    def __init__(
        self,
        *,
        respect_robots: bool = True,
        limits: FetchLimits = FetchLimits(),
        session: GetSession | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_wait: Callable[[str], None] = lambda message: None,
    ) -> None:
        self._respect_robots = respect_robots
        self._limits = limits
        self._session: GetSession = session if session is not None else requests.Session()
        self._clock = clock
        self._sleep = sleep
        self._on_wait = on_wait
        self._robots: dict[str, _RobotsRules] = {}
        self._last_request: dict[str, float] = {}

    def __repr__(self) -> str:
        return f"LocalFetcher(respect_robots={self._respect_robots})"

    @property
    def _timeout(self) -> tuple[float, float]:
        return (self._limits.connect_timeout_seconds, self._limits.read_timeout_seconds)

    def admit(self, url: str) -> None:
        """Check robots.txt allows url and wait out any Crawl-delay; raise RobotsDisallowedError if it doesn't."""
        _require_http_url(url)
        if not self._respect_robots:
            return
        origin = _origin(url)
        rules = self._robots_for(origin)
        if rules.unreadable_because is not None:
            raise RobotsDisallowedError(
                f"Not fetching {url}: its robots.txt couldn't be read ({rules.unreadable_because}), and RFC 9309 "
                f"says to treat that as disallowing everything. {_IGNORE_ROBOTS_HINT}"
            )
        if rules.parser is None:
            return
        if not rules.parser.can_fetch(url, ROBOTS_USER_AGENT):
            raise RobotsDisallowedError(f"Not fetching {url}: its robots.txt asks loupe not to. {_IGNORE_ROBOTS_HINT}")
        delay = rules.parser.crawl_delay(ROBOTS_USER_AGENT) or 0.0
        last = self._last_request.get(origin)
        if delay > 0 and last is not None:
            if delay > self._limits.max_crawl_delay_seconds:
                raise RobotsDisallowedError(
                    f"Not fetching {url}: its robots.txt asks for {delay:g} s between requests, longer than loupe "
                    f"waits ({self._limits.max_crawl_delay_seconds:g} s). Scan one page from this site per run. "
                    f"{_IGNORE_ROBOTS_HINT}"
                )
            wait = last + delay - self._clock()
            if wait > 0:
                self._on_wait(f"waiting {wait:.1f} s before the next request to {urlsplit(url).netloc}, as its robots.txt asks")
                self._sleep(wait)
        self._last_request[origin] = self._clock()

    def fetch_html(self, url: str) -> str:
        """The page's HTML. Raises FetchError, or BlockedError when the site turns the request away."""
        current = url
        for _ in range(self._limits.max_redirects + 1):
            self.admit(current)
            deadline = self._clock() + self._limits.total_seconds
            try:
                response = self._get(current, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
            except requests.RequestException as error:
                raise FetchError(f"Fetching {url} failed: {self._network_problem(error)}") from None
            try:
                location = response.headers.get("location") if response.is_redirect else None
                if location is None:
                    return self._read_page(url, response, deadline)
            finally:
                response.close()
            current = urljoin(current, location)
            if urlsplit(current).scheme not in ("http", "https"):
                raise FetchError(f"Fetching {url} failed: it redirects to a non-web address")
        raise FetchError(f"Fetching {url} failed: more than {self._limits.max_redirects} redirects")

    def _get(self, url: str, accept: str) -> requests.Response:
        return self._session.get(
            url,
            headers={"user-agent": USER_AGENT, "accept": accept},
            timeout=self._timeout,
            stream=True,
            allow_redirects=False,
        )

    def _network_problem(self, error: requests.RequestException) -> str:
        if isinstance(error, requests.ConnectTimeout):
            return f"couldn't connect within {self._limits.connect_timeout_seconds:g} s"
        if isinstance(error, requests.ReadTimeout):
            return f"the site sent nothing for {self._limits.read_timeout_seconds:g} s"
        if isinstance(error, requests.exceptions.SSLError):
            return "the TLS connection failed"
        if isinstance(error, requests.ConnectionError):
            return "couldn't connect"
        return type(error).__name__

    def _read_page(self, url: str, response: requests.Response, deadline: float) -> str:
        blocked = blocked_by_answer(response.status_code, response.headers)
        if blocked is not None:
            raise BlockedError(f"Fetching {url} failed: the site turned loupe away ({blocked})")
        if response.status_code >= 300:
            raise FetchError(f"Fetching {url} failed: HTTP {response.status_code}")
        content_type = response.headers.get("content-type", "")
        if not _is_html(content_type):
            media_type = content_type.split(";", 1)[0].strip()
            raise FetchError(f"Fetching {url} failed: it's {media_type}, not an HTML page")
        try:
            body = self._read_body(response, self._limits.max_page_bytes, deadline, truncate=False)
        except _ReadProblem as problem:
            raise FetchError(f"Fetching {url} failed: {problem}") from None
        html = decode_html(body, content_type)
        challenge = blocked_by_content(html)
        if challenge is not None:
            raise BlockedError(f"Fetching {url} failed: the site turned loupe away ({challenge})")
        if not html.strip():
            raise FetchError(f"Fetching {url} failed: the page is empty")
        return html

    def _read_body(self, response: requests.Response, limit: int, deadline: float, *, truncate: bool) -> bytes:
        """Up to limit bytes of the body. Past the limit it's cut short when truncate is set, a _ReadProblem otherwise."""
        too_large = f"it's larger than {limit / 1_000_000:g} MB"
        declared = response.headers.get("content-length", "").strip()
        if not truncate and declared.isdigit() and int(declared) > limit:
            raise _ReadProblem(too_large)
        # read1 returns whatever has arrived, where iter_content would wait
        # for a whole chunk, so a page sent a few bytes at a time still
        # meets the deadline. Each read can take up to the read timeout.
        raw: BaseHTTPResponse = response.raw
        chunks: list[bytes] = []
        size = 0
        try:
            while True:
                if self._clock() > deadline:
                    raise _ReadProblem(f"it took more than {self._limits.total_seconds:g} s")
                chunk = raw.read1(_CHUNK_BYTES, decode_content=True)
                if not chunk:
                    break
                if size + len(chunk) > limit:
                    if truncate:
                        chunks.append(chunk[: limit - size])
                        break
                    raise _ReadProblem(too_large)
                size += len(chunk)
                chunks.append(chunk)
        except (urllib3.exceptions.HTTPError, OSError):
            raise _ReadProblem("the connection broke while reading it") from None
        return b"".join(chunks)

    def _robots_for(self, origin: str) -> _RobotsRules:
        rules = self._robots.get(origin)
        if rules is None:
            rules = self._load_robots(origin)
            self._robots[origin] = rules
        return rules

    def _load_robots(self, origin: str) -> _RobotsRules:
        current = f"{origin}/robots.txt"
        # RFC 9309: follow at least five redirects, then treat robots.txt as unavailable.
        for _ in range(max(self._limits.max_redirects, 5) + 1):
            deadline = self._clock() + self._limits.total_seconds
            try:
                response = self._get(current, "text/plain,*/*;q=0.5")
            except requests.RequestException as error:
                return _RobotsRules(unreadable_because=self._network_problem(error))
            try:
                status = response.status_code
                location = response.headers.get("location") if response.is_redirect else None
                if location is None:
                    if 200 <= status < 300:
                        body = self._read_body(response, self._limits.max_robots_bytes, deadline, truncate=True)
                        return _RobotsRules(parser=Protego.parse(body.decode("utf-8", errors="replace")))
                    if status == 429 or status >= 500:
                        return _RobotsRules(unreadable_because=f"HTTP {status}")
                    return _RobotsRules()
            except _ReadProblem as problem:
                return _RobotsRules(unreadable_because=str(problem))
            finally:
                response.close()
            current = urljoin(current, location)
            if urlsplit(current).scheme not in ("http", "https"):
                return _RobotsRules()
        return _RobotsRules()
