"""A proxy on this machine that lets a browser reach public addresses only.

loupe mcp fetches a page's HTML from public addresses only, and a rendered
scan then loads that HTML in local Chromium, which fetches the page's
stylesheets, scripts, images, frames and anything its scripts ask for. Without
a guard those requests could reach this machine or its network. So Chromium
is launched with this proxy as its proxy for everything, loopback included
(through "<-loopback>" in its bypass list), and the proxy checks every
request the way local_fetch does for the HTML: it resolves the host, refuses
it unless every address is public, and connects to an address that passed,
so the name can't resolve somewhere else in between.

Plain HTTP requests are forwarded with "Connection: close", one request a
connection, and HTTPS and WebSockets travel through CONNECT tunnels, so the
browser's own TLS reaches the site untouched. A redirect is a new request
from the browser, so each hop is checked.
"""

from __future__ import annotations

import selectors
import socket
import socketserver
import threading
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Final
from urllib.parse import urlsplit

from loupe_scan.local_fetch import FetchError, Resolver, require_public_address, resolve_addresses

# The most a request's line and headers may take.
MAX_HEAD_BYTES: Final[int] = 64 * 1024
CONNECT_TIMEOUT_SECONDS: Final[float] = 10.0
# A connection with nothing moving either way for this long is closed.
IDLE_SECONDS: Final[float] = 120.0
_CHUNK: Final[int] = 65_536
_HOP_HEADERS: Final[frozenset[bytes]] = frozenset({b"connection", b"proxy-connection", b"keep-alive", b"proxy-authorization"})
# A frame the browser was refused shows this, as plain text.
REFUSED_MESSAGE: Final[str] = "loupe refused this request: it goes to a private, loopback or other non-public address"
_REFUSED_BODY: Final[bytes] = f"{REFUSED_MESSAGE}\n".encode("ascii")

# (host, port) to the addresses to connect to. Raises FetchError to refuse.
Admit = Callable[[str, int], Sequence[str]]


def public_addresses(resolve: Resolver = resolve_addresses) -> Admit:
    """Admit a host only when every address it resolves to is public, as loupe mcp's fetches do."""

    def admit(host: str, port: int) -> Sequence[str]:
        name = f"[{host}]" if ":" in host else host
        return require_public_address(f"http://{name}:{port}/", resolve)

    return admit


def _split_authority(authority: str, default_port: int) -> tuple[str, int]:
    """host and port from "host:port", "[v6]:port" or a bare host. Raises ValueError for anything else."""
    parts = urlsplit(f"//{authority}")
    host = parts.hostname
    if not host or parts.username is not None or parts.path or parts.query:
        raise ValueError(f"not a host and port: {authority!r}")
    return host, parts.port or default_port


def _read_head(connection: socket.socket) -> tuple[bytes, bytes] | None:
    """A request's line and headers, and any bytes after them, or None when the client sent no complete head."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(_CHUNK)
        if not chunk or len(data) + len(chunk) > MAX_HEAD_BYTES:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until either side closes or nothing moves for IDLE_SECONDS."""
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        while True:
            ready = selector.select(timeout=IDLE_SECONDS)
            if not ready:
                return
            for key, _ in ready:
                source: socket.socket = key.fileobj  # type: ignore[assignment]
                target: socket.socket = key.data
                try:
                    chunk = source.recv(_CHUNK)
                    if not chunk:
                        return
                    target.sendall(chunk)
                except OSError:
                    return


class _Handler(socketserver.BaseRequestHandler):
    server: PublicOnlyServer

    def handle(self) -> None:
        client: socket.socket = self.request
        client.settimeout(IDLE_SECONDS)
        try:
            read = _read_head(client)
        except OSError:
            return
        if read is None:
            return
        head, rest = read
        lines = head.split(b"\r\n")
        try:
            method, target, version = lines[0].decode("latin-1").split(" ")
        except ValueError:
            self._answer(client, 400, b"")
            return
        if method == "CONNECT":
            self._tunnel(client, target, rest)
        else:
            self._forward(client, method, target, version, lines[1:], rest)

    def _answer(self, client: socket.socket, status: int, body: bytes) -> None:
        reason = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}.get(status, "Error")
        header = f"HTTP/1.1 {status} {reason}\r\ncontent-type: text/plain\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n"
        try:
            client.sendall(header.encode("ascii") + body)
        except OSError:
            pass

    def _open(self, client: socket.socket, host: str, port: int) -> socket.socket | None:
        """A connection to an address host passed the check with, or None once the client has been told why not."""
        try:
            addresses = self.server.admit(host, port)
        except FetchError:
            self.server.count_refused()
            self._answer(client, 403, _REFUSED_BODY)
            return None
        for address in addresses:
            try:
                return socket.create_connection((address, port), timeout=CONNECT_TIMEOUT_SECONDS)
            except OSError:
                continue
        self._answer(client, 502, b"")
        return None

    def _tunnel(self, client: socket.socket, target: str, rest: bytes) -> None:
        try:
            host, port = _split_authority(target, 443)
        except ValueError:
            self._answer(client, 400, b"")
            return
        upstream = self._open(client, host, port)
        if upstream is None:
            return
        with upstream:
            try:
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            except OSError:
                return
            _relay(client, upstream)

    def _forward(self, client: socket.socket, method: str, target: str, version: str, headers: list[bytes], rest: bytes) -> None:
        try:
            parts = urlsplit(target)
            if parts.scheme != "http":
                raise ValueError(target)
            host, port = _split_authority(parts.netloc, 80)
        except ValueError:
            self._answer(client, 400, b"")
            return
        upstream = self._open(client, host, port)
        if upstream is None:
            return
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        kept = [line for line in headers if line.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS]
        request = f"{method} {path} {version}\r\n".encode("latin-1") + b"".join(line + b"\r\n" for line in kept)
        # One request a connection, so a reused browser connection can't reach a host nobody checked.
        request += b"connection: close\r\n\r\n" + rest
        with upstream:
            try:
                upstream.sendall(request)
            except OSError:
                self._answer(client, 502, b"")
                return
            _relay(client, upstream)


class PublicOnlyServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, admit: Admit) -> None:
        self.admit = admit
        self.refused = 0
        self._count_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def count_refused(self) -> None:
        with self._count_lock:
            self.refused += 1


class PublicOnlyProxy:
    """The proxy, serving on 127.0.0.1 from a thread of its own while it's open. Use it in a with block."""

    def __init__(self, admit: Admit | None = None) -> None:
        self._server = PublicOnlyServer(admit if admit is not None else public_addresses())
        self._thread = threading.Thread(target=self._server.serve_forever, name="loupe-public-proxy", daemon=True)

    def __repr__(self) -> str:
        return f"PublicOnlyProxy(port={self.port}, refused={self.refused})"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def refused(self) -> int:
        return self._server.refused

    def __enter__(self) -> PublicOnlyProxy:
        self._thread.start()
        return self

    def __exit__(
        self, error_type: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
