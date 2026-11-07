"""Combine HTML findings with the markdown check and render the result."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from loupe.render import BrowserSource, Landing, RenderedPage, RenderedText, StaticComparison
from loupe.scan import Finding, Kind, ScanMode
from loupe.survival import MarkdownIndex

DEFAULT_QUOTE_CHARACTERS: Final[int] = 160

# Text from fetched pages reaches the terminal through render_text. Control
# characters, including the ESC that starts an ANSI sequence and the 8-bit
# CSI, can move the cursor, clear the screen or rewrite earlier lines, and
# bidirectional overrides can reorder what is shown. Each is printed as an
# escape instead.
_UNPRINTABLE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")


@dataclass(frozen=True)
class CheckedFinding:
    finding: Finding
    # None when the markdown wasn't fetched or the text was too short to compare.
    in_markdown: bool | None


@dataclass
class PageReport:
    url: str
    findings: list[CheckedFinding] = field(default_factory=list)
    html_characters: int = 0
    markdown_characters: int | None = None
    html_error: str | None = None
    markdown_error: str | None = None
    mode: ScanMode = ScanMode.STATIC
    # Set for rendered scans that loaded the page.
    browser: BrowserSource | None = None
    rendered: RenderedPage | None = None

    @property
    def instruction_like(self) -> list[CheckedFinding]:
        return [item for item in self.findings if item.finding.is_instruction_like]

    def count_by_kind(self) -> dict[str, int]:
        counts = Counter(item.finding.kind.value for item in self.findings)
        return {kind.value: counts[kind.value] for kind in Kind if counts[kind.value]}

    def survival_counts(self) -> dict[str, int]:
        return {
            "survived": sum(item.in_markdown is True for item in self.findings),
            "dropped": sum(item.in_markdown is False for item in self.findings),
            "not_compared": sum(item.in_markdown is None for item in self.findings),
        }


def check_findings(findings: list[Finding], markdown: str | None) -> list[CheckedFinding]:
    if markdown is None:
        return [CheckedFinding(finding, None) for finding in findings]
    index = MarkdownIndex(markdown)
    # A removed value can't be compared: only the placeholder is left.
    return [
        CheckedFinding(finding, None if finding.value_removed else index.contains(finding.text))
        for finding in findings
    ]


def quote(text: str, limit: int) -> str:
    """Cut text to at most limit characters, marking the cut. A limit of 0 keeps it whole."""
    if limit < 0:
        raise ValueError("quote limit must not be negative")
    if limit == 0 or len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _survival_label(in_markdown: bool | None) -> str:
    if in_markdown is True:
        return "in markdown"
    if in_markdown is False:
        return "not in markdown"
    return "not compared"


def _text_to_dict(item: RenderedText, quote_limit: int) -> dict[str, object]:
    return {"tag": item.tag, "text": quote(item.text, quote_limit), "text_characters": len(item.text)}


def comparison_counts(comparison: tuple[StaticComparison, ...]) -> dict[str, int]:
    counts = Counter(item.landed for item in comparison)
    return {landing.value: counts[landing] for landing in Landing}


def _comparison_to_dict(item: StaticComparison, quote_limit: int) -> dict[str, object]:
    return {
        "tag": item.tag,
        "detail": item.detail,
        "text": quote(item.text, quote_limit),
        "text_characters": len(item.text),
        "landed": item.landed.value,
    }


def _rendering_to_dict(page: PageReport, quote_limit: int) -> dict[str, object] | None:
    if page.rendered is None:
        return None
    rendered = page.rendered
    return {
        "browser": None if page.browser is None else page.browser.value,
        "browser_version": rendered.browser_version,
        "transfer_bytes": rendered.transfer_bytes,
        "final_url": rendered.final_url,
        "text_elements": rendered.text_elements,
        "scroll_steps": rendered.scroll_steps,
        "reached_bottom": rendered.reached_bottom,
        "centre_checks": rendered.centre_checks,
        "notes": list(rendered.notes),
        "revealed_on_scroll": [_text_to_dict(item, quote_limit) for item in rendered.revealed_on_scroll],
        "static_comparison_counts": comparison_counts(rendered.static_comparison),
        "static_comparison": [_comparison_to_dict(item, quote_limit) for item in rendered.static_comparison],
    }


def page_to_dict(page: PageReport, quote_limit: int = DEFAULT_QUOTE_CHARACTERS) -> dict[str, object]:
    return {
        "url": page.url,
        "scan_mode": page.mode.value,
        "html_characters": page.html_characters,
        "markdown_characters": page.markdown_characters,
        "html_error": page.html_error,
        "markdown_error": page.markdown_error,
        "counts_by_kind": page.count_by_kind(),
        "instruction_like_count": len(page.instruction_like),
        "survival": page.survival_counts(),
        "rendering": _rendering_to_dict(page, quote_limit),
        "findings": [
            {
                "mode": item.finding.mode.value,
                "kind": item.finding.kind.value,
                "detail": item.finding.detail,
                "tag": item.finding.tag,
                "text": quote(item.finding.text, quote_limit),
                "text_characters": len(item.finding.text),
                "instruction_phrases": list(item.finding.instruction_phrases),
                "in_markdown": item.in_markdown,
            }
            for item in page.findings
        ],
    }


def _rendering_lines(rendered: RenderedPage, browser: BrowserSource | None) -> list[str]:
    where = "a browser" if browser is None else browser.description
    if rendered.browser_version:
        where += f" (version {rendered.browser_version})"
    ending = "reached the bottom" if rendered.reached_bottom else "didn't reach the bottom"
    lines = [
        f"  rendered in {where}: {rendered.text_elements} text elements, "
        f"{rendered.scroll_steps} scroll steps, {ending}, {rendered.centre_checks} centred",
        f"  revealed on scroll: {len(rendered.revealed_on_scroll)} (not counted as hidden)",
    ]
    if rendered.transfer_bytes is not None:
        lines.append(f"  transfer: {rendered.transfer_bytes:,} bytes")
    if rendered.static_comparison:
        landings = comparison_counts(rendered.static_comparison)
        counts = ", ".join(f"{landing} {count}" for landing, count in landings.items())
        lines.append(f"  static inline-style candidates: {len(rendered.static_comparison)} ({counts})")
    lines.extend(f"  note: {note}" for note in rendered.notes)
    return lines


def _escape_character(match: re.Match[str]) -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def printable(text: str) -> str:
    """Return text with every control or direction-changing character shown as an escape."""
    return _UNPRINTABLE.sub(_escape_character, text)


def render_text(page: PageReport, quote_limit: int = DEFAULT_QUOTE_CHARACTERS) -> str:
    """The plain-text summary of one page, safe to print to a terminal."""
    return "\n".join(printable(line) for line in _text_lines(page, quote_limit))


def _text_lines(page: PageReport, quote_limit: int) -> list[str]:
    lines = [page.url]
    if page.html_error is not None:
        lines.append(f"  error: {page.html_error}")
        return lines

    markdown_size = "not fetched" if page.markdown_characters is None else f"{page.markdown_characters} chars"
    lines.append(f"  html: {page.html_characters} chars, markdown: {markdown_size}")
    if page.markdown_error is not None:
        lines.append(f"  markdown error: {page.markdown_error}")
    if page.rendered is not None:
        lines.extend(_rendering_lines(page.rendered, page.browser))

    kinds = ", ".join(f"{kind} {count}" for kind, count in page.count_by_kind().items()) or "none"
    lines.append(f"  findings: {len(page.findings)} ({kinds})")
    survival = page.survival_counts()
    lines.append(
        f"  in markdown: {survival['survived']}, not in markdown: {survival['dropped']}, "
        f"not compared: {survival['not_compared']}"
    )

    flagged = page.instruction_like
    lines.append(f"  instruction-like: {len(flagged)}")
    for item in flagged:
        finding = item.finding
        phrases = ", ".join(finding.instruction_phrases)
        lines.append(
            f"    [{finding.kind.value}: {finding.detail}] <{finding.tag}> "
            f"matched {phrases}; {_survival_label(item.in_markdown)}"
        )
        lines.append(f"      {quote(finding.text, quote_limit)}")
    return lines
