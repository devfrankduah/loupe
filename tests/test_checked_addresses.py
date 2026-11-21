"""A public-only fetcher connects to the address that passed its check, and nowhere else.

A name can answer the check with a public address and the connection a
moment later with a private one (DNS rebinding). These tests stand a local
server behind made-up names to show the connection goes where the check
said, with the name kept in the Host header, in SNI and in the certificate
check.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import io

import pytest
import requests

from loupe_scan.local_fetch import CheckedAddresses, FetchError, LocalFetcher, checked_session

from .conftest import NetworkAccessError
from .fakes import make_response
from .route_site import Route, html_route, serve_routes

# A public address, which the tests' network guard refuses to connect to.
PUBLIC = "93.184.216.34"


def test_a_request_connects_to_the_checked_address_and_keeps_the_sites_name_in_the_host_header() -> None:
    with serve_routes({"/page": html_route("<p>ok</p>")}) as site:
        port = int(site.base_url.rsplit(":", 1)[1])
        checked = CheckedAddresses()
        checked.remember("rebind.test", port, ["127.0.0.1"])
        response = checked_session(checked).get(f"http://rebind.test:{port}/page", timeout=5)
        assert response.status_code == 200
        assert site.requests[-1][1]["host"] == f"rebind.test:{port}"


def test_a_host_that_wasnt_checked_is_never_connected_to() -> None:
    with serve_routes({"/page": html_route("<p>ok</p>")}) as site:
        with pytest.raises(requests.ConnectionError, match="wasn't checked"):
            checked_session(CheckedAddresses()).get(site.url("/page"), timeout=5)
        assert site.requests == []


def test_each_checked_address_is_tried_in_turn() -> None:
    with serve_routes({"/page": html_route("<p>ok</p>")}) as site:
        port = int(site.base_url.rsplit(":", 1)[1])
        checked = CheckedAddresses()
        # Nothing listens on ::1 at this port, so the second address is the one that answers.
        checked.remember("rebind.test", port, ["::1", "127.0.0.1"])
        assert checked_session(checked).get(f"http://rebind.test:{port}/page", timeout=5).status_code == 200


def test_proxy_settings_in_the_environment_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    with serve_routes({"/page": html_route("<p>ok</p>")}) as site:
        port = int(site.base_url.rsplit(":", 1)[1])
        checked = CheckedAddresses()
        checked.remember("rebind.test", port, ["127.0.0.1"])
        assert checked_session(checked).get(f"http://rebind.test:{port}/page", timeout=5).status_code == 200


def test_a_name_that_rebinds_to_this_machine_after_the_check_cant_reach_it() -> None:
    # The check sees a public address for localhost, as a rebinding name would
    # answer it. Looking localhost up again to connect would reach the server
    # below; connecting to the address the check passed goes out to PUBLIC,
    # which the tests' network guard stops.
    routes = {"/robots.txt": Route(status=404), "/page": html_route("<p>secret</p>")}
    with serve_routes(routes) as site:
        fetcher = LocalFetcher(public_only=True, resolve=lambda host, port: [PUBLIC])
        with pytest.raises(NetworkAccessError, match=PUBLIC):
            fetcher.fetch_html(site.url("/page").replace("127.0.0.1", "localhost"))
        assert site.requests == []


class _Answers:
    """Answers each URL from a table, without connecting anywhere."""

    def __init__(self, answers: dict[str, requests.Response]) -> None:
        self.answers = answers

    def get(
        self, url: str, *, headers: dict[str, str], timeout: tuple[float, float], stream: bool, allow_redirects: bool
    ) -> requests.Response:
        response = self.answers[url]
        response.raw = io.BytesIO()
        return response


def test_a_robots_txt_redirect_is_checked_and_remembered_like_any_other_address() -> None:
    session = _Answers(
        {
            "http://shop.test/robots.txt": make_response(301, headers={"location": "http://robots.test/robots.txt"}),
            "http://robots.test/robots.txt": make_response(404),
        }
    )
    fetcher = LocalFetcher(public_only=True, resolve=lambda host, port: [PUBLIC], session=session)
    fetcher.admit("http://shop.test/page")
    assert fetcher.checked_addresses.lookup("robots.test", 80) == (PUBLIC,)
    assert fetcher.checked_addresses.lookup("SHOP.test.", 80) == (PUBLIC,)


def test_a_malformed_port_is_a_fetch_error_before_any_connection() -> None:
    with pytest.raises(FetchError, match="couldn't find"):
        LocalFetcher(public_only=True, resolve=lambda host, port: [PUBLIC]).fetch_html("http://rebind.test:99999/")


@contextmanager
def _tls_server(directory: Path, name: str) -> Iterator[tuple[int, Path, list[str]]]:
    """A local HTTPS server with a certificate for name only, recording the server name each client sent."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl isn't installed")
    key, certificate = directory / "key.pem", directory / "certificate.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-days", "1", "-subj", f"/CN={name}", "-addext", f"subjectAltName=DNS:{name}",
            "-keyout", str(key), "-out", str(certificate),
        ],
        check=True,
        capture_output=True,
    )
    names: list[str] = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)

    def record(connection: ssl.SSLObject | ssl.SSLSocket, server_name: str | None, _: ssl.SSLContext) -> None:
        names.append(server_name or "")

    context.sni_callback = record

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"ok"
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], certificate, names
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_https_sends_and_verifies_the_sites_name_while_connecting_to_the_checked_address(tmp_path: Path) -> None:
    with _tls_server(tmp_path, "rebind.test") as (port, certificate, names):
        checked = CheckedAddresses()
        checked.remember("rebind.test", port, ["127.0.0.1"])
        checked.remember("other.test", port, ["127.0.0.1"])
        session = checked_session(checked)
        assert session.get(f"https://rebind.test:{port}/", verify=str(certificate), timeout=5).text == "ok"
        assert names == ["rebind.test"]
        # The certificate is for rebind.test only, so a request for another name fails its check.
        with pytest.raises(requests.exceptions.SSLError):
            session.get(f"https://other.test:{port}/", verify=str(certificate), timeout=5)
        assert names == ["rebind.test", "other.test"]
