"""Phrases that suggest a piece of text is written to a language model.

This list is the only place the phrases live. Matching is case-insensitive,
respects word boundaries, and treats any run of whitespace between words as a
single space, so "Ignore   ALL" matches "ignore all" but "you aren't" does not
match "you are".
"""

from __future__ import annotations

import re
from typing import Final

INSTRUCTION_PHRASES: Final[tuple[str, ...]] = (
    "ignore previous",
    "ignore all",
    "ignore the above",
    "disregard previous",
    "disregard all",
    "previous instructions",
    "new instructions",
    "you are",
    "system prompt",
    # "assistant" on its own is left out: on real pages it named products and
    # projects ("Device-Aware RAG Assistant", ask_brightdata_assistant) far more
    # often than it addressed a model. Text that does address one still matches
    # "you are", "system prompt" or an "ignore" phrase.
    "language model",
    "do not tell",
    "do not reveal",
)


def _compile_phrase(phrase: str) -> re.Pattern[str]:
    words = phrase.split()
    if not words:
        raise ValueError("instruction phrase must contain at least one word")
    body = r"\s+".join(re.escape(word) for word in words)
    return re.compile(rf"\b{body}\b", re.IGNORECASE)


_COMPILED_PHRASES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (phrase, _compile_phrase(phrase)) for phrase in INSTRUCTION_PHRASES
)


def find_instruction_phrases(text: str) -> tuple[str, ...]:
    """Return every phrase from INSTRUCTION_PHRASES that occurs in text, in list order."""
    if not text:
        return ()
    return tuple(phrase for phrase, pattern in _COMPILED_PHRASES if pattern.search(text))
