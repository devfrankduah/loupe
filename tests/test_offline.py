from __future__ import annotations

import socket

import pytest
import requests

from loupe_scan.local_fetch import LocalFetcher

from .conftest import NetworkAccessError
from .route_site import html_route, serve_routes


def test_a_connection_beyond_this_machine_fails_the_test_loudly() -> None:
    # 192.0.2.1 is reserved for documentation, so nothing real is ever there.
    with socket.socket() as probe, pytest.raises(NetworkAccessError):
        probe.connect(("192.0.2.1", 80))
    with pytest.raises(NetworkAccessError):
        requests.get("http://192.0.2.1/", timeout=1)
    with pytest.raises(NetworkAccessError):
        LocalFetcher(respect_robots=False).fetch_html("http://192.0.2.1/cake")


def test_this_machine_is_still_reachable() -> None:
    with serve_routes({"/cake": html_route("<p>Cake</p>")}) as site:
        assert LocalFetcher().fetch_html(site.url("/cake")) == "<p>Cake</p>"
