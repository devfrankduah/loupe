# loupe

loupe fetches public web pages through Bright Data and flags text that people reading the page can't see but an AI agent reading it would.

## Why

Agents don't look at pages. They read them, usually as raw HTML or as markdown converted from it. Anything in the markup that a browser hides from a person (a comment, a `display:none` block, an image's alt text) can still land in the model's context. If that text is written as an instruction, the model may follow it, and the person who asked the agent to read the page never saw it. I wanted to measure how much of this hidden text exists on ordinary pages and how much of it survives the trip to markdown.

## Install

Python 3.10 or newer.

```
git clone https://github.com/devfrankduah/loupe.git
cd loupe
pip install -e ".[test]"
```

## Usage

loupe needs a Bright Data API token in `BRIGHTDATA_API_TOKEN`. It reads the token from the environment and never prints or saves it.

```
export BRIGHTDATA_API_TOKEN=...
loupe https://example.com https://en.wikipedia.org/wiki/Prompt_injection
```

Options:

- `--json PATH` writes the full report as JSON.
- `--quote-chars N` cuts quoted hidden text to N characters (default 160, 0 for no limit).
- `--skip-markdown` scans the HTML only.
- `--zone NAME` picks the Web Unlocker zone (default `mcp_unlocker`).

The exit code is 0 when every fetch worked, 1 when any fetch failed, and 2 for usage errors such as a missing token.

Tests run offline against local fixtures and don't need a token:

```
pytest
```

## How pages are fetched

I send the same requests that Bright Data's own MCP server ([brightdata-mcp](https://github.com/brightdata/brightdata-mcp), version 4.3.0) sends for its two scraping tools:

- HTML, like `scrape_as_html`: `POST https://api.brightdata.com/request` with `{"url", "zone": "mcp_unlocker", "format": "raw"}`. That's Web Unlocker. The MCP server creates the `mcp_unlocker` zone the first time it starts, so on an account that has never run it you need to create that zone first.
- Markdown, like `scrape_as_markdown`: `POST https://api.brightdata.com/datasets/v3/scrape?dataset_id=gd_m6gjtfmeh43we6cqc&format=json&include_errors=true` with `[{"url"}]`, reading the `markdown` field of the first record. That's the Fetch API, which the MCP server switched to on 2026-11-01.

## What it checks

In the HTML:

- HTML comments.
- Elements with the `hidden` attribute.
- Elements with `aria-hidden="true"`. These are hidden from screen readers, not from sighted readers, so I report them separately.
- Elements whose inline `style` sets `display:none`, `visibility:hidden` or `collapse`, `font-size:0`, `opacity:0`, or pushes them off-screen (`position:absolute` or `fixed` with an offset of 1000px or more to the negative side, or a `text-indent` that far).
- `alt`, `title` and `aria-label` attributes.
- `<meta>` tags that have a name, property, http-equiv or itemprop.
- `<noscript>` blocks.

Text inside a hidden element is reported once, on the outermost hidden element. Script and style contents are ignored.

Every finding is checked against a short list of phrases that read like instructions to a model ("ignore previous", "ignore all", "you are", "system prompt", "assistant", "do not tell" and a few more). The list lives in `src/loupe/phrases.py`.

Then loupe fetches the markdown version of the same page and records whether each finding's text appears in it. Both sides are reduced to lowercase words, link targets are dropped, and the finding counts as present only if its full word sequence appears in order. Findings shorter than 12 characters after that reduction aren't compared, because short strings like "Menu" match by accident.

## Results

I ran it once on 2 November 2026 against five pages. example.com is the control. The full output is in `results/2026-11-02.txt` and `results/2026-11-02.json`, with quoted text cut to 80 characters. The pages themselves aren't stored.

| Page | Findings | Instruction-like | In markdown | Not in markdown | Not compared |
|---|---|---|---|---|---|
| example.com | 1 | 0 | 0 | 1 | 0 |
| en.wikipedia.org/wiki/Prompt_injection | 273 | 5 | 163 | 26 | 84 |
| genai.owasp.org/llmrisk/llm01-prompt-injection/ | 52 | 0 | 5 | 26 | 21 |
| github.com/brightdata/brightdata-mcp | 302 | 0 | 59 | 171 | 72 |
| frankduah.me | 147 | 5 | 31 | 60 | 56 |

The control produced one finding, the `viewport` meta tag, and nothing instruction-like.

None of the 10 instruction-like findings is an attack. On Wikipedia they're link `title` attributes: "You are encouraged to create an account and log in" (twice), "Gemini (language model)", "Large language model" and "Edit section: System prompt". On my own site all five match "assistant" in the name of one of my projects, the Device-Aware RAG Assistant.

The markdown for example.com and Wikipedia failed on the first pass with `HTTP 400: Customer is not active`. I had created the `mcp_unlocker` zone moments before, and the account seemed to need a moment to activate. I retried those two markdown fetches once each and both worked, so the run made 12 requests. I hadn't kept the full text of the findings from the first pass, only the 80-character quotes, so on the retry I compared only findings of 80 characters or less (where the quote is the whole text). The 34 longer Wikipedia findings are counted as not compared.

## Does hidden text reach an agent that reads markdown?

For some kinds of hiding, yes. Across all five pages:

| Kind | In markdown | Not in markdown | Not compared |
|---|---|---|---|
| `hidden` attribute | 5 | 1 | 0 |
| Inline style | 27 | 13 | 10 |
| `aria-hidden` | 11 | 3 | 27 |
| HTML comment | 0 | 14 | 20 |
| `<meta>` | 3 | 65 | 43 |
| `alt`, `title`, `aria-label` | 212 | 188 | 133 |
| `<noscript>` | 0 | 0 | 0 |

Text inside elements hidden with the `hidden` attribute or `display:none` reaches the markdown. On the OWASP page, a `display:none` print and email menu and two `hidden` "Share on" labels all show up in it. HTML comments don't: none of the 14 I could compare were in the markdown. Meta content almost never is. So an agent reading this markdown is protected from comments but not from text hidden with markup or inline CSS, which are the easiest places to plant an instruction.

Two caveats on these numbers. The HTML comes from Web Unlocker and the markdown from the Fetch API, so the two fetches are separate services and could have received different versions of a page. A difference can come from either service, not only from the markdown conversion. Also, "in markdown" means the text appears somewhere in the markdown, not that it got there from the hidden element. Many Wikipedia link titles repeat the link's visible text, so they'd match either way.

## Limitations

- Phrase matching is a heuristic, and it produces false positives. Every instruction-like finding in this run was one: Wikipedia's account link title, and my own project name matching "assistant". A real injection worded to avoid the list won't be flagged.
- The scanner can't tell real hiding from presentation. All 48 inline-style findings on frankduah.me are `opacity:0`, the starting state of a scroll-reveal animation. JavaScript fades that content in, so people do see it, but nothing in the HTML says so. Any site that animates content in this way will look full of hidden text.
- Only inline styles are read. An element hidden by a class in a stylesheet isn't detected.
- Nothing is rendered. No CSS is applied and no JavaScript runs, so content hidden or revealed at runtime is missed, and so is text that's hidden by being the same color as the background or covered by another element.
- The markdown comparison is exact word-sequence matching. If the markdown converter splits or reorders a hidden string, it counts as not present.
