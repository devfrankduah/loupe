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
from dataclasses import dataclass
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


# Removing hidden text from the markdown, for loupe mcp's strip mode.

# Each round removes every copy found, and taking words out can join others
# into a new copy, so the check runs again. Past this many rounds the
# markdown is given up on.
_MAX_REMOVAL_ROUNDS: Final[int] = 5
_EXTRA_BLANK_LINES: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_TRAILING_SPACE: Final[re.Pattern[str]] = re.compile(r"[ \t]+$", re.MULTILINE)


@dataclass(frozen=True)
class Stripped:
    markdown: str
    # How many of the texts had at least one copy taken out.
    removed: int
    # How many could still be found after the last round. Above 0, the markdown still holds hidden text.
    remaining: int


def _comparable_words(markdown: str) -> list[tuple[str, int, int]]:
    """Each word the markdown check compares, lowercased, with where it is in markdown."""
    skipped: list[tuple[int, int]] = []
    for match in _INLINE_LINK_TARGET.finditer(markdown):
        if match.group(1) is None:
            skipped.append(match.span())
        else:
            # The title is compared, so only what's around it is skipped.
            skipped.extend([(match.start(), match.start(1)), (match.end(1), match.end())])
    words: list[tuple[str, int, int]] = []
    gaps = iter(skipped)
    gap = next(gaps, None)
    for word in _WORD.finditer(markdown):
        while gap is not None and gap[1] <= word.start():
            gap = next(gaps, None)
        if gap is not None and gap[0] <= word.start() < gap[1]:
            continue
        words.append((word.group().lower(), word.start(), word.end()))
    return words


def _surrounding_punctuation(text: str) -> tuple[str, str]:
    """What text has before its first word and after its last, such as a closing full stop."""
    words = list(_WORD.finditer(text))
    if not words:
        return "", ""
    return text[: words[0].start()].strip(), text[words[-1].end() :].strip()


def _copies(words: list[tuple[str, int, int]], needle: list[str]) -> list[tuple[int, int]]:
    """Where each copy of needle's word sequence starts and ends among words, as character offsets."""
    copies: list[tuple[int, int]] = []
    size = len(needle)
    for index in range(len(words) - size + 1):
        if words[index][0] == needle[0] and [word for word, _, _ in words[index : index + size]] == needle:
            copies.append((words[index][1], words[index + size - 1][2]))
    return copies


def remove_hidden_text(markdown: str, texts: list[str]) -> Stripped:
    """markdown with every copy of each text's word sequence taken out, matched as MarkdownIndex matches.

    A copy is matched on its words alone, so markdown punctuation, escapes
    and line breaks between them don't hide it, and it goes with the
    punctuation the text itself has around its words, where the markdown has
    it too. Every copy goes, including one the page also shows, since there's
    no telling them apart.
    """
    needles: list[tuple[list[str], tuple[str, str]]] = []
    for text in texts:
        sequence = normalize_words(text).split()
        if sequence:
            needles.append((sequence, _surrounding_punctuation(text)))
    removed: set[int] = set()
    for _ in range(_MAX_REMOVAL_ROUNDS):
        words = _comparable_words(markdown)
        spans: list[tuple[int, int]] = []
        for number, (needle, (before, after)) in enumerate(needles):
            for start, end in _copies(words, needle):
                removed.add(number)
                if before and markdown[max(0, start - len(before)) : start] == before:
                    start -= len(before)
                if after and markdown[end : end + len(after)] == after:
                    end += len(after)
                spans.append((start, end))
        if not spans:
            break
        pieces: list[str] = []
        position = 0
        for start, end in sorted(spans):
            if start >= position:
                pieces.append(markdown[position:start])
            position = max(position, end)
        pieces.append(markdown[position:])
        markdown = "".join(pieces)
    words = _comparable_words(markdown)
    remaining = sum(bool(_copies(words, needle)) for needle, _ in needles)
    tidied = _EXTRA_BLANK_LINES.sub("\n\n", _TRAILING_SPACE.sub("", markdown))
    return Stripped(markdown=tidied, removed=len(removed), remaining=remaining)
