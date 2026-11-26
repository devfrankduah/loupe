# Use loupe with an agent

`loupe mcp` runs loupe as an MCP server, so an agent can read web pages through it. Its `fetch_page` tool fetches a page, checks it for text hidden from people, and returns the page as markdown with that text dealt with first. It runs on your machine over stdio and needs no account.

It needs the `mcp` extra, which installs the official MCP Python SDK. From the loupe folder, in the virtual environment the README's quickstart makes:

```
pip install -e ".[mcp]"
```

Add the `render` extra and `playwright install chromium` too, so the server can load pages in Chromium when it needs to: `pip install -e ".[mcp,render]"`, then `playwright install chromium`. Without them, the server reads pages from their markup alone and says in each header what that left unchecked.

## The tools

`fetch_page(url, render=false)` fetches the page once, scans exactly the HTML it got, and returns markdown made from that same HTML under a short header. For the [test page](https://devfrankduah.github.io/loupe/test-page/) it starts:

```
loupe: this page hides text worded like instructions to an AI
page: https://devfrankduah.github.io/loupe/test-page/
scan: rendered in Chromium, fetched from this machine
mode: auto, rendered, as the page has scripts
found: 11 pieces of hidden text, 6 worded like instructions to an AI, 6 reaching the markdown
markdown: 6 pieces of hidden text taken out
---
Quillmore Q6 blender (loupe test page)
```

`scan_page(url, render=false)` returns loupe's report on the page as structured data: the verdict, the counts, which scan ran and why (`scan_decision`, the header's `mode:` line), and each piece of hidden text's kind, how it was hidden, its element, the line its element starts on, its size, whether it's worded like an instruction and whether it reaches the markdown.

## How the tools pick a scan

A markup scan reads the HTML and applies the page's `<style>` blocks and up to five stylesheets linked from its own host for the simple ways CSS hides text, fetching those stylesheets under the same robots.txt, address and size rules as the page. It takes well under a second. A rendered scan loads the page's HTML in local Chromium, which applies every stylesheet and runs the page's scripts, and takes seconds to minutes. `--scan` picks between them for every call:

- `--scan auto`, the default, runs the markup scan, and then the rendered one as well when the page holds anything below that only a browser can judge. The rendered result is the one returned. A page with none of them, a static page whose styles the markup scan reads in full, stays on the markup scan alone.
- `--scan markup` never opens a browser, whatever a call asks.
- `--scan render` always does.

A call's `render=true` asks for a rendered scan in auto mode whatever the page holds, and changes nothing under `--scan markup` or `--scan render`. The header's `mode:` line says which scan ran and why, such as `mode: auto, rendered, as the page has scripts`. If auto wants a browser and Chromium isn't installed, or the page won't load in it, the header says so, names what it found, says that covered text, shadow DOM, frames and what scripts do weren't checked, and gives the install command, and the markup scan's result is returned. A server started with `--scan markup` names what it found that it didn't check in a browser.

A page that runs scripts can show text its markup hides, as many pages do with a menu or an article a stylesheet keeps hidden until a script reveals it, so a markup scan can't be sure that text stays hidden. When a markup scan finds hidden text on such a page, the verdict says so, and the findings stay as they are. Under `--scan markup` the header's first line reads `loupe: this page hides text worded like instructions to an AI, though the page runs scripts, which may show some of the hidden text, and a markup scan doesn't run them (--scan render, or the default --scan auto, does)`; when auto couldn't use a browser, it ends `and without a browser loupe couldn't run them` instead. `scan_page`'s `verdict` adds the same as a sentence of its own.

What makes auto render a page, found from the HTML and the styles the markup scan read, rules that might apply included:

- Frames: an `<iframe>`, `<frame>`, `<object>` or `<embed>` that isn't hidden. Its document isn't in the page's HTML.
- Shadow roots: a shadow root declared in the HTML with `<template shadowrootmode>`. The markup scan applies its own styles but not `:host`, `::slotted` or slots.
- Scripts: a `<script>` that runs, one with no `type`, a JavaScript type or `module` and a source or code, or an event handler attribute such as `onload`. A script can hide, add, move or cover anything, and a shadow root it attaches is in no HTML. JSON-LD and other data blocks don't count, and neither do scripts inside `<noscript>` or an ordinary `<template>`.
- Stylesheets the markup scan couldn't read: one on another host, past the first five, behind `@import`, or that failed.
- Style rules it couldn't settle: a rule bearing on hiding inside `@supports`, `@layer`, `@container` or `@scope`, a media query about anything but the viewport's width, height or orientation, or a selector it can't match.
- Elements placed where they could cover text: `position` `absolute`, `fixed` or `sticky`, `relative` with an offset or a `z-index`, a negative `z-index`, any `transform`, `translate`, `rotate` or `scale`, a negative margin, or grid placement (`grid-area`, `grid-row`, `grid-column`). Only a browser can tell what's painted over what.
- Styles the markup scan can't judge: `overflow` that clips (`hidden`, `clip`, `scroll` or `auto`), since a size, a `line-height` or an indent can clip the box's own text away; opacity that multiplies down to under 0.25, or that's set with `var()`; text under 4px, counting an em as 16px, under 25%, or under 1 of a unit it can't size, such as `vw`; `filter`, `backdrop-filter`, `mix-blend-mode`, `mask`, `content-visibility`, `-webkit-text-fill-color` or `contain` that can hide; a `clip` or `clip-path` that doesn't clip to nothing; and any hiding property set with `var()`.
- Colours it can't compare: text whose colour, or the colour behind it, is set in a notation it doesn't read, such as `hsl()`, `light-dark()` or `color-mix()`, or with a `var()` it can't resolve, and text where only one of the two colours is declared and, against a browser's default black text or white page for the other, they'd be too close to see. Text on an image or a gradient of several colours doesn't count, since a rendered scan doesn't judge that either.
- Content that opens on a click: a closed `<details>` or `<dialog>`, or an element with `popover`.

Elements the markup scan already finds hidden don't count, and neither does what's inside them, except in `aria-hidden`, which sighted readers still see. Scripts count wherever they run. Most pages on the web run scripts, so in practice auto renders most pages and saves the browser on static ones.

Neither tool quotes hidden text in what loupe writes itself, because repeating a hidden instruction hands it straight to the model. That covers `fetch_page`'s header and `scan_page`'s report. It doesn't cover the page: in flag mode `fetch_page` passes the page's markdown through unchanged, hidden text and all, because that's what flag mode is for. A finding is described by its kind, location and size, and errors are given in loupe's own words, since an error from a site can carry text the site chose. `loupe mcp --quote-hidden-text` lets `scan_page` include each piece of hidden text, cut to 160 characters, for when you're the one reading the report.

## The three modes

Whoever starts the server picks what `fetch_page` does with hidden text, and every call gets the same mode:

- `--mode strip`, the default, takes every piece of hidden text loupe finds out of the markdown, wherever it reaches it, and returns the rest. Markdown left empty where text went is tidied away with it: a bullet, heading or quote with nothing left, empty bold or italic markers, an emptied link title, and a link with no text left. Any copy goes, including a copy the page also shows, since the two can't be told apart. If taking text out leaves a new copy behind, loupe tries again, and after five rounds it withholds the markdown rather than return it with hidden text still in it.
- `--mode flag` returns the markdown unchanged, with the header saying how much hidden text reached it. The model reads the hidden text, so use it only when you want the page as it is.
- `--mode block` withholds the whole page when any hidden text is worded like an instruction to an AI, and strips any other page as strip mode does.

loupe decides whether text is worded like an instruction from a short list of phrases, so plenty of manipulative text passes as not instruction-like. Strip mode takes out all the hidden text it finds, whatever the list says.

## What strip can't promise

- The markdown is loupe's own conversion, made with markdownify, and approximates what other tools produce. Strip guarantees only that loupe's markdown no longer contains the hidden text loupe found. If the agent also fetches the page another way, that copy wasn't checked.
- Text coloured like a background loupe can't judge gets through. loupe compares text with a solid colour behind it, but text on an image, a picture or a gradient of several colours counts as visible. A markup scan checks colours only where the page's styles declare both, reading `var()` only for custom properties set once, on `:root` or `html`; auto renders a page whose colours it can't settle. Row 8 of the [test page's ground truth](test-page/ground-truth.md), a line coloured like its solid background, is now found in both modes and stripped.
- A markup scan lets through text a script hides, and text hidden by a stylesheet rule the partial check doesn't apply: one on another host, behind `@import`, or inside `@supports`, `@layer` or a media query it can't settle. Auto renders any page with a script, an unread stylesheet or an unsettled rule, so this applies under `--scan markup`, or when auto couldn't start Chromium, as the header then says. On the test page the two items a stylesheet class hides are caught either way.
- A rendered scan counts text in the HTML that the page's scripts take out as hidden when it reaches the markdown, since a reader never sees it. Text a script only changes, such as a date or a clock, doesn't count, and neither does text taken out of a page whose text runs past 5 million characters.
- Hidden text shorter than 12 characters, once punctuation is dropped, stays unless it's worded like an instruction. Words like "Menu" are hidden on many pages and shown elsewhere on them, and taking out every copy would damage the visible text.
- A finding is matched on its words in order, so hidden text that reaches the markdown with its words changed or reordered can be missed.

## Limits the server sets

These hold for every call. A tool call gives only a URL and `render`, and any other argument is ignored, so nothing in a call can change them, nor the `--scan` a server was started with:

- robots.txt is obeyed, read once per site and again after a day, and any Crawl-delay is waited out between pages. A site that asks for more than 30 seconds gets its first page fetched and later ones refused.
- One page at a time. Calls that arrive together wait their turn, so no site gets two requests from the server at once.
- A page must be no larger than 10 MB, and on the local backend it must arrive within about 60 seconds. The markdown returned is cut to at most 60,000 characters, which `--max-characters N` changes.
- Only public addresses are fetched, redirects and stylesheets included, so a page can't send the agent to this machine or your network. `--allow-private-addresses` lifts that, for testing or a network you trust. Each request connects to an address that passed the check, and to nothing else, while the Host header and TLS's server name and certificate check stay the site's own name, so a name that resolves to a private address the second time it's looked up (DNS rebinding) can't get past it. The server connects directly for this, ignoring proxy and CA bundle settings in its environment. In a rendered scan, the page's HTML is fetched this way, and every request Chromium then makes for the page, its stylesheets, scripts, images, frames, redirects and WebSockets included, goes through a proxy on this machine that applies the same check and connects only to the address that passed. Requests it refuses are counted in the scan's notes.

The local backend fetches from your machine with loupe's own user agent. `--backend brightdata` fetches through Bright Data's Web Unlocker instead, for sites that turn loupe away, and reads the token from `BRIGHTDATA_API_TOKEN` in the server's environment, as the command line does. Without `--backend`, the server uses Bright Data when that variable is set. The markdown is made from the fetched HTML on this machine either way, so the server never fetches a page twice.

## Claude Code

Add the server with the full path to `loupe` in loupe's virtual environment. Options for Claude Code go before `--`, and everything after it starts the server:

```
claude mcp add --transport stdio loupe -- /path/to/loupe/.venv/bin/loupe mcp
```

Put the server's own options at the end, such as `-- /path/to/loupe/.venv/bin/loupe mcp --mode block`. By default the server is added for you in the current project only. `--scope user` adds it for all your projects, and `--scope project` writes it to `.mcp.json` in the project root to share with everyone working there.

`claude mcp list` shows whether it connected, and `/mcp` inside Claude Code shows its tools.

To use the Bright Data backend, pass the token with `--env BRIGHTDATA_API_TOKEN=...` before `--transport`. Claude Code stores it in its own configuration file, `~/.claude.json`, in plain text.

## Claude Desktop

In Claude Desktop, open Settings, then the Developer tab, and choose Edit Config. That opens `claude_desktop_config.json`, which is at `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS and `%APPDATA%\Claude\claude_desktop_config.json` on Windows. Add loupe under `mcpServers`:

```json
{
  "mcpServers": {
    "loupe": {
      "command": "/path/to/loupe/.venv/bin/loupe",
      "args": ["mcp"]
    }
  }
}
```

On Windows, use `"command": "C:\\path\\to\\loupe\\.venv\\Scripts\\loupe.exe"`, with each backslash doubled as JSON needs. Give the full path either way, so Claude Desktop finds loupe whatever its PATH holds. Add options such as `"--mode", "block"` to `args`, and the Bright Data token as `"env": {"BRIGHTDATA_API_TOKEN": "..."}`, which is stored in that file in plain text.

Quit Claude Desktop completely and start it again. If loupe doesn't appear, its logs are in `~/Library/Logs/Claude` on macOS and `%APPDATA%\Claude\logs` on Windows: `mcp.log` for connecting, and `mcp-server-loupe.log` for what loupe printed. loupe prints only to stderr, since stdout carries the protocol.

## Other clients

Any MCP client that starts a local server from a command and its arguments over stdio should work, with the same command and `mcp` argument. These haven't been tried: Cursor, VS Code, Windsurf, Zed and Cline.

These instructions follow Anthropic's [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp) and the MCP project's guide to [connecting local servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers), as of November 2026.
