from __future__ import annotations

import pytest

from loupe.report import quote


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 80, "short"),
        ("abcdefghij", 10, "abcdefghij"),
        ("abcdefghijk", 10, "abcdefg..."),
        ("abcdefghijk", 0, "abcdefghijk"),
        ("abcdefghijk", 2, "ab"),
    ],
)
def test_quote(text: str, limit: int, expected: str) -> None:
    assert quote(text, limit) == expected


def test_quote_rejects_negative_limit() -> None:
    with pytest.raises(ValueError):
        quote("text", -1)
