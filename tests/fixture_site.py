"""Serve tests/fixtures over HTTP on 127.0.0.1 for rendered scans."""

from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .conftest import FIXTURES


@dataclass
class FixtureSite:
    base_url: str
    # Every path the browser asked for, in order, whether or not the file exists.
    requested: list[str] = field(default_factory=list)


@contextmanager
def serve_fixtures() -> Iterator[FixtureSite]:
    """Serve the fixture pages until the block ends, recording what was requested."""
    site = FixtureSite(base_url="")

    class RecordingHandler(SimpleHTTPRequestHandler):
        def do_GET(self) -> None:
            site.requested.append(self.path.split("?", 1)[0])
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(RecordingHandler, directory=str(FIXTURES)))
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
