"""A small HTTP server on 127.0.0.1 whose answer for each path is set by the test."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass(frozen=True)
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {"content-type": "text/html; charset=utf-8"})
    # Send Content-Length. Without it the body ends when the connection closes.
    declare_length: bool = True
    # Wait this long before answering at all.
    delay_seconds: float = 0.0
    # Send the body in pieces of this many bytes with this pause between them.
    drip_bytes: int = 0
    drip_seconds: float = 0.0


def html_route(html: str, *, status: int = 200, headers: dict[str, str] | None = None) -> Route:
    return Route(
        status=status,
        body=html.encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"} if headers is None else headers,
    )


@dataclass
class RouteSite:
    base_url: str
    routes: dict[str, Route]
    # (path, request headers) for every request, in order.
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    # The most requests the server was answering at the same moment. A
    # request counts until just before its last byte is sent, so a client
    # can't start its next request while the last one still counts.
    most_at_once: int = 0
    _answering: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def requested(self) -> list[str]:
        return [path for path, _ in self.requests]


@contextmanager
def serve_routes(routes: dict[str, Route]) -> Iterator[RouteSite]:
    """Serve routes until the block ends. Any other path answers 404."""
    site = RouteSite(base_url="", routes=routes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with site._lock:
                site.requests.append((self.path, {key.lower(): value for key, value in self.headers.items()}))
                site._answering += 1
                site.most_at_once = max(site.most_at_once, site._answering)
            answering = [True]

            def finished() -> None:
                with site._lock:
                    if answering[0]:
                        site._answering -= 1
                        answering[0] = False

            try:
                self._answer(finished)
            finally:
                finished()

        def _answer(self, finished: Callable[[], None]) -> None:
            route = site.routes.get(self.path, Route(status=404, body=b"not found"))
            if route.delay_seconds:
                time.sleep(route.delay_seconds)
            self.send_response(route.status)
            for name, value in route.headers.items():
                self.send_header(name, value)
            if route.declare_length:
                self.send_header("content-length", str(len(route.body)))
            self.end_headers()
            try:
                if route.drip_bytes:
                    starts = range(0, len(route.body), route.drip_bytes)
                    for index, start in enumerate(starts):
                        if index:
                            time.sleep(route.drip_seconds)
                        if index == len(starts) - 1:
                            finished()
                        self.wfile.write(route.body[start : start + route.drip_bytes])
                        self.wfile.flush()
                else:
                    finished()
                    self.wfile.write(route.body)
            except (BrokenPipeError, ConnectionResetError):
                # The client gave up part way, which is what some tests check for.
                pass

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    # A short poll interval keeps each test from waiting half a second to shut the server down.
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
