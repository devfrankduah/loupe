# Changelog

## 0.1.0

The version in `pyproject.toml` has been 0.1.0 since the first commit, and nothing is tagged yet. Each heading below is the day the work landed on `main`.

### 9 November 2026

- The README now runs from what loupe catches through a quickstart, one results table and the limits. The detail moved to `docs/how-it-works.md`.
- CI runs the tests and `mypy --strict` on Python 3.10 and 3.13 for every push to `main` and every pull request, with Chromium installed for the rendered tests.
- The dark mode control is a switch named "Dark mode" in both modes, so a screen reader hears "Dark mode, on" or "Dark mode, off". In dark mode the moon fills in and the border takes the accent color. Light mode looks the same as before.
- `docs/demo.gif`, at the top of the README, shows one real rendered scan of frankduah.me through the Browser API. `scripts/record-demo.sh` records it with asciinema and agg.
- `docs/social-preview.png`, a 1280 by 640 image for the repo's social preview.
- Added a contributing guide, a code of conduct, a security policy, issue forms, a pull request template, a Dependabot config and a citation file.
- The report page's dark mode checkbox moved into the header beside its label. Keyboard focus now starts at the skip link, every control sits in a landmark, and axe-core 4.10.3 finds no violations at desktop or phone width in either mode.

### 8 November 2026

- The README covers virtual environment setup, getting a Bright Data token and Browser API endpoint, and keeping both in `.env`.
- `loupe --help` names the `report` subcommand.
- The repo went public.

### 7 November 2026

- Ran the five pages again, markup only and with `--render` through the Bright Data Browser API. The results are `results/2026-11-07-2.json` and `results/2026-11-07-2-rendered.json`. The first pair from that day, `results/2026-11-07.json` and `results/2026-11-07-rendered.json`, came before the watch described below.
- Any part of the Browser API endpoint that turns up in an error is replaced with `[endpoint]`.
- Bytes and refused requests are counted through Playwright's own events, and each rendered page records the viewport it reported.
- "assistant" no longer counts as instruction-like. On real pages it only ever named products and projects, including five false matches on my own site on 2 November.
- Each traced inline-style candidate is matched against the final page as well as at the first check.
- Centring is limited to elements a scroll animation hides, with a cap of 200 elements and 60 seconds a page.
- The inline-style trace watches each flagged element from the moment the page's HTML arrives, every 250 milliseconds and whenever nodes are added. Before that, frankduah.me's headline carousel made six of its words look never shown.

### 6 November 2026

- Added `--render`, which loads each page in Chromium, local or through Bright Data's Browser API, scrolls it, centres elements still hidden after the scroll, and reports text a browser never shows.
- Text faded below 0.05 opacity counts as hidden.
- Each inline-style finding from the markup scan is traced through the rendered scan.
- `loupe report` sets a markup-only run and a rendered run side by side.
- Per-request meta values (nonces, CSRF tokens, request IDs) are removed before they're reported.
- Control characters in page text are escaped in the terminal summary.
- A remote browser gets longer timeouts, skips images, media and fonts, and counts the bytes it transfers.

### 2 November 2026

- First version: the markup scan, the instruction phrase list, a Bright Data client that sends the same requests as Bright Data's MCP server, the markdown comparison, the terminal summary and `--quote-chars`.
- `loupe report`, which turns results into one HTML page with no scripts, and that page on GitHub Pages.
- Tests ran on pull requests.
- First run on five pages, in `results/2026-11-02.json` and `results/2026-11-02.txt`. The markdown for example.com and Wikipedia failed on the first pass with `HTTP 400: Customer is not active`, because I'd created the `mcp_unlocker` zone moments before. I retried those two markdown fetches once each and both worked, so the run made 12 requests. I'd kept only the 80-character quotes from the first pass, so on the retry I compared only findings of 80 characters or less, and the 34 longer Wikipedia findings count as not compared.
- None of the 10 instruction-like findings in that run was an attack. Five were Wikipedia link titles, and five matched "assistant" in the name of one of my projects, the Device-Aware RAG Assistant.
