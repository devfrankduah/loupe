"""Find text in an HTML document that a person looking at the page won't see.

Only the markup is inspected. Stylesheets, scripts and layout are not
evaluated, so an element hidden by a CSS class is invisible to this scanner;
loupe.render covers that case by asking a real browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

from bs4 import BeautifulSoup, Comment, Tag

from loupe.phrases import find_instruction_phrases

# Offsets at or beyond this many pixels to the left or top push content far
# outside any real viewport. Smaller negative offsets are common in normal
# layouts (pulling a badge over a border, for example) and would be noise.
OFFSCREEN_THRESHOLD_PX: Final[float] = 1000.0

# Browsers resolve em and rem against the font size; 16px is the default
# root size and is close enough for deciding whether something is off-screen.
_PX_PER_EM: Final[float] = 16.0

_LABEL_ATTRIBUTES: Final[tuple[str, ...]] = ("alt", "title", "aria-label")
_META_KEY_ATTRIBUTES: Final[tuple[str, ...]] = ("name", "property", "http-equiv", "itemprop")
_NON_CONTENT_TAGS: Final[frozenset[str]] = frozenset({"script", "style", "template"})
_OFFSET_PROPERTIES: Final[tuple[str, ...]] = ("left", "top", "right", "bottom")
_LENGTH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))([a-z%]*)$")
_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


class Kind(str, Enum):
    COMMENT = "comment"
    HIDDEN_ATTRIBUTE = "hidden-attribute"
    INLINE_STYLE = "inline-style"
    ARIA_HIDDEN = "aria-hidden"
    LABEL_ATTRIBUTE = "label-attribute"
    META = "meta"
    NOSCRIPT = "noscript"
    # Found by loupe.render: a browser drew the page and this text never showed.
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


def _length_in_px(value: str) -> float | None:
    match = _LENGTH_PATTERN.match(value)
    if match is None:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit in ("", "px"):
        return number
    if unit in ("em", "rem"):
        return number * _PX_PER_EM
    return None


def _is_zero_quantity(value: str) -> bool:
    match = _LENGTH_PATTERN.match(value)
    return match is not None and float(match.group(1)) == 0.0


def inline_style_hiding_reasons(style: str) -> tuple[str, ...]:
    """Return the declarations in style that make an element invisible to a reader."""
    declarations = parse_inline_style(style)
    reasons: list[str] = []

    if declarations.get("display") == "none":
        reasons.append("display:none")
    if declarations.get("visibility") in ("hidden", "collapse"):
        reasons.append(f"visibility:{declarations['visibility']}")
    if _is_zero_quantity(declarations.get("font-size", "")):
        reasons.append("font-size:0")
    if _is_zero_quantity(declarations.get("opacity", "")):
        reasons.append("opacity:0")

    if declarations.get("position") in ("absolute", "fixed"):
        for property_name in _OFFSET_PROPERTIES:
            offset = _length_in_px(declarations.get(property_name, ""))
            if offset is not None and offset <= -OFFSCREEN_THRESHOLD_PX:
                reasons.append(f"off-screen {property_name}:{declarations[property_name]}")
                break

    indent = _length_in_px(declarations.get("text-indent", ""))
    if indent is not None and indent <= -OFFSCREEN_THRESHOLD_PX:
        reasons.append(f"off-screen text-indent:{declarations['text-indent']}")

    return tuple(reasons)


def _element_hiding(element: Tag) -> tuple[Kind, str] | None:
    if element.has_attr("hidden"):
        return Kind.HIDDEN_ATTRIBUTE, "hidden"
    style = element.get("style")
    if isinstance(style, str):
        reasons = inline_style_hiding_reasons(style)
        if reasons:
            return Kind.INLINE_STYLE, ", ".join(reasons)
    aria_hidden = element.get("aria-hidden")
    if isinstance(aria_hidden, str) and aria_hidden.strip().lower() == "true":
        return Kind.ARIA_HIDDEN, 'aria-hidden="true"'
    return None


def _visible_text(element: Tag) -> str:
    return normalize_whitespace(" ".join(element.stripped_strings))


def _attribute_text(element: Tag, attribute: str) -> str:
    value = element.get(attribute)
    if isinstance(value, list):
        value = " ".join(value)
    return normalize_whitespace(value) if isinstance(value, str) else ""


def _meta_label(element: Tag) -> str | None:
    for attribute in _META_KEY_ATTRIBUTES:
        key = _attribute_text(element, attribute)
        if key:
            return f"{attribute}={key}"
    return None


def make_finding(kind: Kind, detail: str, tag: str, text: str, mode: ScanMode = ScanMode.STATIC) -> Finding:
    return Finding(
        kind=kind,
        detail=detail,
        tag=tag,
        text=text,
        instruction_phrases=find_instruction_phrases(text),
        mode=mode,
    )


def _is_inside(element: Tag, containers: set[int]) -> bool:
    return any(id(parent) in containers for parent in element.parents)


def scan_html(html: str) -> list[Finding]:
    """Return findings in document order. Findings with no text are dropped.

    Text inside a hidden element is reported once, on the outermost hidden
    element, so nested hiding doesn't inflate the counts.
    """
    soup = BeautifulSoup(html, "html.parser")
    findings: list[Finding] = []
    hidden_containers: set[int] = set()

    for node in soup.descendants:
        if isinstance(node, Comment):
            text = normalize_whitespace(str(node))
            if text:
                findings.append(make_finding(Kind.COMMENT, "<!-- -->", "comment", text))
            continue
        if not isinstance(node, Tag):
            continue

        tag_name = node.name.lower()

        for attribute in _LABEL_ATTRIBUTES:
            text = _attribute_text(node, attribute)
            if text:
                findings.append(make_finding(Kind.LABEL_ATTRIBUTE, attribute, tag_name, text))

        if tag_name == "meta":
            label = _meta_label(node)
            text = _attribute_text(node, "content")
            if label is not None and text:
                findings.append(make_finding(Kind.META, label, tag_name, text))
            continue

        if tag_name in _NON_CONTENT_TAGS or _is_inside(node, hidden_containers):
            continue

        hiding = _element_hiding(node)
        if hiding is not None:
            hidden_containers.add(id(node))
            text = _visible_text(node)
            if text:
                kind, detail = hiding
                findings.append(make_finding(kind, detail, tag_name, text))
            continue

        if tag_name == "noscript":
            text = _visible_text(node)
            if text:
                findings.append(make_finding(Kind.NOSCRIPT, "<noscript>", tag_name, text))

    return findings
