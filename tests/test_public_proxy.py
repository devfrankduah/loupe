"""The proxy that keeps a guarded rendered scan's browser to public addresses.

Every server here is on 127.0.0.1, which the proxy refuses. A made-up name,
allowed.test, stands in for a public site: the tests' admit function lets it
through to 127.0.0.1, so a request that reaches the server shows what was
let through, and one that doesn't shows what was refused.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import requests

from loupe_scan.local_fetch import FetchError
from loupe_scan.public_proxy import PublicOnlyProxy, public_addresses

from .route_site import Route, RouteSite, html_route, serve_routes

ALLOWED = "allowed.test"


def _admit(host: str, port: int) -> Sequence[str]:
    if host == ALLOWED:
        return ["127.0.0.1"]
    return public_addresses()(host, port)


@pytest.fixture
def site() -> Iterator[RouteSite]:
    routes = {
        "/ok": html_route("<p>ok</p>"),
        "/ok?x=1": html_route("<p>ok</p>"),
        "/ok.png": Route(body=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"}),
        "/hop": Route(status=302, headers={"location": "/placeholder"}),
    }
    with serve_routes(routes) as served:
        served.routes["/hop"] = Route(status=302, headers={"location": served.url("/after-hop")})
        yield served


def _port(site: RouteSite) -> int:
    return int(site.base_url.rsplit(":", 1)[1])


def _through(proxy: PublicOnlyProxy, url: str) -> requests.Response:
    session = requests.Session()
    session.trust_env = False
    return session.get(url, proxies={"http": proxy.url}, timeout=10, allow_redirects=False)


def test_a_public_host_is_reached_at_its_checked_address_with_its_name_kept(site: RouteSite) -> None:
    with PublicOnlyProxy(_admit) as proxy:
        response = _through(proxy, f"http://{ALLOWED}:{_port(site)}/ok?x=1")
        assert (response.status_code, response.text, proxy.refused) == (200, "<p>ok</p>", 0)
    path, headers = site.requests[-1]
    assert path == "/ok?x=1"
    assert headers["host"] == f"{ALLOWED}:{_port(site)}"
    assert headers["connection"] == "close" and "proxy-connection" not in headers


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_a_request_to_this_machine_is_refused_and_never_sent(site: RouteSite, host: str) -> None:
    with PublicOnlyProxy(_admit) as proxy:
        response = _through(proxy, f"http://{host}:{_port(site)}/ok")
        assert response.status_code == 403 and "non-public address" in response.text
        assert proxy.refused == 1
    assert site.requests == []


def test_proxy_and_connection_headers_stop_at_the_proxy(site: RouteSite) -> None:
    port = _port(site)
    head = (
        f"GET http://{ALLOWED}:{port}/ok HTTP/1.1\r\nhost: {ALLOWED}:{port}\r\nproxy-connection: keep-alive\r\n"
        "connection: keep-alive\r\nproxy-authorization: Basic c2VjcmV0\r\nkeep-alive: 30\r\n\r\n"
    )
    with PublicOnlyProxy(_admit) as proxy:
        connection, _ = _raw(proxy, head.encode())
        connection.close()
    _, headers = site.requests[-1]
    assert "proxy-authorization" not in headers and "proxy-connection" not in headers and "keep-alive" not in headers
    assert headers["connection"] == "close"


def test_the_default_check_refuses_this_machine_and_a_name_that_doesnt_resolve() -> None:
    admit = public_addresses(resolve=lambda host, port: [])
    with pytest.raises(FetchError):
        admit("nowhere.test", 80)
    with pytest.raises(FetchError):
        public_addresses()("127.0.0.1", 80)
    assert public_addresses(resolve=lambda host, port: ["93.184.216.34"])("example.test", 443) == ["93.184.216.34"]


def _raw(proxy: PublicOnlyProxy, data: bytes) -> tuple[socket.socket, bytes]:
    connection = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    connection.sendall(data)
    return connection, connection.recv(65_536)


def test_a_tunnel_opens_only_to_a_public_host(site: RouteSite) -> None:
    port = _port(site)
    with PublicOnlyProxy(_admit) as proxy:
        tunnel, reply = _raw(proxy, f"CONNECT {ALLOWED}:{port} HTTP/1.1\r\nhost: {ALLOWED}:{port}\r\n\r\n".encode())
        with tunnel:
            assert reply == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            tunnel.sendall(f"GET /ok HTTP/1.1\r\nhost: {ALLOWED}\r\nconnection: close\r\n\r\n".encode())
            answer = b""
            while chunk := tunnel.recv(65_536):
                answer += chunk
        assert answer.startswith(b"HTTP/1.0 200") or answer.startswith(b"HTTP/1.1 200")
        assert answer.endswith(b"<p>ok</p>")
        refused, reply = _raw(proxy, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        refused.close()
        assert reply.startswith(b"HTTP/1.1 403 Forbidden")
        assert proxy.refused == 1
    assert site.requested() == ["/ok"]


@pytest.mark.parametrize(
    "request_head",
    [b"NONSENSE\r\n\r\n", b"GET ftp://allowed.test/x HTTP/1.1\r\n\r\n", b"CONNECT user@allowed.test:443 HTTP/1.1\r\n\r\n", b"GET /relative HTTP/1.1\r\n\r\n"],
)
def test_a_request_it_cant_read_is_refused_as_bad(request_head: bytes) -> None:
    with PublicOnlyProxy(_admit) as proxy:
        connection, reply = _raw(proxy, request_head)
        connection.close()
    assert reply.startswith(b"HTTP/1.1 400 Bad Request")


def test_an_oversized_head_gets_no_answer() -> None:
    with PublicOnlyProxy(_admit) as proxy:
        connection, reply = _raw(proxy, b"GET http://allowed.test/ HTTP/1.1\r\nx: " + b"a" * 70_000)
        connection.close()
    assert reply == b""


# In Chromium


@pytest.fixture(scope="module")
def chromium() -> None:
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")


def test_a_guarded_render_lets_the_browser_reach_public_addresses_only(
    chromium: None, site: RouteSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loupe_scan.render import open_renderer

    # Playwright sends loopback through a proxy by default unless this is set; loupe mustn't depend on that.
    monkeypatch.setenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1")

    port = _port(site)
    local = f"127.0.0.1:{port}"
    html = f"""<!doctype html><html><head><title>Guarded</title></head><body>
<p>Visible text for the scan to read.</p>
<img src="http://{ALLOWED}:{port}/ok.png" alt="">
<img src="http://{local}/private.png" alt="">
<img src="http://{ALLOWED}:{port}/hop" alt="">
<iframe src="http://localhost:{port}/frame" title="frame"></iframe>
<script>
fetch("http://{local}/api").catch(function () {{}});
try {{ new WebSocket("ws://{local}/socket"); }} catch (error) {{}}
</script>
</body></html>"""
    with open_renderer(None, public_only=True, admit=_admit) as renderer:
        rendered = renderer.render_html(f"http://{ALLOWED}:{port}/page", html)
    requested = set(site.requested())
    # The public image and the public hop were fetched; the hop's redirect to this machine wasn't followed.
    assert {"/ok.png", "/hop"} <= requested
    assert requested.isdisjoint({"/private.png", "/after-hop", "/frame", "/api", "/socket"}), requested
    notes = [note for note in rendered.notes if note.startswith("refused ")]
    assert len(notes) == 1 and "non-public address" in notes[0]


def _stun_packets(public_only: bool) -> int:
    """How many UDP packets a page's WebRTC connection sends to a STUN listener on this machine."""
    from loupe_scan.render import open_renderer

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        html = f"""<p>Visible text for the scan to read.</p><script>
const connection = new RTCPeerConnection({{iceServers: [{{urls: "stun:127.0.0.1:{port}"}}]}});
connection.createDataChannel("probe");
connection.createOffer().then(function (offer) {{ return connection.setLocalDescription(offer); }});
</script>"""
        renderer = open_renderer(None, public_only=True, admit=_admit) if public_only else open_renderer(None)
        with renderer as opened:
            opened.render_html(f"http://{ALLOWED}/page", html)
        packets = 0
        for _ in range(10):
            try:
                listener.recvfrom(2048)
                packets += 1
            except socket.timeout:
                pass
        return packets


def test_a_guarded_page_cant_send_webrtc_packets_around_the_proxy(chromium: None) -> None:
    # WebRTC sends UDP itself, which an HTTP proxy never sees, so a page could probe this machine's network with STUN.
    assert _stun_packets(public_only=False) > 0
    assert _stun_packets(public_only=True) == 0


def test_an_unguarded_render_is_unchanged(chromium: None, site: RouteSite) -> None:
    from loupe_scan.render import open_renderer

    html = f'<p>Visible text for the scan to read.</p><img src="{site.url("/ok.png")}" alt="">'
    with open_renderer(None) as renderer:
        rendered = renderer.render_html(site.url("/page"), html)
    assert "/ok.png" in site.requested()
    assert not any(note.startswith("refused ") for note in rendered.notes)


def test_a_remote_browser_cant_be_guarded() -> None:
    from loupe_scan.render import RenderSetupError, open_renderer

    with pytest.raises(RenderSetupError, match="only local Chromium"):
        with open_renderer("wss://browser.example/", public_only=True):
            pass


def test_loupe_mcp_guards_its_rendered_scans_unless_private_addresses_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mcp")
    from contextlib import contextmanager

    from mcp.server.mcpserver.exceptions import ToolError

    from loupe_scan import mcp_server
    from loupe_scan.errors import SetupError
    from loupe_scan.mcp_server import PageScanner, ServerSettings

    opened: list[dict[str, Any]] = []

    @contextmanager
    def fake_open_renderer(endpoint: str | None, **options: Any) -> Iterator[None]:
        opened.append({"endpoint": endpoint, **options})
        raise SetupError("stop here")
        yield None

    monkeypatch.setattr(mcp_server, "open_renderer", fake_open_renderer)
    monkeypatch.setattr(mcp_server, "fetch_with_address", lambda source, url: ("<p>x</p>", url))
    for allow in (False, True):
        scanner = PageScanner(ServerSettings(allow_private_addresses=allow), environment={}, on_note=lambda message: None)
        with pytest.raises(ToolError):
            scanner.scan("https://example.test/", render=True)
    assert opened == [{"endpoint": None, "public_only": True}, {"endpoint": None, "public_only": False}]
