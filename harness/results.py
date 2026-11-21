"""A run's results as JSON, with every trial, and as a Markdown table with raw and guarded side by side."""

from __future__ import annotations

from typing import Final

from loupe_scan._version import __version__

from harness.pages import NULL_CONTROL
from harness.runner import Cell, Run

FORMAT_VERSION: Final[int] = 1


def _cell_to_dict(cell: Cell) -> dict[str, object]:
    interval = cell.interval
    return {
        "agent": cell.agent,
        "page": cell.page,
        "condition": cell.condition.value,
        "hijacked": cell.hijacked,
        "trials": cell.trials,
        "errors": cell.errors,
        "rate": cell.rate,
        "interval_95": None if interval is None else [round(interval[0], 4), round(interval[1], 4)],
    }


def run_to_dict(run: Run) -> dict[str, object]:
    return {
        "format_version": FORMAT_VERSION,
        "loupe_version": __version__,
        "task": run.task,
        "trials_per_cell": run.trials_per_cell,
        "seed": run.seed,
        "base_url": run.base_url,
        "conditions": {condition.value: description for condition, description in run.conditions.items()},
        "pages": [{"page": page.slug, "code": page.code, "hidden_by": page.hidden_by} for page in run.pages],
        "cells": [_cell_to_dict(cell) for cell in run.cells()],
        "trials": [
            {
                "agent": trial.agent,
                "page": trial.page,
                "condition": trial.condition.value,
                "trial": trial.number,
                "hijacked": trial.hijacked,
                "answer": trial.answer,
                "error": trial.error,
                "input_tokens": None if trial.usage is None else trial.usage.input_tokens,
                "output_tokens": None if trial.usage is None else trial.usage.output_tokens,
                "requests": None if trial.usage is None else trial.usage.requests,
                "seconds": round(trial.seconds, 3),
            }
            for trial in run.trials
        ],
        "stopped": run.stopped,
    }


def _percent(value: float) -> str:
    return f"{100 * value:.0f}%"


def describe_cell(cell: Cell) -> str:
    """A cell as "hijacked/trials, rate (interval)", such as "20/20, 100% (84% to 100%)"."""
    interval = cell.interval
    if cell.rate is None or interval is None:
        text = "no answers"
    else:
        text = f"{cell.hijacked}/{cell.trials}, {_percent(cell.rate)} ({_percent(interval[0])} to {_percent(interval[1])})"
    if cell.errors:
        text += f", {cell.errors} error{'' if cell.errors == 1 else 's'}"
    return text


def _usage_lines(run: Run, agent: str) -> list[str]:
    """What an agent's trials used, from the tokens its model reported and the time each took: what a pilot measures."""
    measured = [trial for trial in run.trials if trial.agent == agent and trial.usage is not None]
    if not measured:
        return []
    input_tokens = sum(trial.usage.input_tokens for trial in measured if trial.usage is not None)
    output_tokens = sum(trial.usage.output_tokens for trial in measured if trial.usage is not None)
    seconds = sum(trial.seconds for trial in measured)
    count = len(measured)
    return [
        "",
        f"Used {input_tokens:,} input and {output_tokens:,} output tokens over {count:,} trial{'' if count == 1 else 's'}: "
        f"{input_tokens / count:,.0f} in, {output_tokens / count:,.0f} out and {seconds / count:,.1f} s a trial.",
    ]


def run_to_markdown(run: Run) -> str:
    """One table per agent: each page's hijacks in the raw and guarded conditions, with 95% intervals."""
    cells = {(cell.agent, cell.page, cell.condition): cell for cell in run.cells()}
    conditions = list(run.conditions)
    lines = [
        f"Hijacks out of {run.trials_per_cell} trials per cell, with Wilson 95% intervals. Task: {run.task}",
        "",
        *(f"- {condition.value}: {run.conditions[condition]}" for condition in conditions),
    ]
    if run.stopped is not None:
        lines += ["", f"The run stopped early: {run.stopped}. The rates count the trials that ran."]
    for agent in run.agents:
        lines += ["", f"### {agent}", "", "| Page | " + " | ".join(c.value.capitalize() for c in conditions) + " |"]
        lines.append("| --- |" + " --- |" * len(conditions))
        for page in run.pages:
            row = [describe_cell(cells[(agent, page.slug, condition)]) for condition in conditions]
            lines.append(f"| {page.slug} | " + " | ".join(row) + " |")
        lines += _usage_lines(run, agent)
        unasked = sum(cells[(agent, page.slug, condition)].hijacked for page in run.pages if page.slug == NULL_CONTROL for condition in conditions)
        if unasked:
            lines += [
                "",
                f"Warning: {agent} gave the null control's code {unasked} time{'' if unasked == 1 else 's'}, though "
                "nothing on that page asks for it. Read those answers before trusting this agent's other rates: "
                "the scoring, or the agent, is at fault.",
            ]
    return "\n".join(lines) + "\n"

