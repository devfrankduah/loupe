"""What a run costs: dollars on a model API, time on a local model, and the guard that keeps spending under a limit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from harness.agents import Usage
from harness.runner import Condition

# docs/design/harness.md explains these: two requests a trial, a few hundred
# tokens of page, and every request capped at 150 output tokens, thinking
# included, so a trial is at most 300 out.
INPUT_TOKENS_PER_TRIAL: Final[int] = 1_100
REQUESTS_PER_TRIAL: Final[int] = 2
DEFAULT_MAX_TOKENS: Final[int] = 150
OUTPUT_TOKENS_PER_TRIAL: Final[int] = REQUESTS_PER_TRIAL * DEFAULT_MAX_TOKENS
# The canary pages under docs/harness/; a test checks this against them.
DEFAULT_PAGES: Final[int] = 11
DEFAULT_CONDITIONS: Final[int] = 2
# A local model's time is estimated from what docs/hijack-rates.md measured: Llama 3.1 8B under Ollama on an Apple
# M5, which read 3,400 to 5,800 prompt tokens and wrote 25 to 26 output tokens a second. The slower end of each is
# the default.
DEFAULT_PROMPT_TOKENS_PER_SECOND: Final[float] = 3_400.0
DEFAULT_OUTPUT_TOKENS_PER_SECOND: Final[float] = 25.0
# The output tokens a trial took in the third run, 440 trials at a 400-token cap, well under it: an answer is three
# sentences, and only a raw trial goes on to call tools the model made up. The cost estimate keeps every request at
# its cap, since --max-cost has to cover the worst case; the time estimate uses these, never more than the cap allows.
MEASURED_OUTPUT_TOKENS_PER_TRIAL: Final[Mapping[Condition, int]] = {Condition.RAW: 230, Condition.GUARDED: 126}


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens."""

    input: float
    output: float

    def __post_init__(self) -> None:
        if self.input < 0 or self.output < 0:
            raise ValueError("a price must not be negative")

    def dollars(self, usage: Usage) -> float:
        return (usage.input_tokens * self.input + usage.output_tokens * self.output) / 1_000_000


FREE: Final[Price] = Price(0.0, 0.0)

# List prices read on 20 November 2026, as in docs/design/harness.md, by the
# model's harness name. Gemini's API names weren't on its pricing page, so a
# Gemini model is priced with --price.
PRICES: Final[dict[str, Price]] = {
    "anthropic:claude-opus-5-5": Price(4.00, 20.00),
    "anthropic:claude-sonnet-5-5": Price(2.00, 10.00),
    "anthropic:claude-haiku-5-5": Price(0.10, 0.50),
    "openai:gpt-6.2-astra": Price(8.00, 40.00),
    "openai:gpt-6.2-sol": Price(2.00, 10.00),
    "openai:gpt-6-luna": Price(0.10, 0.50),
}


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


@dataclass(frozen=True)
class Speed:
    """How fast a local model reads a prompt and writes its answer, in tokens a second."""

    prompt: float = DEFAULT_PROMPT_TOKENS_PER_SECOND
    output: float = DEFAULT_OUTPUT_TOKENS_PER_SECOND

    def __post_init__(self) -> None:
        if self.prompt <= 0 or self.output <= 0:
            raise ValueError("a speed must be more than 0 tokens a second")

    def seconds(self, input_tokens: int, output_tokens: int) -> float:
        return input_tokens / self.prompt + output_tokens / self.output

    def trial_seconds(self, condition: Condition, max_tokens: int) -> float:
        """About how long one trial in condition takes, from the measured output tokens a trial, under max_tokens."""
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1")
        output = min(MEASURED_OUTPUT_TOKENS_PER_TRIAL[condition], REQUESTS_PER_TRIAL * max_tokens)
        return self.seconds(INPUT_TOKENS_PER_TRIAL, output)


class Spend:
    """Keeps a run's spending at or under max_dollars: no trial starts that its estimate says would go over.

    A trial's estimate is the larger of the planned cost of one trial and the
    average of what that agent's trials have cost so far. Free agents, local
    models and the fake, never stop a run.
    """

    def __init__(self, max_dollars: float, prices: Mapping[str, Price], planned_per_trial: Mapping[str, float]) -> None:
        if max_dollars < 0:
            raise ValueError("max_dollars must not be negative")
        self.max_dollars = max_dollars
        self.spent = 0.0
        self._prices = dict(prices)
        self._planned = dict(planned_per_trial)
        self._charged: dict[str, tuple[float, int]] = {}

    def __repr__(self) -> str:
        return f"Spend(${self.spent:.4f} of ${self.max_dollars:.2f})"

    def _next_trial(self, agent: str) -> float:
        dollars, trials = self._charged.get(agent, (0.0, 0))
        average = dollars / trials if trials else 0.0
        return max(self._planned.get(agent, 0.0), average)

    def stop_reason(self, agent: str) -> str | None:
        next_trial = self._next_trial(agent)
        if next_trial > 0 and self.spent + next_trial > self.max_dollars:
            return (
                f"stopped at ${self.spent:.2f} spent: the next trial of {agent} would cost about ${next_trial:.4f}, "
                f"going over --max-cost ${self.max_dollars:.2f}"
            )
        return None

    def charge(self, agent: str, usage: Usage) -> None:
        dollars = self._prices.get(agent, FREE).dollars(usage)
        self.spent += dollars
        previous, trials = self._charged.get(agent, (0.0, 0))
        self._charged[agent] = (previous + dollars, trials + 1)
