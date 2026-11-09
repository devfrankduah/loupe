"""Render a results JSON file as one self-contained, plain-language HTML page.

The page loads nothing from the network and runs no JavaScript. Every string
that came from a fetched page (URLs, finding text, error messages) passes
through _escape before it reaches the output, and a Content-Security-Policy
meta tag blocks scripts as a second line of defence.
"""

from __future__ import annotations

import datetime
import html
import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from loupe.render import Landing
from loupe.scan import Kind, ScanMode

REPOSITORY_URL: Final[str] = "https://github.com/devfrankduah/loupe"

# IANA reserves these domains for documentation. Their pages have nothing to
# hide, which makes them a fair baseline for how noisy the scanner is.
CONTROL_HOSTS: Final[frozenset[str]] = frozenset({"example.com", "example.org", "example.net"})


class ReportFormatError(ValueError):
    """The results file isn't in the shape loupe writes."""


def _scan_mode(record: dict[str, object], name: str, where: str) -> ScanMode:
    # Files written before scan modes existed have no mode field and are static.
    value = record.get(name, ScanMode.STATIC.value)
    if value == ScanMode.STATIC.value:
        return ScanMode.STATIC
    if value == ScanMode.RENDERED.value:
        return ScanMode.RENDERED
    raise ReportFormatError(f"{where}.{name} should be \"static\" or \"rendered\"")


@dataclass(frozen=True)
class FindingRecord:
    kind: Kind
    detail: str
    text: str
    instruction_phrases: tuple[str, ...]
    in_markdown: bool | None
    # The length before the results file cut the quote; equal to len(text) when nothing was cut.
    full_length: int
    mode: ScanMode = ScanMode.STATIC

    @property
    def is_complete(self) -> bool:
        return self.full_length <= len(self.text)


@dataclass(frozen=True)
class ComparisonRecord:
    """One element the markup-only scan flagged by its style attribute, and where it landed in a browser."""

    tag: str
    detail: str
    text: str
    landed: Landing


@dataclass(frozen=True)
class RenderingRecord:
    scroll_steps: int
    reached_bottom: bool
    revealed_on_scroll: int
    notes: tuple[str, ...]
    comparison: tuple[ComparisonRecord, ...]


@dataclass(frozen=True)
class PageRecord:
    url: str
    html_error: str | None
    markdown_error: str | None
    markdown_retry_error: str | None
    markdown_retried: bool
    retry_not_compared: int
    findings: tuple[FindingRecord, ...]
    mode: ScanMode = ScanMode.STATIC
    # Set for rendered pages that loaded.
    rendering: RenderingRecord | None = None

    @property
    def host(self) -> str:
        host = urlsplit(self.url).hostname or self.url
        return host.removeprefix("www.")

    @property
    def is_control(self) -> bool:
        return self.host in CONTROL_HOSTS

    @property
    def instruction_like(self) -> tuple[FindingRecord, ...]:
        return tuple(finding for finding in self.findings if finding.instruction_phrases)


@dataclass(frozen=True)
class Tally:
    survived: int = 0
    dropped: int = 0
    not_compared: int = 0

    @property
    def compared(self) -> int:
        return self.survived + self.dropped

    @property
    def total(self) -> int:
        return self.compared + self.not_compared


# Loading


def _field(record: dict[str, object], name: str, where: str) -> object:
    if name not in record:
        raise ReportFormatError(f"{where} is missing {name!r}")
    return record[name]


def _string(record: dict[str, object], name: str, where: str) -> str:
    value = _field(record, name, where)
    if not isinstance(value, str):
        raise ReportFormatError(f"{where}.{name} should be a string")
    return value


def _optional_string(record: dict[str, object], name: str, where: str) -> str | None:
    value = record.get(name)
    if value is not None and not isinstance(value, str):
        raise ReportFormatError(f"{where}.{name} should be a string or null")
    return value


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ReportFormatError(f"{where} should be an object")
    return value


def _list(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ReportFormatError(f"{where} should be a list")
    return value


def _whole_number(record: dict[str, object], name: str, where: str) -> int:
    value = _field(record, name, where)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReportFormatError(f"{where}.{name} should be a whole number")
    return value


def _strings(value: object, where: str) -> tuple[str, ...]:
    items = _list(value, where)
    if not all(isinstance(item, str) for item in items):
        raise ReportFormatError(f"{where} should hold strings")
    return tuple(str(item) for item in items)


def _load_comparison(value: object, where: str) -> ComparisonRecord:
    record = _object(value, where)
    landed = _string(record, "landed", where)
    try:
        landing = Landing(landed)
    except ValueError:
        raise ReportFormatError(f"{where}.landed {landed!r} is not a known outcome") from None
    return ComparisonRecord(
        tag=_string(record, "tag", where),
        detail=_string(record, "detail", where),
        text=_string(record, "text", where),
        landed=landing,
    )


def _load_rendering(value: object, where: str) -> RenderingRecord:
    record = _object(value, where)
    reached_bottom = _field(record, "reached_bottom", where)
    if not isinstance(reached_bottom, bool):
        raise ReportFormatError(f"{where}.reached_bottom should be true or false")
    comparison = _list(record.get("static_comparison", []), f"{where}.static_comparison")
    return RenderingRecord(
        scroll_steps=_whole_number(record, "scroll_steps", where),
        reached_bottom=reached_bottom,
        revealed_on_scroll=len(_list(_field(record, "revealed_on_scroll", where), f"{where}.revealed_on_scroll")),
        notes=_strings(record.get("notes", []), f"{where}.notes"),
        comparison=tuple(
            _load_comparison(item, f"{where}.static_comparison[{index}]") for index, item in enumerate(comparison)
        ),
    )


def _load_finding(value: object, where: str, page_mode: ScanMode) -> FindingRecord:
    record = _object(value, where)
    kind_value = _string(record, "kind", where)
    try:
        kind = Kind(kind_value)
    except ValueError:
        raise ReportFormatError(f"{where}.kind {kind_value!r} is not a known kind") from None
    mode = _scan_mode(record, "mode", where)
    if (kind is Kind.RENDERED_HIDDEN) != (mode is ScanMode.RENDERED):
        raise ReportFormatError(f"{where} pairs kind {kind_value!r} with mode {mode.value!r}")
    if mode is ScanMode.RENDERED and page_mode is not ScanMode.RENDERED:
        raise ReportFormatError(f"{where} is a rendered finding on a page from a static scan")
    phrases = _list(_field(record, "instruction_phrases", where), f"{where}.instruction_phrases")
    if not all(isinstance(phrase, str) for phrase in phrases):
        raise ReportFormatError(f"{where}.instruction_phrases should hold strings")
    in_markdown = record.get("in_markdown")
    if in_markdown is not None and not isinstance(in_markdown, bool):
        raise ReportFormatError(f"{where}.in_markdown should be true, false or null")
    text = _string(record, "text", where)
    full_length = record.get("text_characters", len(text))
    if not isinstance(full_length, int) or isinstance(full_length, bool) or full_length < 0:
        raise ReportFormatError(f"{where}.text_characters should be a whole number")
    return FindingRecord(
        kind=kind,
        detail=_string(record, "detail", where),
        text=text,
        instruction_phrases=tuple(str(phrase) for phrase in phrases),
        in_markdown=in_markdown,
        full_length=full_length,
        mode=mode,
    )


def _load_page(value: object, where: str) -> PageRecord:
    record = _object(value, where)
    mode = _scan_mode(record, "scan_mode", where)
    html_error = _optional_string(record, "html_error", where)
    rendering: RenderingRecord | None = None
    if mode is ScanMode.RENDERED and html_error is None:
        rendering = _load_rendering(_field(record, "rendering", where), f"{where}.rendering")
    findings = _list(_field(record, "findings", where), f"{where}.findings")
    retry_value = record.get("markdown_retry")
    retry = _object(retry_value, f"{where}.markdown_retry") if retry_value is not None else {}
    not_compared = retry.get("not_compared_because_text_was_cut", 0)
    if not isinstance(not_compared, int) or isinstance(not_compared, bool) or not_compared < 0:
        raise ReportFormatError(f"{where}.markdown_retry count should be a whole number")
    return PageRecord(
        url=_string(record, "url", where),
        html_error=html_error,
        markdown_error=_optional_string(record, "markdown_error", where),
        markdown_retry_error=_optional_string(retry, "retry_error", f"{where}.markdown_retry"),
        markdown_retried=retry_value is not None,
        retry_not_compared=not_compared,
        findings=tuple(
            _load_finding(item, f"{where}.findings[{index}]", mode) for index, item in enumerate(findings)
        ),
        mode=mode,
        rendering=rendering,
    )


def load_results(text: str) -> tuple[PageRecord, ...]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ReportFormatError(f"not valid JSON: {error.msg} at line {error.lineno}") from None
    pages = _list(_field(_object(document, "report"), "pages", "report"), "report.pages")
    if not pages:
        raise ReportFormatError("report.pages is empty")
    return tuple(_load_page(page, f"pages[{index}]") for index, page in enumerate(pages))


def run_date_from_filename(path: Path) -> datetime.date | None:
    """The date a results file is named for: 2026-11-02.json, or 2026-11-02-rendered.json."""
    stem = path.stem
    if len(stem) > 10 and stem[10] not in "-_":
        return None
    try:
        return datetime.date.fromisoformat(stem[:10])
    except ValueError:
        return None


# Counting


def tally(findings: tuple[FindingRecord, ...] | list[FindingRecord]) -> Tally:
    counts = Counter(finding.in_markdown for finding in findings)
    return Tally(survived=counts[True], dropped=counts[False], not_compared=counts[None])


def _all_findings(pages: tuple[PageRecord, ...]) -> list[FindingRecord]:
    return [finding for page in pages for finding in page.findings]


def _has_reason(finding: FindingRecord, reason: str) -> bool:
    return finding.kind is Kind.INLINE_STYLE and reason in finding.detail.split(", ")


def _element_hiding(findings: list[FindingRecord]) -> list[FindingRecord]:
    """Findings hidden by the hidden attribute or display:none, the two plainest ways to hide markup."""
    return [
        finding
        for finding in findings
        if finding.kind is Kind.HIDDEN_ATTRIBUTE or _has_reason(finding, "display:none")
    ]


def _how_often(part: int, whole: int) -> str:
    if whole == 0:
        return "could not be checked"
    if part == 0:
        return "never"
    if part == whole:
        return "always"
    return "mostly" if part * 2 > whole else "sometimes"


# Grouping by how the text was hidden


@dataclass(frozen=True)
class HidingMethod:
    name: str
    meaning: str
    matches: Callable[[FindingRecord], bool]


def _is_faded(finding: FindingRecord) -> bool:
    return _has_reason(finding, "opacity:0") and not _has_reason(finding, "display:none")


def _is_element_hidden(finding: FindingRecord) -> bool:
    return finding.kind is Kind.HIDDEN_ATTRIBUTE or _has_reason(finding, "display:none")


# Order matters: each finding is counted under the first method it matches.
HIDING_METHODS: Final[tuple[HidingMethod, ...]] = (
    HidingMethod(
        "Marked hidden, or set to display:none",
        "The page tells the browser not to draw this part at all.",
        _is_element_hidden,
    ),
    HidingMethod(
        "Faded to invisible",
        "Set to zero opacity. Often the first frame of an animation that fades in later.",
        _is_faded,
    ),
    HidingMethod(
        "Other hiding styles",
        "Zero-size text, visibility:hidden, or pushed far off the screen.",
        lambda finding: finding.kind is Kind.INLINE_STYLE,
    ),
    HidingMethod(
        "Hidden from screen readers",
        "aria-hidden. Sighted readers usually still see this text.",
        lambda finding: finding.kind is Kind.ARIA_HIDDEN,
    ),
    HidingMethod(
        "Image and link descriptions",
        "alt, title and aria-label text, shown on hover or read aloud, if at all.",
        lambda finding: finding.kind is Kind.LABEL_ATTRIBUTE,
    ),
    HidingMethod(
        "Page information",
        "Meta tags written for search engines and link previews.",
        lambda finding: finding.kind is Kind.META,
    ),
    HidingMethod(
        "HTML comments",
        "Notes left in the page source. Browsers never show them.",
        lambda finding: finding.kind is Kind.COMMENT,
    ),
    HidingMethod(
        "noscript blocks",
        "Shown only to visitors with JavaScript turned off.",
        lambda finding: finding.kind is Kind.NOSCRIPT,
    ),
)


def group_by_method(findings: list[FindingRecord]) -> list[tuple[HidingMethod, list[FindingRecord]]]:
    groups: dict[str, list[FindingRecord]] = {method.name: [] for method in HIDING_METHODS}
    for finding in findings:
        for method in HIDING_METHODS:
            if method.matches(finding):
                groups[method.name].append(finding)
                break
    return [(method, groups[method.name]) for method in HIDING_METHODS if groups[method.name]]


def choose_example(pages: tuple[PageRecord, ...]) -> tuple[PageRecord, FindingRecord] | None:
    """Pick the clearest real case of element-hidden text that reached the markdown.

    A complete quote beats a cut one, an instruction-like one beats a plain
    one, and a longer one beats a shorter one. Ties keep page order.
    """
    candidates = [
        (page, finding)
        for page in pages
        for finding in page.findings
        if finding.in_markdown is True and _is_element_hidden(finding)
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda pair: (pair[1].is_complete, bool(pair[1].instruction_phrases), len(pair[1].text)),
    )


# Rendering


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _safe_href(url: str) -> str | None:
    return _escape(url) if urlsplit(url).scheme in ("http", "https") else None


# Long enough for "/wiki/Prompt_injection"; longer paths lose their middle.
CARD_PATH_CHARACTERS: Final[int] = 24


def shorten_middle(text: str, limit: int) -> str:
    """Keep the start and end of text, joined by an ellipsis, so it fits in limit characters."""
    if limit < 3:
        raise ValueError("limit must leave room for both ends")
    if len(text) <= limit:
        return text
    keep = limit - 1
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}\u2026{text[-tail:]}"


def _page_label(page: PageRecord) -> str:
    """The card heading: the domain in full, then the path cut down in the middle."""
    host = f'<span class="card-host">{_escape(page.host)}</span>'
    path = urlsplit(page.url).path.rstrip("/")
    if not path:
        return host
    return f'{host}<span class="card-path">{_escape(shorten_middle(path, CARD_PATH_CHARACTERS))}</span>'


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.2f}%" if whole else "0%"


def _how_hidden(finding: FindingRecord) -> str:
    if finding.kind is Kind.HIDDEN_ATTRIBUTE:
        return "sits in an element marked <code>hidden</code>"
    return "sits in an element styled <code>display:none</code>"


def _example_panel(pages: tuple[PageRecord, ...]) -> str:
    chosen = choose_example(pages)
    if chosen is None:
        return (
            '<section class="example" aria-labelledby="example-title">'
            '<h2 id="example-title">One real example</h2>'
            "<p>No text hidden with the hidden attribute or display:none reached the markdown in this run.</p>"
            "</section>"
        )
    page, finding = chosen
    quote = _escape(finding.text)
    cut_note = "" if finding.is_complete else " The quote is cut short, as it is in the saved results."
    skeleton_top = '<span class="line l1"></span><span class="line l2"></span>'
    skeleton_bottom = '<span class="line l3"></span><span class="line l4"></span>'
    return (
        '<section class="example" aria-labelledby="example-title">'
        '<h2 id="example-title" class="vh">One real example</h2>'
        '<div class="sides">'
        '<figure class="side">'
        "<figcaption>What a person sees</figcaption>"
        '<div class="screen">'
        f'<div aria-hidden="true">{skeleton_top}</div>'
        '<p class="hole">Nothing shows here.</p>'
        f'<div aria-hidden="true">{skeleton_bottom}</div>'
        "</div></figure>"
        '<figure class="side ai">'
        "<figcaption>What an AI reads</figcaption>"
        '<div class="screen">'
        f'<div aria-hidden="true">{skeleton_top}</div>'
        f'<p class="revealed"><mark>{quote}</mark></p>'
        f'<div aria-hidden="true">{skeleton_bottom}</div>'
        "</div></figure>"
        "</div>"
        f'<p class="example-source">A real line from {_escape(page.host)}. It {_how_hidden(finding)}, so the browser '
        "never draws it, yet it was still in the markdown version of the page that an AI agent is given."
        f"{cut_note}</p>"
        "</section>"
    )


def _method_row(method: HidingMethod, findings: list[FindingRecord]) -> str:
    counts = tally(findings)
    if counts.compared:
        result = (
            f'<span class="ratio"><b>{counts.survived}</b> of {counts.compared} reached the AI</span>'
            f'<span class="meter" aria-hidden="true"><span class="fill" '
            f'style="width:{_percent(counts.survived, counts.compared)}"></span></span>'
        )
    else:
        result = '<span class="ratio">Too short to compare</span>'
    return (
        '<li class="method">'
        f'<div class="method-text"><span class="method-name">{method.name}</span>'
        f'<span class="method-meaning">{method.meaning}</span></div>'
        f'<div class="method-result">{result}</div>'
        "</li>"
    )


def _result_section(pages: tuple[PageRecord, ...]) -> str:
    findings = _all_findings(pages)
    groups = group_by_method(findings)
    hidden = tally([finding for finding in findings if _is_element_hidden(finding)])
    comments = tally([finding for finding in findings if finding.kind is Kind.COMMENT])
    rows = "".join(_method_row(method, of_method) for method, of_method in groups)
    return (
        '<section class="result" aria-labelledby="result-title">'
        f'<h2 id="result-title">Text marked hidden {_how_often(hidden.survived, hidden.compared)} reached the AI: '
        f"{hidden.survived} of {hidden.compared}. HTML comments {_how_often(comments.survived, comments.compared)} "
        f"did: {comments.survived} of {comments.compared}.</h2>"
        '<p class="measure">Each row is one way a page can hide text. The bar is the share that was still there '
        "in the markdown, out of the findings long enough to compare fairly.</p>"
        f'<ul class="methods">{rows}</ul>'
        "</section>"
    )


def _strip(counts: Tally) -> str:
    segments = (("reached", counts.survived), ("missed", counts.dropped), ("unchecked", counts.not_compared))
    spans = "".join(
        f'<span class="seg {name}" style="width:{_percent(value, counts.total)}"></span>'
        for name, value in segments
        if value
    )
    return f'<span class="strip" aria-hidden="true">{spans}</span>'


def _card_title(page: PageRecord) -> str:
    label = _page_label(page)
    href = _safe_href(page.url)
    full_url = _escape(page.url)
    return f'<a href="{href}" title="{full_url}">{label}</a>' if href else f'<span title="{full_url}">{label}</span>'


def _page_card(page: PageRecord) -> str:
    title = _card_title(page)
    parts = [f'<li class="card{" control" if page.is_control else ""}"><h3>{title}</h3>']
    if page.is_control:
        parts.append(
            '<p class="control-note">The control: a reserved example page with nothing to hide, '
            "so anything found here is background noise.</p>"
        )
    if page.html_error is not None:
        parts.append(f'<p class="card-note">The HTML could not be fetched: {_escape(page.html_error)}</p></li>')
        return "".join(parts)

    counts = tally(page.findings)
    orders = len(page.instruction_like)
    parts.append(
        f'<p class="card-stats"><b>{len(page.findings)}</b> hidden findings, '
        f'<b>{orders}</b> {"sounds" if orders == 1 else "sound"} like an order to an AI</p>'
    )
    parts.append(_strip(counts))
    parts.append(
        f'<p class="card-counts">{counts.survived} reached the AI, {counts.dropped} did not, '
        f"{counts.not_compared} not compared</p>"
    )
    if page.markdown_error is not None:
        parts.append(f'<p class="card-note">The markdown could not be fetched: {_escape(page.markdown_error)}</p>')
    elif page.markdown_retried:
        note = "The markdown fetch failed once and worked on a single retry."
        if page.retry_not_compared:
            note += f" {page.retry_not_compared} longer findings couldn't be compared after it."
        parts.append(f'<p class="card-note">{note}</p>')
    parts.append("</li>")
    return "".join(parts)


def _pages_section(pages: tuple[PageRecord, ...]) -> str:
    cards = "".join(_page_card(page) for page in pages)
    return (
        '<section aria-labelledby="pages-title">'
        '<h2 id="pages-title">Page by page</h2>'
        '<ul class="legend">'
        '<li><span class="swatch reached"></span>Reached the AI</li>'
        '<li><span class="swatch missed"></span>Did not</li>'
        '<li><span class="swatch unchecked"></span>Not compared (too short, or no markdown)</li>'
        "</ul>"
        f'<ul class="cards">{cards}</ul>'
        "</section>"
    )


def _method_section(with_rendering: bool = False) -> str:
    rendering_step = (
        "<li>For a rendered scan it also loads the page in Chromium, scrolls from top to bottom, brings anything "
        "still hidden to the middle of the screen, and records which text a reader could actually see.</li>"
        if with_rendering
        else ""
    )
    return (
        '<section aria-labelledby="method-title">'
        '<h2 id="method-title">How loupe checks a page</h2>'
        '<ol class="process">'
        "<li>It asks Bright Data for the page twice: once as raw HTML, once as the markdown an AI agent is "
        "usually given. These are the same two requests Bright Data's own agent tools make.</li>"
        "<li>It reads the HTML for anything a person won't see: comments, hidden elements, image and link "
        "descriptions, meta tags and noscript blocks. It also notes text that sounds like an order to an AI.</li>"
        "<li>For each hidden piece, it checks whether the same words, in the same order, are in the markdown.</li>"
        f"{rendering_step}"
        "</ol>"
        "</section>"
    )


# Markup-only and rendered, side by side

_LANDING_LABELS: Final[tuple[tuple[Landing, str, str], ...]] = (
    (Landing.VISIBLE_AT_LOAD, "shown", "showed as the page loaded"),
    (Landing.REVEALED_ON_SCROLL, "later", "showed after scrolling"),
    (Landing.STILL_HIDDEN, "never", "never showed"),
    (Landing.NOT_MATCHED, "unmatched", "not found in the live page"),
)


def _url_key(url: str) -> str:
    return url.rstrip("/")


def _stat(value: int, meaning: str) -> str:
    return f"<div><dt>{meaning}</dt><dd>{value}</dd></div>"


def _markup_side(page: PageRecord | None) -> str:
    heading = "<h4>Markup only</h4>"
    if page is None:
        return f'<div class="pair-side">{heading}<p class="card-note">No markup-only scan of this page here.</p></div>'
    if page.html_error is not None:
        return (
            f'<div class="pair-side">{heading}'
            f'<p class="card-note">The HTML could not be fetched: {_escape(page.html_error)}</p></div>'
        )
    styled = sum(finding.kind in (Kind.HIDDEN_ATTRIBUTE, Kind.INLINE_STYLE) for finding in page.findings)
    stats = _stat(styled, "Hidden by a style or the hidden attribute") + _stat(
        len(page.instruction_like), "Sound like an order to an AI"
    )
    return f'<div class="pair-side">{heading}<dl class="pair-stats">{stats}</dl></div>'


def _rendered_side(page: PageRecord) -> str:
    heading = "<h4>In a browser</h4>"
    if page.rendering is None:
        reason = _escape(page.html_error or "no rendering details were saved")
        return (
            f'<div class="pair-side browser">{heading}'
            f'<p class="card-note">The page could not be rendered: {reason}</p></div>'
        )
    never_shown = sum(finding.kind is Kind.RENDERED_HIDDEN for finding in page.findings)
    stats = (
        _stat(never_shown, "Never shown")
        + _stat(page.rendering.revealed_on_scroll, "Shown only after scrolling")
        + _stat(len(page.instruction_like), "Sound like an order to an AI")
    )
    return f'<div class="pair-side browser">{heading}<dl class="pair-stats">{stats}</dl></div>'


def _trace(comparison: tuple[ComparisonRecord, ...]) -> str:
    if not comparison:
        return '<p class="card-note">The markup had no inline-style guesses to trace.</p>'
    counts = Counter(item.landed for item in comparison)
    total = len(comparison)
    spans = "".join(
        f'<span class="seg {css}" style="width:{_percent(counts[landing], total)}"></span>'
        for landing, css, _ in _LANDING_LABELS
        if counts[landing]
    )
    rows = "".join(
        f'<li><span class="swatch {css}"></span><b>{counts[landing]}</b> {label}</li>'
        for landing, css, label in _LANDING_LABELS
    )
    labels = {landing: label for landing, _, label in _LANDING_LABELS}
    items = "".join(
        f'<li><q class="page-text">{_escape(item.text)}</q>'
        f'<span class="match-where">&lt;{_escape(item.tag)}&gt; {_escape(item.detail)}: '
        f"{labels[item.landed]}</span></li>"
        for item in comparison
    )
    guesses = "guess" if total == 1 else "guesses"
    return (
        f"<h4>Where the {total} inline-style {guesses} landed</h4>"
        f'<span class="strip" aria-hidden="true">{spans}</span>'
        f'<ul class="trace">{rows}</ul>'
        '<details class="trace-detail"><summary>Every element, one line each</summary>'
        f'<ul class="matches">{items}</ul></details>'
    )


def _pair_card(markup: PageRecord | None, rendered: PageRecord) -> str:
    parts = [
        f'<li class="pair"><h3>{_card_title(rendered)}</h3>',
        f'<div class="pair-sides">{_markup_side(markup)}{_rendered_side(rendered)}</div>',
    ]
    if rendered.rendering is not None:
        parts.append(_trace(rendered.rendering.comparison))
        # Notes are written to follow "note:" in the terminal, so they start in lower case.
        notes = (note[:1].upper() + note[1:] for note in rendered.rendering.notes)
        parts.extend(f'<p class="card-note">{_escape(note)}</p>' for note in notes)
    parts.append("</li>")
    return "".join(parts)


def _rendered_section(static_pages: tuple[PageRecord, ...], rendered_pages: tuple[PageRecord, ...]) -> str:
    markup_by_url: dict[str, PageRecord] = {}
    for page in static_pages:
        markup_by_url.setdefault(_url_key(page.url), page)
    pairs = "".join(_pair_card(markup_by_url.get(_url_key(page.url)), page) for page in rendered_pages)
    return (
        '<section aria-labelledby="rendered-title">'
        '<h2 id="rendered-title">What changes when a browser draws the page</h2>'
        '<p class="measure">The markup-only scan guesses from style attributes and the hidden attribute. The '
        "rendered scan loads each page in Chromium, scrolls from top to bottom, and records which text a reader "
        "could actually see.</p>"
        f'<ul class="pairs">{pairs}</ul>'
        "</section>"
    )


def _caveats(pages: tuple[PageRecord, ...], with_rendering: bool = False) -> str:
    findings = _all_findings(pages)
    matches = [(page, finding) for page in pages for finding in page.instruction_like]
    inline = [finding for finding in findings if finding.kind is Kind.INLINE_STYLE]
    faded_by_page = Counter(page.host for page in pages for finding in page.findings if _is_faded(finding))
    faded_total = sum(faded_by_page.values())

    match_items = "".join(
        f'<li><q class="page-text">{_escape(finding.text)}</q>'
        f'<span class="match-where">{_escape(page.host)}, matched '
        f"&ldquo;{_escape(', '.join(finding.instruction_phrases))}&rdquo;</span></li>"
        for page, finding in matches
    )
    items = [
        "<li><h3>Sounding like an order isn't the same as being one</h3>"
        "<p>loupe flags hidden text containing phrases such as &ldquo;you are&rdquo; or &ldquo;assistant&rdquo;. "
        f"That caught {len(matches)} pieces of text in this run. Here they are, so you can judge them:</p>"
        f'<ul class="matches">{match_items}</ul></li>'
    ]
    if faded_total:
        busiest_host, busiest_count = faded_by_page.most_common(1)[0]
        items.append(
            "<li><h3>Some &ldquo;hidden&rdquo; text is only waiting to fade in</h3>"
            f"<p>{faded_total} of the {len(inline)} findings hidden by inline style are set to zero opacity, "
            f"{busiest_count} of them on {_escape(busiest_host)}. That's how scroll-in animations start: the text "
            "arrives invisible and JavaScript fades it in, so people do see it. "
            + (
                "The markup-only scan can't tell the difference; the rendered scan above traces each of them."
                if with_rendering
                else "loupe doesn't run JavaScript, so it can't tell the difference."
            )
            + "</p></li>"
        )
    items.append(
        "<li><h3>The two versions come from two services</h3>"
        "<p>Bright Data serves the HTML through Web Unlocker and the markdown through its Fetch API. If they "
        "received slightly different copies of a page, a difference could come from that and not from the "
        "markdown conversion.</p></li>"
    )
    items.append(
        "<li><h3>&ldquo;Reached the AI&rdquo; means the words appear in the markdown</h3>"
        "<p>It doesn't prove they got there from the hidden element. A link's description often repeats the "
        "link's visible text, so it would match either way.</p></li>"
    )
    return (
        '<section aria-labelledby="caveats-title">'
        "<h2 id=\"caveats-title\">What I'm not sure about</h2>"
        f'<ul class="caveats">{"".join(items)}</ul>'
        "</section>"
    )


def _glossary(with_rendering: bool = False) -> str:
    rendered_term = (
        (
            (
                "Rendered scan",
                "A check that loads the page in a real browser, runs its styles and scripts, scrolls through it, "
                "and records which text was ever drawn where a reader could see it.",
            ),
        )
        if with_rendering
        else ()
    )
    terms = (
        (
            "HTML comment",
            "A note in a page's source code, written between <code>&lt;!--</code> and <code>--&gt;</code>. "
            "Browsers skip it, but anything reading the raw source sees it.",
        ),
        (
            "aria-hidden",
            "A marker that tells screen readers to skip an element. It hides nothing from people looking "
            "at the screen.",
        ),
        (
            "Markdown",
            "A plain-text format with light symbols for headings, links and lists. Agent tools often turn a "
            "page into markdown before giving it to a model, because it's much shorter than HTML.",
        ),
        (
            "Prompt injection",
            "Text written to make an AI model follow instructions its user never gave, such as "
            "&ldquo;ignore your previous instructions&rdquo;. Hiding it on a web page is one way to deliver it.",
        ),
    )
    entries = "".join(f"<div><dt>{term}</dt><dd>{meaning}</dd></div>" for term, meaning in terms + rendered_term)
    return (
        '<section aria-labelledby="glossary-title">'
        '<h2 id="glossary-title">Words used here</h2>'
        f'<dl class="glossary">{entries}</dl>'
        "</section>"
    )


_STYLE: Final[str] = """
:root {
  color-scheme: light;
  --paper: #f6f2e9; --sheet: #fffdf8; --ink: #1d1a15; --ink-2: #47423a; --muted: #5f594e;
  --rule: #d8d0bf; --faint: #e9e3d6; --accent: #5a2fd1; --accent-ink: #4a23b8; --wash: #e6defb;
  --missed: #6e675b; --unchecked: #ddd6c7; --focus: #5a2fd1;
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --step-0: clamp(1rem, 0.96rem + 0.2vw, 1.125rem);
  --step-1: clamp(1.2rem, 1.1rem + 0.5vw, 1.45rem);
  --step-2: clamp(1.45rem, 1.2rem + 1.2vw, 2.05rem);
  --step-3: clamp(2.1rem, 1.5rem + 3vw, 3.6rem);
  --measure: 38rem;
}
:root:has(#theme:checked) {
  color-scheme: dark;
  --paper: #18161e; --sheet: #221f2a; --ink: #eeeaf3; --ink-2: #cdc7d8; --muted: #a9a2b6;
  --rule: #3d3849; --faint: #2a2633; --accent: #b9a5ff; --accent-ink: #cbbcff; --wash: #3b2f68;
  --missed: #8f88a0; --unchecked: #3a3545; --focus: #cbbcff;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--paper); color: var(--ink); font: var(--step-0)/1.65 var(--serif);
  font-variant-numeric: lining-nums proportional-nums; }
@media (prefers-reduced-motion: no-preference) {
  body { transition: background-color 160ms ease-out, color 160ms ease-out; }
}
a { color: var(--accent-ink); text-decoration-thickness: 1px; text-underline-offset: 0.18em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }
code { font: 0.86em/1.4 var(--mono); overflow-wrap: anywhere; }
h1, h2, h3 { text-wrap: balance; }
.vh, .theme-input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%);
  white-space: nowrap; margin: 0; }
.page { max-width: 68rem; margin: 0 auto; padding: 0 1rem; }
@media (min-width: 48rem) { .page { padding: 0 2rem; } }

.skip { position: absolute; left: 1rem; top: -4rem; padding: 0.6rem 0.9rem; background: var(--sheet); color: var(--ink); }
.skip:focus { top: 0.5rem; }

.masthead { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.5rem 1rem;
  padding: 0.5rem 0; border-bottom: 1px solid var(--rule); }
.wordmark { font-size: 1.25rem; font-weight: 700; font-style: italic; color: var(--ink); text-decoration: none;
  letter-spacing: -0.01em; min-height: 44px; display: inline-flex; align-items: center; }
.masthead-links { display: flex; align-items: center; gap: 0.25rem 1rem; }
.masthead-links a { min-height: 44px; display: inline-flex; align-items: center; color: var(--ink-2); }
.theme-toggle { min-height: 44px; display: inline-flex; align-items: center; gap: 0.5rem; padding: 0 0.85rem;
  border: 1px solid var(--missed); border-radius: 999px; cursor: pointer; color: var(--ink); background: var(--sheet);
  font-size: 0.9375rem; }
.theme-toggle:hover { border-color: var(--ink); }
:root:has(#theme:focus-visible) .theme-toggle { outline: 2px solid var(--focus); outline-offset: 3px; }
.theme-toggle svg { width: 1.05rem; height: 1.05rem; fill: none; stroke: currentColor; stroke-width: 1.8;
  stroke-linecap: round; stroke-linejoin: round; }
.theme-toggle .when-light, .theme-toggle .when-dark { align-items: center; gap: 0.45rem; }
.theme-toggle .when-light { display: inline-flex; }
.theme-toggle .when-dark { display: none; }
:root:has(#theme:checked) .theme-toggle .when-light { display: none; }
:root:has(#theme:checked) .theme-toggle .when-dark { display: inline-flex; }

.intro { padding: clamp(2rem, 1rem + 5vw, 4.5rem) 0 clamp(1.5rem, 1rem + 2vw, 2.5rem); }
h1 { font-size: var(--step-3); line-height: 1.06; letter-spacing: -0.022em; font-weight: 700;
  max-width: 17em; margin: 0 0 1.25rem; }
.lede { font-size: var(--step-1); line-height: 1.5; color: var(--ink-2); max-width: var(--measure); margin: 0 0 1rem; }
.byline { margin: 0; color: var(--muted); font-style: italic; }

.example { margin: 0 0 clamp(3rem, 2rem + 4vw, 5.5rem); }
.sides { display: grid; gap: 1rem; }
@media (min-width: 48rem) { .sides { grid-template-columns: 1fr 1fr; gap: 1.5rem; } }
.side { margin: 0; }
.side figcaption { font-size: var(--step-1); font-weight: 700; margin: 0 0 0.6rem; }
.side.ai figcaption { color: var(--accent-ink); }
.screen { background: var(--sheet); border: 1px solid var(--rule); border-radius: 10px;
  padding: clamp(1rem, 0.8rem + 1vw, 1.5rem); min-height: 15rem; display: flex; flex-direction: column; gap: 0.7rem; }
.side.ai .screen { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.line { display: block; height: 0.6rem; border-radius: 3px; background: var(--faint); margin: 0 0 0.55rem; }
.line.l1 { width: 92%; } .line.l2 { width: 74%; } .line.l3 { width: 86%; } .line.l4 { width: 58%; }
.hole { margin: 0; flex: 1; min-height: 4.5rem; display: flex; align-items: center; justify-content: center;
  border: 2px dashed var(--missed); border-radius: 6px; color: var(--muted); font-style: italic; font-size: 0.95rem; }
.revealed { margin: 0; flex: 1; min-height: 4.5rem; display: flex; align-items: center; padding: 0.75rem 0.9rem;
  border-radius: 6px; background: var(--wash); }
.revealed mark { background: none; color: var(--ink); font: 500 clamp(0.95rem, 0.9rem + 0.3vw, 1.08rem)/1.5 var(--mono);
  overflow-wrap: anywhere; }
.example-source { max-width: var(--measure); margin: 1rem 0 0; color: var(--ink-2); }

section { margin: 0 0 clamp(3rem, 2rem + 4vw, 5rem); }
h2 { font-size: var(--step-2); line-height: 1.18; letter-spacing: -0.012em; font-weight: 700;
  max-width: 24em; margin: 0 0 0.9rem; }
h3 { font-size: var(--step-0); line-height: 1.35; font-weight: 700; margin: 0 0 0.35rem; }
.measure { max-width: var(--measure); margin: 0 0 1.5rem; color: var(--ink-2); }

.methods { list-style: none; margin: 0; padding: 0; max-width: 52rem; border-top: 2px solid var(--ink); }
.method { display: grid; gap: 0.4rem 2rem; padding: 0.95rem 0; border-bottom: 1px solid var(--rule); }
@media (min-width: 48rem) { .method { grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); align-items: center; } }
.method-name { display: block; font-weight: 700; }
.method-meaning { display: block; color: var(--muted); font-size: 0.94rem; line-height: 1.5; }
.method-result { display: grid; gap: 0.35rem; }
.ratio { font-variant-numeric: lining-nums tabular-nums; }
.ratio b { font-size: 1.2em; }
.meter { display: block; height: 0.9rem; border-radius: 3px; border: 1.5px solid var(--ink-2); background: transparent;
  overflow: hidden; }
.meter .fill { display: block; height: 100%; background: var(--accent); }

.legend { display: flex; flex-wrap: wrap; gap: 0.4rem 1.4rem; list-style: none; margin: 0 0 1.1rem; padding: 0;
  color: var(--ink-2); font-size: 0.95rem; }
.legend li { display: inline-flex; align-items: center; gap: 0.5rem; }
.swatch { display: inline-block; width: 0.95rem; height: 0.95rem; border-radius: 3px; }
.swatch.reached, .seg.reached { background: var(--accent); }
.swatch.missed, .seg.missed { background: var(--missed); }
.swatch.unchecked, .seg.unchecked { background: var(--unchecked); }
.cards { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.9rem; }
@media (min-width: 40rem) { .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (min-width: 64rem) { .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
.card { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 1rem 1.1rem 1.1rem; }
.card.control { background: transparent; border-style: dashed; }
.card h3 a { color: var(--ink); display: inline-flex; flex-direction: column; justify-content: center;
  min-height: 44px; max-width: 100%; }
.card-host, .card-path { display: block; max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-path { font-weight: 400; color: var(--ink-2); font-size: 0.94rem; }
.control-note, .card-note { margin: 0.35rem 0 0.6rem; color: var(--muted); font-size: 0.92rem; line-height: 1.5; }
.card-note { margin: 0.6rem 0 0; }
.card-stats { margin: 0.2rem 0 0.6rem; color: var(--ink-2); font-variant-numeric: lining-nums tabular-nums; }
.card-stats b { color: var(--ink); font-size: 1.15em; }
.strip { display: flex; height: 0.7rem; border-radius: 3px; overflow: hidden; background: var(--unchecked); }
.seg { display: block; height: 100%; }
.card-counts { margin: 0.45rem 0 0; font-size: 0.92rem; color: var(--ink-2); font-variant-numeric: lining-nums tabular-nums; }

.process { max-width: var(--measure); margin: 0; padding-left: 1.4rem; }
.process li { margin: 0 0 0.7rem; padding-left: 0.3rem; }
.process li::marker { font-weight: 700; color: var(--accent-ink); }

.caveats { list-style: none; margin: 0; padding: 0; max-width: 44rem; }
.caveats > li { padding: 1rem 0; border-top: 1px solid var(--rule); }
.caveats p { margin: 0; color: var(--ink-2); }
.matches { list-style: none; margin: 0.9rem 0 0; padding: 0; display: grid; gap: 0.5rem; }
.matches li { padding: 0.6rem 0.8rem; background: var(--sheet); border: 1px solid var(--rule); border-radius: 6px; }
.page-text { display: block; font: 0.9rem/1.5 var(--mono); color: var(--ink); overflow-wrap: anywhere; }
.match-where { display: block; margin-top: 0.2rem; font-size: 0.88rem; color: var(--muted); font-style: italic; }

.glossary { margin: 0; max-width: 44rem; display: grid; gap: 0; }
.glossary div { padding: 0.85rem 0; border-top: 1px solid var(--rule); }
@media (min-width: 48rem) { .glossary div { display: grid; grid-template-columns: 11rem minmax(0, 1fr); gap: 1.5rem; } }
.glossary dt { font-weight: 700; }
.glossary dd { margin: 0; color: var(--ink-2); }

.colophon { border-top: 1px solid var(--rule); padding: 1.5rem 0 2.5rem; color: var(--muted); font-size: 0.92rem; }
.colophon p { margin: 0 0 0.4rem; max-width: var(--measure); }
"""

# Only pages with rendered results carry these rules, so a markup-only page is unchanged.
_RENDERED_STYLE: Final[str] = """
:root { --later: #a891f0; }
:root:has(#theme:checked) { --later: #6f5bb8; }
h4 { font-size: 0.98rem; line-height: 1.35; font-weight: 700; margin: 1rem 0 0.5rem; }
.pairs { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; max-width: 52rem; }
.pair { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 1rem 1.1rem 1.1rem; }
.pair h3 a { color: var(--ink); display: inline-flex; flex-direction: column; justify-content: center;
  min-height: 44px; max-width: 100%; }
.pair-sides { display: grid; gap: 0.8rem; margin-top: 0.4rem; }
@media (min-width: 40rem) { .pair-sides { grid-template-columns: 1fr 1fr; } }
.pair-side { border: 1px solid var(--rule); border-radius: 6px; padding: 0.7rem 0.85rem; }
.pair-side.browser { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.pair-side h4 { margin: 0 0 0.4rem; }
.pair-side.browser h4 { color: var(--accent-ink); }
.pair-stats { margin: 0; display: grid; gap: 0.3rem; }
.pair-stats div { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
.pair-stats dt { color: var(--ink-2); font-size: 0.94rem; }
.pair-stats dd { margin: 0; font-weight: 700; font-size: 1.15em; font-variant-numeric: lining-nums tabular-nums; }
.swatch.shown, .seg.shown { background: var(--accent); }
.swatch.later, .seg.later { background: var(--later); }
.swatch.never, .seg.never { background: var(--missed); }
.swatch.unmatched, .seg.unmatched { background: var(--unchecked); }
.trace { list-style: none; margin: 0.6rem 0 0; padding: 0; display: grid; gap: 0.25rem; color: var(--ink-2);
  font-size: 0.94rem; font-variant-numeric: lining-nums tabular-nums; }
@media (min-width: 40rem) { .trace { grid-template-columns: 1fr 1fr; } }
.trace li { display: flex; align-items: center; gap: 0.5rem; }
.trace b { color: var(--ink); }
.trace-detail { margin-top: 0.7rem; }
.trace-detail summary { min-height: 44px; display: flex; align-items: center; cursor: pointer; color: var(--accent-ink); }
"""

_MOON: Final[str] = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>'
)
_SUN: Final[str] = (
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>'
    "</svg>"
)


def _page_count(static_pages: tuple[PageRecord, ...], rendered_pages: tuple[PageRecord, ...]) -> str:
    count = len(static_pages) if not rendered_pages else len({_url_key(page.url) for page in static_pages + rendered_pages})
    return "one page" if count == 1 else f"{count} pages"


def render_report_page(pages: tuple[PageRecord, ...], run_date: datetime.date | None = None) -> str:
    if not pages:
        raise ReportFormatError("there are no pages to report")
    static_pages = tuple(page for page in pages if page.mode is ScanMode.STATIC)
    rendered_pages = tuple(page for page in pages if page.mode is ScanMode.RENDERED)
    with_rendering = bool(rendered_pages)
    date_text = f" on {run_date.day} {run_date:%B %Y}" if run_date else ""
    page_count = _page_count(static_pages, rendered_pages)
    sections: list[str] = []
    if static_pages:
        sections += [_example_panel(static_pages), _result_section(static_pages), _pages_section(static_pages)]
    if with_rendering:
        sections.append(_rendered_section(static_pages, rendered_pages))
    sections.append(_method_section(with_rendering))
    if static_pages:
        sections.append(_caveats(static_pages, with_rendering))
    sections.append(_glossary(with_rendering))
    body = "".join(sections)
    style = _STYLE + (_RENDERED_STYLE if with_rendering else "")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>loupe results</title>
<meta name="description" content="Does text hidden on a web page reach the AI agent that reads it? Results from {page_count}.">
<style>{style}</style>
</head>
<body>
<a class="skip" href="#main">Skip to the results</a>
<div class="page">
<header class="masthead">
<a class="wordmark" href="{REPOSITORY_URL}">loupe</a>
<div class="masthead-links">
<a href="{REPOSITORY_URL}">Code</a>
<input type="checkbox" id="theme" class="theme-input">
<label for="theme" class="theme-toggle"><span class="when-light">{_MOON}Dark mode</span><span class="when-dark">{_SUN}Light mode</span></label>
</div>
</header>
<main id="main">
<div class="intro">
<h1>A web page can hide text from you and still hand it to the AI reading it.</h1>
<p class="lede">loupe fetches real pages, finds the text a person looking at them can't see, and checks whether that text is still there in the version an AI agent is given to read.</p>
<p class="byline">Frank Duah, measured{date_text} across {page_count}</p>
</div>
{body}
</main>
<footer class="colophon">
<p>The code, the tests and the raw results are at <a href="{REPOSITORY_URL}">github.com/devfrankduah/loupe</a>. Quoted page text comes from the results file, which keeps short quotes; the pages themselves aren't stored.</p>
</footer>
</div>
</body>
</html>
"""
