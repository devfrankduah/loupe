"""Combine HTML findings with the markdown check and render the result."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Final

from loupe.scan import Finding, Kind
from loupe.survival import MarkdownIndex

PREVIEW_CHARACTERS: Final[int] = 160


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
    return [CheckedFinding(finding, index.contains(finding.text)) for finding in findings]


def _preview(text: str) -> str:
    if len(text) <= PREVIEW_CHARACTERS:
        return text
    return text[: PREVIEW_CHARACTERS - 3].rstrip() + "..."


def _survival_label(in_markdown: bool | None) -> str:
    if in_markdown is True:
        return "in markdown"
    if in_markdown is False:
        return "not in markdown"
    return "not compared"


def page_to_dict(page: PageReport) -> dict[str, object]:
    return {
        "url": page.url,
        "html_characters": page.html_characters,
        "markdown_characters": page.markdown_characters,
        "html_error": page.html_error,
        "markdown_error": page.markdown_error,
        "counts_by_kind": page.count_by_kind(),
        "instruction_like_count": len(page.instruction_like),
        "survival": page.survival_counts(),
        "findings": [
            {
                "kind": item.finding.kind.value,
                "detail": item.finding.detail,
                "tag": item.finding.tag,
                "text": item.finding.text,
                "instruction_phrases": list(item.finding.instruction_phrases),
                "in_markdown": item.in_markdown,
            }
            for item in page.findings
        ],
    }


def render_text(page: PageReport) -> str:
    lines = [page.url]
    if page.html_error is not None:
        lines.append(f"  error: {page.html_error}")
        return "\n".join(lines)

    markdown_size = "not fetched" if page.markdown_characters is None else f"{page.markdown_characters} chars"
    lines.append(f"  html: {page.html_characters} chars, markdown: {markdown_size}")
    if page.markdown_error is not None:
        lines.append(f"  markdown error: {page.markdown_error}")

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
        lines.append(f"      {_preview(finding.text)}")
    return "\n".join(lines)
