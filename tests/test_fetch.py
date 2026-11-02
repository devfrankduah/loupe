from __future__ import annotations

import pytest
import requests

from loupe.fetch import (
    FETCH_DATASET_ID,
    FETCH_SCRAPE_URL,
    UNLOCKER_REQUEST_URL,
    BrightDataClient,
    BrightDataError,
    MissingTokenError,
    read_token_from_environment,
)
from tests.fakes import FakeSession, json_response, make_response

TOKEN = "test-token-1234567890"


def _client(session: FakeSession, unlocker_zone: str = "mcp_unlocker") -> BrightDataClient:
    return BrightDataClient(TOKEN, unlocker_zone=unlocker_zone, session=session)


def test_html_request_matches_scrape_as_html() -> None:
    session = FakeSession([make_response(200, "<html>ok</html>", content_type="text/html; charset=utf-8")])
    html = _client(session).fetch_html("https://example.com/")

    assert html == "<html>ok</html>"
    (call,) = session.calls
    assert call.url == UNLOCKER_REQUEST_URL
    assert call.json == {"url": "https://example.com/", "zone": "mcp_unlocker", "format": "raw"}
    assert call.params is None
    assert call.headers["authorization"] == f"Bearer {TOKEN}"


def test_html_request_uses_configured_zone() -> None:
    session = FakeSession([make_response(200, "<p>x</p>")])
    _client(session, unlocker_zone="my_zone").fetch_html("https://example.com/")
    assert session.calls[0].json == {"url": "https://example.com/", "zone": "my_zone", "format": "raw"}


def test_html_without_charset_is_decoded_as_utf8() -> None:
    session = FakeSession([make_response(200, "café".encode("utf-8"), content_type="text/html")])
    assert _client(session).fetch_html("https://example.com/") == "café"


def test_markdown_request_matches_scrape_as_markdown() -> None:
    session = FakeSession([json_response(200, [{"url": "https://example.com/", "markdown": "# Hi"}])])
    markdown = _client(session).fetch_markdown("https://example.com/")

    assert markdown == "# Hi"
    (call,) = session.calls
    assert call.url == FETCH_SCRAPE_URL
    assert call.json == [{"url": "https://example.com/"}]
    assert call.params == {"dataset_id": FETCH_DATASET_ID, "format": "json", "include_errors": "true"}


def test_markdown_record_error_is_reported() -> None:
    session = FakeSession([json_response(200, [{"error_code": "dead_page", "error": "404"}])])
    with pytest.raises(BrightDataError, match="dead_page: 404"):
        _client(session).fetch_markdown("https://example.com/missing")


def test_markdown_still_running_is_a_timeout() -> None:
    session = FakeSession([json_response(202, {"snapshot_id": "s_1"})])
    with pytest.raises(BrightDataError, match="timed out"):
        _client(session).fetch_markdown("https://example.com/")


@pytest.mark.parametrize("payload", [[], {}, [{"markdown": ""}], [{"markdown": None}], ["text"]])
def test_markdown_without_content_is_an_error(payload: object) -> None:
    session = FakeSession([json_response(200, payload)])
    with pytest.raises(BrightDataError):
        _client(session).fetch_markdown("https://example.com/")


def test_markdown_non_json_body_is_an_error() -> None:
    session = FakeSession([make_response(200, "<html>", content_type="text/html")])
    with pytest.raises(BrightDataError, match="non-JSON"):
        _client(session).fetch_markdown("https://example.com/")


def test_http_error_message_includes_status_and_code_but_never_the_token() -> None:
    body = f"invalid token {TOKEN}"
    session = FakeSession([make_response(401, body, headers={"x-brd-err-code": "client_10000"})])
    with pytest.raises(BrightDataError) as caught:
        _client(session).fetch_html("https://example.com/")

    message = str(caught.value)
    assert "HTTP 401 (client_10000)" in message
    assert TOKEN not in message
    assert "[redacted]" in message


def test_network_error_message_never_contains_the_token() -> None:
    session = FakeSession([requests.ConnectionError(f"connection reset while sending {TOKEN}")])
    with pytest.raises(BrightDataError) as caught:
        _client(session).fetch_markdown("https://example.com/")
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None


def test_client_repr_does_not_contain_the_token() -> None:
    assert TOKEN not in repr(_client(FakeSession([])))


def test_non_http_urls_are_rejected_before_any_request() -> None:
    session = FakeSession([])
    with pytest.raises(BrightDataError, match="not an http"):
        _client(session).fetch_html("file:///etc/passwd")
    assert session.calls == []


def test_empty_token_is_rejected() -> None:
    with pytest.raises(MissingTokenError):
        BrightDataClient("   ")


def test_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRIGHTDATA_API_TOKEN", f"  {TOKEN}\n")
    assert read_token_from_environment() == TOKEN


def test_missing_token_raises() -> None:
    with pytest.raises(MissingTokenError):
        read_token_from_environment()
