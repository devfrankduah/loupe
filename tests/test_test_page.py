"""The test page in docs/test-page/, checked against the ground truth written beside it.

The expected results come from the table in docs/test-page/ground-truth.md, so
the page, its ground truth and these tests can't drift apart. The rendered
half of the check is in test_render_browser.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loupe.scan import Finding, Kind, scan_html

from .conftest import FIXTURES

PUBLISHED = Path(__file__).resolve().parent.parent / "docs" / "test-page"
FIXTURE = FIXTURES / "test-page"
GROUND_TRUTH = PUBLISHED / "ground-truth.md"
MISSED = "missed"


@dataclass(frozen=True)
class Planted:
    number: int
    starts_with: str
    markup: str
    rendered: str
    flagged: bool


def ground_truth() -> list[Planted]:
    rows: list[Planted] = []
    for line in GROUND_TRUTH.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 7 and cells[0].isdigit():
            rows.append(Planted(int(cells[0]), cells[3], cells[4], cells[5], {"yes": True, "no": False}[cells[6]]))
    return rows


def expected_totals() -> dict[str, tuple[int, int]]:
    pattern = re.compile(r"^- (Markup|Rendered) scan: (\d+) findings, (\d+) instruction-like\.$", re.MULTILINE)
    return {
        mode.lower(): (int(findings), int(flagged))
        for mode, findings, flagged in pattern.findall(GROUND_TRUTH.read_text(encoding="utf-8"))
    }


def describe(finding: Finding) -> str:
    if finding.kind in (Kind.RENDERED_HIDDEN, Kind.LABEL_ATTRIBUTE):
        return f"{finding.kind.value} ({finding.detail})"
    return f"{finding.kind.value}"


def outcome(findings: list[Finding], item: Planted) -> tuple[str, bool]:
    """What loupe reported for one planted item: how it was found, and whether it was flagged."""
    matches = [finding for finding in findings if finding.text.startswith(item.starts_with)]
    assert len(matches) <= 1, f"row {item.number} was reported {len(matches)} times"
    if not matches:
        return MISSED, False
    return describe(matches[0]), matches[0].is_instruction_like


def expected(item: Planted, found_as: str) -> tuple[str, bool]:
    return found_as, item.flagged and found_as != MISSED


def test_ground_truth_lists_eight_distinct_planted_items() -> None:
    rows = ground_truth()
    assert [row.number for row in rows] == list(range(1, 9))
    assert len({row.starts_with for row in rows}) == len(rows)
    assert set(expected_totals()) == {"markup", "rendered"}


def test_every_planted_item_is_in_the_page_and_the_fixture_is_the_published_page() -> None:
    for name in ("index.html", "blender.svg"):
        assert (FIXTURE / name).read_bytes() == (PUBLISHED / name).read_bytes(), name
    page = (FIXTURE / "index.html").read_text(encoding="utf-8")
    for row in ground_truth():
        # Row 3 is added by a script from base64, so its words never appear in the HTML.
        assert (row.starts_with in page) == (row.number != 3), row.number


def test_markup_scan_matches_the_ground_truth() -> None:
    findings = scan_html((FIXTURE / "index.html").read_text(encoding="utf-8"))
    for row in ground_truth():
        assert outcome(findings, row) == expected(row, row.markup), row.number
    found, flagged = expected_totals()["markup"]
    assert len(findings) == found
    assert sum(finding.is_instruction_like for finding in findings) == flagged
