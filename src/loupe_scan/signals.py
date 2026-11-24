"""What a markup-only scan can't judge on a page, and so what makes fetch_page's auto mode render it.

The markup scan reads the HTML and applies the page's styles in part. These
are the things on a page it can't settle without drawing the page, each
found from the HTML and the styles it read. docs/mcp.md lists them for
people.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum


class Signal(str, Enum):
    """One kind of thing a browser has to judge. The order is the order they're named in."""

    # An <iframe>, <frame>, <object> or <embed> that isn't hidden: its document isn't in the HTML.
    FRAMES = "frames"
    # A shadow root declared in the HTML, whose :host rules and slots the markup scan can't apply.
    SHADOW_ROOTS = "shadow roots"
    # A <script> that runs, or an event handler attribute: scripts can hide, add, move or cover anything.
    SCRIPTS = "scripts"
    # A stylesheet on another host, past the limit, through @import, or that failed.
    UNREAD_STYLESHEETS = "stylesheets loupe couldn't read"
    # A rule bearing on hiding inside a condition the markup can't settle, such as @supports or @layer.
    UNSETTLED_RULES = "style rules loupe couldn't settle"
    # Positioned, transformed or grid-placed elements, negative margins or z-index: what lets one element cover another.
    PLACED = "elements placed where they could cover text"
    # Overflow clipping with a size, low opacity, tiny text, filters, blending, masks, clip shapes, content-visibility,
    # or a hiding property set with var().
    STYLES = "styles loupe can't judge without a browser"
    # Text whose colour or background is set in a way it can't read, or that may be coloured like the page itself.
    COLOURS = "colours loupe can't compare without a browser"
    # A closed <details> or <dialog>, or a popover: content a click or a script shows.
    CLOSED = "content that opens on a click"


def in_order(signals: Iterable[Signal]) -> tuple[Signal, ...]:
    found = set(signals)
    return tuple(signal for signal in Signal if signal in found)


def describe(signals: Iterable[Signal]) -> str:
    """The signals as a phrase, such as "frames, scripts and shadow roots"."""
    names = [signal.value for signal in in_order(signals)]
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
