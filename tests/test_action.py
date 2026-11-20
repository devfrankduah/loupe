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
