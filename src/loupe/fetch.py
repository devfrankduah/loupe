"""Fetch pages through Bright Data, using the same requests as brightdata-mcp.

The request shapes come from brightdata-mcp 4.3.0 (server.js and
fetch_api.js):

* scrape_as_html posts {url, zone, format: "raw"} to /request, where zone is
  the Web Unlocker zone the MCP server creates on first start (mcp_unlocker).
* scrape_as_markdown posts [{url}] to the Fetch API at /datasets/v3/scrape
  with a fixed dataset id and reads the "markdown" field of the first record.

The token is kept on the client object and never appears in its repr or in
any error message this module raises.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Final, Protocol, Union

import requests

from loupe import __version__

TOKEN_ENVIRONMENT_VARIABLE: Final[str] = "BRIGHTDATA_API_TOKEN"
API_BASE_URL: Final[str] = "https://api.brightdata.com"
UNLOCKER_REQUEST_URL: Final[str] = f"{API_BASE_URL}/request"
FETCH_SCRAPE_URL: Final[str] = f"{API_BASE_URL}/datasets/v3/scrape"
FETCH_DATASET_ID: Final[str] = "gd_m6gjtfmeh43we6cqc"
DEFAULT_UNLOCKER_ZONE: Final[str] = "mcp_unlocker"

# The MCP server gives the Fetch API 45 seconds. Web Unlocker requests can be
# slower on hard targets, so they get more room.
MARKDOWN_TIMEOUT_SECONDS: Final[tuple[float, float]] = (10.0, 45.0)
HTML_TIMEOUT_SECONDS: Final[tuple[float, float]] = (10.0, 90.0)
_ERROR_BODY_LIMIT: Final[int] = 200
_REDACTED: Final[str] = "[redacted]"


JsonValue = Union[None, bool, int, float, str, Sequence["JsonValue"], Mapping[str, "JsonValue"]]


class HttpSession(Protocol):
    """The part of requests.Session the client uses, so tests can pass a fake."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: JsonValue,
        params: dict[str, str] | None,
        timeout: tuple[float, float],
    ) -> requests.Response: ...


class BrightDataError(RuntimeError):
    """A fetch failed. The message is safe to print."""


class MissingTokenError(BrightDataError):
    pass


def read_token_from_environment() -> str:
    token = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE, "").strip()
    if not token:
        raise MissingTokenError(f"{TOKEN_ENVIRONMENT_VARIABLE} is not set")
    return token


def _require_http_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise BrightDataError(f"not an http(s) URL: {url!r}")


def _decode_body(response: requests.Response) -> str:
    # requests falls back to ISO-8859-1 for text/* without a charset, which
    # garbles most modern pages. UTF-8 with replacement is the safer default.
    content_type = response.headers.get("content-type", "")
    if "charset=" in content_type.lower() and response.encoding:
        return response.text
    return response.content.decode("utf-8", errors="replace")


class BrightDataClient:
    def __init__(
        self,
        token: str,
        *,
        unlocker_zone: str = DEFAULT_UNLOCKER_ZONE,
        session: HttpSession | None = None,
    ) -> None:
        if not token or not token.strip():
            raise MissingTokenError("Bright Data token is empty")
        if not unlocker_zone or not unlocker_zone.strip():
            raise ValueError("unlocker_zone must not be empty")
        self._token = token.strip()
        self._unlocker_zone = unlocker_zone.strip()
        self._session: HttpSession = session if session is not None else requests.Session()

    def __repr__(self) -> str:
        return f"BrightDataClient(unlocker_zone={self._unlocker_zone!r})"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._token}",
            "user-agent": f"loupe/{__version__}",
            "content-type": "application/json",
        }

    def _redact(self, text: str) -> str:
        return text.replace(self._token, _REDACTED)

    def _post(
        self,
        what: str,
        url: str,
        endpoint: str,
        timeout: tuple[float, float],
        *,
        json_body: JsonValue,
        params: dict[str, str] | None = None,
    ) -> requests.Response:
        try:
            response = self._session.post(
                endpoint,
                headers=self._headers(),
                json=json_body,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException as error:
            message = f"{what} request for {url} failed: {type(error).__name__}: {error}"
            raise BrightDataError(self._redact(message)) from None

        if response.status_code >= 400:
            error_code = response.headers.get("x-brd-err-code", "")
            body = _decode_body(response)[:_ERROR_BODY_LIMIT].strip()
            message = f"{what} request for {url} failed: HTTP {response.status_code}"
            if error_code:
                message += f" ({error_code})"
            if body:
                message += f": {body}"
            raise BrightDataError(self._redact(message))
        return response

    def fetch_html(self, url: str) -> str:
        """Fetch raw HTML through Web Unlocker, as scrape_as_html does."""
        _require_http_url(url)
        response = self._post(
            "HTML",
            url,
            UNLOCKER_REQUEST_URL,
            HTML_TIMEOUT_SECONDS,
            json_body={"url": url, "zone": self._unlocker_zone, "format": "raw"},
        )
        html = _decode_body(response)
        if not html.strip():
            raise BrightDataError(f"HTML request for {url} returned an empty body")
        return html

    def fetch_markdown(self, url: str) -> str:
        """Fetch markdown through the Fetch API, as scrape_as_markdown does."""
        _require_http_url(url)
        response = self._post(
            "Markdown",
            url,
            FETCH_SCRAPE_URL,
            MARKDOWN_TIMEOUT_SECONDS,
            json_body=[{"url": url}],
            params={"dataset_id": FETCH_DATASET_ID, "format": "json", "include_errors": "true"},
        )
        # 202 means the scrape is still running after the synchronous window.
        if response.status_code == 202:
            raise BrightDataError(f"Markdown request for {url} timed out on Bright Data's side")

        try:
            records = response.json()
        except ValueError:
            raise BrightDataError(f"Markdown request for {url} returned non-JSON data") from None
        if not isinstance(records, list) or not records or not isinstance(records[0], dict):
            raise BrightDataError(f"Markdown request for {url} returned no records")

        record = records[0]
        error_code = record.get("error_code")
        if error_code:
            details = str(record.get("error", ""))[:_ERROR_BODY_LIMIT]
            raise BrightDataError(self._redact(f"Markdown request for {url} failed: {error_code}: {details}"))

        markdown = record.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise BrightDataError(f"Markdown request for {url} returned no markdown")
        return markdown
