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
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from loupe.scan import Kind

REPOSITORY_URL: Final[str] = "https://github.com/devfrankduah/loupe"

# IANA reserves these domains for documentation. Their pages have nothing to
# hide, which makes them a fair baseline for how noisy the scanner is.
CONTROL_HOSTS: Final[frozenset[str]] = frozenset({"example.com", "example.org", "example.net"})


_KIND_LABELS: Final[dict[Kind, tuple[str, str]]] = {
    Kind.HIDDEN_ATTRIBUTE: (
        "Marked hidden",
        "The hidden attribute tells the browser not to draw the element at all.",
    ),
    Kind.INLINE_STYLE: (
        "Hidden by inline style",
        "Styling written on the element itself: display:none, invisible, zero size, zero opacity, or moved off-screen.",
    ),
    Kind.COMMENT: (
        "HTML comments",
        "Notes left in the page source. Browsers never show them.",
    ),
    Kind.ARIA_HIDDEN: (
        "aria-hidden",
        "Hidden from screen readers only. Sighted readers usually still see it.",
    ),
    Kind.NOSCRIPT: (
        "noscript blocks",
        "Shown only to visitors who have JavaScript turned off.",
    ),
    Kind.LABEL_ATTRIBUTE: (
        "alt, title and aria-label",
        "Descriptions attached to images, links and buttons. Shown on hover or read aloud, if at all.",
    ),
    Kind.META: (
        "Meta tags",
        "Information about the page for search engines and link previews.",
    ),
}


class ReportFormatError(ValueError):
    """The results file isn't in the shape loupe writes."""


@dataclass(frozen=True)
class FindingRecord:
    kind: Kind
    detail: str
    text: str
    instruction_phrases: tuple[str, ...]
    in_markdown: bool | None


@dataclass(frozen=True)
class PageRecord:
    url: str
    html_error: str | None
    markdown_error: str | None
    markdown_retry_error: str | None
    markdown_retried: bool
    retry_not_compared: int
    findings: tuple[FindingRecord, ...]

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


def _load_finding(value: object, where: str) -> FindingRecord:
    record = _object(value, where)
    kind_value = _string(record, "kind", where)
    try:
        kind = Kind(kind_value)
    except ValueError:
        raise ReportFormatError(f"{where}.kind {kind_value!r} is not a known kind") from None
    phrases = _list(_field(record, "instruction_phrases", where), f"{where}.instruction_phrases")
    if not all(isinstance(phrase, str) for phrase in phrases):
        raise ReportFormatError(f"{where}.instruction_phrases should hold strings")
    in_markdown = record.get("in_markdown")
    if in_markdown is not None and not isinstance(in_markdown, bool):
        raise ReportFormatError(f"{where}.in_markdown should be true, false or null")
    return FindingRecord(
        kind=kind,
        detail=_string(record, "detail", where),
        text=_string(record, "text", where),
        instruction_phrases=tuple(str(phrase) for phrase in phrases),
        in_markdown=in_markdown,
    )


def _load_page(value: object, where: str) -> PageRecord:
    record = _object(value, where)
    findings = _list(_field(record, "findings", where), f"{where}.findings")
    retry_value = record.get("markdown_retry")
    retry = _object(retry_value, f"{where}.markdown_retry") if retry_value is not None else {}
    not_compared = retry.get("not_compared_because_text_was_cut", 0)
    if not isinstance(not_compared, int) or isinstance(not_compared, bool) or not_compared < 0:
        raise ReportFormatError(f"{where}.markdown_retry count should be a whole number")
    return PageRecord(
        url=_string(record, "url", where),
        html_error=_optional_string(record, "html_error", where),
        markdown_error=_optional_string(record, "markdown_error", where),
        markdown_retry_error=_optional_string(retry, "retry_error", f"{where}.markdown_retry"),
        markdown_retried=retry_value is not None,
        retry_not_compared=not_compared,
        findings=tuple(_load_finding(item, f"{where}.findings[{index}]") for index, item in enumerate(findings)),
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
    try:
        return datetime.date.fromisoformat(path.stem)
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


# Rendering


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def _safe_href(url: str) -> str | None:
    return _escape(url) if urlsplit(url).scheme in ("http", "https") else None


def _page_label(page: PageRecord) -> str:
    parts = urlsplit(page.url)
    path = parts.path.rstrip("/")
    return f"{page.host}{path}" if path else page.host


def _percent(part: int, whole: int) -> str:
    return f"{100 * part / whole:.2f}%" if whole else "0%"


def _bar(counts: Tally) -> str:
    # The bar repeats the numbers printed next to it, so assistive tech skips it.
    segments = [
        ("in", counts.survived),
        ("out", counts.dropped),
        ("na", counts.not_compared),
    ]
    spans = "".join(
        f'<span class="seg {name}" style="width:{_percent(value, counts.total)}"></span>'
        for name, value in segments
        if value
    )
    return f'<div class="bar" aria-hidden="true">{spans}</div>'


def _counts_line(counts: Tally) -> str:
    return (
        '<ul class="key">'
        f'<li><span class="swatch in"></span>{counts.survived} in the markdown</li>'
        f'<li><span class="swatch out"></span>{counts.dropped} not in the markdown</li>'
        f'<li><span class="swatch na"></span>{counts.not_compared} not compared</li>'
        "</ul>"
    )


def _headline(pages: tuple[PageRecord, ...]) -> str:
    findings = _all_findings(pages)
    hiding = tally(_element_hiding(findings))
    comments = tally([finding for finding in findings if finding.kind is Kind.COMMENT])
    return (
        '<section class="headline" aria-labelledby="headline">'
        '<h2 id="headline" class="vh">The finding</h2>'
        f'<p class="big">Text hidden with the <code>hidden</code> attribute or <code>display:none</code> '
        f"{_how_often(hiding.survived, hiding.compared)} still reached the markdown an agent reads: "
        f"<b>{hiding.survived} of {hiding.compared}</b>. "
        f"HTML comments {_how_often(comments.survived, comments.compared)} did: "
        f"<b>{comments.survived} of {comments.compared}</b>.</p>"
        '<p class="sub">Markdown is the cleaned-up text version of a page that many agent tools hand to the model, '
        "Bright Data's own scraping tool among them. So the second number is good news, and the first means "
        "hiding text with markup does not keep it away from the model. The counts leave out text too short to "
        "compare fairly.</p>"
        "</section>"
    )


def _how_it_works() -> str:
    steps = (
        (
            "Fetch the page twice",
            "loupe asks Bright Data for the raw HTML and, separately, for the markdown version, "
            "the same two requests Bright Data's own agent tools make.",
        ),
        (
            "Find what a reader can't see",
            "It reads the HTML for comments, hidden elements, image and link descriptions, meta tags "
            "and noscript blocks, and notes any that sound like orders to an AI.",
        ),
        (
            "Look for it in the markdown",
            "For each piece of hidden text, it checks whether the same words, in the same order, "
            "appear in the markdown an agent would read.",
        ),
    )
    items = "".join(
        f'<li><span class="step-number">{number}</span><h3>{title}</h3><p>{body}</p></li>'
        for number, (title, body) in enumerate(steps, start=1)
    )
    return (
        '<section aria-labelledby="how">'
        '<h2 id="how">How it works</h2>'
        f'<ol class="steps">{items}</ol>'
        "</section>"
    )


def _page_card(page: PageRecord) -> str:
    label = _escape(_page_label(page))
    href = _safe_href(page.url)
    title = f'<a href="{href}">{label}</a>' if href else label
    badge = '<span class="badge">Control</span>' if page.is_control else ""
    parts = [f'<li class="card"><h3>{title}{badge}</h3>']

    if page.is_control:
        parts.append(
            '<p class="note">A reserved example page with nothing to hide, '
            "so anything found here is the scanner's background noise.</p>"
        )
    if page.html_error is not None:
        parts.append(f'<p class="note">The HTML could not be fetched: {_escape(page.html_error)}</p></li>')
        return "".join(parts)

    counts = tally(page.findings)
    parts.append(
        '<dl class="stats">'
        f"<div><dt>Hidden findings</dt><dd>{len(page.findings)}</dd></div>"
        f"<div><dt>Sound like orders to an AI</dt><dd>{len(page.instruction_like)}</dd></div>"
        "</dl>"
    )
    parts.append(_bar(counts))
    parts.append(_counts_line(counts))

    if page.markdown_error is not None:
        parts.append(f'<p class="note">The markdown could not be fetched: {_escape(page.markdown_error)}</p>')
    elif page.markdown_retried:
        note = "The markdown fetch failed the first time and worked on one retry."
        if page.retry_not_compared:
            note += (
                f" {page.retry_not_compared} findings longer than their saved quotes "
                "couldn't be compared on the retry."
            )
        parts.append(f'<p class="note">{note}</p>')
    parts.append("</li>")
    return "".join(parts)


def _pages_section(pages: tuple[PageRecord, ...]) -> str:
    cards = "".join(_page_card(page) for page in pages)
    return (
        '<section aria-labelledby="pages">'
        '<h2 id="pages">Page by page</h2>'
        '<p class="prose">Each bar splits that page\'s hidden findings by what happened to them in the markdown. '
        '"Not compared" means the text was too short to match fairly, or the markdown wasn\'t available.</p>'
        f'<ul class="cards">{cards}</ul>'
        "</section>"
    )


def _kinds_section(pages: tuple[PageRecord, ...]) -> str:
    findings = _all_findings(pages)
    rows = []
    for kind, (name, meaning) in _KIND_LABELS.items():
        of_kind = [finding for finding in findings if finding.kind is kind]
        if not of_kind:
            continue
        counts = tally(of_kind)
        rows.append(
            '<li class="kind">'
            f'<div class="kind-head"><span class="kind-name">{name}</span>'
            f'<span class="kind-total">{counts.total} found</span></div>'
            f'<p class="meaning">{meaning}</p>'
            f"{_bar(counts)}{_counts_line(counts)}"
            "</li>"
        )
    return (
        '<section aria-labelledby="kinds">'
        '<h2 id="kinds">What happened to each kind of hidden text</h2>'
        f'<ul class="kinds">{"".join(rows)}</ul>'
        "</section>"
    )


def _glossary() -> str:
    terms = (
        (
            "HTML comment",
            "A note in a page's source code, written between <code>&lt;!--</code> and <code>--&gt;</code>. "
            "Browsers skip it, but anything that reads the raw source sees it.",
        ),
        (
            "aria-hidden",
            "A marker that tells screen readers to skip an element. It doesn't hide anything from people "
            "looking at the screen.",
        ),
        (
            "Markdown",
            "A plain-text format with light symbols for headings, links and lists. Agent tools often turn a "
            "page into markdown before giving it to a model, because it's shorter than HTML.",
        ),
        (
            "Prompt injection",
            "Text written to make an AI model follow instructions its user never gave, for example "
            '"ignore your previous instructions". Hiding that text on a web page is one way to deliver it.',
        ),
    )
    entries = "".join(f"<div><dt>{term}</dt><dd>{meaning}</dd></div>" for term, meaning in terms)
    return (
        '<section aria-labelledby="glossary">'
        '<h2 id="glossary">Words used here</h2>'
        f'<dl class="glossary">{entries}</dl>'
        "</section>"
    )


def _caveats(pages: tuple[PageRecord, ...]) -> str:
    findings = _all_findings(pages)
    matches = [(page, finding) for page in pages for finding in page.instruction_like]
    inline = [finding for finding in findings if finding.kind is Kind.INLINE_STYLE]
    opacity_by_page = Counter(
        page.host for page in pages for finding in page.findings if _has_reason(finding, "opacity:0")
    )
    opacity_total = sum(opacity_by_page.values())

    match_items = "".join(
        f'<li><span class="where">{_escape(page.host)}, {_escape(finding.detail)}</span>'
        f"<q>{_escape(finding.text)}</q>"
        f'<span class="phrase">matched {_escape(", ".join(finding.instruction_phrases))}</span></li>'
        for page, finding in matches
    )
    items = [
        "<li><h3>Sounding like an order isn't the same as being one</h3>"
        f"<p>loupe flags text that contains phrases such as &ldquo;you are&rdquo; or &ldquo;assistant&rdquo;. "
        f"That caught {len(matches)} pieces of text in this run. Here they are, so you can judge them yourself:</p>"
        f'<ul class="matches">{match_items}</ul></li>'
    ]
    if opacity_total:
        busiest_host, busiest_count = opacity_by_page.most_common(1)[0]
        items.append(
            "<li><h3>Some &ldquo;hidden&rdquo; text is only waiting to fade in</h3>"
            f"<p>{opacity_total} of the {len(inline)} inline-style findings are <code>opacity:0</code>, "
            f"{busiest_count} of them on {_escape(busiest_host)}. That's how scroll-in animations start: the page "
            "arrives invisible and JavaScript fades it in, so people do see it. loupe doesn't run JavaScript, "
            "so it can't tell the difference.</p></li>"
        )
    items.append(
        "<li><h3>The two versions come from two services</h3>"
        "<p>Bright Data serves the HTML through Web Unlocker and the markdown through its Fetch API. If the two "
        "services received slightly different versions of a page, a difference could come from that and not "
        "from the markdown conversion.</p></li>"
    )
    items.append(
        "<li><h3>&ldquo;In the markdown&rdquo; means the words appear there</h3>"
        "<p>It doesn't prove they got there from the hidden element. A link's description often repeats the "
        "link's visible text, so it would match either way.</p></li>"
    )
    return (
        '<section aria-labelledby="caveats">'
        "<h2 id=\"caveats\">What I'm not sure about</h2>"
        f'<ul class="caveats">{"".join(items)}</ul>'
        "</section>"
    )


_STYLE: Final[str] = """
:root {
  color-scheme: light dark;
  --surface: #ffffff; --panel: #f2f3f5; --ink: #191c21; --ink-2: #434951; --muted: #585f68;
  --link: #1c4d8f; --rule: #d5d9de; --grid: #e4e7ea; --accent: #2a78d6; --block: #5b636d; --focus: #1c4d8f;
  --serif: Charter, "Bitstream Charter", "Sitka Text", Cambria, serif;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
}
@media (prefers-color-scheme: dark) {
  :root {
    --surface: #15171b; --panel: #1f2227; --ink: #e9ebee; --ink-2: #c1c6cd; --muted: #9da4ad;
    --link: #9cc2f4; --rule: #363b42; --grid: #2a2e34; --accent: #3987e5; --block: #8a929c; --focus: #9cc2f4;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--surface); color: var(--ink); font: 1.0625rem/1.62 var(--serif); }
a { color: var(--link); text-decoration-thickness: 1px; text-underline-offset: 0.16em; }
a:hover { text-decoration-thickness: 2px; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
code { font: 0.875em/1.4 var(--mono); overflow-wrap: anywhere; }
.wrap { max-width: 64rem; margin: 0 auto; padding: 0 1rem; }
.vh { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.skip { position: absolute; left: 1rem; top: -3rem; padding: 0.4rem 0.8rem; background: var(--surface);
  font: 0.9375rem/1.4 var(--sans); }
.skip:focus { top: 0.5rem; }

.site-header { border-bottom: 1px solid var(--rule); font-family: var(--sans); }
.site-header .wrap { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between;
  gap: 0.25rem 1.5rem; padding-top: 0.9rem; padding-bottom: 0.9rem; }
.wordmark { font-weight: 650; font-size: 1.0625rem; color: var(--ink); text-decoration: none; letter-spacing: -0.01em; }
.site-header ul { display: flex; gap: 1.25rem; margin: 0; padding: 0; list-style: none; font-size: 0.9375rem; }
.site-header nav a { color: var(--ink-2); text-decoration: none; }
.site-header nav a:hover { color: var(--ink); text-decoration: underline; }

h1 { font: 600 clamp(1.8rem, 1.2rem + 2.4vw, 2.5rem)/1.12 var(--serif); letter-spacing: -0.015em;
  max-width: 22em; margin: 2.5rem 0 0.9rem; text-wrap: balance; }
h2 { font: 600 1.45rem/1.25 var(--serif); margin: 3.25rem 0 0.75rem; letter-spacing: -0.01em; }
h3 { font: 600 1rem/1.35 var(--sans); margin: 0 0 0.4rem; }
.lede { font-size: 1.1875rem; line-height: 1.55; color: var(--ink-2); max-width: 36em; margin: 0 0 1rem; }
.byline { font: 0.9375rem/1.5 var(--sans); color: var(--muted); margin: 0; }
.prose { max-width: 40em; margin: 0 0 1rem; }

.headline { margin-top: 2.5rem; max-width: 44em; border-left: 3px solid var(--accent); padding: 0.1rem 0 0.1rem 1.25rem; }
.headline .big { font: 600 clamp(1.25rem, 1rem + 1vw, 1.6rem)/1.35 var(--serif); margin: 0 0 0.75rem; }
.headline .sub { color: var(--ink-2); margin: 0; max-width: 40em; }

.steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; counter-reset: none; }
@media (min-width: 48rem) { .steps { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 2.25rem; } }
.steps li { position: relative; border-top: 2px solid var(--ink); padding-top: 0.75rem; font-family: var(--sans); }
@media (min-width: 48rem) {
  .steps li:not(:last-child)::after { content: ""; position: absolute; top: -2px; right: -2.25rem; width: 2.25rem;
    border-top: 2px dashed var(--rule); }
}
.steps p { margin: 0; color: var(--ink-2); font-size: 0.9375rem; line-height: 1.55; }
.step-number { display: inline-block; font: 600 0.8125rem/1 var(--sans); color: var(--muted); margin-bottom: 0.35rem; }

.cards { list-style: none; margin: 0; padding: 0; display: grid; gap: 1rem; }
@media (min-width: 48rem) { .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
.card { border: 1px solid var(--rule); border-radius: 8px; padding: 1rem 1.1rem; font-family: var(--sans); }
.card h3 { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.25rem 0.6rem; font-size: 1rem;
  overflow-wrap: anywhere; }
.badge { font: 600 0.75rem/1.4 var(--sans); color: var(--ink); border: 1px solid var(--ink-2); border-radius: 4px;
  padding: 0 0.4rem; }
.stats { display: flex; flex-wrap: wrap; gap: 0.5rem 2rem; margin: 0.6rem 0 0.75rem; }
.stats dt { font-size: 0.8125rem; color: var(--muted); }
.stats dd { margin: 0; font: 600 1.6rem/1.2 var(--sans); font-variant-numeric: tabular-nums; }
.note { margin: 0.6rem 0 0; font-size: 0.875rem; line-height: 1.5; color: var(--muted); }

.bar { display: flex; height: 0.85rem; border: 1px solid var(--rule); border-radius: 3px; overflow: hidden;
  background: var(--surface); }
.seg { display: block; height: 100%; }
.seg.in, .swatch.in { background: var(--accent); }
.seg.out, .swatch.out { background: var(--block); }
.seg.na, .swatch.na { background: var(--grid); }
.key { display: flex; flex-wrap: wrap; gap: 0.2rem 1.1rem; margin: 0.45rem 0 0; padding: 0; list-style: none;
  font: 0.8125rem/1.45 var(--sans); color: var(--ink-2); font-variant-numeric: tabular-nums; }
.key li { display: inline-flex; align-items: center; gap: 0.4rem; }
.swatch { display: inline-block; width: 0.75rem; height: 0.75rem; border-radius: 2px; border: 1px solid var(--rule); }

.kinds { list-style: none; margin: 0; padding: 0; max-width: 44em; }
.kind { padding: 0.9rem 0; border-bottom: 1px solid var(--grid); font-family: var(--sans); }
.kind-head { display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }
.kind-name { font-weight: 600; }
.kind-total { font-size: 0.875rem; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.kind .meaning { margin: 0.1rem 0 0.5rem; font-size: 0.875rem; line-height: 1.5; color: var(--muted); }

.glossary { margin: 0; max-width: 44em; }
.glossary div { padding: 0.7rem 0; border-bottom: 1px solid var(--grid); }
.glossary dt { font: 600 1rem/1.4 var(--sans); }
.glossary dd { margin: 0.15rem 0 0; color: var(--ink-2); }

.caveats { list-style: none; margin: 0; padding: 0; max-width: 44em; }
.caveats > li { padding: 0.9rem 0; border-bottom: 1px solid var(--grid); }
.caveats p { margin: 0; color: var(--ink-2); }
.matches { list-style: none; margin: 0.75rem 0 0; padding: 0; font-family: var(--sans); font-size: 0.875rem; }
.matches li { padding: 0.5rem 0.75rem; margin-bottom: 0.4rem; background: var(--panel); border-radius: 6px; }
.matches .where, .matches .phrase { display: block; color: var(--muted); font-size: 0.8125rem; }
.matches q { display: block; color: var(--ink); overflow-wrap: anywhere; }

.site-footer { border-top: 1px solid var(--rule); margin-top: 3.5rem; padding: 1.5rem 0 2.5rem;
  font: 0.8125rem/1.6 var(--sans); color: var(--ink-2); }
.site-footer p { margin: 0 0 0.4rem; overflow-wrap: anywhere; }
"""


def render_report_page(pages: tuple[PageRecord, ...], run_date: datetime.date | None = None) -> str:
    if not pages:
        raise ReportFormatError("there are no pages to report")
    date_text = f", run on {run_date.day} {run_date:%B %Y}" if run_date else ""
    page_count = f"{len(pages)} page" if len(pages) == 1 else f"{len(pages)} pages"
    body = "".join(
        (
            _headline(pages),
            _how_it_works(),
            _pages_section(pages),
            _kinds_section(pages),
            _caveats(pages),
            _glossary(),
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>loupe results</title>
<meta name="description" content="Does text hidden on a web page reach an AI agent that reads it? Results from {page_count}.">
<style>{_STYLE}</style>
</head>
<body>
<a class="skip" href="#main">Skip to the results</a>
<header class="site-header"><div class="wrap">
<a class="wordmark" href="{REPOSITORY_URL}">loupe</a>
<nav aria-label="Site"><ul><li><a href="{REPOSITORY_URL}">Code</a></li><li><a href="{REPOSITORY_URL}#readme">README</a></li></ul></nav>
</div></header>
<main id="main" class="wrap">
<h1>Does text hidden on a web page reach an AI agent that reads it?</h1>
<p class="lede">loupe fetches real web pages, finds the text a person looking at the page can't see, and checks whether that text is still there in the version an AI agent is given to read.</p>
<p class="byline">Frank Duah{date_text}, {page_count}</p>
{body}
</main>
<footer class="site-footer"><div class="wrap">
<p>Built by Frank Duah. The code, the tests and the raw results are at <a href="{REPOSITORY_URL}">github.com/devfrankduah/loupe</a>.</p>
<p>Quoted page text comes from the results file, which keeps short quotes. The pages themselves aren't stored.</p>
</div></footer>
</body>
</html>
"""
