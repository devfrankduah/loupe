from __future__ import annotations

import pytest

from loupe.backend import Backend, choose_backend

TOKEN = {"BRIGHTDATA_API_TOKEN": "a-token"}
ENDPOINT = {"BRIGHTDATA_BROWSER_WSS": "wss://user:pass@browser.invalid:9222"}


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({}, Backend.LOCAL),
        ({"BRIGHTDATA_API_TOKEN": "  ", "BRIGHTDATA_BROWSER_WSS": ""}, Backend.LOCAL),
        (TOKEN, Backend.BRIGHTDATA),
        (ENDPOINT, Backend.BRIGHTDATA),
        ({**TOKEN, **ENDPOINT}, Backend.BRIGHTDATA),
    ],
)
def test_the_backend_follows_the_credentials(environment: dict[str, str], expected: Backend) -> None:
    assert choose_backend(None, environment) is expected


@pytest.mark.parametrize("requested", list(Backend))
@pytest.mark.parametrize("environment", [{}, {**TOKEN, **ENDPOINT}])
def test_asking_for_a_backend_overrides_the_credentials(requested: Backend, environment: dict[str, str]) -> None:
    assert choose_backend(requested, environment) is requested
