"""Work out which elements a page's styles hide, without a browser: the markup scan's stylesheet check.

The check reads the style attribute of every element, the page's <style>
blocks and, when the caller can fetch them, up to MAX_LINKED_STYLESHEETS
stylesheets linked from the page's own host. It applies their rules with
selector matching and the cascade (importance, the style attribute,
specificity, then order) for the simple ways CSS hides text: display:none,
visibility:hidden, opacity:0, font-size:0, an offset or text-indent of
1000px or more off the page, and clip or clip-path that leaves nothing
showing. It also flags text coloured like its background, with a WCAG
contrast ratio under 1.5:1, where the styles declare both the text's colour,
on the element or inherited, and a solid background behind it; a colour it
can't read, an image or a gradient of several colours leaves it unjudged.

A shadow root declared in the HTML, with <template shadowrootmode>, is
checked with the <style> blocks and linked stylesheets inside it, and the
page's own rules don't reach it, as in a browser. Its selectors are matched
in a copy of the shadow root on its own, so they can't see past it. That's
partial too: :host, ::slotted and ::part rules can't be matched, the colours
the host passes down aren't followed, and the host's own children are judged
as if every one of them were slotted in.

It's a partial check, and it would rather miss hidden text than report text
a reader can see. Rules it can't be sure apply to the 1280 by 800 desktop
viewport the rendered scan uses never hide anything here, and when one of
them could show an element another rule hides, the element isn't reported.
Those are rules inside @supports, @layer, @container and @scope, media
queries asking about anything but the media type, width, height and
orientation, and selectors that can't be matched. It doesn't follow
@import, read stylesheets from other hosts, resolve var() or lay the page
out, and it can't see what scripts do. A rendered scan checks all of that in
a real browser.
"""

from __future__ import annotations

import bisect
import copy
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol
from urllib.parse import urljoin, urlsplit

import soupsieve
from bs4 import BeautifulSoup, Tag

from loupe_scan.errors import PageFetchError
from loupe_scan.signals import Signal

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
# The properties the colour check reads: the text's colour and what's painted behind it.
COLOUR_PROPERTIES: Final[frozenset[str]] = frozenset({"color", "background-color", "background-image"})
# What only a browser can judge: where an element is placed, and styles that can hide text in ways the checks
# don't evaluate. loupe_scan.signals names them, for fetch_page's auto mode.
_MARGINS: Final[tuple[str, ...]] = ("margin-top", "margin-right", "margin-bottom", "margin-left")
_GRID_PLACEMENT: Final[frozenset[str]] = frozenset(
    {
        "grid-area", "grid-row", "grid-column", "grid-row-start", "grid-row-end", "grid-column-start",
        "grid-column-end",
    }
)
_TRANSFORMS: Final[frozenset[str]] = frozenset({"transform", "translate", "rotate", "scale"})
_PAINT_EFFECTS: Final[dict[str, frozenset[str]]] = {
    # Each property, and the values that leave text alone.
    "filter": frozenset({"none"}),
    "backdrop-filter": frozenset({"none"}),
    "mix-blend-mode": frozenset({"normal"}),
    "mask": frozenset({"none"}),
    "mask-image": frozenset({"none"}),
    "-webkit-mask": frozenset({"none"}),
    "-webkit-mask-image": frozenset({"none"}),
    "content-visibility": frozenset({"visible"}),
    "-webkit-text-fill-color": frozenset({"currentcolor", "inherit", "initial", "unset"}),
    "contain": frozenset({"none", "layout", "style", "size", "inline-size"}),
}
_SIGNAL_PROPERTIES: Final[frozenset[str]] = frozenset(
    {"z-index", "overflow-x", "overflow-y", *_MARGINS, *_GRID_PLACEMENT, *_TRANSFORMS, *_PAINT_EFFECTS}
)
_JUDGED_PROPERTIES: Final[frozenset[str]] = HIDING_PROPERTIES | COLOUR_PROPERTIES
_COLLECTED_PROPERTIES: Final[frozenset[str]] = _JUDGED_PROPERTIES | _SIGNAL_PROPERTIES
# Text whose contrast with the colour behind it is under this ratio is too
# faint to see: the same threshold as the rendered scan's, in visibility.js.
MIN_CONTRAST: Final[float] = 1.5
CONTRAST_REASON: Final[str] = "coloured like its background"
# The CSS level 1 colour names. Any other name leaves a colour unjudged.
_NAMED_COLOURS: Final[dict[str, tuple[int, int, int]]] = {
    "black": (0, 0, 0), "silver": (192, 192, 192), "gray": (128, 128, 128), "grey": (128, 128, 128),
    "white": (255, 255, 255), "maroon": (128, 0, 0), "red": (255, 0, 0), "purple": (128, 0, 128),
    "fuchsia": (255, 0, 255), "green": (0, 128, 0), "lime": (0, 255, 0), "olive": (128, 128, 0),
    "yellow": (255, 255, 0), "navy": (0, 0, 128), "blue": (0, 0, 255), "teal": (0, 128, 128), "aqua": (0, 255, 255),
}
_HEX_COLOUR: Final[re.Pattern[str]] = re.compile(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})")
_RGB_COLOUR: Final[re.Pattern[str]] = re.compile(
    r"rgba?\(\s*([\d.]+%?)[\s,]+([\d.]+%?)[\s,]+([\d.]+%?)(?:\s*[,/]\s*([\d.]+%?))?\s*\)"
)
_GRADIENT: Final[re.Pattern[str]] = re.compile(r"(?:repeating-)?(?:linear|radial|conic)-gradient\((.*)\)")
# Vendor-prefixed gradients, which the colour check reads as a picture it can't judge.
_PREFIXED_GRADIENT: Final[re.Pattern[str]] = re.compile(r"-(?:webkit|moz|ms|o)-(?:(?:repeating-)?(?:linear|radial)-)?gradient\(")
# What the background shorthand holds besides a colour and an image: the image's absence, its position and size and
# the slash between them, how it repeats and scrolls, and the boxes it's drawn in and clipped to.
_BACKGROUND_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "none", "left", "right", "center", "top", "bottom", "auto", "cover", "contain",
        "repeat", "repeat-x", "repeat-y", "no-repeat", "space", "round", "scroll", "fixed", "local",
        "border-box", "padding-box", "content-box", "text", "border-area",
    }
)
_IMAGE_FUNCTIONS: Final[tuple[str, ...]] = (
    "url(", "image(", "image-set(", "-webkit-image-set(", "cross-fade(", "-webkit-cross-fade(", "element(",
    "-moz-element(", "paint(", "src(",
)
# Values that set a background back to its initial value, transparent and with no image: background isn't inherited,
# and browsers' own stylesheets give ordinary elements none.
_RESETTING_KEYWORDS: Final[frozenset[str]] = frozenset({"initial", "unset", "revert", "revert-layer"})
# Functions that give a length in a position or a size.
_MATH_FUNCTIONS: Final[tuple[str, ...]] = ("calc(", "-webkit-calc(", "-moz-calc(", "min(", "max(", "clamp(", "env(")
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
    # Shadow roots declared in the HTML, each checked with its own styles. style_blocks and the linked counts
    # include theirs.
    shadow_roots: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "style_blocks": self.style_blocks,
            "linked_read": self.linked_read,
            "linked_not_read": self.linked_not_read,
            "uncertain_rules": self.uncertain_rules,
            "shadow_roots": self.shadow_roots,
        }

    def description(self) -> str:
        text = (
            f"partial check of {_count(self.style_blocks, '<style> block')} and "
            f"{_count(self.linked_read, 'linked stylesheet')}; {self.linked_not_read:,} linked not read, "
            f"{_count(self.uncertain_rules, 'rule')} it couldn't be sure of"
        )
        if self.shadow_roots:
            text += (
                f"; {_count(self.shadow_roots, 'shadow root')} checked with its own styles, "
                "partially (no :host, ::slotted or slots)"
            )
        return text


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


def _background_longhands(value: str) -> list[tuple[str, str]]:
    """The background shorthand's colour and image. Several layers, or an image, leave the image unjudged.

    A part that isn't a position, size, repeat, attachment, box or image is the colour, even one parse_colour can't
    read, such as light-dark() or color-mix(): read as missing, it would make the background transparent.
    """
    if _split_values(value, ",")[1:] or value == "inherit":
        return [("background-color", "unjudged"), ("background-image", "unjudged")]
    if value in _RESETTING_KEYWORDS:
        return [("background-color", "transparent"), ("background-image", "none")]
    colour = "transparent"
    image = "none"
    colours_seen = 0
    for token in _split_values(value):
        for part in _split_values(token, "/"):
            if part in _BACKGROUND_KEYWORDS or _LENGTH_PATTERN.match(part) or part.startswith(_MATH_FUNCTIONS):
                continue
            if part.startswith(_IMAGE_FUNCTIONS) or _GRADIENT.fullmatch(part) or _PREFIXED_GRADIENT.match(part):
                image = part
                continue
            colours_seen += 1
            colour = part if colours_seen == 1 else "unjudged"
    return [("background-color", colour), ("background-image", image)]


def _longhands(name: str, value: str) -> list[tuple[str, str]]:
    """The properties a declaration sets that the checks read, with shorthands expanded, and custom properties."""
    if name in _COLLECTED_PROPERTIES or name.startswith("--"):
        return [(name, value)]
    if name == "background":
        return _background_longhands(value)
    if name == "font":
        return [("font-size", _font_size_of(value))]
    if name == "inset":
        edges = _split_values(value)
        if not 1 <= len(edges) <= 4:
            return []
        top, right, bottom, left = (edges * 4)[:4] if len(edges) == 1 else _edges(edges)
        return [("top", top), ("right", right), ("bottom", bottom), ("left", left)]
    if name == "margin":
        edges = _split_values(value)
        if not 1 <= len(edges) <= 4:
            return []
        return list(zip(_MARGINS, (edges * 4)[:4] if len(edges) == 1 else _edges(edges)))
    if name == "overflow":
        axes = _split_values(value)
        if not 1 <= len(axes) <= 2:
            return []
        return [("overflow-x", axes[0]), ("overflow-y", axes[-1])]
    if name == "all":
        return [(longhand, value) for longhand in sorted(_COLLECTED_PROPERTIES)]
    return []


Colour = tuple[float, float, float, float]


def _channel(text: str, scale: float) -> float | None:
    try:
        value = float(text[:-1]) * scale / 100 if text.endswith("%") else float(text)
    except ValueError:
        return None
    return min(max(value, 0.0), scale)


def parse_colour(value: str) -> Colour | None:
    """(r, g, b, alpha) for a hex, rgb() or rgba() colour, a CSS level 1 name or transparent; None for anything else."""
    value = value.strip().lower()
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if value in _NAMED_COLOURS:
        named = _NAMED_COLOURS[value]
        return (float(named[0]), float(named[1]), float(named[2]), 1.0)
    hexadecimal = _HEX_COLOUR.fullmatch(value)
    if hexadecimal is not None:
        digits = hexadecimal.group(1)
        if len(digits) in (3, 4):
            digits = "".join(digit * 2 for digit in digits)
        parts = [float(int(digits[index : index + 2], 16)) for index in range(0, len(digits), 2)]
        return (parts[0], parts[1], parts[2], parts[3] / 255 if len(parts) == 4 else 1.0)
    functional = _RGB_COLOUR.fullmatch(value)
    if functional is None:
        return None
    red, green, blue = (_channel(functional.group(index), 255.0) for index in (1, 2, 3))
    opacity = 1.0 if functional.group(4) is None else _channel(functional.group(4), 1.0)
    if red is None or green is None or blue is None or opacity is None:
        return None
    return (red, green, blue, opacity)


def single_colour_gradient(value: str) -> Colour | None:
    """The one colour a gradient paints when all its stops are that colour, or None."""
    match = _GRADIENT.fullmatch(value.strip().lower())
    if match is None:
        return None
    colours = [parse_colour(_split_values(stop)[0]) for stop in _split_values(match.group(1), ",") if _split_values(stop)]
    stops = [colour for colour in colours if colour is not None]
    # The first argument may be a direction or a shape rather than a colour; any other non-colour leaves it unjudged.
    if not stops or len(stops) < len(colours) - 1 or (len(stops) < len(colours) and colours[0] is not None):
        return None
    return stops[0] if all(stop == stops[0] for stop in stops) else None


def _blend(top: Colour, bottom: Colour) -> Colour:
    alpha = top[3] + bottom[3] * (1 - top[3])
    if alpha == 0:
        return (0.0, 0.0, 0.0, 0.0)

    def channel(index: int) -> float:
        return (top[index] * top[3] + bottom[index] * bottom[3] * (1 - top[3])) / alpha

    return (channel(0), channel(1), channel(2), alpha)


def _luminance(colour: Colour) -> float:
    def linear(value: float) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * linear(colour[0]) + 0.7152 * linear(colour[1]) + 0.0722 * linear(colour[2])


def contrast_ratio(text: Colour, background: Colour) -> float:
    """WCAG's contrast ratio between text drawn over an opaque background and that background."""
    drawn = _luminance(_blend(text, background))
    behind = _luminance(background)
    return (max(drawn, behind) + 0.05) / (min(drawn, behind) + 0.05)


_WHITE: Final[Colour] = (255.0, 255.0, 255.0, 1.0)
# A browser's default text colour.
_BLACK: Final[Colour] = (0.0, 0.0, 0.0, 1.0)
# Opacity multiplies down the tree, and the rendered scan calls text under 0.05 hidden. Under this, a browser
# should settle it.
_FADED_BELOW: Final[float] = 0.25
_NOT_PLACED: Final[frozenset[str]] = frozenset({"static", "initial", "unset", "inherit", "revert", "revert-layer"})
_NO_OFFSET: Final[frozenset[str]] = frozenset({"auto", "initial", "unset", "inherit", "revert", "revert-layer"})
_CLIPPING_OVERFLOW: Final[frozenset[str]] = frozenset({"hidden", "clip", "scroll", "auto"})
# Text this small or smaller, in px, can't be read; the rendered scan calls under 2px hidden.
_TINY_TEXT_PX: Final[float] = 4.0


def _opacity(value: str) -> float | None:
    """An opacity as a number from 0 to 1, or None when it can't be read. Keywords leave it at 1."""
    if value in _NO_OFFSET:
        return 1.0
    try:
        number = float(value[:-1]) / 100 if value.endswith("%") else float(value)
    except ValueError:
        return None
    return min(max(number, 0.0), 1.0)


def _placed(values: Callable[[str], list[str]]) -> bool:
    """True when element is positioned, transformed or grid-placed, or has a negative margin or z-index."""
    positions = values("position")
    if any(position not in _NOT_PLACED and position != "relative" for position in positions):
        return True
    if "relative" in positions and (
        any(value not in _NO_OFFSET and not _is_zero_quantity(value) for name in _OFFSET_PROPERTIES for value in values(name))
        or any(value not in _NO_OFFSET for value in values("z-index"))
    ):
        return True
    if any(value.startswith("-") for value in values("z-index")):
        return True
    if any(value not in ("none", *_NOT_PLACED) for name in _TRANSFORMS for value in values(name)):
        return True
    if any(value.startswith("-") and not _is_zero_quantity(value) for name in _MARGINS for value in values(name)):
        return True
    return any(value not in _NO_OFFSET for name in _GRID_PLACEMENT for value in values(name))


def _styled_past_judging(values: Callable[[str], list[str]]) -> bool:
    """True for styles that can hide text in ways the checks don't evaluate."""
    # A box that clips its overflow can clip its own text away, by a size, a line-height or an indent, which only a
    # browser lays out.
    if any(value in _CLIPPING_OVERFLOW for name in ("overflow-x", "overflow-y") for value in values(name)):
        return True
    for name, harmless in _PAINT_EFFECTS.items():
        if any(value not in harmless for value in values(name)):
            return True
    # Clipping to nothing is judged where it's certain, and an element found hidden isn't asked about.
    if any(value != "none" for value in values("clip-path")) or any(value != "auto" for value in values("clip")):
        return True
    for size in values("font-size"):
        match = _LENGTH_PATTERN.match(size)
        if match is None:
            continue
        pixels = length_in_px(size)
        number = float(match.group(1))
        if (pixels is not None and 0 < pixels < _TINY_TEXT_PX) or (pixels is None and 0 < number < (25 if match.group(2) == "%" else 1)):
            return True
    # A hiding property set with var() could hold anything.
    return any("var(" in value for name in HIDING_PROPERTIES for value in values(name))


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
    # Each custom property declaration: (selector, name, value, whether its rule applies for certain).
    custom: list[tuple[str, str, str, bool]] = field(default_factory=list)

    def add(self, selectors: Sequence[str], declarations: Sequence[Declaration], certain: bool) -> None:
        self.custom.extend(
            (selector.strip(), declaration.name, declaration.value, certain)
            for declaration in declarations
            if declaration.name.startswith("--")
            for selector in selectors
        )
        declarations = [declaration for declaration in declarations if not declaration.name.startswith("--")]
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
            if not applies and any(declaration.name in _JUDGED_PROPERTIES for declaration in declarations):
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


_VAR: Final[re.Pattern[str]] = re.compile(r"var\(\s*(--[^\s,()]+)\s*(?:,\s*([^()]*))?\)")
# var() inside a custom property's value is followed this many times, which also ends a cycle.
_MAX_VAR_DEPTH: Final[int] = 8
_ROOT_SELECTORS: Final[frozenset[str]] = frozenset({":root", "html"})


class CustomProperties:
    """The custom properties var() can be read from: each set once on the page, on :root or html, and nowhere else.

    One set on the root reaches every element, so its value is known
    wherever it's used. One set anywhere else, or more than once, could hold
    a different value in different places, so a var() naming it is left
    unread. A var() naming one set nowhere takes its fallback.
    """

    def __init__(self, values: Mapping[str, str], declared: frozenset[str]) -> None:
        self._values = dict(values)
        self._declared = declared

    def __repr__(self) -> str:
        return f"CustomProperties(readable={len(self._values)}, declared={len(self._declared)})"

    @classmethod
    def from_declarations(cls, rules: Iterable[tuple[str, str, str, bool]], elsewhere: Iterable[str]) -> CustomProperties:
        """rules holds (selector, name, value, certain) for every rule's custom properties, and elsewhere the name of
        every one set in a style attribute."""
        counts: dict[str, int] = {}
        on_root: dict[str, str] = {}
        for selector, name, value, certain in rules:
            counts[name] = counts.get(name, 0) + 1
            if certain and selector in _ROOT_SELECTORS:
                on_root[name] = value
        for name in elsewhere:
            counts[name] = counts.get(name, 0) + 1
        readable = {name: value for name, value in on_root.items() if counts[name] == 1}
        return cls(readable, frozenset(counts))

    def substitute(self, value: str) -> str | None:
        """value with every var() in it replaced, or None when one can't be read."""
        for _ in range(_MAX_VAR_DEPTH):
            if "var(" not in value:
                return value
            unread = False

            def replace(match: re.Match[str]) -> str:
                nonlocal unread
                name, fallback = match.group(1), match.group(2)
                if name in self._values:
                    return self._values[name]
                if name not in self._declared and fallback is not None and fallback.strip():
                    return fallback.strip()
                unread = True
                return ""

            value = _VAR.sub(replace, value)
            if unread:
                return None
        return None


NO_CUSTOM_PROPERTIES: Final[CustomProperties] = CustomProperties({}, frozenset())


class StyleCascade:
    """A page's rules that bear on hiding, applied element by element with the cascade."""

    def __init__(self, rules: Sequence[_Rule], custom: CustomProperties = NO_CUSTOM_PROPERTIES) -> None:
        self._custom = custom
        self._buckets: dict[_IndexKey, list[_Rule]] = {}
        for rule in rules:
            self._buckets.setdefault(index_key(rule.selector), []).append(rule)
        self._resolved: dict[int, _Resolved] = {}

    def _matching(self, element: Tag) -> Iterable[_Rule]:
        for key in _element_keys(element):
            for rule in self._buckets.get(key, ()):
                if rule.compiled is None or rule.compiled.match(element):
                    yield rule

    def _resolve(self, element: Tag) -> _Resolved:
        """The winning declaration of each property for element, and what might apply instead."""
        cached = self._resolved.get(id(element))
        if cached is not None:
            return cached
        resolved = _Resolved({}, {})
        for rule in self._matching(element):
            for declaration in rule.declarations:
                key: _CascadeKey = (
                    declaration.important,
                    False,
                    rule.specificity if rule.specificity is not None else _MOST_SPECIFIC,
                    rule.order,
                )
                if rule.certain:
                    _keep_highest(resolved.winners, declaration.name, key, declaration.value, True)
                else:
                    resolved.alternatives.setdefault(declaration.name, []).append((key, declaration.value))
        style = element.get("style")
        if isinstance(style, str):
            _add_style_attribute(resolved.winners, style)
        self._resolved[id(element)] = resolved
        return resolved

    def hiding(self, element: Tag) -> StyleHiding | None:
        """How element's styles hide it, or None. Only what's certain counts: see the module docstring."""
        if element.name.lower() in _WHOLE_PAGE_ELEMENTS:
            winners: dict[str, tuple[_CascadeKey, str, bool]] = {}
            style = element.get("style")
            if isinstance(style, str):
                _add_style_attribute(winners, style)
            return _decide(winners, {})
        resolved = self._resolve(element)
        decided = _decide(resolved.winners, resolved.alternatives)
        if decided is not None:
            return decided
        return self._colour_hiding(element)

    def _settled(self, element: Tag, name: str) -> tuple[str, bool] | None | _Unsettled:
        """element's winning value of name and whether a stylesheet set it, None when nothing sets it, or
        UNSETTLED when a rule that might apply could set it instead."""
        resolved = self._resolve(element)
        winner = resolved.winners.get(name)
        for key, _ in resolved.alternatives.get(name, ()):
            if winner is None or key > winner[0]:
                return UNSETTLED
        if winner is None:
            return None
        value = self._custom.substitute(winner[1])
        # A value with a var() it can't read could be anything.
        return UNSETTLED if value is None else (value, winner[2])

    def _text_colour(self, element: Tag) -> tuple[Colour, bool] | None:
        """The colour element's text is drawn in, set on it or inherited, or None when it can't be told."""
        for node in (element, *element.parents):
            if not isinstance(node, Tag) or node.name == "[document]":
                break
            settled = self._settled(node, "color")
            if settled is UNSETTLED:
                return None
            if isinstance(settled, tuple):
                colour = parse_colour(settled[0])
                return None if colour is None else (colour, settled[1])
        return None

    def _background(self, element: Tag) -> tuple[Colour, bool] | None:
        """The colour behind element's text, from its own and its ancestors' backgrounds, or None when it can't be told."""
        layers: list[Colour] = []
        from_stylesheet = False
        for node in (element, *element.parents):
            if not isinstance(node, Tag) or node.name == "[document]":
                break
            image = self._settled(node, "background-image")
            colour = self._settled(node, "background-color")
            if image is UNSETTLED or colour is UNSETTLED:
                return None
            if isinstance(image, tuple) and image[0] != "none":
                painted = single_colour_gradient(image[0])
                if painted is None:
                    return None
                layers.append(painted)
                from_stylesheet = from_stylesheet or image[1]
            if isinstance(colour, tuple):
                parsed = parse_colour(colour[0])
                if parsed is None:
                    return None
                if parsed[3] > 0:
                    layers.append(parsed)
                    from_stylesheet = from_stylesheet or colour[1]
            if layers and layers[-1][3] >= 1:
                break
        if not layers:
            return None
        background = _WHITE
        for layer in reversed(layers):
            background = _blend(layer, background)
        return background, from_stylesheet

    def _colour_hiding(self, element: Tag) -> StyleHiding | None:
        """CONTRAST_REASON when both colours are declared and too close to see apart, as the module docstring says."""
        text = self._text_colour(element)
        if text is None:
            return None
        background = self._background(element)
        if background is None:
            return None
        if contrast_ratio(text[0], background[0]) >= MIN_CONTRAST:
            return None
        return StyleHiding((CONTRAST_REASON,), text[1] or background[1])

    # What a browser has to judge

    def concerns(self, element: Tag, *, has_text: bool, skip: frozenset[Signal] = frozenset()) -> set[Signal]:
        """What about element's styles only a browser can judge: Signal.PLACED, Signal.STYLES and, for an element
        with text of its own, Signal.COLOURS. Signals in skip, already found, aren't looked for again.

        Every rule that might apply counts, not only the winning one.
        """
        found: set[Signal] = set()
        resolved = self._resolve(element)

        def values(name: str) -> list[str]:
            winner = resolved.winners.get(name)
            return ([winner[1]] if winner is not None else []) + [value for _, value in resolved.alternatives.get(name, ())]

        if Signal.PLACED not in skip and _placed(values):
            found.add(Signal.PLACED)
        if Signal.STYLES not in skip and (_styled_past_judging(values) or self._faded(element)):
            found.add(Signal.STYLES)
        if has_text and Signal.COLOURS not in skip and self._colours_unjudged(element):
            found.add(Signal.COLOURS)
        return found

    def _faded(self, element: Tag) -> bool:
        """True when the opacities of element and its ancestors multiply to under 0.25, or one can't be read."""
        opacity = 1.0
        for node in (element, *element.parents):
            if not isinstance(node, Tag) or node.name == "[document]":
                break
            resolved = self._resolve(node)
            winner = resolved.winners.get("opacity")
            candidates = ([winner[1]] if winner is not None else []) + [value for _, value in resolved.alternatives.get("opacity", ())]
            lowest = 1.0
            for value in candidates:
                number = _opacity(value)
                if number is None:
                    return True
                lowest = min(lowest, number)
            opacity *= lowest
        return opacity < _FADED_BELOW

    def _colours_unjudged(self, element: Tag) -> bool:
        """True when element's text colour or what's behind it is set in a way the colour check can't read, or when
        only one of them is declared and, against the browser's default for the other, they'd be too close to see."""
        text: Colour | None = None
        for node in (element, *element.parents):
            if not isinstance(node, Tag) or node.name == "[document]":
                break
            settled = self._settled(node, "color")
            if settled is UNSETTLED:
                return True
            if isinstance(settled, tuple):
                text = parse_colour(settled[0])
                if text is None:
                    return True
                break
        layers: list[Colour] = []
        for node in (element, *element.parents):
            if not isinstance(node, Tag) or node.name == "[document]":
                break
            image = self._settled(node, "background-image")
            colour = self._settled(node, "background-color")
            if image is UNSETTLED or colour is UNSETTLED:
                return True
            if isinstance(image, tuple) and image[0] != "none":
                painted = single_colour_gradient(image[0])
                # Text on a picture or a gradient of several colours isn't judged in a browser either.
                if painted is None:
                    return False
                layers.append(painted)
            if isinstance(colour, tuple):
                parsed = parse_colour(colour[0])
                if parsed is None:
                    return True
                if parsed[3] > 0:
                    layers.append(parsed)
            if layers and layers[-1][3] >= 1:
                break
        # With both declared, the colour check judged this itself, and text it found hidden isn't asked about.
        behind = _WHITE
        for layer in reversed(layers):
            behind = _blend(layer, behind)
        return contrast_ratio(_BLACK if text is None else text, behind) < MIN_CONTRAST


class _Unsettled:
    def __repr__(self) -> str:
        return "UNSETTLED"


UNSETTLED: Final[_Unsettled] = _Unsettled()


@dataclass
class _Resolved:
    winners: dict[str, tuple[_CascadeKey, str, bool]]
    # Values that might apply instead, with the most a rule's key could be.
    alternatives: dict[str, list[tuple[_CascadeKey, str]]]


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


# Elements attachShadow() accepts, besides custom elements, and so the ones a declared shadow root can attach to.
_SHADOW_HOST_TAGS: Final[frozenset[str]] = frozenset(
    {
        "article", "aside", "blockquote", "body", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header",
        "main", "nav", "p", "section", "span",
    }
)
_CUSTOM_ELEMENT_NAME: Final[re.Pattern[str]] = re.compile(r"[a-z][^\sA-Z/>]*-[^\sA-Z/>]*")
_RESERVED_ELEMENT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "annotation-xml", "color-profile", "font-face", "font-face-src", "font-face-uri", "font-face-format",
        "font-face-name", "missing-glyph",
    }
)


def _can_host_shadow_root(element: Tag) -> bool:
    name = element.name
    if name in _SHADOW_HOST_TAGS:
        return True
    return _CUSTOM_ELEMENT_NAME.fullmatch(name) is not None and name not in _RESERVED_ELEMENT_NAMES


def _declares_shadow_root(element: Tag) -> bool:
    return element.name == "template" and _attribute(element, "shadowrootmode").strip().lower() in ("open", "closed")


class ShadowTree:
    """A shadow root declared in the HTML: its template, and its own rules, matched in a copy of it on its own."""

    def __init__(self, template: Tag) -> None:
        self.template = template
        self.rules = _RuleList()
        self.cascade = StyleCascade(())
        # id of each element in the shadow root to its copy in a tree of the shadow root's own.
        self.copies: dict[int, Tag] = {}

    def __repr__(self) -> str:
        return f"ShadowTree(rules={len(self.rules.rules)}, elements={len(self.copies)})"

    def hiding(self, element: Tag) -> StyleHiding | None:
        copied = self.copies.get(id(element))
        return None if copied is None else self.cascade.hiding(copied)

    def concerns(self, element: Tag, *, has_text: bool, skip: frozenset[Signal]) -> set[Signal]:
        copied = self.copies.get(id(element))
        return set() if copied is None else self.cascade.concerns(copied, has_text=has_text, skip=skip)


class _Inert:
    """The content of an ordinary <template>, which a browser never shows or styles."""

    def __repr__(self) -> str:
        return "INERT"


INERT: Final[_Inert] = _Inert()
Scope = ShadowTree | _Inert


def _scopes(soup: BeautifulSoup) -> tuple[dict[int, Scope], list[ShadowTree]]:
    """The scope of every element inside a template, and each shadow root declared in the HTML, in one walk.

    A browser turns a <template shadowrootmode> into its parent's shadow root
    as it reads the page when the parent can have one and doesn't yet: any
    later one, any other template, and everything inside an ordinary template,
    is content that never shows.
    """
    scopes: dict[int, Scope] = {}
    trees: list[ShadowTree] = []
    hosts: set[int] = set()
    pending: list[tuple[Tag, Scope | None]] = [(soup, None)]
    while pending:
        node, scope = pending.pop()
        children: list[tuple[Tag, Scope | None]] = []
        for child in node.contents:
            if not isinstance(child, Tag):
                continue
            inner = scope
            if child.name == "template":
                if (
                    scope is not INERT
                    and _declares_shadow_root(child)
                    and id(node) not in hosts
                    and _can_host_shadow_root(node)
                ):
                    hosts.add(id(node))
                    tree = ShadowTree(child)
                    trees.append(tree)
                    inner = tree
                else:
                    inner = INERT
            if scope is not None:
                scopes[id(child)] = scope
            children.append((child, inner))
        pending.extend(reversed(children))
    return scopes, trees


def _copy_on_its_own(tree: ShadowTree) -> None:
    """Map each element of tree to a copy of it in a document holding only the shadow root's content.

    Elements of a shadow root inside it are mapped too, but they're judged with their own tree's copies.
    """
    fragment = BeautifulSoup("", "html.parser")
    for child in list(tree.template.contents):
        fragment.append(copy.copy(child))
    originals = [node for node in tree.template.descendants if isinstance(node, Tag)]
    copies = [node for node in fragment.descendants if isinstance(node, Tag)]
    if len(originals) != len(copies):
        return
    tree.copies = {id(original): copied for original, copied in zip(originals, copies)}


@dataclass(frozen=True)
class PageStyles:
    cascade: StyleCascade
    check: StylesheetCheck
    # The scope of each element inside a template: a shadow root declared in the HTML, or INERT. Every other
    # element is the page's own.
    scopes: Mapping[int, Scope] = field(default_factory=dict)
    # The templates a browser makes shadow roots of.
    shadow_templates: frozenset[int] = frozenset()

    def hiding(self, element: Tag) -> StyleHiding | None:
        """How element's styles hide it, from the rules of the tree it's in, or None. Content that never shows isn't
        judged."""
        scope = self.scopes.get(id(element))
        if scope is None:
            return self.cascade.hiding(element)
        if isinstance(scope, ShadowTree):
            return scope.hiding(element)
        return None

    def concerns(self, element: Tag, *, has_text: bool, skip: frozenset[Signal] = frozenset()) -> set[Signal]:
        """What about element's styles only a browser can judge, from the rules of the tree it's in. See
        StyleCascade.concerns."""
        scope = self.scopes.get(id(element))
        if scope is None:
            return self.cascade.concerns(element, has_text=has_text, skip=skip)
        if isinstance(scope, ShadowTree):
            return scope.concerns(element, has_text=has_text, skip=skip)
        return set()

    def in_shadow_root(self, element: Tag) -> bool:
        return isinstance(self.scopes.get(id(element)), ShadowTree)

    def never_shown(self, element: Tag) -> bool:
        """True inside an ordinary <template>, whose content a browser never shows."""
        return self.scopes.get(id(element)) is INERT

    def is_shadow_root(self, template: Tag) -> bool:
        """True for a <template> that a browser makes its parent's shadow root."""
        return id(template) in self.shadow_templates


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


# A browser running scripts never reads what's in a <noscript>. Templates are left out by scope instead.
_NO_STYLES_INSIDE: Final[frozenset[str]] = frozenset({"noscript"})


def read_page_styles(soup: BeautifulSoup, page_url: str | None, source: StylesheetSource | None) -> PageStyles:
    """Every rule bearing on hiding from soup's <style> blocks and the same-host stylesheets source can fetch.

    page_url is the address the HTML came from, after any redirects. Without
    it or a source, linked stylesheets are counted but not read. Each shadow
    root declared in the HTML gets the rules of the styles inside it, and the
    page the rest.
    """
    scopes, trees = _scopes(soup)
    reader = _StyleReader(page_url, source, _base_url(soup, page_url) if page_url is not None else None)
    page_rules = _RuleList()
    reader.read(
        (element for element in soup.find_all(["style", "link"]) if id(element) not in scopes),
        page_rules,
    )
    for tree in trees:
        reader.read(
            (element for element in tree.template.find_all(["style", "link"]) if scopes.get(id(element)) is tree),
            tree.rules,
        )
    rule_lists = [page_rules, *(tree.rules for tree in trees)]
    custom = CustomProperties.from_declarations(
        (declaration for rules in rule_lists for declaration in rules.custom), _custom_properties_in_attributes(soup)
    )
    for tree in trees:
        _copy_on_its_own(tree)
        tree.cascade = StyleCascade(tree.rules.rules, custom)
    check = StylesheetCheck(
        style_blocks=reader.style_blocks,
        linked_read=reader.linked_read,
        linked_not_read=reader.linked_not_read + sum(rules.imports for rules in rule_lists),
        uncertain_rules=sum(rules.uncertain for rules in rule_lists),
        shadow_roots=len(trees),
    )
    return PageStyles(
        StyleCascade(page_rules.rules, custom),
        check,
        scopes,
        frozenset(id(tree.template) for tree in trees),
    )


def _custom_properties_in_attributes(soup: BeautifulSoup) -> list[str]:
    """The name of every custom property a style attribute sets, once for each time it's set."""
    names: list[str] = []
    for element in soup.find_all(style=True):
        style = element.get("style")
        if isinstance(style, str) and "--" in style:
            names.extend(declaration.name for declaration in parse_declarations(style) if declaration.name.startswith("--"))
    return names


class _StyleReader:
    """Reads <style> and <link> elements into rule lists, counting what it read and sharing one fetch of each URL."""

    def __init__(self, page_url: str | None, source: StylesheetSource | None, base: str | None) -> None:
        self._page_url = page_url
        self._source = source
        self._base = base
        self._fetched: dict[str, str | None] = {}
        self.style_blocks = 0
        self.linked_read = 0
        self.linked_not_read = 0

    def __repr__(self) -> str:
        return f"_StyleReader(style_blocks={self.style_blocks}, linked_read={self.linked_read})"

    def read(self, elements: Iterable[Tag], rules: _RuleList) -> None:
        for element in elements:
            if not isinstance(element, Tag) or _inside(element, _NO_STYLES_INSIDE) or not _is_css_type(element):
                continue
            applies = media_applies(_attribute(element, "media"))
            if element.name == "style":
                self.style_blocks += 1
                if applies is not False:
                    parse_stylesheet(element.get_text(), rules, certain=applies is True)
                continue
            relations = _attribute(element, "rel").lower().split()
            href = _attribute(element, "href").strip()
            if "stylesheet" not in relations or "alternate" in relations or element.has_attr("disabled") or not href:
                continue
            if applies is False:
                continue
            url = urljoin(self._base, href) if self._base is not None else None
            text = _fetch_once(
                self._fetched, url, self._page_url, self._source, len(self._fetched) >= MAX_LINKED_STYLESHEETS
            )
            if text is None:
                self.linked_not_read += 1
                continue
            self.linked_read += 1
            parse_stylesheet(text, rules, certain=applies is True)


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
