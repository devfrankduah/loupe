from __future__ import annotations

import ipaddress
import socket
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def planted_html() -> str:
    return (FIXTURES / "planted.html").read_text(encoding="utf-8")


@pytest.fixture
def visible_html() -> str:
    return (FIXTURES / "visible.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Credentials in the developer's shell must never reach a test run: no
    # Bright Data token, and no remote browser, so rendering stays local.
    monkeypatch.delenv("BRIGHTDATA_API_TOKEN", raising=False)
    monkeypatch.delenv("BRIGHTDATA_BROWSER_WSS", raising=False)


class NetworkAccessError(AssertionError):
    """A test tried to reach a host other than this machine.

    It's an AssertionError so no network error handler, which catch OSError,
    can turn it into an ordinary "couldn't connect".
    """


def _is_loopback(host: object) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def only_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests run on the local backend against servers on 127.0.0.1. Nothing in
    # this process may reach the internet, Bright Data included.
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse_outside(address: object) -> None:
        if isinstance(address, tuple) and address and not _is_loopback(address[0]):
            raise NetworkAccessError(f"a test tried to connect to {address[0]}")

    def connect(self: socket.socket, address: object) -> None:
        refuse_outside(address)
        real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        refuse_outside(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
