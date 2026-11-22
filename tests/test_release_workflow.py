"""The release workflow: it publishes to PyPI only when a GitHub release is published, with trusted publishing."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return RELEASE.read_text(encoding="utf-8")


def _job(name: str) -> str:
    text = _text()
    start = text.index(f"\n  {name}:\n")
    following = re.search(r"\n  [\w-]+:\n", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def _run_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run: \|$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        block = [following for following in lines[index + 1 :] if following.strip()]
        body: list[str] = []
        for following in block:
            if len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


def test_only_a_published_release_runs_it() -> None:
    text = _text()
    assert "\non:\n  release:\n    types: [published]\n\n" in text
    for trigger in ("push:", "pull_request", "workflow_dispatch", "schedule:", "workflow_run", "workflow_call"):
        assert trigger not in text, trigger


def test_only_the_publish_job_can_ask_for_an_id_token_and_it_runs_in_the_pypi_environment() -> None:
    text = _text()
    assert "\npermissions: {}\n" in text
    assert text.count("id-token: write") == 1
    publish = _job("publish")
    assert "id-token: write" in publish
    assert "    environment:\n      name: pypi\n" in publish
    assert "pypa/gh-action-pypi-publish@" in publish
    assert "needs: build" in publish
    build = _job("build")
    assert "    permissions:\n      contents: read\n" in build
    assert "persist-credentials: false" in build
    assert "twine check --strict dist/*" in build


def test_no_script_has_an_expression_pasted_into_it() -> None:
    blocks = _run_blocks(_text())
    assert len(blocks) == 2
    assert all("${{" not in block for block in blocks)


@pytest.mark.parametrize(("tag", "passes"), [("v0.2.0", True), ("0.2.0", False), ("v0.2.1", False), ("v0.2.0; true", False)])
def test_the_tag_check_passes_only_the_packages_own_version(tag: str, passes: bool) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed")
    from loupe_scan import __version__

    script = next(block for block in _run_blocks(_text()) if "TAG" in block)
    result = subprocess.run(
        [bash, "-e", "-c", script.replace("python -I", "python3 -I")],
        cwd=ROOT,
        env={"TAG": tag.replace("0.2.0", __version__), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
