"""Until loupe is published, nothing may tell people to install "loupe" from PyPI: that's a different package."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
# pip install loupe, pip install 'loupe[render]' and the like, but not a longer name such as loupe-scan.
_PYPI_LOUPE = re.compile(r"pip3? install\s+(?:-U\s+|--upgrade\s+)*['\"]?loupe(?![\w-])")
_CHECKED = ("*.md", "*.py", "*.html", "*.yml", "*.yaml", "*.toml", "*.sh", "*.cff")
_SKIPPED = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache"}


def _project_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _CHECKED:
        files.extend(path for path in ROOT.rglob(pattern) if _SKIPPED.isdisjoint(path.relative_to(ROOT).parts))
    return files


def test_the_pattern_catches_the_wrong_instruction_and_allows_the_right_ones() -> None:
    for wrong in ("pip install loupe", "pip install 'loupe[render]'", 'pip3 install -U "loupe"'):
        assert _PYPI_LOUPE.search(wrong), wrong
    for right in ('pip install -e ".[render]"', "pip install loupe-scan", "pip install loupe_scan"):
        assert not _PYPI_LOUPE.search(right), right


def test_no_file_tells_people_to_install_loupe_from_pypi() -> None:
    files = _project_files()
    assert any(path.name == "README.md" for path in files)
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in files
        if path.name != Path(__file__).name
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1)
        if _PYPI_LOUPE.search(line)
    ]
    assert offenders == []
