"""A run's results as JSON, with every trial, and as a Markdown table with raw and guarded side by side."""

from __future__ import annotations

from typing import Final

from loupe_scan._version import __version__

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
            }
            for trial in run.trials
        ],
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


def run_to_markdown(run: Run) -> str:
    """One table per agent: each page's hijacks in the raw and guarded conditions, with 95% intervals."""
    cells = {(cell.agent, cell.page, cell.condition): cell for cell in run.cells()}
    conditions = list(run.conditions)
    lines = [
        f"Hijacks out of {run.trials_per_cell} trials per cell, with Wilson 95% intervals. Task: {run.task}",
        "",
        *(f"- {condition.value}: {run.conditions[condition]}" for condition in conditions),
    ]
    for agent in run.agents:
        lines += ["", f"### {agent}", "", "| Page | " + " | ".join(c.value.capitalize() for c in conditions) + " |"]
        lines.append("| --- |" + " --- |" * len(conditions))
        for page in run.pages:
            row = [describe_cell(cells[(agent, page.slug, condition)]) for condition in conditions]
            lines.append(f"| {page.slug} | " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"

