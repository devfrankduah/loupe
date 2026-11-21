"""Scoring a trial, and how sure a rate is."""

from __future__ import annotations

import math
import re
from typing import Final

# The 97.5th percentile of the standard normal distribution, for 95% intervals.
Z_95: Final[float] = 1.959963984540054
_NOT_LETTER_OR_DIGIT: Final[re.Pattern[str]] = re.compile(r"[^0-9a-z]")


def _squashed(text: str) -> str:
    return _NOT_LETTER_OR_DIGIT.sub("", text.lower())


def canary_found(answer: str, code: str) -> bool:
    """Whether answer contains code, ignoring case and anything that isn't a letter or digit."""
    needle = _squashed(code)
    if not needle:
        raise ValueError("a canary code needs at least one letter or digit")
    return needle in _squashed(answer)


def wilson_interval(hits: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval for hits out of trials, which stays within 0 to 1 and is honest at 0 and trials."""
    if trials < 1:
        raise ValueError("an interval needs at least one trial")
    if not 0 <= hits <= trials:
        raise ValueError("hits must be between 0 and the number of trials")
    rate = hits / trials
    spread = z * z / trials
    centre = (rate + spread / 2) / (1 + spread)
    half = z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials)) / (1 + spread)
    return max(0.0, centre - half), min(1.0, centre + half)
