"""Recognise an answer that turns an automated visitor away.

Sites that block bots answer 403 or 429, or serve a challenge page in place
of the content. The checks here are deliberately narrow so a real page is
never mistaken for a block: two status codes, the header Cloudflare sets on
its challenges, and the exact titles of Cloudflare's challenge pages.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

_BLOCKING_STATUSES: Final[dict[int, str]] = {
    403: "HTTP 403, forbidden",
    429: "HTTP 429, too many requests",
}
# Cloudflare documents cf-mitigated: challenge on every challenge it serves.
_CHALLENGE_HEADER: Final[str] = "cf-mitigated"
_CHALLENGE_TITLES: Final[frozenset[str]] = frozenset({"just a moment...", "attention required! | cloudflare"})
_TITLE: Final[re.Pattern[str]] = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# A challenge page puts its title near the top, so only the start is searched.
_TITLE_SEARCH_CHARACTERS: Final[int] = 65_536


def blocked_by_answer(status: int, headers: Mapping[str, str]) -> str | None:
    """Why the status line and headers look like a block, or None when they don't."""
    if headers.get(_CHALLENGE_HEADER, "").strip().lower() == "challenge":
        return "a Cloudflare challenge"
    return _BLOCKING_STATUSES.get(status)


def blocked_by_content(html: str) -> str | None:
    """Why the page itself looks like a challenge page, or None when it doesn't."""
    match = _TITLE.search(html[:_TITLE_SEARCH_CHARACTERS])
    if match is None:
        return None
    title = " ".join(match.group(1).split()).lower()
    return "a Cloudflare challenge page" if title in _CHALLENGE_TITLES else None
