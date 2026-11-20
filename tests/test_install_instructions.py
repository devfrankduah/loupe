"""Until loupe-scan is published, every install instruction has to point at this repository.

"loupe" on PyPI is a different, unrelated package, so "pip install loupe" installs the wrong thing
for good. "loupe-scan" isn't on PyPI yet, so "pip install loupe-scan" either fails or, if someone
else registers the name first, installs their code. The release work lifts the second check.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
_INSTALL = r"pip3? install\s+(?:-U\s+|--upgrade\s+)*['\"]?"
# pip install loupe, pip install 'loupe[render]' and the like, but not a longer name such as loupe-scan.
_PYPI_LOUPE = re.compile(_INSTALL + r"loupe(?![\w-])")
# The name loupe will be published under, in either spelling pip accepts.
_UNPUBLISHED_LOUPE_SCAN = re.compile(_INSTALL + r"loupe[-_]scan(?![\w-])", re.IGNORECASE)
_CHECKED = ("*.md", "*.py", "*.html", "*.yml", "*.yaml", "*.toml", "*.sh", "*.cff")
_SKIPPED = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


def _project_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _CHECKED:
        files.extend(path for path in ROOT.rglob(pattern) if _SKIPPED.isdisjoint(path.relative_to(ROOT).parts))
    return files


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    files = _project_files()
    assert any(path.name == "README.md" for path in files)
    return [
        f"{path.relative_to(ROOT)}:{number}"
        for path in files
        if path.name != Path(__file__).name
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if pattern.search(line)
    ]


def test_the_patterns_catch_the_wrong_instructions_and_allow_the_right_ones() -> None:
    for wrong in ("pip install loupe", "pip install 'loupe[render]'", 'pip3 install -U "loupe"'):
        assert _PYPI_LOUPE.search(wrong), wrong
    for wrong in ("pip install loupe-scan", "pip install 'loupe_scan[render]'", "pip3 install --upgrade Loupe-Scan"):
        assert _UNPUBLISHED_LOUPE_SCAN.search(wrong), wrong
    for right in ('pip install -e ".[render]"', "pip install -e '.[test,render]'", 'pip install "$GITHUB_ACTION_PATH"'):
        assert not _PYPI_LOUPE.search(right), right
        assert not _UNPUBLISHED_LOUPE_SCAN.search(right), right
    assert not _PYPI_LOUPE.search("pip install loupe-scan")


def test_no_file_tells_people_to_install_loupe_from_pypi() -> None:
    assert _offenders(_PYPI_LOUPE) == []


def test_no_file_tells_people_to_install_loupe_scan_from_pypi_before_it_is_published() -> None:
    assert _offenders(_UNPUBLISHED_LOUPE_SCAN) == []
