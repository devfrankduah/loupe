"""Serve tests/fixtures over HTTP on 127.0.0.1 for rendered scans."""

from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .conftest import FIXTURES


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def serve_fixtures() -> Iterator[str]:
    """Yield the base URL of a local server for the fixture pages, stopping it afterwards."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(FIXTURES)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
