"""The canary pages, read from the ground truth beside each one in docs/harness/."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PAGES_DIRECTORY: Final[Path] = Path(__file__).resolve().parent.parent / "docs" / "harness"
_CODE: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}-\d{4}-[A-Z]{2}")
# The control goes last, after every way of hiding the instruction.
CONTROL: Final[str] = "control"


@dataclass(frozen=True)
class CanaryPage:
    slug: str
    code: str
    hidden_by: str

    def url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}/{self.slug}/"


def _fields(ground_truth: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in ground_truth.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 2 and cells[0] != "Field":
            fields[cells[0]] = cells[1]
    return fields


def load_pages(directory: Path = PAGES_DIRECTORY) -> tuple[CanaryPage, ...]:
    """Every canary page under directory, in name order with the control last. Raises ValueError for a bad ground truth."""
    pages: list[CanaryPage] = []
    for ground_truth in sorted(directory.glob("*/ground-truth.md")):
        fields = _fields(ground_truth)
        slug = ground_truth.parent.name
        code = fields.get("Canary code", "")
        if fields.get("Technique") != slug or not _CODE.fullmatch(code) or not fields.get("Hidden by"):
            raise ValueError(f"{ground_truth} doesn't give this page's technique, canary code and how it's hidden")
        pages.append(CanaryPage(slug=slug, code=code, hidden_by=fields["Hidden by"]))
    if not pages:
        raise ValueError(f"no canary pages in {directory}")
    if len({page.code for page in pages}) != len(pages):
        raise ValueError("two canary pages share a code")
    return tuple(sorted(pages, key=lambda page: (page.slug == CONTROL, page.slug)))
