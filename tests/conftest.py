from __future__ import annotations

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
