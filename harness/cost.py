"""What a run costs on a model API, from the tokens a trial uses and the model's prices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# docs/design/harness.md explains these: two requests a trial, a few hundred
# tokens of page, and room for a model that reasons before answering.
INPUT_TOKENS_PER_TRIAL: Final[int] = 1_100
OUTPUT_TOKENS_PER_TRIAL: Final[int] = 900
DEFAULT_PAGES: Final[int] = 8
DEFAULT_CONDITIONS: Final[int] = 2


@dataclass(frozen=True)
class Estimate:
    trials: int
    input_tokens: int
    output_tokens: int
    dollars: float


def estimate(
    *,
    input_price: float,
    output_price: float,
    trials_per_cell: int = 20,
    pages: int = DEFAULT_PAGES,
    conditions: int = DEFAULT_CONDITIONS,
    input_tokens_per_trial: int = INPUT_TOKENS_PER_TRIAL,
    output_tokens_per_trial: int = OUTPUT_TOKENS_PER_TRIAL,
) -> Estimate:
    """The cost of one model's run, with prices in dollars per million tokens."""
    for name, value in (("input_price", input_price), ("output_price", output_price)):
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    for name, count in (
        ("trials_per_cell", trials_per_cell),
        ("pages", pages),
        ("conditions", conditions),
        ("input_tokens_per_trial", input_tokens_per_trial),
        ("output_tokens_per_trial", output_tokens_per_trial),
    ):
        if count < 1:
            raise ValueError(f"{name} must be at least 1")
    trials = trials_per_cell * pages * conditions
    input_tokens = trials * input_tokens_per_trial
    output_tokens = trials * output_tokens_per_trial
    dollars = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    return Estimate(trials=trials, input_tokens=input_tokens, output_tokens=output_tokens, dollars=dollars)
