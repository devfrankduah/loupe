"""Decide whether the text of a finding made it into the markdown version of a page.

Both sides are reduced to lowercase words separated by single spaces before
comparing, which removes markdown punctuation, escapes and line wrapping.
Link and image targets are dropped first so "[read more](https://...)"
compares as "read more", while a link title ("[x](url "title")") is kept
because it can carry a title attribute through. A finding survives when its whole word sequence appears in the
markdown, in order and contiguous.
"""

from __future__ import annotations

import re
from typing import Final

# Shorter text matches by accident ("menu", "close") and says nothing about
# whether the hidden copy specifically was carried over.
MIN_COMPARABLE_CHARACTERS: Final[int] = 12

_WORD: Final[re.Pattern[str]] = re.compile(r"[^\W_]+")
_INLINE_LINK_TARGET: Final[re.Pattern[str]] = re.compile(r"\]\([^)\s]*(?:\s+\"([^\"]*)\")?\)")


def normalize_words(text: str) -> str:
    return " ".join(word.lower() for word in _WORD.findall(text))


def _keep_link_title(match: re.Match[str]) -> str:
    title = match.group(1)
    return f"] {title} " if title else "] "


class MarkdownIndex:
    """The markdown of one page, normalized once so many findings can be checked cheaply."""

    def __init__(self, markdown: str) -> None:
        without_link_targets = _INLINE_LINK_TARGET.sub(_keep_link_title, markdown)
        self._padded_words = f" {normalize_words(without_link_targets)} "

    def contains(self, text: str) -> bool | None:
        """True or False when text is long enough to compare, None when it isn't."""
        needle = normalize_words(text)
        if len(needle) < MIN_COMPARABLE_CHARACTERS:
            return None
        return f" {needle} " in self._padded_words
