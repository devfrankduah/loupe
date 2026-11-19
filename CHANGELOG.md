# Changelog

## 0.1.0

The version in `pyproject.toml` has been 0.1.0 since the first commit, and nothing is tagged yet. Each heading below is the day the work landed on `main`.

### 19 November 2026

- The results and every mention of a personal site that was one of the five pages scanned are gone from `main`, along with the demo GIF and screenshots that showed it; the git history keeps them. The README, `docs/how-it-works.md` and the report page now use the 19 November runs of four public pages and both test pages, and the animated test page takes over as the example of false alarms. The report page's byline no longer names a person, and its caveats no longer cite "assistant" as a listed phrase.
- A second test page at `docs/animated-page/`: a landing page for a made-up travel pack with scroll fades, a headline that rotates over an invisible layout copy, a live clock, a skip link and a collapsed menu, and nothing planted. Its ground truth says every hidden-text flag from the markup scan is a false alarm and the rendered scan clears all of them, and the tests check both modes against it.

### 9 November 2026

- A test page at `docs/test-page/`: a product page for a made-up blender that hides six instructions to AI and two fake reviews, modelled on The Guardian's December 2024 test of ChatGPT search. Its ground truth says what loupe should report for each, including the background-coloured line it misses, and the tests check both scan modes against it.
- The README now runs from what loupe catches through a quickstart, one results table and the limits. The detail moved to `docs/how-it-works.md`.
- CI runs the tests and `mypy --strict` on Python 3.10 and 3.13 for every push to `main` and every pull request, with Chromium installed for the rendered tests.
- The dark mode control is a switch named "Dark mode" in both modes, so a screen reader hears "Dark mode, on" or "Dark mode, off". In dark mode the moon fills in and the border takes the accent color. Light mode looks the same as before.
- A demo GIF at the top of the README, recorded by `scripts/record-demo.sh` with asciinema and agg.
- `docs/social-preview.png`, a 1280 by 640 image for the repo's social preview.
- Added a contributing guide, a code of conduct, a security policy, issue forms, a pull request template, a Dependabot config and a citation file.
- The report page's dark mode checkbox moved into the header beside its label. Keyboard focus now starts at the skip link, every control sits in a landmark, and axe-core 4.10.3 finds no violations at desktop or phone width in either mode.

### 8 November 2026

- The README covers virtual environment setup, getting a Bright Data token and Browser API endpoint, and keeping both in `.env`.
- `loupe --help` names the `report` subcommand.
- The repo went public.

### 7 November 2026

- Ran the five pages again, markup only and with `--render` through the Bright Data Browser API, in `results/2026-11-07-2.json` and `results/2026-11-07-2-rendered.json`, after an earlier pair from the same day that came before the watch described below. All of the 2 and 7 November results were removed from `main` on 19 November; the git history keeps them.
- Any part of the Browser API endpoint that turns up in an error is replaced with `[endpoint]`.
- Bytes and refused requests are counted through Playwright's own events, and each rendered page records the viewport it reported.
- "assistant" no longer counts as instruction-like. On real pages it only ever named products and projects, including five false matches in one project's name on 2 November.
- Each traced inline-style candidate is matched against the final page as well as at the first check.
- Centring is limited to elements a scroll animation hides, with a cap of 200 elements and 60 seconds a page.
- The inline-style trace watches each flagged element from the moment the page's HTML arrives, every 250 milliseconds and whenever nodes are added. Before that, a headline carousel on one of the scanned pages made six of its words look never shown.

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
- First run on five pages, in `results/2026-11-02.json` and `results/2026-11-02.txt`. The markdown for example.com and Wikipedia failed on the first pass with `HTTP 400: Customer is not active`, because the `mcp_unlocker` zone had been created moments before. One retry of each worked, so the run made 12 requests. Only the 80-character quotes had been kept from the first pass, so the retry compared only findings of 80 characters or less, and the 34 longer Wikipedia findings count as not compared.
- None of the 10 instruction-like findings in that run was an attack. Five were Wikipedia link titles, and five matched "assistant" in a project's name.
