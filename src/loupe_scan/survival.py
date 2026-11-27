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
# Each text is looked for only where its rarest word is, so an ordinary page
# costs about one pass over its words. A page built so every text could start
# anywhere, the same word over and over, would still cost texts times words;
# past this many word comparisons in all, the markdown is given up on, as when
# copies keep forming, rather than holding loupe mcp for hours.
_MAX_WORD_COMPARISONS: Final[int] = 50_000_000
_EXTRA_BLANK_LINES: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_TRAILING_SPACE: Final[re.Pattern[str]] = re.compile(r"[ \t]+$", re.MULTILINE)
# Left where text was taken out, so the markdown it emptied can be tidied
# there and nowhere else. It isn't a word character, so it never joins or
# splits the words the check compares.
_CUT: Final[str] = "\x00"
# Markdown that wrapped text now taken out, and is empty: emphasis, strikethrough, code, link text and link titles.
_EMPTIED: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"\*\*[ \t]*\x00[ \t]*\*\*"), _CUT),
    (re.compile(r"(?<![\w*])\*[ \t]*\x00[ \t]*\*(?![\w*])"), _CUT),
    (re.compile(r"__[ \t]*\x00[ \t]*__"), _CUT),
    (re.compile(r"(?<!\w)_[ \t]*\x00[ \t]*_(?!\w)"), _CUT),
    (re.compile(r"~~[ \t]*\x00[ \t]*~~"), _CUT),
    (re.compile(r"`[ \t]*\x00[ \t]*`"), _CUT),
    (re.compile(r"(?<!!)\[[ \t]*\x00[ \t]*\]\([^)\s]*(?:\s+\"[^\"]*\")?\)"), _CUT),
    (re.compile(r"\]\(([^)\s]*)\s+\"[ \t]*\x00[ \t]*\"\)"), "](\\1)" + _CUT),
)
_CUTS: Final[re.Pattern[str]] = re.compile(r"[ \t]*\x00(?:[ \t]*\x00)*[ \t]*")
_INDENTATION: Final[re.Pattern[str]] = re.compile(r"[ \t]*")
# A line left holding only a list marker, heading marks or quote marks.
_BARE_MARKUP_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])|#{1,6}|(?:>\s*)+)?\s*$")


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


class _TooMuchWork(Exception):
    """Looking for the texts would take more than _MAX_WORD_COMPARISONS."""


class _Budget:
    def __init__(self, comparisons: int) -> None:
        self.left = comparisons

    def spend(self, comparisons: int) -> None:
        self.left -= comparisons
        if self.left < 0:
            raise _TooMuchWork


@dataclass(frozen=True)
class _Words:
    """The markdown's comparable words, their text alone, and where each word occurs, made once a round."""

    spans: list[tuple[str, int, int]]
    tokens: list[str]
    places: dict[str, list[int]]

    @classmethod
    def of(cls, markdown: str) -> _Words:
        spans = _comparable_words(markdown)
        tokens = [word for word, _, _ in spans]
        places: dict[str, list[int]] = {}
        for index, word in enumerate(tokens):
            places.setdefault(word, []).append(index)
        return cls(spans, tokens, places)


def _copies(words: _Words, needle: list[str], budget: _Budget) -> list[tuple[int, int]]:
    """Where each copy of needle's word sequence starts and ends among words, as character offsets, in order.

    Only the places its rarest word occurs are tried. Raises _TooMuchWork when budget runs out.
    """
    size = len(needle)
    budget.spend(size)
    offset, places = min(
        ((index, words.places.get(word, [])) for index, word in enumerate(needle)), key=lambda item: len(item[1])
    )
    budget.spend(len(places) * size)
    copies: list[tuple[int, int]] = []
    for place in places:
        # A start before the first word slices fewer than size words, so it can't match.
        start = place - offset
        if words.tokens[start : start + size] == needle:
            copies.append((words.spans[start][1], words.spans[start + size - 1][2]))
    return copies


def remove_hidden_text(markdown: str, texts: list[str]) -> Stripped:
    """markdown with every copy of each text's word sequence taken out, matched as MarkdownIndex matches.

    A copy is matched on its words alone, so markdown punctuation, escapes
    and line breaks between them don't hide it, and it goes with the
    punctuation the text itself has around its words, where the markdown has
    it too. Every copy goes, including one the page also shows, since there's
    no telling them apart.

    A page that would take more than _MAX_WORD_COMPARISONS to search comes
    back with every text counted as remaining, so the markdown isn't used.
    """
    ends_with_newline = markdown.endswith("\n")
    # Texts with the same words and punctuation are looked for once, and each is counted.
    numbers_by_needle: dict[tuple[tuple[str, ...], tuple[str, str]], list[int]] = {}
    for number, text in enumerate(texts):
        sequence = tuple(normalize_words(text).split())
        if sequence:
            numbers_by_needle.setdefault((sequence, _surrounding_punctuation(text)), []).append(number)
    needles = [(list(sequence), punctuation, numbers) for (sequence, punctuation), numbers in numbers_by_needle.items()]
    budget = _Budget(_MAX_WORD_COMPARISONS)
    removed: set[int] = set()
    try:
        markdown, remaining = _remove_copies(markdown, needles, removed, budget)
    except _TooMuchWork:
        return Stripped(markdown=markdown, removed=len(removed), remaining=sum(len(numbers) for _, _, numbers in needles))
    tidied = _EXTRA_BLANK_LINES.sub("\n\n", _TRAILING_SPACE.sub("", _tidy_cuts(markdown))).strip("\n")
    if tidied and ends_with_newline:
        tidied += "\n"
    return Stripped(markdown=tidied, removed=len(removed), remaining=remaining)


def _remove_copies(
    markdown: str, needles: list[tuple[list[str], tuple[str, str], list[int]]], removed: set[int], budget: _Budget
) -> tuple[str, int]:
    """markdown with the needles' copies cut out, round by round, and how many texts could still be found.

    Adds the number of each text with a copy taken out to removed. Raises _TooMuchWork when budget runs out.
    """
    for _ in range(_MAX_REMOVAL_ROUNDS):
        words = _Words.of(markdown)
        spans: list[tuple[int, int]] = []
        for needle, (before, after), numbers in needles:
            for start, end in _copies(words, needle, budget):
                removed.update(numbers)
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
                pieces.append(_CUT)
            position = max(position, end)
        pieces.append(markdown[position:])
        markdown = "".join(pieces)
    words = _Words.of(markdown)
    return markdown, sum(len(numbers) for needle, _, numbers in needles if _copies(words, needle, budget))


def _tidy_line(line: str) -> str | None:
    """A line text was taken out of, with the markdown that held only that text removed, or None to drop it."""
    previous = None
    while previous != line:
        previous = line
        for pattern, replacement in _EMPTIED:
            line = pattern.sub(replacement, line)
    indentation = _INDENTATION.match(line)
    start = 0 if indentation is None else indentation.end()
    line = line[:start] + _CUTS.sub(_close_up, line[start:])
    return None if _BARE_MARKUP_LINE.match(line) else line


def _close_up(match: re.Match[str]) -> str:
    """What a cut and the spaces around it become: one space between words, none at an edge or before punctuation."""
    before = match.string[match.start() - 1] if match.start() else ""
    after = match.string[match.end()] if match.end() < len(match.string) else ""
    if not before or not after or after in ".,;:!?)]}" or before in "([{":
        return ""
    return " "


def _tidy_cuts(markdown: str) -> str:
    """markdown with the lines text was taken out of tidied, and every _CUT mark gone."""
    lines: list[str] = []
    for line in markdown.split("\n"):
        if _CUT not in line:
            lines.append(line)
            continue
        tidied = _tidy_line(line)
        if tidied is not None:
            lines.append(tidied)
    return "\n".join(lines)
