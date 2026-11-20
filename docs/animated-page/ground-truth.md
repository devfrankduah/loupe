# Animated page ground truth

[The animated page](https://devfrankduah.github.io/loupe/animated-page/) is a landing page for the Wrenfold 30L, a travel pack that doesn't exist. Nothing on it is aimed at an AI and nothing is planted. It uses the effects that ordinary marketing pages use: sections that fade in as they scroll into view, a headline that changes every 3 seconds over an invisible copy that holds its size, a live clock, a skip link and a collapsed menu.

Those effects make a code-only scan report hidden text, because the page's HTML starts the headline and every section at `opacity:0` and only a script fades them in. Every one of those flags is a false alarm, and the browser scan should clear all of them. The [blender test page](../test-page/ground-truth.md) shows the other side: text that really is hidden on purpose.

`tests/test_animated_page.py` reads the table below and checks every row against a scan of the same page, kept as a test fixture in `tests/fixtures/animated-page/`, in both modes.

| # | Element | How it hides or moves | Starts with | Markup scan | Rendered scan | Trace |
|---|---|---|---|---|---|---|
| 1 | Rotating headline | inline `opacity:0`, faded in after load, then changed every 3 seconds | packs flat for the overhead bin. | inline-style | cleared | visible at load |
| 2 | Features section | inline `opacity:0`, faded in when it scrolls into view | Built for one bag | inline-style | cleared | visible at load |
| 3 | Materials section | the same | Ballistic nylon and recycled lining | inline-style | cleared | revealed on scroll |
| 4 | Sizes section | the same | Fits under the seat on most airlines | inline-style | cleared | revealed on scroll |
| 5 | Reviews section | the same | What buyers say | inline-style | cleared | revealed on scroll |
| 6 | Layout copy of the headline | a stylesheet class sets `visibility:hidden` | comes with a five-year warranty. | not flagged | rendered-hidden (visibility:hidden) | none |
| 7 | Skip link | a stylesheet class keeps it above the page until it has focus | Skip to content | not flagged | rendered-hidden (outside the page) | none |
| 8 | Collapsed menu | a closed `<details>` | Features Materials Sizes Reviews | not flagged | rendered-hidden (inside a closed <details>) | none |
| 9 | Live clock | its text changes every second | Local time | not flagged | not flagged | none |

Cleared means the code-only flag is gone and the browser never reports the element as hidden. The trace column says when the browser first saw each flagged element: visible at load if it showed by the first full check, revealed on scroll if it showed only once the scan scrolled to it.

The browser does report rows 6 to 8 as hidden, and it's right that a reader can't see them at that moment. None of them hides anything worth worrying about. The layout copy repeats the headline's own four statements, the skip link appears when it gets keyboard focus, and the menu opens on a click. loupe counts text that needs focus or a click as hidden, which is one of its known limits.

Besides these rows, both scans report the page's three `<meta>` tags (viewport, robots and description), none of them instruction-like. In total:

- Markup scan: 8 findings, 0 instruction-like.
- Rendered scan: 6 findings, 0 instruction-like.
- Trace: 5 candidates, 2 visible at load, 3 revealed on scroll, 0 still hidden, 0 not matched.

Whether each element reaches the markdown depends on the converter, Bright Data's Fetch API or markdownify on the local backend, so the table doesn't predict it. The live results record it, and on 19 and 20 November the same elements reached the markdown on both backends.

A Pause motion button stops the headline and the clock, and with reduced motion turned on, nothing fades or rotates. axe-core 4.10.3 finds no violations, and nothing that needs manual review, at desktop or phone width, with and without reduced motion.
