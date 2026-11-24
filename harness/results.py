"""A run's results as JSON, with every trial, and as a Markdown table with raw and guarded side by side."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Final

from loupe_scan._version import __version__

from harness.agents import Usage
from harness.pages import NULL_CONTROL, CanaryPage
from harness.runner import Cell, Condition, Run, Trial

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
                "prompt_seconds": None if trial.usage is None else round(trial.usage.prompt_seconds, 3),
                "output_seconds": None if trial.usage is None else round(trial.usage.output_seconds, 3),
                "seconds": round(trial.seconds, 3),
            }
            for trial in run.trials
        ],
        "stopped": run.stopped,
        "guarded_scans": dict(run.guarded_scans),
    }


def _str(record: Mapping[str, object], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str):
        raise ValueError(f"a saved run's {name!r} should be text")
    return value


def _int(record: Mapping[str, object], name: str) -> int:
    value = record.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"a saved run's {name!r} should be a whole number")
    return value


def _seconds(record: Mapping[str, object], name: str) -> float:
    value = record.get(name)
    if value is None:
        return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"a saved run's {name!r} should be a number")
    return float(value)


def _maybe_str(record: Mapping[str, object], name: str) -> str | None:
    return None if record.get(name) is None else _str(record, name)


def _maybe_bool(record: Mapping[str, object], name: str) -> bool | None:
    value = record.get(name)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"a saved run's {name!r} should be true, false or null")
    return value


def _records(document: Mapping[str, object], name: str) -> list[Mapping[str, object]]:
    value = document.get(name)
    if not isinstance(value, list):
        raise ValueError(f"a saved run's {name!r} should be a list")
    records: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"a saved run's {name!r} should hold objects")
        records.append(item)
    return records


def _trial(record: Mapping[str, object]) -> Trial:
    usage = None
    if record.get("input_tokens") is not None:
        usage = Usage(
            _int(record, "input_tokens"),
            _int(record, "output_tokens"),
            _int(record, "requests"),
            _seconds(record, "prompt_seconds"),
            _seconds(record, "output_seconds"),
        )
    return Trial(
        agent=_str(record, "agent"),
        page=_str(record, "page"),
        condition=Condition(_str(record, "condition")),
        number=_int(record, "trial"),
        hijacked=_maybe_bool(record, "hijacked"),
        answer=_maybe_str(record, "answer"),
        error=_maybe_str(record, "error"),
        usage=usage,
        seconds=_seconds(record, "seconds"),
    )


def run_from_dict(document: Mapping[str, object]) -> Run:
    """A run read back from run_to_dict's JSON. Raises ValueError for anything else."""
    if document.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"not a harness run in format {FORMAT_VERSION}")
    conditions = document.get("conditions")
    if not isinstance(conditions, Mapping):
        raise ValueError("a saved run's 'conditions' should be an object")
    return Run(
        pages=tuple(
            CanaryPage(slug=_str(page, "page"), code=_str(page, "code"), hidden_by=_str(page, "hidden_by"))
            for page in _records(document, "pages")
        ),
        agents=tuple(dict.fromkeys(_str(cell, "agent") for cell in _records(document, "cells"))),
        trials_per_cell=_int(document, "trials_per_cell"),
        task=_str(document, "task"),
        seed=_int(document, "seed"),
        base_url=_str(document, "base_url"),
        conditions={Condition(str(name)): str(description) for name, description in conditions.items()},
        trials=tuple(_trial(record) for record in _records(document, "trials")),
        stopped=_maybe_str(document, "stopped"),
        guarded_scans=_guarded_scans(document),
    )


def _guarded_scans(document: Mapping[str, object]) -> dict[str, str]:
    """The guarded tool's scan on each page, from runs saved since it was recorded."""
    scans = document.get("guarded_scans", {})
    if not isinstance(scans, Mapping) or not all(isinstance(value, str) for value in scans.values()):
        raise ValueError("a saved run's 'guarded_scans' should map pages to text")
    return {str(page): str(why) for page, why in scans.items()}


def rescore(run: Run, unfinished: Callable[[str], bool], reason: str) -> tuple[Run, int]:
    """run with every answer unfinished picks out turned into an error, and how many were."""
    changed = 0
    trials: list[Trial] = []
    for trial in run.trials:
        if trial.error is None and trial.answer is not None and unfinished(trial.answer):
            changed += 1
            trial = dataclasses.replace(trial, hijacked=None, error=f"{reason} (rescored)")
        trials.append(trial)
    return dataclasses.replace(run, trials=tuple(trials)), changed


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
    prompt_seconds = sum(trial.usage.prompt_seconds for trial in measured if trial.usage is not None)
    output_seconds = sum(trial.usage.output_seconds for trial in measured if trial.usage is not None)
    count = len(measured)
    line = (
        f"Used {input_tokens:,} input and {output_tokens:,} output tokens over {count:,} trial{'' if count == 1 else 's'}: "
        f"{input_tokens / count:,.0f} in, {output_tokens / count:,.0f} out and {seconds / count:,.1f} s a trial."
    )
    if prompt_seconds > 0 and output_seconds > 0:
        line += (
            f" The model read {input_tokens / prompt_seconds:,.0f} prompt tokens and wrote "
            f"{output_tokens / output_seconds:,.0f} output tokens a second."
        )
    return ["", line]


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
    if run.guarded_scans:
        lines += ["", "The guarded tool's scan on each page, as its header said:", ""]
        lines += [f"- {page.slug}: {run.guarded_scans[page.slug]}" for page in run.pages if page.slug in run.guarded_scans]
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

