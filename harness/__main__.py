"""python -m harness run ... runs the harness; python -m harness estimate ... prices a run. See --help for each.

A run with --model calls real models: Ollama on this machine, or Anthropic,
OpenAI and Gemini with their keys in the environment. --dry-run prints the
plan, the tokens and the cost or time it expects, and calls nothing. A run
that would cost more than --max-cost, which is 0 unless set, is refused
before it starts, and stops early if its spending gets there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import requests
from loupe_scan.mcp_server import ScanChoice

from harness.agents import AGENTS, DEFAULT_TASK, AgentFactory
from harness.cost import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OUTPUT_TOKENS_PER_SECOND,
    DEFAULT_PROMPT_TOKENS_PER_SECOND,
    INPUT_TOKENS_PER_TRIAL,
    MEASURED_OUTPUT_TOKENS_PER_TRIAL,
    OUTPUT_TOKENS_PER_TRIAL,
    PRICES,
    REQUESTS_PER_TRIAL,
    FREE,
    Price,
    Speed,
    Spend,
    estimate,
)
from harness.models import (
    CUT_OFF_CALL,
    KEY_VARIABLES,
    MAX_TOOL_CALLS,
    OLLAMA_URL,
    HarnessSetupError,
    HttpSession,
    ModelSpec,
    Settings,
    cut_off_call,
    live_models,
)
from harness.pages import PAGES_DIRECTORY, load_pages
from harness.results import rescore, run_from_dict, run_to_dict, run_to_markdown
from harness.runner import DEFAULT_TRIALS, Condition, Trial, run_trials
from harness.tools import GuardedTool, PageTool, RawOutput, RawTool

# How the plan describes each way the guarded tool scans.
_GUARDED_SCANS: Final[dict[ScanChoice, str]] = {
    ScanChoice.AUTO: "in auto mode, its default: markup first, rendered in Chromium when the page needs a browser",
    ScanChoice.MARKUP: "markup only",
    ScanChoice.RENDER: "rendered in Chromium",
}

PUBLISHED_PAGES: Final[str] = "https://devfrankduah.github.io/loupe/harness/"
EXIT_REFUSED: Final[int] = 2


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


def _speed(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return number


def _model_price(value: str) -> tuple[str, Price]:
    name, separator, prices = value.rpartition("=")
    entry, slash, output = prices.partition("/")
    try:
        if not separator or not slash:
            raise ValueError
        return name, Price(float(entry), float(output))
    except ValueError:
        raise argparse.ArgumentTypeError("give it as PROVIDER:MODEL=INPUT/OUTPUT, in dollars per million tokens") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness", description="Measure how often hidden text hijacks an agent.")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run agents on every canary page, raw and guarded")
    run.add_argument("--base-url", default=PUBLISHED_PAGES, help=f"where the canary pages are served (default: {PUBLISHED_PAGES})")
    run.add_argument("--agent", action="append", choices=sorted(AGENTS), help="a fake agent to run, more than once for several")
    run.add_argument(
        "--model",
        action="append",
        type=ModelSpec.parse,
        metavar="PROVIDER:MODEL",
        help="a real model to run, such as ollama:qwen3:4b or anthropic:claude-haiku-5-5, more than once for several "
        "(default: the fake agent alone)",
    )
    run.add_argument("--trials", type=_positive, help=f"trials per agent, page and condition (default: {DEFAULT_TRIALS})")
    run.add_argument("--pilot", action="store_true", help="one trial per page and condition, to check a model works and measure it")
    run.add_argument("--dry-run", action="store_true", help="print the plan and what it would use, then stop without fetching or calling anything")
    run.add_argument("--max-tokens", type=_positive, default=DEFAULT_MAX_TOKENS, help=f"output tokens each request may use, thinking included (default: {DEFAULT_MAX_TOKENS})")
    run.add_argument("--max-cost", type=_price, default=0.0, metavar="DOLLARS", help="the most the run may spend on model APIs (default: 0, so only local models run)")
    run.add_argument("--price", action="append", type=_model_price, metavar="PROVIDER:MODEL=IN/OUT", help="a model's price in dollars per million tokens, for one the harness doesn't know")
    run.add_argument("--ollama-url", default=OLLAMA_URL, help=f"where Ollama listens (default: {OLLAMA_URL})")
    run.add_argument("--prompt-speed", type=_speed, default=DEFAULT_PROMPT_TOKENS_PER_SECOND, metavar="TOKENS", help="a local model's prompt tokens a second, for the dry run's time estimate")
    run.add_argument("--output-speed", type=_speed, default=DEFAULT_OUTPUT_TOKENS_PER_SECOND, metavar="TOKENS", help="a local model's output tokens a second, for the dry run's time estimate")
    run.add_argument("--seed", type=int, default=0, help="seed for the order the trials run in (default: 0)")
    run.add_argument("--raw-output", type=RawOutput, choices=list(RawOutput), default=RawOutput.MARKDOWN, metavar="{markdown,html}", help="what the raw tool returns (default: markdown)")
    run.add_argument(
        "--guarded-scan",
        type=ScanChoice,
        choices=list(ScanChoice),
        default=ScanChoice.AUTO,
        metavar="{auto,markup,render}",
        help="how the guarded tool scans pages, as loupe mcp --scan does (default: auto, fetch_page's default)",
    )
    run.add_argument(
        "--condition",
        action="append",
        type=Condition,
        choices=list(Condition),
        metavar="{raw,guarded}",
        help="a condition to run, more than once for several (default: both)",
    )
    run.add_argument("--allow-private-addresses", action="store_true", help="let the guarded tool fetch pages served from this machine, for testing")
    run.add_argument("--json", type=Path, metavar="PATH", help="also write the results, with every trial, as JSON to PATH")

    rescored = commands.add_parser("rescore", help="score a saved run again with the current checks on answers")
    rescored.add_argument("path", type=Path, metavar="RESULTS", help="a run's JSON, as run --json writes it")
    rescored.add_argument("--json", type=Path, metavar="PATH", help="also write the rescored run as JSON to PATH")

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


@dataclass(frozen=True)
class _Planned:
    name: str
    local: bool
    # None for a model with no known price; the fake agent is free.
    price: Price | None
    # Trials in each condition.
    trials_per_condition: int
    conditions: tuple[Condition, ...]

    @property
    def trials(self) -> int:
        return self.trials_per_condition * len(self.conditions)

    @property
    def input_tokens(self) -> int:
        return self.trials * INPUT_TOKENS_PER_TRIAL

    def output_tokens(self, max_tokens: int) -> int:
        return self.trials * REQUESTS_PER_TRIAL * max_tokens

    def local_seconds(self, speed: Speed, max_tokens: int) -> float:
        return sum(self.trials_per_condition * speed.trial_seconds(condition, max_tokens) for condition in self.conditions)


def _dollars(amount: float) -> str:
    # A pilot on a cheap model costs a fraction of a cent, which two places would round to nothing.
    return f"${amount:,.4f}" if 0 < amount < 0.01 else f"${amount:,.2f}"


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 1:
        return "under a minute"
    hours, minutes = divmod(minutes, 60)
    return f"about {hours} h {minutes} min" if hours else f"about {minutes} min"


def _plan_lines(
    planned: Sequence[_Planned],
    specs: Sequence[ModelSpec],
    arguments: argparse.Namespace,
    trials: int,
    pages: int,
    conditions: Sequence[Condition],
    environment: Mapping[str, str],
) -> list[str]:
    total_trials = sum(item.trials for item in planned)
    speed = Speed(arguments.prompt_speed, arguments.output_speed)
    count = len(conditions)
    lines = [
        f"Plan: {len(planned)} agent{'' if len(planned) == 1 else 's'} x {pages} pages x {count} condition{'' if count == 1 else 's'} x {trials} "
        f"trial{'' if trials == 1 else 's'} = {total_trials:,} trials, in an order shuffled by seed {arguments.seed}.",
        f"Pages from {arguments.base_url}; the guarded tool is loupe's fetch_page in strip mode, "
        f"{_GUARDED_SCANS[arguments.guarded_scan]}.",
        f"Each request may use {arguments.max_tokens} output tokens, thinking included, and an answer at most "
        f"{MAX_TOOL_CALLS} tool calls. Tokens assume {INPUT_TOKENS_PER_TRIAL:,} in and {REQUESTS_PER_TRIAL} capped "
        "requests out a trial.",
        "",
        "| Agent | Trials | Input tokens | Output tokens | Cost | Time |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    total = 0.0
    for item in planned:
        output = item.output_tokens(arguments.max_tokens)
        if item.local:
            cost, time = "$0 (runs here)", _duration(item.local_seconds(speed, arguments.max_tokens))
        elif item.price is None:
            cost, time = "no price known", "not estimated"
        else:
            dollars = (item.input_tokens * item.price.input + output * item.price.output) / 1_000_000
            total += dollars
            cost, time = (_dollars(dollars) if item.price != FREE else "$0"), "not estimated"
        lines.append(f"| {item.name} | {item.trials:,} | {item.input_tokens:,} | {output:,} | {cost} | {time} |")
    verdict = "within" if total <= arguments.max_cost else "over"
    lines += ["", f"Estimated cost: {_dollars(total)}, {verdict} --max-cost {_dollars(arguments.max_cost)}."]
    if any(item.local for item in planned):
        written = " and ".join(
            f"{MEASURED_OUTPUT_TOKENS_PER_TRIAL[condition]} {condition.value}" for condition in conditions
        )
        lines.append(
            f"Local times assume {arguments.prompt_speed:,g} prompt and {arguments.output_speed:,g} output tokens a "
            f"second and the output tokens a trial measured in docs/hijack-rates.md, {written}, never more than the "
            "cap allows. They leave out loading the model and rendering each page once. A pilot measures them."
        )
    providers = sorted({spec.provider for spec in specs if not spec.local}, key=lambda provider: provider.value)
    for provider in providers:
        variable = KEY_VARIABLES[provider]
        state = "set" if environment.get(variable, "").strip() else "not set"
        lines.append(f"{variable}: {state}.")
    return lines


def _run(arguments: argparse.Namespace, environment: Mapping[str, str], session_factory: Callable[[], HttpSession]) -> int:
    if arguments.pilot and arguments.trials is not None:
        print("error: --pilot runs one trial per cell, so it takes no --trials", file=sys.stderr)
        return EXIT_REFUSED
    trials = 1 if arguments.pilot else arguments.trials or DEFAULT_TRIALS
    pages = load_pages(PAGES_DIRECTORY)
    specs: list[ModelSpec] = list(dict.fromkeys(arguments.model or []))
    fakes = arguments.agent or ([] if specs else sorted(AGENTS))
    prices = {**PRICES, **dict(arguments.price or [])}
    conditions: list[Condition] = list(dict.fromkeys(arguments.condition or list(Condition)))
    per_condition = len(pages) * trials
    chosen = tuple(conditions)
    planned = [_Planned(name, False, FREE, per_condition, chosen) for name in fakes] + [
        _Planned(spec.name, spec.local, FREE if spec.local else prices.get(spec.name), per_condition, chosen)
        for spec in specs
    ]
    lines = _plan_lines(planned, specs, arguments, trials, len(pages), conditions, environment)
    if arguments.dry_run:
        print("\n".join(lines + ["Dry run: nothing was fetched and no model was called."]))
        return 0
    unpriced = [item.name for item in planned if item.price is None]
    if unpriced:
        print(f"error: no price known for {', '.join(unpriced)}; give one with --price", file=sys.stderr)
        return EXIT_REFUSED
    planned_dollars = {
        item.name: (INPUT_TOKENS_PER_TRIAL * (item.price or FREE).input + REQUESTS_PER_TRIAL * arguments.max_tokens * (item.price or FREE).output) / 1_000_000
        for item in planned
    }
    if sum(planned_dollars[item.name] * item.trials for item in planned) > arguments.max_cost:
        print("\n".join(lines), file=sys.stderr)
        print("error: the run would cost more than --max-cost; raise it to run", file=sys.stderr)
        return EXIT_REFUSED
    try:
        models = live_models(
            specs,
            environment=environment,
            session_factory=session_factory,
            settings=Settings(max_tokens=arguments.max_tokens, ollama_url=arguments.ollama_url),
        )
    except HarnessSetupError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_REFUSED
    agents: dict[str, AgentFactory] = {name: AGENTS[name] for name in fakes}
    agents.update(models)
    tools: dict[Condition, PageTool] = {}
    if Condition.RAW in conditions:
        tools[Condition.RAW] = RawTool(arguments.raw_output)
    if Condition.GUARDED in conditions:
        tools[Condition.GUARDED] = GuardedTool(
            scan=arguments.guarded_scan, allow_private_addresses=arguments.allow_private_addresses
        )
    spend = Spend(arguments.max_cost, {name: prices.get(name, FREE) for name in agents}, planned_dollars)
    run = run_trials(
        agents,
        tools,
        pages,
        base_url=arguments.base_url,
        trials=trials,
        task=DEFAULT_TASK,
        seed=arguments.seed,
        on_trial=_progress,
        budget=spend,
    )
    print(run_to_markdown(run), end="")
    if spend.spent:
        print(f"\nSpent about {_dollars(spend.spent)} on model APIs, from the tokens they reported.")
    if arguments.json is not None:
        arguments.json.write_text(json.dumps(run_to_dict(run), indent=2) + "\n", encoding="utf-8")
    return 0


def _rescore(arguments: argparse.Namespace) -> int:
    """A saved run with every answer that's a tool call cut off at the token cap counted as an error."""
    try:
        document = json.loads(arguments.path.read_text(encoding="utf-8"))
        if not isinstance(document, Mapping):
            raise ValueError("not a JSON object")
        run, changed = rescore(run_from_dict(document), cut_off_call, CUT_OFF_CALL)
    except (OSError, ValueError) as error:
        print(f"error: {arguments.path} isn't a run the harness can read: {error}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"Counted {changed:,} answer{'' if changed == 1 else 's'} cut off while writing out a tool call as {'an error' if changed == 1 else 'errors'}.\n")
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


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] = os.environ,
    session_factory: Callable[[], HttpSession] = requests.Session,
) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "run":
        return _run(arguments, environment, session_factory)
    if arguments.command == "rescore":
        return _rescore(arguments)
    return _estimate(arguments)


if __name__ == "__main__":
    sys.exit(main())
