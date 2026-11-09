# How loupe works

This is the detail behind the README's short version: what loupe sends to Bright Data, what each check looks for, how a rendered scan decides what's visible, and what the 7 November runs showed.

## How pages are fetched

I send the same requests that Bright Data's own MCP server ([brightdata-mcp](https://github.com/brightdata/brightdata-mcp), version 4.3.0) sends for its two scraping tools:

- HTML, like `scrape_as_html`: `POST https://api.brightdata.com/request` with `{"url", "zone": "mcp_unlocker", "format": "raw"}`. That's Web Unlocker. The MCP server creates the `mcp_unlocker` zone the first time it starts, so on an account that has never run it you need to create that zone first. `--zone` picks a different one.
- Markdown, like `scrape_as_markdown`: `POST https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_m6gjtfmeh43we6cqc&format=json&include_errors=true` with `[{"url"}]`, reading the `markdown` field of the first record. That's the Fetch API, which the MCP server switched to on 2026-11-01.

## What the markup scan checks

In the HTML:

- HTML comments.
- Elements with the `hidden` attribute.
- Elements with `aria-hidden="true"`. Screen readers skip them but sighted readers still see them, so I report them separately.
- Elements whose inline `style` sets `display:none`, `visibility:hidden` or `collapse`, `font-size:0`, `opacity:0`, or pushes them off-screen (`position:absolute` or `fixed` with an offset of 1000px or more to the negative side, or a `text-indent` that far).
- `alt`, `title` and `aria-label` attributes.
- `<meta>` tags that have a name, property, http-equiv or itemprop.
- `<noscript>` blocks.

Text inside a hidden element is reported once, on the outermost hidden element. Script and style contents are ignored.

Every finding is checked against a short list of phrases that read like instructions to a model ("ignore previous", "ignore all", "you are", "system prompt", "do not tell" and a few more). The list lives in `src/loupe/phrases.py`. "assistant" was on it until 7 November. On real pages it only ever named products and projects, so it was dropped.

## The markdown comparison

loupe fetches the markdown version of the same page and records whether each finding's text appears in it. Both sides are reduced to lowercase words, link targets are dropped, and the finding counts as present only if its full word sequence appears in order. Findings shorter than 12 characters after that reduction aren't compared, because short strings like "Menu" match by accident.

## Rendered scan

The markup checks read markup only, so they miss text hidden by a stylesheet class or by a script, and they flag text that starts invisible and fades in as you scroll. `--render` asks a real browser instead.

For each page, loupe opens Chromium at 1280 by 800, loads the page, and waits for the load event, for the network to go quiet and for opening animations to finish. Then it records whether each element's own text is visible. Visible means all of these:

- `checkVisibility()` passes with `opacityProperty`, `visibilityProperty` and `contentVisibilityAuto`
- the text has a box at least 2px each way, inside the part of the page a reader can scroll to
- the computed `font-size` is above 0
- it isn't clipped away by `overflow`, `clip` or `clip-path`
- its opacity, multiplied through its ancestors, is at least 0.05

Then loupe scrolls from the top to the bottom one screen at a time, pausing at each step and checking again. After that, it brings each element that is still hidden only by opacity, position or clipping (the ways scroll animations hide things) to the middle of the screen and checks once more, because some reveals fire only at one scroll position. Text hidden with `display:none`, `visibility:hidden` or a closed `<details>` needs a click to show, so it isn't centred. A last check runs once transitions settle.

Text that was never seen visible becomes a `rendered-hidden` finding, with the reason (such as `display:none`, `clipped` or `inside a closed <details>`). It goes through the same phrase list and markdown comparison as everything else. Text hidden at load but seen later is listed separately as revealed on scroll and isn't counted as hidden. The markup checks for comments, meta tags, label attributes, `aria-hidden` and `<noscript>` still run on the HTML as served. The rendered result replaces the inline-style and `hidden` attribute guesses.

### The inline-style trace

Each element an inline-style guess flagged is traced and reported as visible at load, revealed on scroll, still hidden, or not matched. The trace starts as soon as the page's HTML arrives. It checks each flagged element every 250 milliseconds and whenever nodes are added, and records the first time it was visible: at or before the first full check counts as visible at load, later counts as revealed on scroll. Elements are matched by tag and text, since a framework can swap a node for a copy after load, and a page can carry several copies of the same text, so the most visible copy decides. If the page replaces its whole document while it loads, the trace starts again on the new one and a note says so.

### Isolation and the remote browser

The checks run in an isolated JavaScript world, so a page's own scripts can't patch `checkVisibility` or `getClientRects` to make hidden text look visible.

The browser is local Chromium unless `BRIGHTDATA_BROWSER_WSS` holds a CDP websocket endpoint for Bright Data's Browser API. That value carries credentials. loupe never prints, logs or saves it, and a failed connection reports only that it failed. A remote browser gets two-minute timeouts. It skips images, media and fonts, but never stylesheets, since they decide what shows, and each page records the bytes transferred and the requests it refused. A remote page fails if it measures no bytes, or if it loaded images over http(s) and nothing was refused, because then neither the byte limit nor the blocking can be trusted. Every page gets a fresh browser.

## The JSON

Each page has a `scan_mode` and each finding a `mode` (`static` or `rendered`). A rendered page adds a `rendering` block with the browser version, scroll steps, notes, the revealed text and the inline-style trace. Quoted text is cut to `--quote-chars` characters, and per-request meta values (nonces, CSRF tokens, request IDs and the like) are replaced with a placeholder.

## What the 7 November runs showed

The runs behind the README's table are `results/2026-11-07-2.json` (markup only) and `results/2026-11-07-2-rendered.json` (`--render` through the Browser API, with images, media and fonts blocked), with `.txt` summaries beside them. Quoted text is cut to 80 characters, and the pages themselves aren't stored. The browser run measured 4,190,724 bytes and refused 101 image, media and font requests. GitHub's network was still busy after 10 seconds, so its first check ran with requests open, and it measured about 2.1 MB against 0.4 MB earlier the same day.

The browser finds far more on Wikipedia and GitHub, because most of their hidden text is hidden by stylesheets the markup scan can't read. On Wikipedia, 99 of the 107 findings are `display:none`. GitHub's reasons are more mixed: `display:none`, text too small to see, sections collapsed inside a closed `<details>`, and clipped text.

frankduah.me goes the other way. The markup scan flagged 48 elements for inline `opacity:0`. In the browser, 29 showed as the page loaded, 18 showed after scrolling, none stayed hidden, and 1 wasn't found in the live page: a clock that rewrites its own text.

All five instruction-like matches are Wikipedia link titles, such as "You are encouraged to create an account" and "Large language model". None of them is an attack.

Rendering the same five pages in local Chromium found the same hidden and revealed text on all five.

## What reaches the markdown

For some kinds of hiding, a lot. Across the five pages in the markup-only run:

| Kind | In markdown | Not in markdown | Not compared |
|---|---|---|---|
| `hidden` attribute | 5 | 1 | 0 |
| Inline style | 27 | 13 | 10 |
| `aria-hidden` | 11 | 3 | 27 |
| HTML comment | 0 | 18 | 16 |
| `<meta>` | 3 | 61 | 47 |
| `alt`, `title`, `aria-label` | 217 | 221 | 106 |
| `<noscript>` | 0 | 0 | 0 |

Text inside elements hidden with the `hidden` attribute or `display:none` reaches the markdown. On the OWASP page, a `display:none` print and email menu and two `hidden` "Share on" labels all show up in it. HTML comments don't: none of the 18 I could compare were in the markdown. Meta content almost never is. So an agent reading this markdown is protected from comments but not from text hidden with markup or inline CSS, which are the easiest places to plant an instruction.

The README's limitations apply here: the HTML and the markdown come from two separate Bright Data services, and "in markdown" means only that the text appears somewhere in the markdown.
