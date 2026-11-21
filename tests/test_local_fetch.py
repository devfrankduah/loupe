"""The local backend's fetch, against a real HTTP server on 127.0.0.1."""

from __future__ import annotations

import gzip
import re
import socket

import pytest
import requests

from loupe_scan.errors import BlockedError
from loupe_scan.local_fetch import (
    USER_AGENT,
    FetchError,
    FetchLimits,
    LocalFetcher,
    RobotsDisallowedError,
    decode_html,
)

from .route_site import Route, RouteSite, html_route, serve_routes

PAGE = "<!doctype html><title>Lemon cake</title><p>Bake for 45 minutes.</p>"


def _robots(text: str, status: int = 200) -> Route:
    return Route(status=status, body=text.encode("utf-8"), headers={"content-type": "text/plain"})


def _page_requests(site: RouteSite) -> list[str]:
    return [path for path in site.requested() if path != "/robots.txt"]


def test_a_page_is_fetched_with_a_user_agent_naming_loupe_and_its_repository() -> None:
    with serve_routes({"/cake": html_route(PAGE)}) as site:
        assert LocalFetcher().fetch_html(site.url("/cake")) == PAGE
    page_headers = dict(site.requests)["/cake"]
    assert page_headers["user-agent"] == USER_AGENT
    assert USER_AGENT.startswith("loupe/") and "https://github.com/devfrankduah/loupe" in USER_AGENT
    assert "text/html" in page_headers["accept"]


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        ("café".encode("latin-1"), "text/html; charset=ISO-8859-1", "café"),
        (b'<meta charset="windows-1252"><p>caf\xe9</p>', "text/html", '<meta charset="windows-1252"><p>café</p>'),
        (b"\xef\xbb\xbf<p>caf\xc3\xa9</p>", "text/html; charset=latin-1", "<p>café</p>"),
        ("<p>café</p>".encode("utf-8"), "text/html", "<p>café</p>"),
        (b"<p>caf\xff</p>", "text/html; charset=no-such-charset", "<p>caf�</p>"),
    ],
)
def test_html_is_decoded_from_its_mark_header_or_declaration(body: bytes, content_type: str, expected: str) -> None:
    assert decode_html(body, content_type) == expected


def test_a_path_robots_txt_disallows_is_never_requested() -> None:
    routes = {"/robots.txt": _robots("User-agent: *\nDisallow: /private\n"), "/private/cake": html_route(PAGE)}
    with serve_routes(routes) as site:
        with pytest.raises(RobotsDisallowedError, match="robots.txt asks loupe not to.*--ignore-robots"):
            LocalFetcher().fetch_html(site.url("/private/cake"))
    assert _page_requests(site) == []


def test_robots_txt_is_read_once_per_site_and_its_wildcards_are_obeyed() -> None:
    routes = {
        "/robots.txt": _robots("User-agent: loupe\nDisallow: /*.php$\n\nUser-agent: *\nDisallow: /\n"),
        "/cake": html_route(PAGE),
        "/pie": html_route(PAGE),
        "/index.php": html_route(PAGE),
    }
    with serve_routes(routes) as site:
        fetcher = LocalFetcher()
        fetcher.fetch_html(site.url("/cake"))
        fetcher.fetch_html(site.url("/pie"))
        with pytest.raises(RobotsDisallowedError):
            fetcher.fetch_html(site.url("/index.php"))
    assert site.requested() == ["/robots.txt", "/cake", "/pie"]


@pytest.mark.parametrize("status", [404, 401, 403, 410])
def test_a_missing_or_unavailable_robots_txt_allows_everything(status: int) -> None:
    with serve_routes({"/robots.txt": _robots("User-agent: *\nDisallow: /\n", status), "/cake": html_route(PAGE)}) as site:
        assert LocalFetcher().fetch_html(site.url("/cake")) == PAGE


@pytest.mark.parametrize("status", [429, 500, 503])
def test_a_robots_txt_that_cant_be_read_disallows_everything(status: int) -> None:
    with serve_routes({"/robots.txt": _robots("", status), "/cake": html_route(PAGE)}) as site:
        with pytest.raises(RobotsDisallowedError, match=f"couldn't be read \\(HTTP {status}\\).*RFC 9309"):
            LocalFetcher().fetch_html(site.url("/cake"))
    assert _page_requests(site) == []


def test_ignoring_robots_never_asks_for_robots_txt() -> None:
    routes = {"/robots.txt": _robots("User-agent: *\nDisallow: /\n"), "/cake": html_route(PAGE)}
    with serve_routes(routes) as site:
        assert LocalFetcher(respect_robots=False).fetch_html(site.url("/cake")) == PAGE
    assert site.requested() == ["/cake"]


def test_only_the_start_of_a_huge_robots_txt_is_read() -> None:
    rules = "User-agent: *\nDisallow: /private\n"
    routes = {"/robots.txt": _robots(rules + "#" * 4000 + "\nDisallow: /cake\n"), "/cake": html_route(PAGE)}
    with serve_routes(routes) as site:
        fetcher = LocalFetcher(limits=FetchLimits(max_robots_bytes=len(rules) + 100))
        assert fetcher.fetch_html(site.url("/cake")) == PAGE
        with pytest.raises(RobotsDisallowedError):
            fetcher.fetch_html(site.url("/private"))


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_site_that_cant_be_reached_is_an_error_either_way() -> None:
    url = f"http://127.0.0.1:{_closed_port()}/cake"
    with pytest.raises(RobotsDisallowedError, match="couldn't be read \\(couldn't connect\\)"):
        LocalFetcher().fetch_html(url)
    with pytest.raises(FetchError, match="failed: couldn't connect$"):
        LocalFetcher(respect_robots=False).fetch_html(url)


def _redirect(to: str, status: int = 302) -> Route:
    return Route(status=status, headers={"location": to})


def test_redirects_are_followed_up_to_the_limit() -> None:
    routes = {f"/hop{n}": _redirect(f"/hop{n + 1}") for n in range(5)}
    routes["/hop5"] = html_route(PAGE)
    with serve_routes(routes) as site:
        assert LocalFetcher().fetch_html(site.url("/hop0")) == PAGE
        with pytest.raises(FetchError, match="more than 4 redirects"):
            LocalFetcher(limits=FetchLimits(max_redirects=4)).fetch_html(site.url("/hop0"))


def test_each_redirect_target_is_checked_against_robots_txt() -> None:
    routes = {
        "/robots.txt": _robots("User-agent: *\nDisallow: /private\n"),
        "/cake": _redirect("/private/cake", 301),
        "/private/cake": html_route(PAGE),
    }
    with serve_routes(routes) as site:
        with pytest.raises(RobotsDisallowedError, match="/private/cake"):
            LocalFetcher().fetch_html(site.url("/cake"))
    assert _page_requests(site) == ["/cake"]


def test_a_redirect_away_from_the_web_is_refused() -> None:
    with serve_routes({"/cake": _redirect("ftp://127.0.0.1/cake")}) as site:
        with pytest.raises(FetchError, match="non-web address"):
            LocalFetcher().fetch_html(site.url("/cake"))


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        (html_route("<p>Forbidden</p>", status=403), "HTTP 403, forbidden"),
        (html_route("<p>Slow down</p>", status=429), "HTTP 429, too many requests"),
        (html_route("<p>Checking</p>", status=503, headers={"cf-mitigated": "challenge"}), "a Cloudflare challenge"),
        (html_route("<html><head><title>Just a moment...</title></head></html>"), "a Cloudflare challenge page"),
        (html_route("<title>\n  Attention Required! | Cloudflare\n</title>"), "a Cloudflare challenge page"),
    ],
)
def test_a_site_that_turns_loupe_away_is_a_block(route: Route, reason: str) -> None:
    with serve_routes({"/cake": route}) as site:
        with pytest.raises(BlockedError) as caught:
            LocalFetcher().fetch_html(site.url("/cake"))
    assert str(caught.value).endswith(f"the site turned loupe away ({reason})")


@pytest.mark.parametrize(
    "route",
    [
        html_route("<p>Server error</p>", status=500),
        html_route("<p>Gone</p>", status=404),
    ],
)
def test_other_errors_are_not_blocks(route: Route) -> None:
    with serve_routes({"/cake": route}) as site:
        with pytest.raises(FetchError) as caught:
            LocalFetcher().fetch_html(site.url("/cake"))
    assert not isinstance(caught.value, BlockedError)
    assert f"HTTP {route.status}" in str(caught.value)


def test_a_page_that_only_mentions_a_challenge_is_not_a_block() -> None:
    html = "<title>Just a moment... a short story</title><p>Attention Required! | Cloudflare is a page title.</p>"
    with serve_routes({"/story": html_route(html)}) as site:
        assert LocalFetcher().fetch_html(site.url("/story")) == html


def test_a_page_over_the_size_limit_is_refused_by_its_declared_length_or_its_body() -> None:
    big = html_route("<p>" + "x" * 5000 + "</p>")
    routes = {"/declared": big, "/undeclared": Route(body=big.body, headers=big.headers, declare_length=False)}
    with serve_routes(routes) as site:
        fetcher = LocalFetcher(limits=FetchLimits(max_page_bytes=1000))
        for path in routes:
            with pytest.raises(FetchError, match="larger than 0.001 MB"):
                fetcher.fetch_html(site.url(path))


def test_the_default_size_limit_is_ten_megabytes() -> None:
    assert FetchLimits().max_page_bytes == 10_000_000


def test_a_page_that_trickles_in_is_given_up_on() -> None:
    route = Route(body=b"<p>" + b"x" * 40 + b"</p>", drip_bytes=4, drip_seconds=0.1, declare_length=False)
    with serve_routes({"/slow": route}) as site:
        with pytest.raises(FetchError, match="took more than 0.3 s"):
            LocalFetcher(limits=FetchLimits(total_seconds=0.3)).fetch_html(site.url("/slow"))


def test_a_site_that_sends_nothing_times_out() -> None:
    with serve_routes({"/stuck": Route(body=b"<p>late</p>", delay_seconds=1.0)}) as site:
        with pytest.raises(FetchError, match="sent nothing for 0.2 s"):
            LocalFetcher(respect_robots=False, limits=FetchLimits(read_timeout_seconds=0.2)).fetch_html(
                site.url("/stuck")
            )


@pytest.mark.parametrize(
    ("route", "problem"),
    [
        (Route(body=b"%PDF-1.7", headers={"content-type": "application/pdf"}), "it's application/pdf, not an HTML page"),
        (html_route("  \n "), "the page is empty"),
    ],
)
def test_a_body_that_isnt_a_page_is_an_error(route: Route, problem: str) -> None:
    with serve_routes({"/file": route}) as site:
        with pytest.raises(FetchError, match=problem):
            LocalFetcher().fetch_html(site.url("/file"))


def test_non_web_urls_are_refused_before_any_request() -> None:
    with pytest.raises(FetchError, match="not an http"):
        LocalFetcher().fetch_html("file:///etc/passwd")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_a_crawl_delay_is_waited_out_between_requests_to_one_site() -> None:
    routes = {"/robots.txt": _robots("User-agent: *\nCrawl-delay: 2\n"), "/a": html_route(PAGE), "/b": html_route(PAGE)}
    clock = FakeClock()
    waits: list[str] = []
    with serve_routes(routes) as site:
        fetcher = LocalFetcher(clock=clock.time, sleep=clock.sleep, on_wait=waits.append)
        fetcher.fetch_html(site.url("/a"))
        clock.now += 0.5
        fetcher.fetch_html(site.url("/b"))
    assert clock.slept == [pytest.approx(1.5)]
    assert waits == ["waiting 1.5 s before the next request to " + site.base_url.removeprefix("http://") + ", as its robots.txt asks"]


def test_a_crawl_delay_longer_than_loupe_waits_stops_the_second_page() -> None:
    routes = {"/robots.txt": _robots("User-agent: *\nCrawl-delay: 3600\n"), "/a": html_route(PAGE), "/b": html_route(PAGE)}
    clock = FakeClock()
    with serve_routes(routes) as site:
        fetcher = LocalFetcher(clock=clock.time, sleep=clock.sleep)
        fetcher.fetch_html(site.url("/a"))
        with pytest.raises(RobotsDisallowedError, match="3600 s between requests, longer than loupe waits \\(30 s\\)"):
            fetcher.fetch_html(site.url("/b"))
    assert clock.slept == []
    assert _page_requests(site) == ["/a"]


def test_a_network_error_message_is_short_and_names_the_page() -> None:
    class BrokenSession:
        def get(self, url: str, **_: object) -> requests.Response:
            raise requests.exceptions.SSLError("certificate verify failed: very long detail")

    with pytest.raises(FetchError) as caught:
        LocalFetcher(respect_robots=False, session=BrokenSession()).fetch_html("https://a.test/cake")
    assert str(caught.value) == "Fetching https://a.test/cake failed: the TLS connection failed"


def test_a_compressed_page_is_decompressed_and_its_size_limit_counts_what_it_expands_to() -> None:
    expanded = "<p>" + "x" * 5000 + "</p>"
    headers = {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"}
    route = Route(body=gzip.compress(expanded.encode("utf-8")), headers=headers)
    with serve_routes({"/cake": route}) as site:
        assert LocalFetcher().fetch_html(site.url("/cake")) == expanded
        assert len(route.body) < 1000
        with pytest.raises(FetchError, match="larger than 0.001 MB"):
            LocalFetcher(limits=FetchLimits(max_page_bytes=1000)).fetch_html(site.url("/cake"))


def test_a_declared_length_over_the_limit_is_refused_before_reading_any_of_it() -> None:
    # Read at this pace the body would hit the time limit long before the size limit.
    route = Route(body=b"x" * 5000, headers={"content-type": "text/html"}, drip_bytes=100, drip_seconds=0.2)
    with serve_routes({"/big": route}) as site:
        with pytest.raises(FetchError, match="larger than 0.001 MB"):
            LocalFetcher(limits=FetchLimits(max_page_bytes=1000, total_seconds=0.5)).fetch_html(site.url("/big"))


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "192.168.0.10", "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1", "fe80::1%en0", "::ffff:127.0.0.1", "fd00::1"],
)
def test_a_public_only_fetcher_refuses_a_host_on_a_non_public_address(address: str) -> None:
    from loupe_scan.local_fetch import NonPublicAddressError, require_public_address

    with pytest.raises(NonPublicAddressError, match=f"is at {re.escape(address)}, a private, loopback"):
        require_public_address("https://shop.example/", lambda host, port: ["93.184.215.14", address])


def test_a_public_only_fetcher_lets_a_public_address_through_and_says_when_a_name_doesnt_resolve() -> None:
    from loupe_scan.local_fetch import require_public_address

    looked_up: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> list[str]:
        looked_up.append((host, port))
        return ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]

    require_public_address("https://shop.example/cake", resolve)
    require_public_address("http://shop.example:8080/cake", resolve)
    assert looked_up == [("shop.example", 443), ("shop.example", 8080)]

    def fail(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such host")

    with pytest.raises(FetchError, match="couldn't find nowhere.example"):
        require_public_address("https://nowhere.example/", fail)


def test_a_public_only_fetcher_checks_every_redirect_before_following_it() -> None:
    from loupe_scan.local_fetch import NonPublicAddressError

    routes = {"/robots.txt": _robots(""), "/away": Route(status=302, headers={"location": "http://router.internal/admin"})}
    with serve_routes(routes) as site:
        # The test server is public as far as this resolver knows; the redirect's target isn't. A plain
        # session connects wherever the name resolves, so the server on 127.0.0.1 can stand in for a public site.
        addresses = {"127.0.0.1": ["93.184.215.14"], "router.internal": ["192.168.1.1"]}
        fetcher = LocalFetcher(public_only=True, resolve=lambda host, port: addresses[host], session=requests.Session())
        with pytest.raises(NonPublicAddressError, match="router.internal is at 192.168.1.1"):
            fetcher.fetch_html(site.url("/away"))
    assert site.requested() == ["/robots.txt", "/away"]


def test_robots_txt_is_read_again_once_it_is_a_day_old() -> None:
    now = [1000.0]
    with serve_routes({"/robots.txt": _robots(""), "/cake": html_route(PAGE)}) as site:
        fetcher = LocalFetcher(clock=lambda: now[0])
        fetcher.fetch_html(site.url("/cake"))
        now[0] += 86_400
        fetcher.fetch_html(site.url("/cake"))
        now[0] += 1
        fetcher.fetch_html(site.url("/cake"))
    assert site.requested() == ["/robots.txt", "/cake", "/cake", "/robots.txt", "/cake"]
