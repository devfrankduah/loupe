"""The animated page in docs/animated-page/, checked against the ground truth written beside it.

Every hidden-text flag the markup scan raises on this page is a false alarm, and
the rendered scan should clear all of them. The rendered half of the check is in
test_render_browser.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from loupe_scan.scan import Finding, Kind, normalize_whitespace, scan_html

from .conftest import FIXTURES
from .ground_truth import read_table, read_totals

PUBLISHED = Path(__file__).resolve().parent.parent / "docs" / "animated-page"
FIXTURE = FIXTURES / "animated-page"
GROUND_TRUTH = PUBLISHED / "ground-truth.md"
NOT_FLAGGED = "not flagged"
CLEARED = "cleared"
NO_TRACE = "none"
HIDDEN_TEXT_KINDS = frozenset({Kind.HIDDEN_ATTRIBUTE, Kind.INLINE_STYLE})


@dataclass(frozen=True)
class Element:
    number: int
    starts_with: str
    markup: str
    rendered: str
    trace: str


def ground_truth() -> list[Element]:
    return [
        Element(int(row["#"]), row["Starts with"], row["Markup scan"], row["Rendered scan"], row["Trace"])
        for row in read_table(GROUND_TRUTH)
    ]


def expected_totals() -> dict[str, tuple[int, int]]:
    return read_totals(GROUND_TRUTH)


def expected_trace() -> dict[str, int]:
    """The trace line's counts: candidates, visible at load, revealed on scroll, still hidden, not matched."""
    match = re.search(
        r"^- Trace: (\d+) candidates, (\d+) visible at load, (\d+) revealed on scroll, (\d+) still hidden, (\d+) not matched\.$",
        GROUND_TRUTH.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, "the ground truth has no trace line"
    names = ("candidates", "visible at load", "revealed on scroll", "still hidden", "not matched")
    return dict(zip(names, map(int, match.groups())))


def matching(findings: list[Finding], element: Element) -> list[Finding]:
    return [finding for finding in findings if finding.text.startswith(element.starts_with)]


def check_markup_scan(findings: list[Finding]) -> None:
    """Every row and the totals of the ground truth for the markup scan."""
    for row in ground_truth():
        flagged = [finding.kind.value for finding in matching(findings, row) if finding.kind in HIDDEN_TEXT_KINDS]
        assert flagged == ([] if row.markup == NOT_FLAGGED else [row.markup]), row.number
    hidden_text_flags = [finding for finding in findings if finding.kind in HIDDEN_TEXT_KINDS]
    assert len(hidden_text_flags) == sum(row.markup != NOT_FLAGGED for row in ground_truth())
    found, instruction_like = expected_totals()["markup"]
    assert len(findings) == found
    assert sum(finding.is_instruction_like for finding in findings) == instruction_like


def check_rendered_scan(findings: list[Finding], traced: list[tuple[str, str]]) -> None:
    """Every row, the totals and the trace for the rendered scan. traced holds (text, where it landed) pairs."""
    for row in ground_truth():
        hidden = [f"{finding.kind.value} ({finding.detail})" for finding in matching(findings, row)]
        assert hidden == ([] if row.rendered in (CLEARED, NOT_FLAGGED) else [row.rendered]), row.number
        landed = [where for text, where in traced if text.startswith(row.starts_with)]
        assert landed == ([] if row.trace == NO_TRACE else [row.trace]), row.number
    found, instruction_like = expected_totals()["rendered"]
    assert len(findings) == found
    assert sum(finding.is_instruction_like for finding in findings) == instruction_like
    trace = expected_trace()
    landings = [where for _, where in traced]
    assert len(landings) == trace["candidates"]
    for outcome_name in ("visible at load", "revealed on scroll", "still hidden", "not matched"):
        assert landings.count(outcome_name) == trace[outcome_name], outcome_name


def test_ground_truth_lists_nine_distinct_elements() -> None:
    rows = ground_truth()
    assert [row.number for row in rows] == list(range(1, 10))
    assert len({row.starts_with for row in rows}) == len(rows)
    assert set(expected_totals()) == {"markup", "rendered"}
    # Every element the markup scan flags must be one the browser clears.
    assert {row.number for row in rows if row.markup != NOT_FLAGGED} == {row.number for row in rows if row.rendered == CLEARED}


def test_the_fixture_is_the_published_page_and_holds_no_instructions() -> None:
    assert (FIXTURE / "index.html").read_bytes() == (PUBLISHED / "index.html").read_bytes()
    page = (FIXTURE / "index.html").read_text(encoding="utf-8")
    text = normalize_whitespace(BeautifulSoup(page, "html.parser").get_text(" "))
    for row in ground_truth():
        assert row.starts_with in text, row.number
    assert all(not finding.is_instruction_like for finding in scan_html(page))


def test_markup_scan_flags_exactly_the_false_alarms_in_the_ground_truth() -> None:
    check_markup_scan(scan_html((FIXTURE / "index.html").read_text(encoding="utf-8")))
