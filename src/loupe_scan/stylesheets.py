"""Work out which elements a page's styles hide, without a browser: the markup scan's stylesheet check.

The check reads the style attribute of every element, the page's <style>
blocks and, when the caller can fetch them, up to MAX_LINKED_STYLESHEETS
stylesheets linked from the page's own host. It applies their rules with
selector matching and the cascade (importance, the style attribute,
specificity, then order) for the simple ways CSS hides text: display:none,
visibility:hidden, opacity:0, font-size:0, an offset or text-indent of
1000px or more off the page, and clip or clip-path that leaves nothing
showing.

It's a partial check, and it would rather miss hidden text than report text
a reader can see. Rules it can't be sure apply to the 1280 by 800 desktop
viewport the rendered scan uses never hide anything here, and when one of
them could show an element another rule hides, the element isn't reported.
Those are rules inside @supports, @layer, @container and @scope, media
queries asking about anything but the media type, width, height and
orientation, and selectors that can't be matched. It doesn't follow
@import, read stylesheets from other hosts, resolve var(), lay the page out
or compare colours, and it can't see what scripts do. A rendered scan
checks all of that in a real browser.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit

import soupsieve
from bs4 import BeautifulSoup, Tag

from loupe_scan.errors import PageFetchError

# The rendered scan's viewport (RenderSettings), which media queries are judged against.
ASSUMED_VIEWPORT_WIDTH_PX: Final[float] = 1280.0
ASSUMED_VIEWPORT_HEIGHT_PX: Final[float] = 800.0
MAX_LINKED_STYLESHEETS: Final[int] = 5

# Offsets at or beyond this many pixels to the left or top push content far
# outside any real viewport. Smaller negative offsets are common in normal
# layouts (pulling a badge over a border, for example) and would be noise.
OFFSCREEN_THRESHOLD_PX: Final[float] = 1000.0
# Browsers resolve em and rem against the font size; 16px is the default
# root size and is close enough for deciding whether something is off-screen.
PX_PER_EM: Final[float] = 16.0

_LENGTH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+))([a-z%]*)$")
_OFFSET_PROPERTIES: Final[tuple[str, ...]] = ("left", "top", "right", "bottom")
# The properties whose values can hide an element. Shorthands are expanded into these.
HIDING_PROPERTIES: Final[frozenset[str]] = frozenset(
    {"display", "visibility", "opacity", "font-size", "position", "text-indent", "clip", "clip-path", *_OFFSET_PROPERTIES}
)
_FONT_SIZE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {"xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large", "xxx-large", "larger", "smaller", "math"}
)
# Elements a page often hides whole in its stylesheet until a script shows
# them, against a flash of unstyled content. The markup can't tell that from
# hiding, and calling the whole page hidden would empty strip mode's markdown.
_WHOLE_PAGE_ELEMENTS: Final[frozenset[str]] = frozenset({"html", "body"})
# Pseudo-elements written with one colon, as CSS 2 allowed.
_LEGACY_PSEUDO_ELEMENTS: Final[frozenset[str]] = frozenset({"before", "after", "first-line", "first-letter"})
# Pseudo-classes whose specificity is that of their most specific argument.
_FORWARDING_PSEUDO_CLASSES: Final[frozenset[str]] = frozenset({"is", "not", "has", "matches", "-webkit-any", "-moz-any"})
# At-rules whose rules may or may not apply, depending on things the markup can't settle.
_UNCERTAIN_AT_RULES: Final[frozenset[str]] = frozenset({"supports", "layer", "container", "scope", "document", "-moz-document"})
_NO_MEDIA_TYPES: Final[frozenset[str]] = frozenset(
    {"print", "speech", "tty", "tv", "projection", "handheld", "braille", "embossed", "aural"}
)

Specificity = tuple[int, int, int]
# (important, from the style attribute, specificity, order): the higher key wins, as in the cascade.
_CascadeKey = tuple[bool, bool, Specificity, int]
_MOST_SPECIFIC: Final[Specificity] = (1_000_000, 0, 0)


class StylesheetSource(Protocol):
    def fetch_stylesheet(self, url: str) -> str:
        """The stylesheet's text. Raises PageFetchError when it can't be read."""
        ...


@dataclass(frozen=True)
class StylesheetCheck:
    """What the stylesheet check read of one page. It's always partial: see the module docstring."""

    style_blocks: int = 0
    linked_read: int = 0
    # Linked from another host, past MAX_LINKED_STYLESHEETS, through @import, with no way to fetch them, or failed.
    linked_not_read: int = 0
    # Rules bearing on hiding that it couldn't be sure apply.
    uncertain_rules: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "style_blocks": self.style_blocks,
            "linked_read": self.linked_read,
            "linked_not_read": self.linked_not_read,
            "uncertain_rules": self.uncertain_rules,
        }

    def description(self) -> str:
        return (
            f"partial check of {_count(self.style_blocks, '<style> block')} and "
            f"{_count(self.linked_read, 'linked stylesheet')}; {self.linked_not_read:,} linked not read, "
            f"{_count(self.uncertain_rules, 'rule')} it couldn't be sure of"
        )


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


# Values


def length_in_px(value: str) -> float | None:
    match = _LENGTH_PATTERN.match(value)
    if match is None:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit in ("", "px"):
        return number
    if unit in ("em", "rem"):
        return number * PX_PER_EM
    return None


def _is_zero_quantity(value: str) -> bool:
    match = _LENGTH_PATTERN.match(value)
    return match is not None and float(match.group(1)) == 0.0


def _split_values(text: str, separator: str | None = None) -> list[str]:
    """text split at separator (whitespace when None), outside brackets and quotes."""
    parts: list[str] = []
    depth = 0
    quote = ""
    current: list[str] = []
    for character in text:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        if depth == 0 and (character == separator or (separator is None and character.isspace())):
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


def _clip_rect_is_empty(value: str) -> bool:
    """True for a clip rect() at most 1px wide or high, as visually hidden text uses."""
    match = re.fullmatch(r"rect\((.*)\)", value)
    if match is None:
        return False
    edges = _split_values(match.group(1).replace(",", " "))
    if len(edges) != 4:
        return False
    lengths = [length_in_px(edge) for edge in edges]
    top, right, bottom, left = lengths
    if top is None or right is None or bottom is None or left is None:
        return False
    return right - left <= 1 or bottom - top <= 1


def _clip_path_is_empty(value: str) -> bool:
    """True for a clip-path inset() of half or more from opposite sides, or a circle() of radius 0."""
    match = re.fullmatch(r"(inset|circle)\((.*)\)", value)
    if match is None:
        return False
    shape, arguments = match.group(1), _split_values(match.group(2))
    if shape == "circle":
        return bool(arguments) and _is_zero_quantity(arguments[0])
    if "round" in arguments:
        arguments = arguments[: arguments.index("round")]
    if not 1 <= len(arguments) <= 4 or not all(argument.endswith("%") for argument in arguments):
        return False
    percentages = [float(argument[:-1]) for argument in arguments if _LENGTH_PATTERN.match(argument)]
    if len(percentages) != len(arguments):
        return False
    # inset() takes 1 to 4 edges, top, right, bottom, left, the way margin does.
    top, right, bottom, left = (percentages * 4)[:4] if len(percentages) == 1 else _four_edges(percentages)
    return top + bottom >= 100 or left + right >= 100


def _four_edges(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


@dataclass(frozen=True)
class HidingReason:
    # Which way of hiding it is, such as "display" or "off-screen", whatever the exact value.
    family: str
    # The properties the reason rests on: if any of them changed, it might not hold.
    properties: tuple[str, ...]
    description: str


def hiding_reasons(values: Mapping[str, str]) -> tuple[HidingReason, ...]:
    """The ways these computed values hide an element, in a fixed order. values hold lowercase longhands."""
    reasons: list[HidingReason] = []
    if values.get("display") == "none":
        reasons.append(HidingReason("display", ("display",), "display:none"))
    if values.get("visibility") in ("hidden", "collapse"):
        reasons.append(HidingReason("visibility", ("visibility",), f"visibility:{values['visibility']}"))
    if _is_zero_quantity(values.get("font-size", "")):
        reasons.append(HidingReason("font-size", ("font-size",), "font-size:0"))
    if _is_zero_quantity(values.get("opacity", "")):
        reasons.append(HidingReason("opacity", ("opacity",), "opacity:0"))
    positioned = values.get("position") in ("absolute", "fixed")
    if positioned:
        for name in _OFFSET_PROPERTIES:
            offset = length_in_px(values.get(name, ""))
            if offset is not None and offset <= -OFFSCREEN_THRESHOLD_PX:
                reasons.append(HidingReason("off-screen", ("position", name), f"off-screen {name}:{values[name]}"))
                break
    indent = length_in_px(values.get("text-indent", ""))
    if indent is not None and indent <= -OFFSCREEN_THRESHOLD_PX:
        reasons.append(HidingReason("text-indent", ("text-indent",), f"off-screen text-indent:{values['text-indent']}"))
    if positioned and _clip_rect_is_empty(values.get("clip", "")):
        reasons.append(HidingReason("clip", ("position", "clip"), "clipped to nothing by clip"))
    if _clip_path_is_empty(values.get("clip-path", "")):
        reasons.append(HidingReason("clip-path", ("clip-path",), "clipped to nothing by clip-path"))
    return tuple(reasons)


# Declarations


@dataclass(frozen=True)
class Declaration:
    name: str
    value: str
    important: bool


_IMPORTANT: Final[re.Pattern[str]] = re.compile(r"!\s*important\s*$")


def _font_size_of(font: str) -> str:
    """The font size the font shorthand sets: "medium" when it names none, as a system font does."""
    for token in _split_values(font):
        if token.startswith(("'", '"')):
            break
        size = token.split("/", 1)[0] if "/" in token else token
        match = _LENGTH_PATTERN.match(size)
        if size in _FONT_SIZE_KEYWORDS or (match is not None and (match.group(2) or float(match.group(1)) == 0.0)):
            return size
    return "medium"


def _longhands(name: str, value: str) -> list[tuple[str, str]]:
    """The hiding properties a declaration sets, with shorthands expanded."""
    if name in HIDING_PROPERTIES:
        return [(name, value)]
    if name == "font":
        return [("font-size", _font_size_of(value))]
    if name == "inset":
        edges = _split_values(value)
        if not 1 <= len(edges) <= 4:
            return []
        top, right, bottom, left = (edges * 4)[:4] if len(edges) == 1 else _edges(edges)
        return [("top", top), ("right", right), ("bottom", bottom), ("left", left)]
    if name == "all":
        return [(longhand, value) for longhand in sorted(HIDING_PROPERTIES)]
    return []


def _edges(values: list[str]) -> tuple[str, str, str, str]:
    if len(values) == 2:
        return values[0], values[1], values[0], values[1]
    if len(values) == 3:
        return values[0], values[1], values[2], values[1]
    return values[0], values[1], values[2], values[3]


def parse_declarations(text: str) -> list[Declaration]:
    """The hiding properties a declaration list sets, in order, lowercased and with shorthands expanded."""
    declarations: list[Declaration] = []
    for item in _split_values(text, ";"):
        name, separator, value = item.partition(":")
        if not separator:
            continue
        name = name.strip().lower()
        value = value.strip().lower()
        important = _IMPORTANT.search(value) is not None
        value = _IMPORTANT.sub("", value).strip()
        if not name or not value:
            continue
        declarations.extend(Declaration(longhand, part, important) for longhand, part in _longhands(name, value))
    return declarations


# Selectors


class _PseudoElement(Exception):
    """The selector styles a pseudo-element, never the element's own text."""


class _UnreadableSelector(Exception):
    pass


_HEX_ESCAPE: Final[re.Pattern[str]] = re.compile(r"([0-9a-fA-F]{1,6})[ \t\n\r\f]?")


def _read_name(text: str, start: int) -> tuple[int, str]:
    """The CSS name starting at start, with its escapes decoded, and the index after it."""
    characters: list[str] = []
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\":
            hexadecimal = _HEX_ESCAPE.match(text, index + 1)
            if hexadecimal is not None:
                code = int(hexadecimal.group(1), 16)
                characters.append(chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�")
                index = hexadecimal.end()
            elif index + 1 < len(text):
                characters.append(text[index + 1])
                index += 2
            else:
                index += 1
        elif character.isalnum() or character in "-_" or ord(character) > 127:
            characters.append(character)
            index += 1
        else:
            break
    return index, "".join(characters)


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """The index of the bracket closing the one at start, outside quotes and escapes."""
    depth = 0
    quote = ""
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise _UnreadableSelector("unbalanced brackets")


def split_selector_list(text: str) -> list[str]:
    return _split_values(text, ",")


def specificity(selector: str) -> Specificity:
    """The specificity of one complex selector. Raises _PseudoElement or _UnreadableSelector."""
    ids = classes = types = 0
    index = 0
    while index < len(selector):
        character = selector[index]
        if character == "#":
            index, _ = _read_name(selector, index + 1)
            ids += 1
        elif character == ".":
            index, _ = _read_name(selector, index + 1)
            classes += 1
        elif character == "[":
            index = _closing(selector, index, "[", "]") + 1
            classes += 1
        elif character == ":":
            if selector.startswith("::", index):
                raise _PseudoElement()
            index, name = _read_name(selector, index + 1)
            name = name.lower()
            if index < len(selector) and selector[index] == "(":
                end = _closing(selector, index, "(", ")")
                argument = selector[index + 1 : end]
                index = end + 1
                if name in _FORWARDING_PSEUDO_CLASSES:
                    found = _most_specific(split_selector_list(argument))
                    ids, classes, types = ids + found[0], classes + found[1], types + found[2]
                elif name in ("nth-child", "nth-last-child"):
                    classes += 1
                    _, of, among = argument.partition(" of ")
                    if of:
                        found = _most_specific(split_selector_list(among))
                        ids, classes, types = ids + found[0], classes + found[1], types + found[2]
                elif name != "where":
                    classes += 1
            elif name in _LEGACY_PSEUDO_ELEMENTS:
                raise _PseudoElement()
            elif not name:
                raise _UnreadableSelector("a colon with no name")
            else:
                classes += 1
        elif character.isalpha() or character in "-_\\" or ord(character) > 127:
            index, _ = _read_name(selector, index)
            types += 1
        elif character in "*|>+~ \t\n\r\f":
            index += 1
        else:
            raise _UnreadableSelector(f"unexpected {character!r}")
    return (ids, classes, types)


def _most_specific(selectors: list[str]) -> Specificity:
    return max((specificity(selector) for selector in selectors), default=(0, 0, 0))


def _top_level_compounds(selector: str) -> list[str]:
    """selector split into compound selectors at its combinators, outside brackets and quotes."""
    compounds: list[str] = []
    current: list[str] = []
    depth = 0
    quote = ""
    index = 0
    while index < len(selector):
        character = selector[index]
        if character == "\\" and index + 1 < len(selector):
            current.append(selector[index : index + 2])
            index += 2
            continue
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and (character.isspace() or character in ">+~"):
            compounds.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    compounds.append("".join(current))
    return [compound for compound in compounds if compound]


def _without_arguments(compound: str) -> str:
    """compound with every bracketed part taken out, keeping escapes whole."""
    kept: list[str] = []
    depth = 0
    index = 0
    while index < len(compound):
        character = compound[index]
        if character == "\\" and index + 1 < len(compound):
            if depth == 0:
                kept.append(compound[index : index + 2])
            index += 2
            continue
        if character in "([":
            depth += 1
        elif character in ")]":
            depth = max(0, depth - 1)
        elif depth == 0:
            kept.append(character)
        index += 1
    return "".join(kept)


_IndexKey = tuple[str, str]
_ANY: Final[_IndexKey] = ("any", "")


def index_key(selector: str) -> _IndexKey:
    """Something every element the selector matches must have: an id, a class or a tag name, or nothing."""
    compounds = _top_level_compounds(selector)
    if not compounds:
        return _ANY
    compound = _without_arguments(compounds[-1])
    found: dict[str, str] = {}
    index = 0
    while index < len(compound):
        character = compound[index]
        if character in "#.":
            index, name = _read_name(compound, index + 1)
            kind = "id" if character == "#" else "class"
            if name:
                found.setdefault(kind, name)
        elif index == 0 and (character.isalpha() or character in "-_\\" or ord(character) > 127):
            index, name = _read_name(compound, index)
            if name:
                found["tag"] = name.lower()
        else:
            index += 1
    for kind in ("id", "class", "tag"):
        if kind in found:
            return (kind, found[kind])
    return _ANY


def _element_keys(element: Tag) -> list[_IndexKey]:
    keys: list[_IndexKey] = [("tag", element.name.lower()), _ANY]
    element_id = element.get("id")
    if isinstance(element_id, str) and element_id:
        keys.append(("id", element_id))
    keys.extend(("class", name) for name in dict.fromkeys(_attribute(element, "class").split()))
    return keys


# Stylesheets


@dataclass(frozen=True)
class _Rule:
    selector: str
    # None when the selector couldn't be read, so the rule might match any element.
    specificity: Specificity | None
    declarations: tuple[Declaration, ...]
    # True when the rule applies for certain at the assumed viewport and its selector can be matched.
    certain: bool
    order: int
    compiled: soupsieve.SoupSieve | None


@dataclass
class _RuleList:
    rules: list[_Rule] = field(default_factory=list)
    uncertain: int = 0
    imports: int = 0
    order: int = 0

    def add(self, selectors: Sequence[str], declarations: Sequence[Declaration], certain: bool) -> None:
        if not declarations:
            return
        for selector in selectors:
            self.order += 1
            try:
                found: Specificity | None = specificity(selector)
            except _PseudoElement:
                continue
            except _UnreadableSelector:
                found = None
            compiled: soupsieve.SoupSieve | None = None
            if found is not None:
                try:
                    compiled = soupsieve.compile(selector)
                except (soupsieve.SelectorSyntaxError, NotImplementedError, ValueError, TypeError):
                    compiled = None
            applies = certain and compiled is not None
            if not applies:
                self.uncertain += 1
            self.rules.append(_Rule(selector, found, tuple(declarations), applies, self.order, compiled))


_COMMENT_OR_STRING: Final[re.Pattern[str]] = re.compile(
    r"(\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')|/\*.*?(?:\*/|\Z)", re.DOTALL
)
_STRUCTURE: Final[re.Pattern[str]] = re.compile(r"\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?|\\.|[{};()]", re.DOTALL)


def _without_comments(css: str) -> str:
    return _COMMENT_OR_STRING.sub(lambda match: match.group(1) or " ", css)


@dataclass(frozen=True)
class _Outline:
    """Where a stylesheet's blocks and statements end, found in one pass."""

    css: str
    # Positions of {, } and ; outside brackets, strings and escapes, in order.
    positions: list[int]
    closing: dict[int, int]


def _outline(css: str) -> _Outline:
    positions: list[int] = []
    closing: dict[int, int] = {}
    open_braces: list[int] = []
    parentheses = 0
    for match in _STRUCTURE.finditer(css):
        token = match.group()
        if token == "(":
            parentheses += 1
        elif token == ")":
            parentheses = max(0, parentheses - 1)
        elif token in "{};" and len(token) == 1 and parentheses == 0:
            position = match.start()
            positions.append(position)
            if token == "{":
                open_braces.append(position)
            elif token == "}" and open_braces:
                closing[open_braces.pop()] = position
    for position in open_braces:
        closing[position] = len(css)
    return _Outline(css, positions, closing)


# A statement or block: its prelude, and the span of its block or None.
_Item = tuple[str, tuple[int, int] | None]


def _items(outline: _Outline, start: int, end: int) -> list[_Item]:
    """The statements and blocks between start and end: (prelude, the block's span or None)."""
    items: list[_Item] = []
    css = outline.css
    cursor = start
    index = bisect.bisect_left(outline.positions, start)
    while index < len(outline.positions) and outline.positions[index] < end:
        position = outline.positions[index]
        character = css[position]
        if character == ";":
            items.append((css[cursor:position], None))
            cursor = position + 1
        elif character == "{":
            close = min(outline.closing.get(position, end), end)
            items.append((css[cursor:position], (position + 1, close)))
            cursor = close + 1
            index = bisect.bisect_left(outline.positions, cursor)
            continue
        else:
            # A stray } closes nothing at this level.
            cursor = position + 1
        index += 1
    if css[cursor:end].strip():
        items.append((css[cursor:end], None))
    return items


def _nested(parents: Sequence[str], selector: str) -> str:
    """A nested rule's selector, written out in full as CSS nesting defines it."""
    parent = f":is({', '.join(parents)})"
    selector = selector.strip()
    if "&" in selector:
        return selector.replace("&", parent)
    return f"{parent} {selector}"


def _collect(outline: _Outline, items: list[_Item], parents: Sequence[str] | None, certain: bool, rules: _RuleList) -> None:
    if parents is not None:
        declarations = [
            declaration
            for prelude, block in items
            if block is None and not prelude.strip().startswith("@")
            for declaration in parse_declarations(prelude)
        ]
        rules.add(parents, declarations, certain)
    for prelude, block in items:
        prelude = prelude.strip()
        if prelude.startswith("@"):
            name_end, name = _read_name(prelude, 1)
            name = name.lower()
            if block is None:
                rules.imports += name == "import"
                continue
            inner = _items(outline, *block)
            if name == "media":
                applies = media_applies(prelude[name_end:])
                if applies is not False:
                    _collect(outline, inner, parents, certain and applies is True, rules)
            elif name in _UNCERTAIN_AT_RULES:
                _collect(outline, inner, parents, False, rules)
            continue
        if block is None:
            continue
        selectors = split_selector_list(prelude)
        if parents is not None:
            selectors = [_nested(parents, selector) for selector in selectors]
        if selectors:
            _collect(outline, _items(outline, *block), selectors, certain, rules)


def parse_stylesheet(css: str, rules: _RuleList, *, certain: bool = True) -> None:
    """Add css's rules that bear on hiding to rules, in order."""
    outline = _outline(_without_comments(css))
    _collect(outline, _items(outline, 0, len(outline.css)), None, certain, rules)


# Media queries


_RANGE: Final[re.Pattern[str]] = re.compile(
    r"^(?:(?P<low>[^<>=]+?)\s*(?P<low_op><=|<|>=|>|=)\s*)?(?P<name>width|height)"
    r"(?:\s*(?P<high_op><=|<|>=|>|=)\s*(?P<high>[^<>=]+?))?$"
)


def _compare(left: float, operator: str, right: float) -> bool:
    return {
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
        "=": left == right,
    }[operator]


def _feature(text: str) -> bool | None:
    text = " ".join(text.split())
    name, colon, value = text.partition(":")
    if colon:
        name, value = name.strip(), value.strip()
        if name in ("min-width", "max-width", "width", "min-height", "max-height", "height"):
            size = length_in_px(value)
            if size is None:
                return None
            actual = ASSUMED_VIEWPORT_WIDTH_PX if name.endswith("width") else ASSUMED_VIEWPORT_HEIGHT_PX
            operator = ">=" if name.startswith("min-") else "<=" if name.startswith("max-") else "="
            return _compare(actual, operator, size)
        if name == "orientation" and value in ("landscape", "portrait"):
            return (value == "landscape") == (ASSUMED_VIEWPORT_WIDTH_PX >= ASSUMED_VIEWPORT_HEIGHT_PX)
        return None
    match = _RANGE.match(text)
    if match is None or (match.group("low") is None and match.group("high") is None):
        return None
    actual = ASSUMED_VIEWPORT_WIDTH_PX if match.group("name") == "width" else ASSUMED_VIEWPORT_HEIGHT_PX
    result = True
    if match.group("low") is not None:
        low = length_in_px(match.group("low").strip())
        if low is None:
            return None
        result = result and _compare(low, match.group("low_op"), actual)
    if match.group("high") is not None:
        high = length_in_px(match.group("high").strip())
        if high is None:
            return None
        result = result and _compare(actual, match.group("high_op"), high)
    return result


def _media_query(query: str) -> bool | None:
    words = query.split(None, 1)
    negated = bool(words) and words[0] == "not"
    if words and words[0] in ("not", "only"):
        query = words[1] if len(words) > 1 else ""
    if re.search(r"\bor\b|\bnot\b", query):
        return None
    result: bool | None = True
    for part in re.split(r"\s+and\s+", query.strip()):
        part = part.strip()
        if part.startswith("(") and part.endswith(")"):
            answer = _feature(part[1:-1])
        elif part in ("all", "screen"):
            answer = True
        elif part in _NO_MEDIA_TYPES:
            answer = False
        else:
            answer = None
        if answer is False:
            result = False
            break
        if answer is None:
            result = None
    if result is None:
        return None
    return not result if negated else result


def media_applies(media: str) -> bool | None:
    """Whether a media query list holds at the assumed desktop viewport: None when it can't be told."""
    queries = split_selector_list(media.strip().lower())
    if not queries:
        return True
    answers = [_media_query(query) for query in queries]
    if any(answer is True for answer in answers):
        return True
    if all(answer is False for answer in answers):
        return False
    return None


# Applying the rules


@dataclass(frozen=True)
class StyleHiding:
    reasons: tuple[str, ...]
    # True when a rule in a stylesheet, not the style attribute alone, decides at least one reason.
    from_stylesheet: bool


class StyleCascade:
    """A page's rules that bear on hiding, applied element by element with the cascade."""

    def __init__(self, rules: Sequence[_Rule]) -> None:
        self._buckets: dict[_IndexKey, list[_Rule]] = {}
        for rule in rules:
            self._buckets.setdefault(index_key(rule.selector), []).append(rule)

    def _matching(self, element: Tag) -> Iterable[_Rule]:
        for key in _element_keys(element):
            for rule in self._buckets.get(key, ()):
                if rule.compiled is None or rule.compiled.match(element):
                    yield rule

    def hiding(self, element: Tag) -> StyleHiding | None:
        """How element's styles hide it, or None. Only what's certain counts: see the module docstring."""
        winners: dict[str, tuple[_CascadeKey, str, bool]] = {}
        # Values that might apply instead, with the most a rule's key could be.
        alternatives: dict[str, list[tuple[_CascadeKey, str]]] = {}
        whole_page = element.name.lower() in _WHOLE_PAGE_ELEMENTS
        if not whole_page:
            for rule in self._matching(element):
                for declaration in rule.declarations:
                    key: _CascadeKey = (
                        declaration.important,
                        False,
                        rule.specificity if rule.specificity is not None else _MOST_SPECIFIC,
                        rule.order,
                    )
                    if rule.certain:
                        _keep_highest(winners, declaration.name, key, declaration.value, True)
                    else:
                        alternatives.setdefault(declaration.name, []).append((key, declaration.value))
        style = element.get("style")
        if isinstance(style, str):
            _add_style_attribute(winners, style)
        return _decide(winners, alternatives)


def _add_style_attribute(winners: dict[str, tuple[_CascadeKey, str, bool]], style: str) -> None:
    for position, declaration in enumerate(parse_declarations(style)):
        key: _CascadeKey = (declaration.important, True, (0, 0, 0), position)
        _keep_highest(winners, declaration.name, key, declaration.value, False)


def _decide(
    winners: Mapping[str, tuple[_CascadeKey, str, bool]], alternatives: Mapping[str, list[tuple[_CascadeKey, str]]]
) -> StyleHiding | None:
    values = {name: value for name, (_, value, _) in winners.items()}
    kept: list[str] = []
    from_stylesheet = False
    for reason in hiding_reasons(values):
        if _could_be_undone(reason, values, winners, alternatives):
            continue
        kept.append(reason.description)
        from_stylesheet = from_stylesheet or any(winners[name][2] for name in reason.properties if name in winners)
    if not kept:
        return None
    return StyleHiding(tuple(kept), from_stylesheet)


def style_attribute_hiding_reasons(style: str) -> tuple[str, ...]:
    """The ways a style attribute hides its element on its own, with !important beating a later declaration."""
    winners: dict[str, tuple[_CascadeKey, str, bool]] = {}
    _add_style_attribute(winners, style)
    hiding = _decide(winners, {})
    return () if hiding is None else hiding.reasons


def _keep_highest(
    winners: dict[str, tuple[_CascadeKey, str, bool]], name: str, key: _CascadeKey, value: str, from_stylesheet: bool
) -> None:
    current = winners.get(name)
    if current is None or key > current[0]:
        winners[name] = (key, value, from_stylesheet)


def _could_be_undone(
    reason: HidingReason,
    values: Mapping[str, str],
    winners: Mapping[str, tuple[_CascadeKey, str, bool]],
    alternatives: Mapping[str, list[tuple[_CascadeKey, str]]],
) -> bool:
    """True when a rule that might apply could win one of the reason's properties and undo it."""
    for name in reason.properties:
        current = winners.get(name)
        for key, value in alternatives.get(name, ()):
            if current is not None and key <= current[0]:
                continue
            changed = {**values, name: value}
            if not any(other.family == reason.family for other in hiding_reasons(changed)):
                return True
    return False


# A page's stylesheets


@dataclass(frozen=True)
class PageStyles:
    cascade: StyleCascade
    check: StylesheetCheck


def _inside(element: Tag, names: frozenset[str]) -> bool:
    return any(parent.name in names for parent in element.parents if isinstance(parent, Tag))


def _attribute(element: Tag, name: str) -> str:
    value = element.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value if isinstance(value, str) else ""


def _is_css_type(element: Tag) -> bool:
    kind = _attribute(element, "type").strip().lower()
    return not kind or kind == "text/css"


def _base_url(soup: BeautifulSoup, page_url: str) -> str:
    base = soup.find("base", href=True)
    if isinstance(base, Tag):
        try:
            return urljoin(page_url, _attribute(base, "href").strip())
        except ValueError:
            return page_url
    return page_url


def _same_host(url: str, page_url: str) -> bool:
    try:
        sheet, page = urlsplit(url), urlsplit(page_url)
    except ValueError:
        return False
    if sheet.scheme not in ("http", "https") or not sheet.hostname or sheet.hostname != page.hostname:
        return False
    # A browser won't load an http stylesheet into an https page.
    return not (page.scheme == "https" and sheet.scheme != "https")


_NO_ELEMENT_STYLES: Final[frozenset[str]] = frozenset({"template", "noscript"})


def read_page_styles(soup: BeautifulSoup, page_url: str | None, source: StylesheetSource | None) -> PageStyles:
    """Every rule bearing on hiding from soup's <style> blocks and the same-host stylesheets source can fetch.

    page_url is the address the HTML came from, after any redirects. Without
    it or a source, linked stylesheets are counted but not read.
    """
    rules = _RuleList()
    style_blocks = linked_read = linked_not_read = 0
    fetched: dict[str, str | None] = {}
    base = _base_url(soup, page_url) if page_url is not None else None
    for element in soup.find_all(["style", "link"]):
        if not isinstance(element, Tag) or _inside(element, _NO_ELEMENT_STYLES) or not _is_css_type(element):
            continue
        applies = media_applies(_attribute(element, "media"))
        if element.name == "style":
            style_blocks += 1
            if applies is not False:
                parse_stylesheet(element.get_text(), rules, certain=applies is True)
            continue
        relations = _attribute(element, "rel").lower().split()
        href = _attribute(element, "href").strip()
        if "stylesheet" not in relations or "alternate" in relations or element.has_attr("disabled") or not href:
            continue
        if applies is False:
            continue
        url = urljoin(base, href) if base is not None else None
        text = _fetch_once(fetched, url, page_url, source, len(fetched) >= MAX_LINKED_STYLESHEETS)
        if text is None:
            linked_not_read += 1
            continue
        linked_read += 1
        parse_stylesheet(text, rules, certain=applies is True)
    check = StylesheetCheck(
        style_blocks=style_blocks,
        linked_read=linked_read,
        linked_not_read=linked_not_read + rules.imports,
        uncertain_rules=rules.uncertain,
    )
    return PageStyles(StyleCascade(rules.rules), check)


def _fetch_once(
    fetched: dict[str, str | None],
    url: str | None,
    page_url: str | None,
    source: StylesheetSource | None,
    over_limit: bool,
) -> str | None:
    if url is None or page_url is None or source is None or not _same_host(url, page_url):
        return None
    if url in fetched:
        return fetched[url]
    if over_limit:
        return None
    try:
        text: str | None = source.fetch_stylesheet(url)
    except PageFetchError:
        text = None
    fetched[url] = text
    return text
