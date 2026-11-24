"""What a markup scan says only a browser can judge on a page: what makes fetch_page's auto mode render it."""

from __future__ import annotations

import pytest

from loupe_scan.scan import scan_markup
from loupe_scan.signals import Signal, describe
from loupe_scan.stylesheets import CustomProperties

TEXT = "<p>Fresh bread every morning, baked before the shop opens.</p>"


def _signals(html: str) -> set[Signal]:
    return set(scan_markup(html).needs_browser)


def _styled(css: str, body: str = TEXT) -> str:
    return f"<html><head><style>{css}</style></head><body>{body}</body></html>"


@pytest.mark.parametrize(
    "html",
    [
        TEXT,
        _styled("body { color: #222; background: #fff } p { margin: 0 0 1rem; font-size: 1.1rem }"),
        _styled(":root { --ink: #222; --page: #fdfdfd } body { color: var(--ink); background: var(--page) }"),
        # Hidden in ways the markup judges itself, so it needs no browser.
        _styled(".gone { display: none }", f'{TEXT}<p class="gone">Hidden by a class.</p>'),
        f'{TEXT}<p style="position: absolute; left: -9999px">Pushed off the page.</p>',
        f"{TEXT}<!-- a comment --><img src=x.png alt='A loaf'><noscript>Turn scripts on.</noscript>",
        # Scripts that don't run, and frames that never show.
        f'{TEXT}<script type="application/ld+json">{{"name": "Bakery"}}</script><script src="">  </script>',
        f'{TEXT}<template><script>alert(1)</script><iframe src="/x"></iframe></template>',
        f'{TEXT}<noscript><script>alert(1)</script><img src=x onerror="alert(1)"></noscript>',
        f'{TEXT}<div hidden><iframe src="/menu"></iframe></div>',
        _styled(".gone { display: none } .lid { position: absolute }", f'{TEXT}<div class="gone"><div class="lid"></div></div>'),
        # Opacity that leaves text readable, a size without clipping, and a relative box that doesn't move.
        _styled("p { opacity: 0.9; width: 40rem } .box { position: relative }", f'<div class="box">{TEXT}</div>'),
        f'<details open><summary>Hours</summary>{TEXT}</details><dialog open>{TEXT}</dialog>',
        # A browser reads these as text, so nothing written in them is an element or runs.
        f'{TEXT}<xmp><iframe src="/x"></iframe><script>go()</script><b onclick="go()">b</b></xmp>',
        f'{TEXT}<video><iframe src="/x"></iframe></video>',
        _styled("p { position: relative; top: 0; left: auto } div { overflow: hidden } b { margin: -0px }", f"<div>{TEXT}<b>b</b></div>"),
    ],
    ids=[
        "plain", "colours", "root-tokens", "stylesheet-hidden", "off-screen", "comment-alt-noscript", "data-blocks",
        "template", "noscript", "hidden-frame", "hidden-cover", "harmless-styles", "open", "raw-text", "fallback-frame",
        "no-move",
    ],
)
def test_a_page_the_markup_can_judge_needs_no_browser(html: str) -> None:
    assert _signals(html) == set()


@pytest.mark.parametrize(
    ("html", "signal"),
    [
        (f'{TEXT}<iframe src="/menu"></iframe>', Signal.FRAMES),
        (f'{TEXT}<object data="/menu.pdf"></object>', Signal.FRAMES),
        (f'{TEXT}<embed src="/menu.svg">', Signal.FRAMES),
        (f'<div><template shadowrootmode="open">{TEXT}</template></div>', Signal.SHADOW_ROOTS),
        (f"{TEXT}<script>document.body.append('x')</script>", Signal.SCRIPTS),
        (f'{TEXT}<script type="module" src="/app.js"></script>', Signal.SCRIPTS),
        (f'{TEXT}<script type="text/javascript; charset=utf-8">go()</script>', Signal.SCRIPTS),
        (f'{TEXT}<img src="x.png" onerror="hide()">', Signal.SCRIPTS),
        (f'<link rel="stylesheet" href="https://cdn.example/site.css">{TEXT}', Signal.UNREAD_STYLESHEETS),
        (_styled("@import url(/more.css);"), Signal.UNREAD_STYLESHEETS),
        (_styled("@layer base { p { display: none } }"), Signal.UNSETTLED_RULES),
        (_styled("@supports (display: grid) { .x { visibility: hidden } }"), Signal.UNSETTLED_RULES),
        (_styled(".lid { position: absolute; inset: 0 }", f'{TEXT}<div class="lid"></div>'), Signal.PLACED),
        (_styled("header { position: fixed }", f"<header></header>{TEXT}"), Signal.PLACED),
        (_styled("p { position: sticky }"), Signal.PLACED),
        (_styled("p { position: relative; top: 2px }"), Signal.PLACED),
        (_styled("p { position: relative; z-index: -1 }"), Signal.PLACED),
        (_styled("p { position: relative; z-index: 2 }"), Signal.PLACED),
        (_styled(".a { z-index: -1 }", f'<p class="a">{TEXT}</p>'), Signal.PLACED),
        (_styled("p { transform: translateY(-40px) }"), Signal.PLACED),
        (_styled("p { translate: 0 -40px }"), Signal.PLACED),
        (_styled("p { margin: 0 0 -3rem }"), Signal.PLACED),
        (_styled("p { margin-top: -1px }"), Signal.PLACED),
        (_styled("p { grid-area: 1 / 1 }"), Signal.PLACED),
        (_styled("div { overflow: hidden; height: 0 }", f"<div>{TEXT}</div>"), Signal.STYLES),
        (_styled("div { overflow-y: clip; max-height: 2px }", f"<div>{TEXT}</div>"), Signal.STYLES),
        (_styled("p { opacity: 0.1 }"), Signal.STYLES),
        (_styled("div { opacity: 0.4 } p { opacity: 0.4 }", f"<div>{TEXT}</div>"), Signal.STYLES),
        (_styled("p { opacity: var(--fade) }"), Signal.STYLES),
        (_styled("p { opacity: calc(0.1) }"), Signal.STYLES),
        (_styled("p { font-size: 1px }"), Signal.STYLES),
        (_styled("p { font-size: 0.1em }"), Signal.STYLES),
        (_styled("p { font-size: 10% }"), Signal.STYLES),
        (_styled("p { filter: opacity(0) }"), Signal.STYLES),
        (_styled("p { mix-blend-mode: screen }"), Signal.STYLES),
        (_styled("p { mask-image: linear-gradient(transparent, transparent) }"), Signal.STYLES),
        (_styled("p { clip-path: polygon(0 0, 0 0, 0 0) }"), Signal.STYLES),
        (_styled("p { content-visibility: hidden }"), Signal.STYLES),
        (_styled("p { display: var(--shown) }"), Signal.STYLES),
        (_styled("p { -webkit-text-fill-color: transparent }"), Signal.STYLES),
        # Colours the check can't read, and text that may be coloured like the page without both being declared.
        (_styled("p { color: var(--nowhere) }"), Signal.COLOURS),
        (_styled("p { color: hsl(0 0% 100%) }"), Signal.COLOURS),
        (_styled("p { color: #fff }"), Signal.COLOURS),
        (_styled("p { color: transparent }"), Signal.COLOURS),
        (_styled("body { background: #000 }"), Signal.COLOURS),
        (_styled("body { background-color: oklch(0.2 0 0) } p { color: #ddd }"), Signal.COLOURS),
        (_styled(":root { --ink: #fff } .x { --ink: #000 } p { color: var(--ink) }"), Signal.COLOURS),
        (f"<details><summary>Hours</summary>{TEXT}</details>", Signal.CLOSED),
        (f"{TEXT}<dialog>{TEXT}</dialog>", Signal.CLOSED),
        (f'{TEXT}<div popover="auto">{TEXT}</div>', Signal.CLOSED),
    ],
)
def test_what_only_a_browser_can_judge_is_named(html: str, signal: Signal) -> None:
    assert _signals(html) == {signal}


def test_a_script_in_a_shadow_root_runs() -> None:
    html = f'<div><template shadowrootmode="open"><script>go()</script></template></div>{TEXT}'
    assert _signals(html) == {Signal.SHADOW_ROOTS, Signal.SCRIPTS}


def test_a_cover_inside_an_aria_hidden_element_still_counts() -> None:
    # aria-hidden hides nothing from sighted readers, so what's inside can still cover text.
    html = _styled(".lid { position: absolute; inset: 0 }", f'{TEXT}<div aria-hidden="true"><div class="lid"></div></div>')
    assert _signals(html) == {Signal.PLACED}


def test_only_an_elements_own_text_has_its_colours_compared() -> None:
    # The wrapper's colour can't be read, but its text is all in a child that sets a colour of its own.
    html = _styled("div { color: hsl(0 0% 50%) } p { color: #222 }", f"<div>{TEXT}</div>")
    assert _signals(html) == set()


def test_a_var_naming_nothing_readable_leaves_the_value_unread() -> None:
    custom = CustomProperties({"--ink": "#fff"}, frozenset({"--ink", "--set-elsewhere"}))
    assert custom.substitute("var(--ink)") == "#fff"
    assert custom.substitute("var(--nowhere, #000)") == "#000"
    assert custom.substitute("var(--set-elsewhere, #000)") is None
    assert custom.substitute("var(--nowhere)") is None
    assert custom.substitute("#123") == "#123"


def test_an_aria_hidden_cover_counts_too() -> None:
    html = _styled(".lid { position: absolute; inset: 0 }", f'{TEXT}<div class="lid" aria-hidden="true"></div>')
    assert _signals(html) == {Signal.PLACED}


def test_text_on_a_picture_isnt_a_reason_to_render() -> None:
    # The rendered scan doesn't judge text on a picture or a gradient of several colours either.
    html = _styled("body { background: url(bread.jpg) } p { color: #fff }")
    assert _signals(html) == set()
    assert _signals(_styled("body { background: linear-gradient(#fff, #000) } p { color: #fff }")) == set()


def test_shadow_roots_are_judged_with_their_own_rules() -> None:
    shadow = '<style>p { position: absolute }</style><p>Inside the shadow root, placed.</p>'
    html = _styled("p { position: absolute }", f'<div><template shadowrootmode="open">{shadow}</template></div>')
    assert _signals(html) == {Signal.SHADOW_ROOTS, Signal.PLACED}
    # The page's rule reaches no element of its own, and none in the shadow root.
    quiet = _styled("p { position: absolute }", '<div><template shadowrootmode="open"><b>Shadow text.</b></template></div>')
    assert _signals(quiet) == {Signal.SHADOW_ROOTS}


def test_signals_come_in_order_and_read_as_a_phrase() -> None:
    html = f'<details><p>x</p></details><script>go()</script><iframe src="/x"></iframe>{TEXT}'
    found = scan_markup(html).needs_browser
    assert found == (Signal.FRAMES, Signal.SCRIPTS, Signal.CLOSED)
    assert describe(found) == "frames, scripts and content that opens on a click"
    assert describe((Signal.SCRIPTS,)) == "scripts"
    assert describe(()) == ""


def test_a_rule_bearing_only_on_placement_isnt_counted_as_unsettled() -> None:
    check = scan_markup(_styled("@supports (display: grid) { .menu { transform: translateX(1rem) } }")).stylesheets
    assert check.uncertain_rules == 0
