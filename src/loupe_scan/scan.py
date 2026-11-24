"""Find text in an HTML document that a person looking at the page won't see.

The markup is inspected without a browser. Style attributes and the page's
own stylesheets are applied for the simple ways CSS hides text, a partial
check that loupe_scan.stylesheets describes. Scripts and layout are not
evaluated; loupe_scan.render covers those by asking a real browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from bs4 import BeautifulSoup, CData, Comment, Tag
from bs4.element import NavigableString, PageElement, TemplateString

from loupe_scan.phrases import find_instruction_phrases
from loupe_scan.signals import Signal, in_order
from loupe_scan.stylesheets import (
    PageStyles,
    StylesheetCheck,
    StylesheetSource,
    read_page_styles,
    style_attribute_hiding_reasons,
)

_LABEL_ATTRIBUTES: Final[tuple[str, ...]] = ("alt", "title", "aria-label")
_META_KEY_ATTRIBUTES: Final[tuple[str, ...]] = ("name", "property", "http-equiv", "itemprop")
_NON_CONTENT_TAGS: Final[frozenset[str]] = frozenset({"script", "style", "template"})
# Elements whose content a browser shows only when it can't show the element
# itself: a frame, a plugin, a canvas, a video or a sound. Every current
# browser can, so a reader never sees it, though converters keep it. A canvas
# also shows it with scripts turned off.
FALLBACK_CONTENT_TAGS: Final[frozenset[str]] = frozenset({"iframe", "noframes", "noembed", "canvas", "video", "audio"})
# A rendered scan never judges text inside these, so it's reported on its own
# even inside another hidden element, whose finding then leaves it out.
_REPORTED_APART: Final[frozenset[str]] = FALLBACK_CONTENT_TAGS | {"noscript"}
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")
# Text, not comments, scripts or styles. Beautiful Soup makes all the text inside a template a TemplateString.
_TEXT_TYPES: Final[tuple[type[NavigableString], ...]] = (NavigableString, CData, TemplateString)
# Added to the detail of a finding inside a shadow root declared in the HTML, as a rendered scan's are.
IN_SHADOW_ROOT: Final[str] = " (in a shadow root)"
# Elements that hold a document of their own.
_FRAME_TAGS: Final[frozenset[str]] = frozenset({"iframe", "frame", "object", "embed"})
# A browser reads what's inside these as text, so an element written there is never one, and never runs.
_RAW_TEXT_HOLDERS: Final[frozenset[str]] = frozenset(
    {"noscript", "iframe", "noframes", "noembed", "textarea", "title", "xmp", "plaintext", "script", "style"}
)
# The type values a browser runs a <script> for: none, a JavaScript MIME type, or module.
_SCRIPT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "", "module", "application/ecmascript", "application/javascript", "application/x-ecmascript",
        "application/x-javascript", "text/ecmascript", "text/javascript", "text/javascript1.0", "text/javascript1.1",
        "text/javascript1.2", "text/javascript1.3", "text/javascript1.4", "text/javascript1.5", "text/jscript",
        "text/livescript", "text/x-ecmascript", "text/x-javascript",
    }
)

PER_REQUEST_PLACEHOLDER: Final[str] = "[per-request value removed]"

# Meta names whose values are minted for each request: nonces, signatures,
# CSRF tokens, request and visitor ids. They identify one fetch, say nothing
# about the page, and shouldn't end up in published results.
_PER_REQUEST_META_NAME: Final[re.Pattern[str]] = re.compile(
    r"(?:^|[-_:.])"
    r"(?:nonce|hmac|csrf|xsrf|visitor|token|signature|traceparent|(?:request|req|session|trace)[-_]?id)"
    r"(?:$|[-_:.])",
    re.IGNORECASE,
)


class Kind(str, Enum):
    COMMENT = "comment"
    HIDDEN_ATTRIBUTE = "hidden-attribute"
    INLINE_STYLE = "inline-style"
    # Hidden by a rule in the page's <style> blocks or linked stylesheets, found by the partial stylesheet check.
    STYLESHEET = "stylesheet"
    ARIA_HIDDEN = "aria-hidden"
    LABEL_ATTRIBUTE = "label-attribute"
    META = "meta"
    NOSCRIPT = "noscript"
    # Inside an element in FALLBACK_CONTENT_TAGS.
    FALLBACK_CONTENT = "fallback-content"
    # Found by loupe_scan.render: a browser drew the page and this text never showed.
    RENDERED_HIDDEN = "rendered-hidden"


class ScanMode(str, Enum):
    """How a finding was produced: from the markup alone, or from the page as a browser drew it."""

    STATIC = "static"
    RENDERED = "rendered"


@dataclass(frozen=True)
class Finding:
    kind: Kind
    detail: str
    tag: str
    text: str
    instruction_phrases: tuple[str, ...]
    mode: ScanMode = ScanMode.STATIC
    # True when text is PER_REQUEST_PLACEHOLDER standing in for a per-request meta value.
    value_removed: bool = False
    # The line of the HTML its element starts on (for a comment, the element holding it), when the markup scan
    # found it. Left out of comparisons, since it says where a finding is, not what it is.
    line: int | None = field(default=None, compare=False)

    @property
    def is_instruction_like(self) -> bool:
        return bool(self.instruction_phrases)


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def parse_inline_style(style: str) -> dict[str, str]:
    """Parse a style attribute into lowercase property/value pairs.

    Later declarations win, as they do in the browser. "!important" is
    dropped because it doesn't change what a single inline declaration means.
    """
    declarations: dict[str, str] = {}
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name = name.strip().lower()
        property_value = value.lower().replace("!important", "").strip()
        if property_name and property_value:
            declarations[property_name] = property_value
    return declarations


def inline_style_hiding_reasons(style: str) -> tuple[str, ...]:
    """Return the declarations in style that make an element invisible to a reader, with no stylesheet applied."""
    return style_attribute_hiding_reasons(style)


def _element_hiding(element: Tag, styles: PageStyles) -> tuple[Kind, str] | None:
    if element.has_attr("hidden"):
        return Kind.HIDDEN_ATTRIBUTE, "hidden"
    styled = styles.hiding(element)
    if styled is not None:
        kind = Kind.STYLESHEET if styled.from_stylesheet else Kind.INLINE_STYLE
        return kind, ", ".join(styled.reasons)
    aria_hidden = element.get("aria-hidden")
    if isinstance(aria_hidden, str) and aria_hidden.strip().lower() == "true":
        return Kind.ARIA_HIDDEN, 'aria-hidden="true"'
    return None


def _text_of(element: Tag, styles: PageStyles, skipped: frozenset[str] = frozenset()) -> str:
    """element's text: that of any shadow root declared in it too, but not of an ordinary <template>, whose
    content never shows, nor of any descendant named in skipped."""
    parts: list[str] = []
    pending: list[PageElement] = list(reversed(element.contents))
    while pending:
        node = pending.pop()
        if isinstance(node, Tag):
            name = node.name.lower()
            if name not in skipped and (name != "template" or styles.is_shadow_root(node)):
                pending.extend(reversed(node.contents))
        elif isinstance(node, NavigableString) and type(node) in _TEXT_TYPES:
            text = node.strip()
            if text:
                parts.append(text)
    return normalize_whitespace(" ".join(parts))


def _has_own_text(element: Tag) -> bool:
    return any(
        isinstance(child, NavigableString) and type(child) in _TEXT_TYPES and child.strip() for child in element.contents
    )


def _in_raw_text(element: Tag) -> bool:
    return any(parent.name in _RAW_TEXT_HOLDERS for parent in element.parents if isinstance(parent, Tag))


def _runs(script: Tag) -> bool:
    """True for a <script> a browser runs: a JavaScript type and a source or code of its own."""
    kind = _attribute_text(script, "type").partition(";")[0].strip().lower()
    return kind in _SCRIPT_TYPES and bool(_attribute_text(script, "src") or script.get_text().strip())


def _is_closed(element: Tag) -> bool:
    """True for content a browser shows only once a click or a script opens it."""
    if element.name in ("details", "dialog") and not element.has_attr("open"):
        return True
    return element.has_attr("popover")


def _attribute_text(element: Tag, attribute: str) -> str:
    value = element.get(attribute)
    if isinstance(value, list):
        value = " ".join(value)
    return normalize_whitespace(value) if isinstance(value, str) else ""


def _meta_key(element: Tag) -> tuple[str, str] | None:
    for attribute in _META_KEY_ATTRIBUTES:
        key = _attribute_text(element, attribute)
        if key:
            return attribute, key
    return None


def is_per_request_meta(name: str, value: str) -> bool:
    """True when a meta tag's value looks minted for this one request.

    A value with spaces is prose, not a token, so it is kept whatever the name
    and still goes through the instruction phrase check.
    """
    return _PER_REQUEST_META_NAME.search(name) is not None and _WHITESPACE.search(value) is None


def make_finding(
    kind: Kind,
    detail: str,
    tag: str,
    text: str,
    mode: ScanMode = ScanMode.STATIC,
    *,
    value_removed: bool = False,
    line: int | None = None,
) -> Finding:
    return Finding(
        kind=kind,
        detail=detail,
        tag=tag,
        text=text,
        instruction_phrases=find_instruction_phrases(text),
        mode=mode,
        value_removed=value_removed,
        line=line,
    )


def _is_inside(element: Tag, containers: set[int]) -> bool:
    return any(id(parent) in containers for parent in element.parents)


@dataclass(frozen=True)
class MarkupScan:
    findings: list[Finding]
    # What the partial stylesheet check read.
    stylesheets: StylesheetCheck
    # What on the page only a browser can judge, in Signal's order: what makes fetch_page's auto mode render it.
    needs_browser: tuple[Signal, ...] = ()


def scan_html(html: str) -> list[Finding]:
    """Return findings in document order, applying the page's <style> blocks but no linked stylesheet."""
    return scan_markup(html).findings


def scan_markup(html: str, *, page_url: str | None = None, stylesheets: StylesheetSource | None = None) -> MarkupScan:
    """Return findings in document order, and what the stylesheet check read. Findings with no text are dropped.

    page_url is where the HTML came from, after any redirects. With it and
    stylesheets, up to MAX_LINKED_STYLESHEETS stylesheets linked from the
    same host are fetched from stylesheets and applied too.

    Text inside a hidden element is reported once, on the outermost hidden
    element, so nested hiding doesn't inflate the counts. Text inside noscript
    or an element in FALLBACK_CONTENT_TAGS is the exception: it's reported on
    that element, on its own, and left out of any hidden element around it.

    A shadow root declared in the HTML is checked with its own styles, as
    loupe_scan.stylesheets describes, and its findings say so in their
    detail. The content of an ordinary <template> is left alone.
    """
    soup = BeautifulSoup(html, "html.parser")
    styles = read_page_styles(soup, page_url, stylesheets)
    findings: list[Finding] = []
    hidden_containers: set[int] = set()
    # The hidden containers a reader can't see, aria-hidden ones aside.
    unseen: set[int] = set()
    # Elements in _REPORTED_APART already reported, so one inside another isn't reported again.
    apart: set[int] = set()
    signals: set[Signal] = set()

    for node in soup.descendants:
        if isinstance(node, Comment):
            text = normalize_whitespace(str(node))
            if text:
                holder = node.parent
                line = holder.sourceline if isinstance(holder, Tag) else None
                findings.append(make_finding(Kind.COMMENT, "<!-- -->", "comment", text, line=line))
            continue
        if not isinstance(node, Tag):
            continue

        tag_name = node.name.lower()

        for attribute in _LABEL_ATTRIBUTES:
            text = _attribute_text(node, attribute)
            if text:
                findings.append(make_finding(Kind.LABEL_ATTRIBUTE, attribute, tag_name, text, line=node.sourceline))

        if tag_name == "meta":
            meta_key = _meta_key(node)
            text = _attribute_text(node, "content")
            if meta_key is not None and text:
                attribute, key = meta_key
                label = f"{attribute}={key}"
                if is_per_request_meta(key, text):
                    findings.append(
                        make_finding(
                            Kind.META, label, tag_name, PER_REQUEST_PLACEHOLDER, value_removed=True, line=node.sourceline
                        )
                    )
                else:
                    findings.append(make_finding(Kind.META, label, tag_name, text, line=node.sourceline))
            continue

        if styles.never_shown(node):
            continue
        runs_script = (tag_name == "script" and _runs(node)) or any(name.lower().startswith("on") for name in node.attrs)
        if Signal.SCRIPTS not in signals and runs_script and not _in_raw_text(node):
            signals.add(Signal.SCRIPTS)
        if tag_name in _NON_CONTENT_TAGS:
            continue
        where = IN_SHADOW_ROOT if styles.in_shadow_root(node) else ""
        if tag_name in _FRAME_TAGS and not _is_inside(node, unseen) and not _in_raw_text(node):
            signals.add(Signal.FRAMES)

        if tag_name in _REPORTED_APART:
            if not _is_inside(node, apart):
                apart.add(id(node))
                hidden_containers.add(id(node))
                unseen.add(id(node))
                text = _text_of(node, styles)
                if text:
                    kind = Kind.NOSCRIPT if tag_name == "noscript" else Kind.FALLBACK_CONTENT
                    findings.append(make_finding(kind, f"<{tag_name}>{where}", tag_name, text, line=node.sourceline))
            continue

        if _is_inside(node, hidden_containers):
            if not _is_inside(node, unseen):
                _add_concerns(node, styles, signals)
            continue

        hiding = _element_hiding(node, styles)
        if hiding is not None:
            hidden_containers.add(id(node))
            kind, detail = hiding
            if kind is not Kind.ARIA_HIDDEN:
                unseen.add(id(node))
            else:
                _add_concerns(node, styles, signals)
            text = _text_of(node, styles, _REPORTED_APART)
            if text:
                findings.append(make_finding(kind, f"{detail}{where}", tag_name, text, line=node.sourceline))
            continue
        _add_concerns(node, styles, signals)

    check = styles.check
    if check.shadow_roots:
        signals.add(Signal.SHADOW_ROOTS)
    if check.linked_not_read:
        signals.add(Signal.UNREAD_STYLESHEETS)
    if check.uncertain_rules:
        signals.add(Signal.UNSETTLED_RULES)
    return MarkupScan(findings, check, in_order(signals))


def _add_concerns(element: Tag, styles: PageStyles, signals: set[Signal]) -> None:
    """Add what about an element a reader may see only a browser can judge."""
    if Signal.CLOSED not in signals and _is_closed(element):
        signals.add(Signal.CLOSED)
    signals.update(styles.concerns(element, has_text=_has_own_text(element), skip=frozenset(signals)))
