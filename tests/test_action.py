"""action.yml: every input is documented, and none can reach a shell script as code."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
ACTION = ROOT / "action.yml"
CI_DOCS = ROOT / "docs" / "ci.md"


def _section_keys(text: str, section: str) -> list[str]:
    """The keys directly under a top-level section of action.yml."""
    keys: list[str] = []
    inside = False
    for line in text.splitlines():
        if re.match(r"^\S", line):
            inside = line == f"{section}:"
        elif inside and (match := re.match(r"^  ([a-z][a-z-]*):", line)):
            keys.append(match.group(1))
    return keys


def _run_scripts(text: str) -> list[str]:
    """The body of every run: block in action.yml."""
    scripts: list[str] = []
    lines = text.splitlines()
    for number, line in enumerate(lines):
        match = re.match(r"^(\s*)run: \|\s*$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        body: list[str] = []
        for following in lines[number + 1 :]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        scripts.append("\n".join(body))
    return scripts


def test_a_workflow_step_with_a_pipe_fails_when_a_command_before_the_pipe_fails() -> None:
    # A step with no shell runs as bash -e, which takes a pipeline's last exit code, so pytest | tee passed 49 failures.
    piped = 0
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        if any(re.search(r"(?<!\|)\|(?!\|)", script) for script in _run_scripts(text)):
            piped += 1
            assert "\ndefaults:\n  run:\n    shell: bash\n" in text, workflow.name
    assert piped > 0


def test_the_action_takes_the_inputs_the_docs_list() -> None:
    action = ACTION.read_text(encoding="utf-8")
    inputs = _section_keys(action, "inputs")
    assert inputs == ["urls", "render", "fail-on", "backend", "artifact-name"]
    documented = re.findall(r"^\| `([a-z-]+)` \|", CI_DOCS.read_text(encoding="utf-8"), re.MULTILINE)
    assert set(inputs) | set(_section_keys(action, "outputs")) == set(documented)


def test_no_script_in_the_action_has_an_expression_pasted_into_it() -> None:
    scripts = _run_scripts(ACTION.read_text(encoding="utf-8"))
    assert len(scripts) == 3
    for script in scripts:
        assert "${{" not in script, script


def test_every_action_is_pinned_to_a_commit_with_its_release_in_a_comment() -> None:
    workflows = [ACTION, *sorted((ROOT / ".github" / "workflows").glob("*.yml"))]
    pinned = 0
    for path in workflows:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:- )?uses: (\S+)(.*)$", line)
            if match is None or match.group(1) == "./":
                continue
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", match.group(1)), line
            assert re.fullmatch(r" # v\d+\.\d+\.\d+", match.group(2)), line
            pinned += 1
    assert pinned == 12


def test_the_action_adds_its_summary_to_the_job_summary() -> None:
    (scan,) = [script for script in _run_scripts(ACTION.read_text(encoding="utf-8")) if "--urls-file" in script]
    assert 'arguments+=(--markdown-summary "$GITHUB_STEP_SUMMARY")' in scan


def _scan_to(path: Path, arguments: list[str], port: int) -> None:
    from loupe_scan import cli

    cli.main([*arguments, "--json-output", str(path)])
    # The action's job serves the pages on port 8000; this test serves them wherever it can.
    path.write_text(path.read_text(encoding="utf-8").replace(f"127.0.0.1:{port}/", "127.0.0.1:8000/"), encoding="utf-8")


def test_the_check_ci_runs_on_the_actions_results_passes_on_results_made_here(tmp_path: Path) -> None:
    import subprocess
    import sys

    import pytest

    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        try:
            playwright.chromium.launch(headless=True).close()
        except PlaywrightError as error:
            pytest.skip(f"Chromium for Playwright isn't installed: {str(error).splitlines()[0]}")

    from .fixture_site import serve_fixtures

    never, rendered, missing = (tmp_path / f"{name}.json" for name in ("never", "rendered", "missing"))
    with serve_fixtures(ROOT / "docs") as site:
        port = int(site.base_url.rsplit(":", 1)[1])
        _scan_to(never, [f"{site.base_url}/test-page/", f"{site.base_url}/animated-page/", "--fail-on", "never"], port)
        _scan_to(rendered, [f"{site.base_url}/test-page/", "--render"], port)
        _scan_to(missing, [f"{site.base_url}/no-such-page/", "$(touch${IFS}injected)", "--fail-on", "never"], port)
    script = ROOT / ".github" / "scripts" / "check_action_results.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script), str(never), str(rendered), str(missing)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "The action's results match the test pages.\n"


# What the action installs

LOCKS = ROOT / ".github"
CORE_LOCK = LOCKS / "action-requirements.txt"
RENDER_LOCK = LOCKS / "action-render-requirements.txt"


def _pins(lock: Path) -> dict[str, tuple[str, int]]:
    """Each package a lock pins, by its normalized name: its version and how many hashes it carries."""
    pins: dict[str, tuple[str, int]] = {}
    name = ""
    for line in lock.read_text(encoding="utf-8").splitlines():
        if match := re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line):
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            pins[name] = (match.group(2), 0)
        elif line.strip().startswith("--hash=sha256:") and name:
            version, hashes = pins[name]
            pins[name] = (version, hashes + 1)
    return pins


def _requirements(names: list[str]) -> list[tuple[str, str]]:
    """(normalized name, specifier) for each requirement string."""
    from packaging.requirements import Requirement

    parsed = [Requirement(text) for text in names]
    return [(re.sub(r"[-_.]+", "-", requirement.name).lower(), str(requirement.specifier)) for requirement in parsed]


def _declared(array: str) -> list[str]:
    """The quoted requirements in one of pyproject.toml's arrays, such as dependencies = [...].

    shortcut: reads the arrays with a regular expression, not a TOML parser, since tomllib arrived in Python 3.11
    and loupe supports 3.10; use tomllib once 3.10 is dropped.
    """
    match = re.search(rf"^{re.escape(array)} = \[(.*?)\]$", (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    assert match is not None, array
    return re.findall(r'"([^"]+)"', re.sub(r"#.*", "", match.group(1)))


def test_the_action_installs_only_what_a_lock_pins_by_hash() -> None:
    (install,) = [script for script in _run_scripts(ACTION.read_text(encoding="utf-8")) if "pip install" in script]
    commands = [line.strip() for line in install.splitlines() if "pip install" in line]
    assert commands == [
        '"$venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes --only-binary :all: -r "$lock"',
        '"$venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-deps --no-build-isolation "$GITHUB_ACTION_PATH"',
    ]
    assert 'lock="$GITHUB_ACTION_PATH/.github/action-requirements.txt"' in install
    assert 'lock="$GITHUB_ACTION_PATH/.github/action-render-requirements.txt"' in install


def test_the_locks_pin_every_dependency_within_pyprojects_ranges_and_agree() -> None:
    from packaging.specifiers import SpecifierSet

    core = _requirements(_declared("dependencies")) + _requirements(_declared("requires"))
    render = core + _requirements(_declared("render"))
    for lock, wanted in ((CORE_LOCK, core), (RENDER_LOCK, render)):
        pins = _pins(lock)
        for name, specifier in wanted:
            assert name in pins, (lock.name, name)
            assert SpecifierSet(specifier).contains(pins[name][0]), (lock.name, name, pins[name][0], specifier)
        assert all(hashes > 0 for _, hashes in pins.values()), lock.name
    core_pins, render_pins = _pins(CORE_LOCK), _pins(RENDER_LOCK)
    assert "playwright" not in core_pins
    assert {name: core_pins[name] for name in core_pins} == {name: render_pins[name] for name in core_pins}
