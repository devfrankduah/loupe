"""A stand-in for requests.Session that records calls instead of making them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import requests

from loupe.fetch import JsonValue


def make_response(
    status_code: int,
    body: str | bytes = b"",
    *,
    content_type: str = "text/html",
    headers: dict[str, str] | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8") if isinstance(body, str) else body
    response.headers["content-type"] = content_type
    response.headers.update(headers or {})
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


def json_response(status_code: int, payload: object) -> requests.Response:
    return make_response(status_code, json.dumps(payload), content_type="application/json")


@dataclass
class RecordedCall:
    url: str
    headers: dict[str, str]
    json: JsonValue
    params: dict[str, str] | None
    timeout: tuple[float, float]


@dataclass
class FakeSession:
    responses: list[requests.Response | Exception]
    calls: list[RecordedCall] = field(default_factory=list)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: JsonValue,
        params: dict[str, str] | None,
        timeout: tuple[float, float],
    ) -> requests.Response:
        self.calls.append(RecordedCall(url, headers, json, params, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
