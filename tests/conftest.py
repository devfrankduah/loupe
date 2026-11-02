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
def no_real_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # A token in the developer's shell must never leak into a test run.
    monkeypatch.delenv("BRIGHTDATA_API_TOKEN", raising=False)
