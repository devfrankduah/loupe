# Changelog

## Unreleased

Two of these break existing scripts: `--json PATH` is now `--json-output PATH`, and exit code 1 now means a finding `--fail-on` counts, where it used to mean a failed fetch.

- The package is `loupe-scan`, imported as `loupe_scan`, and the command is still `loupe`. It installs from this repository until it's on PyPI, and a test fails if any file tells people to install `loupe` or `loupe-scan` from PyPI before then.
- `--json` prints the results as a JSON array instead of the text report, and `--json-output PATH` writes the same array to a file, in place of `--json PATH`. Each page carries `schema_version` 1 and an `error` object with a `kind`, which replaces `html_error`. `docs/json.md` describes the format and `docs/schema/results-v1.json` is its JSON Schema, which the tests check the output against in both scan modes. `loupe report` still reads the older files.
- Exit codes: 0 when every page was scanned and none had a finding `--fail-on` counts, 1 when one did, and 2 when a page couldn't be scanned in full or loupe couldn't run. 1 used to mean a failed fetch. `--fail-on` takes `instructions`, the default, `hidden` or `never`, and a nonzero exit says why on stderr.
- A typed Python API: `scan()`, `Scanner` for several pages, `ScanResult` with `to_json()`, and `ScanError`, raised for a page that couldn't be scanned so it can't pass for a clean one. The package ships `py.typed`. `docs/python.md` has an example of checking a page before handing it to a model, and the tests run it.
- `--urls-file` reads URLs from a file or standard input. Each URL is scanned once, one page at a time, reading each site's robots.txt once and waiting out its Crawl-delay, and several pages end with a summary table.
- A GitHub Action, `action.yml`, installs loupe at the ref a workflow names, scans the `urls` input, uploads the JSON as an artifact and fails the step by `fail-on`. `docs/ci.md` has a workflow for scanning a site after each deploy, and CI runs the action against the test pages.
- `python -m loupe_scan` runs the command, and the version lives in one place, `src/loupe_scan/_version.py`.
- New test dependency: jsonschema, with types-jsonschema for the type check.
- `docs/social-preview.png` carries the repository's new description: loupe runs locally, with Bright Data as an option for sites that block bots.

## 0.1.0

The version in `pyproject.toml` has been 0.1.0 since the first commit, and nothing is tagged yet. Each heading below is the day the work landed on `main`.

### 20 November 2026

- loupe works with no account. A local backend fetches pages from your machine, renders them in local Chromium with the same checks as the Browser API, and makes the markdown with markdownify (MIT), labelled an approximation of what agent tools produce. It picks itself when no Bright Data value is set, `--backend` overrides that either way, and the output and JSON name the backend each page used.
- The local fetch names loupe and its repository in its user agent, obeys robots.txt (with `--ignore-robots` for pages you own) and any Crawl-delay, and has time, redirect and 10 MB limits. A 403, a 429 or a Cloudflare challenge is reported as a block, with a link to the Bright Data section of the README.
- Each page's output starts with a plain-English verdict: how many pieces of hidden text read like instructions to an AI and how many of those reach the markdown, then the total amount of hidden text. The JSON keeps it as `verdict`.
- The tests run on the local backend only, against fixtures served from 127.0.0.1 in both scan modes, and fail any connection beyond the machine. Both test pages' ground truths pass end to end on the local backend, and live local runs of both pages match the 19 November Bright Data runs finding for finding.
- The README's quickstart needs no account, and Bright Data moves to a section for sites that block bots and for scanning at scale. The demo GIF shows the quickstart scan on the local backend.
- New dependencies: markdownify, Protego, and urllib3 2.2 or newer, which requests already installs.
- The Playwright install hint now installs from the repository folder, since `loupe` on PyPI is an unrelated package.

### 19 November 2026

- The README is rewritten for anyone building an agent that reads the web: what loupe catches and who it's for first, then a quickstart that needs no account, a plain guide to reading the output, and the deeper sections after.
- `docs/demo.gif` is recorded again, on the blender test page through the Browser API, with quotes cut to 80 characters so all 20 lines fit the window. New screenshots of the rebuilt report page are `docs/screenshot-desktop-2026-11-19.png` and `docs/screenshot-phone-2026-11-19.png`. `docs/social-preview.png` was generated again from the rebuilt page and came out unchanged.
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
