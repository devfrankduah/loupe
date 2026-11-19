"""Read the ground-truth tables that sit beside each test page in docs/.

A test page's expected results live in its ground-truth.md, so the page, the
table and the tests can't drift apart: the tests read the table instead of
repeating it.
"""

from __future__ import annotations

import re
from pathlib import Path

_TOTAL = re.compile(r"^- (Markup|Rendered) scan: (\d+) findings, (\d+) instruction-like\.$", re.MULTILINE)


def read_table(path: Path) -> list[dict[str, str]]:
    """Each numbered row of the first table in path, keyed by its column headings."""
    headings: list[str] = []
    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not headings:
            headings = cells
        elif cells[0].isdigit():
            if len(cells) != len(headings):
                raise ValueError(f"{path.name}: row {cells[0]} has {len(cells)} cells, not {len(headings)}")
            rows.append(dict(zip(headings, cells)))
    return rows


def read_totals(path: Path) -> dict[str, tuple[int, int]]:
    """The expected (findings, instruction-like) count for each scan mode."""
    return {
        mode.lower(): (int(findings), int(flagged))
        for mode, findings, flagged in _TOTAL.findall(path.read_text(encoding="utf-8"))
    }
