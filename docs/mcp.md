# Use loupe with an agent

`loupe mcp` runs loupe as an MCP server, so an agent can read web pages through it. Its `fetch_page` tool fetches a page, checks it for text hidden from people, and returns the page as markdown with that text dealt with first. It runs on your machine over stdio and needs no account.

It needs the `mcp` extra, which installs the official MCP Python SDK. From the loupe folder, in the virtual environment the README's quickstart makes:

```
pip install -e ".[mcp]"
```

Add the `render` extra and `playwright install chromium` too if the agent will ask for rendered scans.

## The tools

`fetch_page(url, render=false)` fetches the page once, scans exactly the HTML it got, and returns markdown made from that same HTML under a short header. For the [test page](https://devfrankduah.github.io/loupe/test-page/) it starts:

```
loupe: this page hides text worded like instructions to an AI
page: https://devfrankduah.github.io/loupe/test-page/
scan: markup only, fetched from this machine
found: 7 pieces of hidden text, 3 worded like instructions to an AI, 3 reaching the markdown
markdown: 3 pieces of hidden text taken out
note: a markup-only scan misses text hidden by stylesheets and scripts; render=true checks those too
---
Quillmore Q6 blender (loupe test page)
```

`scan_page(url, render=false)` returns loupe's report on the page as structured data: the verdict, the counts, and each piece of hidden text's kind, how it was hidden, its element, the line its element starts on, its size, whether it's worded like an instruction and whether it reaches the markdown.

With `render=true` the page's HTML is loaded in local Chromium, which also catches text hidden by stylesheets and scripts. It takes seconds to minutes a page, against well under a second for the markup alone.

Neither tool quotes hidden text, because repeating a hidden instruction hands it straight to the model. A finding is described by its kind, location and size, and errors are given in loupe's own words, since an error from a site can carry text the site chose. `loupe mcp --quote-hidden-text` lets `scan_page` include each piece of hidden text, cut to 160 characters, for when you're the one reading the report.

## The three modes

Whoever starts the server picks what `fetch_page` does with hidden text, and every call gets the same mode:

- `--mode strip`, the default, takes every piece of hidden text loupe finds out of the markdown, wherever it reaches it, and returns the rest. Any copy goes, including a copy the page also shows, since the two can't be told apart. If taking text out leaves a new copy behind, loupe tries again, and after five rounds it withholds the markdown rather than return it with hidden text still in it.
- `--mode flag` returns the markdown unchanged, with the header saying how much hidden text reached it. The model reads the hidden text, so use it only when you want the page as it is.
- `--mode block` withholds the whole page when any hidden text is worded like an instruction to an AI, and strips any other page as strip mode does.

loupe decides whether text is worded like an instruction from a short list of phrases, so plenty of manipulative text passes as not instruction-like. Strip mode takes out all the hidden text it finds, whatever the list says.

## What strip can't promise

- The markdown is loupe's own conversion, made with markdownify, and approximates what other tools produce. Strip guarantees only that loupe's markdown no longer contains the hidden text loupe found. If the agent also fetches the page another way, that copy wasn't checked.
- Text coloured like its background gets through. loupe doesn't compare text colour with the background, so it counts that text as visible, in both scan modes. Row 8 of the [test page's ground truth](test-page/ground-truth.md) is such a line, and strip leaves it in.
- Without `render`, text a stylesheet or script hides gets through too. A markup-only scan reads the HTML alone, so on the test page two planted items hidden by a stylesheet class stay in the markdown. The header's `note:` line says so whenever the scan was markup only.
- Hidden text shorter than 12 characters, once punctuation is dropped, stays unless it's worded like an instruction. Words like "Menu" are hidden on many pages and shown elsewhere on them, and taking out every copy would damage the visible text.
- A finding is matched on its words in order, so hidden text that reaches the markdown with its words changed or reordered can be missed.

## Limits the server sets

These hold for every call. A tool call gives only a URL and `render`, and any other argument is ignored, so nothing in a call can change them:

- robots.txt is obeyed, read once per site and again after a day, and any Crawl-delay is waited out between pages. A site that asks for more than 30 seconds gets its first page fetched and later ones refused.
- One page at a time. Calls that arrive together wait their turn, so no site gets two requests from the server at once.
- A page must be no larger than 10 MB, and on the local backend it must arrive within about 60 seconds. The markdown returned is cut to at most 60,000 characters, which `--max-characters N` changes.
- Only public addresses are fetched, redirects included, so a page can't send the agent to this machine or your network. `--allow-private-addresses` lifts that, for testing or a network you trust. The address is looked up again when the request connects, so a site whose name resolves differently the second time could still get past the check.

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
