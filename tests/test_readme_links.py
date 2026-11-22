"""The README is also the project's page on PyPI, where a relative link or image points nowhere."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"
_MARKDOWN_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)")
_REFERENCE_TARGET = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)", re.MULTILINE)
_HTML_TARGET = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ABSOLUTE = re.compile(r"(?:https?://|mailto:)")
# Absolute links into this repository: a file on GitHub, or a raw file an image shows.
_IN_REPOSITORY = re.compile(
    r"https://(?:github\.com/devfrankduah/loupe/blob/main|raw\.githubusercontent\.com/devfrankduah/loupe/main)/([^#?]+)"
)


def _prose(text: str) -> str:
    """text without its fenced code blocks, whose brackets and URLs aren't links."""
    kept: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            kept.append(line)
    return "\n".join(kept)


def _targets(text: str) -> list[str]:
    prose = _prose(text)
    return [
        match.group(1)
        for pattern in (_MARKDOWN_TARGET, _REFERENCE_TARGET, _HTML_TARGET)
        for match in pattern.finditer(prose)
    ]


def test_every_link_and_image_in_the_readme_is_absolute() -> None:
    targets = _targets(README.read_text(encoding="utf-8"))
    assert len(targets) > 30
    assert [target for target in targets if not _ABSOLUTE.match(target)] == []


def test_every_link_into_the_repository_names_a_file_that_exists() -> None:
    paths = [
        match.group(1)
        for target in _targets(README.read_text(encoding="utf-8"))
        if (match := _IN_REPOSITORY.match(target)) is not None
    ]
    assert len(paths) > 15
    assert [path for path in paths if not (ROOT / path).is_file()] == []


def test_images_load_from_raw_files_so_pypi_can_show_them() -> None:
    text = _prose(README.read_text(encoding="utf-8"))
    images = re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text) + re.findall(r"<img[^>]*\ssrc=\"([^\"]+)\"", text)
    assert len(images) >= 5
    for image in images:
        assert image.startswith(("https://raw.githubusercontent.com/", "https://img.shields.io/", "https://github.com/devfrankduah/loupe/actions/")), image


def test_the_patterns_catch_each_kind_of_relative_link() -> None:
    text = (
        "[a](docs/a.md) ![b](docs/b.png) [c](#anchor) <img src=\"c.png\"> <a href='d.md'>d</a>\n"
        "[e]: docs/e.md\n```\n[ignored](inside/a/code/block)\n```\n[f](https://example.com/f)"
    )
    assert _targets(text) == ["docs/a.md", "docs/b.png", "#anchor", "https://example.com/f", "docs/e.md", "c.png", "d.md"]
