"""python -m harness run ... runs the harness; python -m harness estimate ... prices a run. See --help for each."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from harness.agents import AGENTS, DEFAULT_TASK
from harness.cost import INPUT_TOKENS_PER_TRIAL, OUTPUT_TOKENS_PER_TRIAL, estimate
from harness.pages import PAGES_DIRECTORY, load_pages
from harness.results import run_to_dict, run_to_markdown
from harness.runner import DEFAULT_TRIALS, Condition, Trial, run_trials
from harness.tools import GuardedTool, PageTool, RawOutput, RawTool

PUBLISHED_PAGES = "https://devfrankduah.github.io/loupe/harness/"


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _price(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness", description="Measure how often hidden text hijacks an agent.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run every agent on every canary page, raw and guarded")
    run.add_argument("--base-url", default=PUBLISHED_PAGES, help=f"where the canary pages are served (default: {PUBLISHED_PAGES})")
    run.add_argument("--agent", action="append", choices=sorted(AGENTS), help="an agent to run, more than once for several (default: all)")
    run.add_argument("--trials", type=_positive, default=DEFAULT_TRIALS, help=f"trials per agent, page and condition (default: {DEFAULT_TRIALS})")
    run.add_argument("--seed", type=int, default=0, help="seed for the order the trials run in (default: 0)")
    run.add_argument("--raw-output", type=RawOutput, choices=list(RawOutput), default=RawOutput.MARKDOWN, metavar="{markdown,html}", help="what the raw tool returns (default: markdown)")
    run.add_argument("--markup-only", action="store_true", help="give the guarded tool a markup-only scan instead of a rendered one")
    run.add_argument("--allow-private-addresses", action="store_true", help="let the guarded tool fetch pages served from this machine, for testing")
    run.add_argument("--json", type=Path, metavar="PATH", help="also write the results, with every trial, as JSON to PATH")

    priced = commands.add_parser("estimate", help="price one model's run from its prices per million tokens")
    priced.add_argument("--input-price", type=_price, required=True, metavar="DOLLARS", help="dollars per million input tokens")
    priced.add_argument("--output-price", type=_price, required=True, metavar="DOLLARS", help="dollars per million output tokens")
    priced.add_argument("--trials", type=_positive, default=DEFAULT_TRIALS, help=f"trials per page and condition (default: {DEFAULT_TRIALS})")
    priced.add_argument("--input-tokens", type=_positive, default=INPUT_TOKENS_PER_TRIAL, help=f"input tokens a trial (default: {INPUT_TOKENS_PER_TRIAL:,})")
    priced.add_argument("--output-tokens", type=_positive, default=OUTPUT_TOKENS_PER_TRIAL, help=f"output tokens a trial (default: {OUTPUT_TOKENS_PER_TRIAL:,})")
    return parser


def _progress(trial: Trial) -> None:
    outcome = "error" if trial.hijacked is None else "hijacked" if trial.hijacked else "not hijacked"
    print(f"{trial.agent} {trial.page} {trial.condition.value} #{trial.number}: {outcome}", file=sys.stderr, flush=True)


def _run(arguments: argparse.Namespace) -> int:
    pages = load_pages(PAGES_DIRECTORY)
    names = arguments.agent or sorted(AGENTS)
    tools: dict[Condition, PageTool] = {
        Condition.RAW: RawTool(arguments.raw_output),
        Condition.GUARDED: GuardedTool(render=not arguments.markup_only, allow_private_addresses=arguments.allow_private_addresses),
    }
    run = run_trials(
        {name: AGENTS[name] for name in names},
        tools,
        pages,
        base_url=arguments.base_url,
        trials=arguments.trials,
        task=DEFAULT_TASK,
        seed=arguments.seed,
        on_trial=_progress,
    )
    print(run_to_markdown(run), end="")
    if arguments.json is not None:
        arguments.json.write_text(json.dumps(run_to_dict(run), indent=2) + "\n", encoding="utf-8")
    return 0


def _estimate(arguments: argparse.Namespace) -> int:
    result = estimate(
        input_price=arguments.input_price,
        output_price=arguments.output_price,
        trials_per_cell=arguments.trials,
        input_tokens_per_trial=arguments.input_tokens,
        output_tokens_per_trial=arguments.output_tokens,
    )
    print(
        f"{result.trials:,} trials, {result.input_tokens:,} input and {result.output_tokens:,} output tokens: "
        f"${result.dollars:,.2f} for one model"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    return _run(arguments) if arguments.command == "run" else _estimate(arguments)


if __name__ == "__main__":
    sys.exit(main())
