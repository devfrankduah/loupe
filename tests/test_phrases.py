from __future__ import annotations

import pytest

from loupe.phrases import INSTRUCTION_PHRASES, find_instruction_phrases


@pytest.mark.parametrize("phrase", INSTRUCTION_PHRASES)
def test_every_listed_phrase_matches_itself(phrase: str) -> None:
    assert phrase in find_instruction_phrases(f"before {phrase} after")


def test_matching_ignores_case_and_extra_whitespace() -> None:
    assert find_instruction_phrases("IGNORE\n   Previous rules") == ("ignore previous",)


def test_matching_respects_word_boundaries() -> None:
    assert find_instruction_phrases("you aren't here; the assistants left") == ()


def test_returns_all_matches_in_list_order() -> None:
    text = "Assistant: ignore all of it. You are free."
    assert find_instruction_phrases(text) == ("ignore all", "you are", "assistant")


def test_empty_text_has_no_matches() -> None:
    assert find_instruction_phrases("") == ()
