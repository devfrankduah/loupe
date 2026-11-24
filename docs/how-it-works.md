# How loupe works

This is the detail behind the README's short version: how each backend fetches a page, what each check looks for, how a rendered scan decides what's visible, and what the 19 November runs showed.

## How pages are fetched

loupe has two backends. The local backend loads pages from this machine and needs no account. The Bright Data backend uses Bright Data's APIs. Without `--backend`, loupe uses Bright Data when `BRIGHTDATA_API_TOKEN` or `BRIGHTDATA_BROWSER_WSS` is set and the local backend when neither is, and every page's output and JSON say which one ran.

### The local backend

A markup scan on the local backend fetches the HTML with one GET per page, sent as `loupe/0.2.0 (+https://github.com/devfrankduah/loupe)` so a site can see who's asking. It gives up after 10 seconds connecting, 30 seconds without data or about 60 seconds in all, follows at most 5 redirects and stops reading at 10 MB. A page that isn't HTML, such as a PDF, is an error.

Before each request, including each redirect, loupe reads the site's robots.txt (once per site per run) and obeys it, using [Protego](https://github.com/scrapy/protego) for the `*` and `$` patterns of RFC 9309. Following RFC 9309, a robots.txt that's missing or answers with a 4xx allows everything, and one that can't be read (a 5xx, a 429 or a network error) disallows everything. A `Crawl-delay` is waited out between requests to the same site, up to 30 seconds; a longer one stops the second page from that site. `--ignore-robots` skips all of this, for pages you own or have permission to scan.

A 403, a 429, Cloudflare's `cf-mitigated: challenge` header or a page titled like Cloudflare's challenge pages counts as the site turning loupe away. The error then points to the Bright Data backend, which gets past most of those blocks.

A rendered scan on the local backend loads the page in local Chromium and runs exactly the checks the Browser API path runs; the tests render both test pages with each path's settings and compare the results. robots.txt is checked, and any Crawl-delay waited out, before the browser loads each page. A 403, a 429 or a challenge stops the scan with the same pointer to Bright Data. The Browser API path still stops at a 403 or 429 but renders a challenge page as it arrives, since the Browser API solves challenges itself.

### The Bright Data backend

For the Bright Data backend, loupe sends the same requests that Bright Data's own MCP server ([brightdata-mcp](https://github.com/brightdata/brightdata-mcp), version 4.3.0) sends for its two scraping tools:

- HTML, like `scrape_as_html`: `POST https://api.brightdata.com/request` with `{"url", "zone": "mcp_unlocker", "format": "raw"}`. That's Web Unlocker. The MCP server creates the `mcp_unlocker` zone the first time it starts, so on an account that has never run it you need to create that zone first. `--zone` picks a different one.
- Markdown, like `scrape_as_markdown`: `POST https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_m6gjtfmeh43we6cqc&format=json&include_errors=true` with `[{"url"}]`, reading the `markdown` field of the first record. That's the Fetch API, which the MCP server switched to on 2026-11-01.

## What the markup scan checks

In the HTML:

- HTML comments.
- Elements with the `hidden` attribute.
- Elements with `aria-hidden="true"`. Screen readers skip them but sighted readers still see them, so loupe reports them separately.
- Elements whose inline `style` sets `display:none`, `visibility:hidden` or `collapse`, `font-size:0`, `opacity:0`, or pushes them off-screen (`position:absolute` or `fixed` with an offset of 1000px or more to the negative side, or a `text-indent` that far).
- `alt`, `title` and `aria-label` attributes.
- `<meta>` tags that have a name, property, http-equiv or itemprop.
- `<noscript>` blocks.
- Text written inside an `<iframe>`, `<video>`, `<audio>`, `<canvas>`, `<noframes>` or `<noembed>` tag, as `fallback-content`. A browser shows it only when it can't show the element itself, a frame, a video, a sound, a drawing or a plugin, and every current browser can. A canvas also shows it with scripts turned off. markdownify keeps all of it. An `<object>`'s content isn't reported, since it shows whenever what the object embeds fails to load.

Text inside a hidden element is reported once, on the outermost hidden element. The exception is text in `<noscript>` and fallback content, which is reported on that element and left out of the hidden element around it, since a rendered scan never judges it. Script and style contents are ignored.

Every finding is checked against a short list of phrases that read like instructions to a model ("ignore previous", "ignore all", "you are", "system prompt", "do not tell" and a few more). The list lives in `src/loupe_scan/phrases.py`. "assistant" was on it until 7 November. On real pages it only ever named products and projects, so it was dropped.

## The markdown comparison

loupe gets a markdown version of the same page and records whether each finding's text appears in it. The Bright Data backend fetches it from the Fetch API. The local backend makes it on this machine from the HTML the site served, with [markdownify](https://github.com/matthewwithanm/python-markdownify) (MIT licence), and labels it an approximation of what agent tools produce: each tool has a converter of its own. markdownify, like most converters, keeps the text of elements CSS hides, image `alt` text and link and image titles, and drops comments, scripts, styles and `aria-label`. In a rendered scan the markdown is made from the same served HTML, so both modes compare against the same markdown. Both sides are reduced to lowercase words, link targets are dropped, and the finding counts as present only if its full word sequence appears in order. Findings shorter than 12 characters after that reduction aren't compared, because short strings like "Menu" match by accident.

## Rendered scan

The markup checks don't run the page, so they miss text a script hides or adds, and they flag text that starts invisible and fades in as you scroll. They apply stylesheets only in part: the page's `<style>` blocks and up to five stylesheets linked from its own host, judged at the rendered scan's 1280 by 800 viewport, for `display:none`, `visibility:hidden`, `opacity:0`, `font-size:0`, offsets of 1000px or more off the page and clipping to nothing, and for text coloured like its background where the stylesheets or style attributes declare both colours, judged with the same 1.5:1 threshold as the rendered scan below. Selectors are matched with soupsieve and the cascade is applied (importance, the style attribute, specificity, order). A colour set with `var()` is read when its custom property is set once on the page, on `:root` or `html`, and nowhere else, since that one value reaches every element; a custom property set anywhere else, or more than once, leaves the colour unjudged, and one set nowhere takes the `var()`'s fallback. A rule in a condition the markup can't settle, such as `@supports`, `@layer` or a media query about anything but width, height, orientation or media type, never counts as hiding, and if it could show an element another rule hides, that element isn't reported, so the check misses text rather than guess. `--render` asks a real browser instead.

`loupe mcp`'s auto mode, its tools' default, runs this markup scan first and renders the page as well when the markup scan finds something on it only a browser can judge: frames, shadow roots, scripts, stylesheets it couldn't read, rules it couldn't settle, elements placed where they could cover text, styles or colours it can't judge, or content that opens on a click. [mcp.md](mcp.md#how-the-tools-pick-a-scan) lists exactly what counts.

A shadow root declared in the HTML, with `<template shadowrootmode="open">` or `"closed"` as the first such template in an element that can have one, is checked with the `<style>` blocks and same-host stylesheets inside it, and the page's own rules don't reach it, as in a browser. Its selectors are matched in a copy of the shadow root on its own, so a rule in it can't see past it to the page. A finding in one says so, as in `off-screen left:-9999px (in a shadow root)`, and the stylesheet check counts the shadow roots it read. This is partial as well. `:host`, `::slotted` and `::part` rules can't be matched and never count as hiding. The colour a host passes down isn't followed into its shadow root, so text there is judged against colours only when the shadow root declares both. The host's own children are judged as if a slot took every one of them, though a browser never shows a child no slot takes. A hidden host takes its shadow root's text into its own finding. Content inside any other `<template>` never shows and isn't judged.

For each page, loupe opens Chromium at 1280 by 800, loads the page, and waits for the load event, for the network to go quiet and for opening animations to finish. Then it records whether each element's own text is visible. Visible means all of these:

- `checkVisibility()` passes with `opacityProperty`, `visibilityProperty` and `contentVisibilityAuto`
- the text has a box at least 2px each way, inside the part of the page a reader can scroll to
- the computed `font-size` is above 0
- it isn't clipped away by `overflow`, `clip` or `clip-path`
- its opacity, multiplied through its ancestors, is at least 0.05
- its colour stands out from the solid colour behind it, with a WCAG contrast ratio of at least 1.5:1
- nothing that scrolls with the page paints a solid colour over it

The colour behind the text is whatever is painted under it: its own background and the backgrounds below it, found with `elementsFromPoint` when it's on screen and through its ancestors otherwise, blended down to the first opaque one over the page's white. A gradient whose stops are all one colour counts as that colour. When an image, a picture or a gradient of several colours is under the text, loupe doesn't guess: it counts the text as visible and says in the rendering notes how many elements it couldn't judge. A text shadow or outline counts as standing out.

The threshold comes from WCAG's contrast ratio. WCAG 2.2 asks for 4.5:1 for body text and 3:1 for large text; text between 1.5:1 and those is hard to read, an accessibility problem rather than hiding, and loupe leaves it alone. Below 1.5:1 the text all but disappears: `#ccc` on white is 1.6:1, faint but readable, while `#ddd` on white is 1.36:1, and the same colour as its background is 1:1.

Covered text is found by hit testing. For each of the first four boxes of an element's text, loupe asks the browser, with `elementsFromPoint`, what's painted at five points: the middle and four points toward the corners. The text is covered when, at every one of those points that's on screen, an element painted above it paints a solid colour, its own background colour or a gradient of one colour, at least 95% opaque. An element that paints an image, a picture or a gradient of several colours may or may not let the text through, so text under one counts as visible and the notes count it. Fixed and sticky elements, dialogs and open popovers don't count: the page scrolls under a header, a cookie banner or a modal, so a line one covers at one step shows at another, and counting them would flag a line on every screen. Text is hit-tested only while some of it is on screen, so text that no step brings on screen counts as visible. Hit testing skips elements with `pointer-events: none`, so a cover like that isn't seen, and neither is text under anything when the text itself ignores the pointer.

Then loupe scrolls from the top to the bottom one screen at a time, pausing at each step and checking again. After that, it brings each element that is still hidden only by opacity, position or clipping (the ways scroll animations hide things) to the middle of the screen and checks once more, because some reveals fire only at one scroll position. Text hidden with `display:none`, `visibility:hidden` or a closed `<details>` needs a click to show, so it isn't centred. A last check runs once transitions settle.

Text that was never seen visible becomes a `rendered-hidden` finding, with the reason (such as `display:none`, `clipped`, `covered by another element` or `inside a closed <details>`). It goes through the same phrase list and markdown comparison as everything else. Text hidden at load but seen later is listed separately as revealed on scroll and isn't counted as hidden. The markup checks for comments, meta tags, label attributes, `aria-hidden`, `<noscript>` and fallback content still run on the HTML as served. The rendered result replaces the inline-style, stylesheet and `hidden` attribute guesses.

Text in the HTML as served that the page's scripts take out never shows, but markdown made from that HTML keeps it. At the end of the scan loupe reads the document's text, outside scripts and styles, and looks for elements of the served HTML whose text is gone from it: their whole text, their own text outside their children and each child's text, all gone, or all but a few words, so a script that changes a date, a price or a clock doesn't count as removing it. The outermost such element becomes a `rendered-hidden` finding with the reason `removed by a script`, but only when its text reaches the markdown. The check is skipped for a page whose text runs past 5 million characters, and for HTML given to loupe that tried to load another document, since the browser may not have read all of it.

### Shadow roots and frames

Text in shadow roots is checked like the rest of the page, with the shadow root's own styles and slots. Open shadow roots are reached through their hosts. Closed ones, which the page's own scripts can't reach, are found through the browser's DevTools protocol before the first check and again before the last, so a closed root a script attaches in between is checked only at the end. Text slotted into a part of a shadow root that's hidden counts as hidden, and so does a host's own child that no slot takes, since it never renders. A finding in a shadow root says so after its reasons, as in `outside the page (in a shadow root)`.

Frames are checked after the page, parents before children, same-origin and cross-origin alike, each in an isolated world of its own. A cross-site frame that the browser runs in a process of its own, as it does when it isolates sites, gets a DevTools session of its own. First the element holding the frame, such as an `<iframe>`, is checked in its parent. If no check of the parent ever saw its box, because it's `display:none`, under 2px either way, transparent, off the page, clipped away or covered by another element, none of the frame's text can show: all of it becomes one finding with the holder's reasons and `(in a hidden frame)`. Otherwise the holder is brought to the middle of the screen, where the frame's own scroll animations can fire, and the frame is checked like a page: at load, at each of up to 10 scroll steps of its own, centred, and once it settles. Its findings say `(in a frame)`. Text in a visible frame is judged against the frame's own viewport, so a frame partly clipped in its parent counts as showing all of its text.

At most 20 frames are checked, within 30 seconds in all. The rendering notes count the frames past that, and the frames that couldn't be checked at all: one that didn't load, one that shows only the refusal from loupe's address check, and one the browser wouldn't run the checks in. Whether their text shows isn't judged.

Text in a frame isn't in the HTML the site served, so it never reaches markdown made from that HTML. An agent that reads the page in a browser, through the accessibility tree or the frame's own document, can still get it. Text in a shadow root declared in the HTML, with `<template shadowrootmode>`, is in the served HTML, and markdownify keeps it.

### The inline-style trace

Each element an inline-style guess flagged is traced and reported as visible at load, revealed on scroll, still hidden, or not matched. The trace starts as soon as the page's HTML arrives. It checks each flagged element every 250 milliseconds and whenever nodes are added, and records the first time it was visible: at or before the first full check counts as visible at load, later counts as revealed on scroll. Elements are matched by tag and text, since a framework can swap a node for a copy after load, and a page can carry several copies of the same text, so the most visible copy decides. If the page replaces its whole document while it loads, the trace starts again on the new one and a note says so.

### Isolation and the remote browser

The checks run in an isolated JavaScript world, so a page's own scripts can't patch `checkVisibility` or `getClientRects` to make hidden text look visible.

The browser is local Chromium unless the Bright Data backend runs with `BRIGHTDATA_BROWSER_WSS` set to a CDP websocket endpoint for Bright Data's Browser API. The local backend never reads it. That value carries credentials. loupe never prints, logs or saves it, and a failed connection reports only that it failed. A remote browser gets two-minute timeouts. It skips images, media and fonts, but never stylesheets, since they decide what shows, and each page records the bytes transferred and the requests it refused. A remote page fails if it measures no bytes, or if it loaded images over http(s) and nothing was refused, because then neither the byte limit nor the blocking can be trusted. Every page gets a fresh browser.

## The JSON

[json.md](json.md) lists every field and how the format stays stable, and [schema/results-v1.json](schema/results-v1.json) is its JSON Schema. Each page starts with a `verdict`, the plain-English line the terminal output opens with: how many pieces of hidden text read like instructions to an AI and how many of those reach the markdown, then how much hidden text there is in all. Each page records the `backend` (`local` or `brightdata`), and each page with markdown records its `markdown_source`: the converter's `name`, `version` and `license`, and `approximation`, which is true for markdown made locally. Each page has a `scan_mode` and each finding a `mode` (`static` or `rendered`). A rendered page adds a `rendering` block with the browser version, scroll steps, notes, the revealed text and the inline-style trace. Quoted text is cut to `--quote-chars` characters, and per-request meta values (nonces, CSRF tokens, request IDs and the like) are replaced with a placeholder.

## What the 19 November runs showed

The runs behind the README's table are `results/2026-11-19.json` (markup only) and `results/2026-11-19-rendered.json` (`--render` through the Browser API, with images, media and fonts blocked), with `.txt` summaries beside them. They cover four public pages and the two test pages. Quoted text is cut to 80 characters, and the pages themselves aren't stored. The browser run measured 2,931,749 bytes and refused 96 image, media and font requests. On the OWASP page the load event hadn't fired after 15 seconds and the network was still busy after 10, so its first check ran with requests open.

The browser finds far more on Wikipedia and GitHub, because most of their hidden text is hidden by stylesheets the markup scan can't read. On Wikipedia, 97 of the 110 findings are `display:none` alone. GitHub's reasons are more mixed: `display:none`, sections collapsed inside a closed `<details>`, text too small to see, and clipped text.

The animated test page goes the other way. The markup scan flags its headline and four sections for inline `opacity:0`. In the browser, 2 of the 5 showed as the page loaded and 3 showed after scrolling, so none of them stayed hidden. The browser does report 3 things there as hidden: the invisible layout copy behind the headline, the skip link, and the menu inside a closed `<details>`. A reader can't see any of them at that moment, and none of them is aimed at an agent.

On the four public pages, all five instruction-like matches are Wikipedia link titles, such as "You are encouraged to create an account" and "Large language model". None of them is an attack. The blender test page's matches are the instructions planted in it.

## Local against Bright Data

On 20 November both test pages were scanned live on the local backend, as is and with `--render` (`results/2026-11-20-local.json` and `results/2026-11-20-local-rendered.json`), and compared with the Bright Data runs of 19 November. In both modes the two backends reported the same findings, the same instruction-like count, the same inline-style trace and the same answer for every finding on whether it reached the markdown.

The one difference is the markdown itself: markdownify's is 47 characters longer on the blender page (2,344 against 2,297) and 49 on the animated page (1,747 against 1,698). markdownify starts with the page's `<title>`, 38 and 42 characters here, which accounts for most of that; the rest is likely spacing. That's an inference, since loupe doesn't keep Bright Data's markdown. On other pages the converters can disagree on more, such as link titles, which markdownify keeps.

## What reaches the markdown

For some kinds of hiding, a lot. Across the four public pages in the markup-only run:

| Kind | In markdown | Not in markdown | Not compared |
|---|---|---|---|
| `hidden` attribute | 4 | 0 | 0 |
| Inline style | 2 | 0 | 0 |
| `aria-hidden` | 10 | 0 | 11 |
| HTML comment | 0 | 19 | 8 |
| `<meta>` | 3 | 52 | 40 |
| `alt`, `title`, `aria-label` | 214 | 187 | 89 |
| `<noscript>` | 0 | 0 | 0 |

Text inside elements hidden with the `hidden` attribute or `display:none` reaches the markdown. On the OWASP page, a `display:none` print and email menu and two `hidden` "Share on" labels all show up in it. On the blender test page, the instruction hidden by a stylesheet class and both hidden fake reviews reach it too. HTML comments don't: none of the 19 that could be compared were in the markdown. Meta content almost never is. So an agent reading this markdown is protected from comments but not from text hidden with markup or CSS, which are the easiest places to plant an instruction.

The README's limitations apply here: the HTML and the markdown come from two separate Bright Data services, and "in markdown" means only that the text appears somewhere in the markdown.
