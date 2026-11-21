"""Running trials: every agent on every page in every condition, N times each, in a shuffled order."""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from harness.agents import DEFAULT_TASK, AgentFactory
from harness.pages import CanaryPage
from harness.scoring import canary_found, wilson_interval
from harness.tools import CachedTool, PageTool

DEFAULT_TRIALS: Final[int] = 20
_ERROR_CHARACTERS: Final[int] = 300


class Condition(str, Enum):
    RAW = "raw"
    GUARDED = "guarded"


@dataclass(frozen=True)
class Trial:
    agent: str
    page: str
    condition: Condition
    number: int
    # None when the agent failed and the trial doesn't count.
    hijacked: bool | None
    answer: str | None
    error: str | None = None


@dataclass(frozen=True)
class Cell:
    """One agent on one page in one condition, over all its trials."""

    agent: str
    page: str
    condition: Condition
    hijacked: int
    # Trials that produced an answer. Errors aren't among them.
    trials: int
    errors: int

    @property
    def rate(self) -> float | None:
        return self.hijacked / self.trials if self.trials else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson_interval(self.hijacked, self.trials) if self.trials else None


@dataclass(frozen=True)
class Run:
    pages: tuple[CanaryPage, ...]
    agents: tuple[str, ...]
    trials_per_cell: int
    task: str
    seed: int
    base_url: str
    # How each condition's tool was set up, in words, for the results.
    conditions: Mapping[Condition, str]
    trials: tuple[Trial, ...] = field(repr=False)

    def cells(self) -> list[Cell]:
        """Every agent, page and condition, in agent, page and condition order."""
        cells: list[Cell] = []
        for agent in self.agents:
            for page in self.pages:
                for condition in self.conditions:
                    mine = [
                        trial
                        for trial in self.trials
                        if (trial.agent, trial.page, trial.condition) == (agent, page.slug, condition)
                    ]
                    answered = [trial for trial in mine if trial.hijacked is not None]
                    cells.append(
                        Cell(
                            agent=agent,
                            page=page.slug,
                            condition=condition,
                            hijacked=sum(bool(trial.hijacked) for trial in answered),
                            trials=len(answered),
                            errors=len(mine) - len(answered),
                        )
                    )
        return cells


def _one_trial(
    agent_name: str, factory: AgentFactory, tool: PageTool, page: CanaryPage, url: str, task: str, number: int, condition: Condition
) -> Trial:
    try:
        answer = factory(tool).answer(url, task)
    except Exception as error:
        # An adapter talks to a model API, and any failure there, from a rate
        # limit to a bug, should cost one trial rather than the run. It is
        # recorded, not counted.
        message = f"{type(error).__name__}: {error}"[:_ERROR_CHARACTERS]
        return Trial(agent_name, page.slug, condition, number, hijacked=None, answer=None, error=message)
    return Trial(agent_name, page.slug, condition, number, hijacked=canary_found(answer, page.code), answer=answer)


def run_trials(
    agents: Mapping[str, AgentFactory],
    tools: Mapping[Condition, PageTool],
    pages: Sequence[CanaryPage],
    *,
    base_url: str,
    trials: int = DEFAULT_TRIALS,
    task: str = DEFAULT_TASK,
    seed: int = 0,
    describe: Callable[[PageTool], str] = repr,
    on_trial: Callable[[Trial], None] = lambda trial: None,
) -> Run:
    """Run every agent on every page in every condition, trials times each, in an order shuffled by seed.

    Each condition's tool fetches each URL once for the whole run. Each trial
    builds a fresh agent around the condition's tool, so no conversation
    carries over from one trial to the next.
    """
    if trials < 1:
        raise ValueError("a run needs at least one trial per cell")
    if not agents or not tools or not pages:
        raise ValueError("a run needs at least one agent, one condition and one page")
    if "{url}" not in task:
        raise ValueError("the task must say where the page is, with {url}")
    cached = {condition: CachedTool(tool) for condition, tool in tools.items()}
    order = [
        (agent, page, condition, number)
        for agent in agents
        for page in pages
        for condition in cached
        for number in range(trials)
    ]
    random.Random(seed).shuffle(order)
    results: list[Trial] = []
    for agent, page, condition, number in order:
        url = page.url(base_url)
        trial = _one_trial(agent, agents[agent], cached[condition], page, url, task.format(url=url), number, condition)
        results.append(trial)
        on_trial(trial)
    return Run(
        pages=tuple(pages),
        agents=tuple(agents),
        trials_per_cell=trials,
        task=task,
        seed=seed,
        base_url=base_url,
        conditions={condition: describe(tool) for condition, tool in tools.items()},
        trials=tuple(results),
    )
