# Contributing to loupe

Bug reports, false positives, missed hidden text and fixes are all welcome. For anything bigger than a small fix, open an issue first to agree on the approach before writing it.

## Set up

You need Python 3.10 or newer. On macOS or Linux:

```
git clone https://github.com/devfrankduah/loupe.git
cd loupe
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test,render]" mypy types-jsonschema
playwright install chromium
```

The `render` extra and Chromium are only needed for the rendered tests. Without them, those tests are skipped.

## Run the tests and the type check

```
pytest
mypy --strict src tests
```

The tests run on the local backend against fixtures served from 127.0.0.1 and never need a Bright Data token. `tests/conftest.py` removes `BRIGHTDATA_API_TOKEN` and `BRIGHTDATA_BROWSER_WSS` from every test's environment, so a token in your shell can't reach a test run, and it fails any test that tries to connect beyond this machine.

CI runs both commands on Python 3.10 and 3.13 for every push to `main` and every pull request. It installs Chromium, which adds about 15 seconds, so the rendered tests run there too, and it fails if any test is skipped.

## Add a fixture

Fixtures are small HTML pages in `tests/fixtures/`. To add a case:

1. Write the page, and mark each piece of text you want to test with a `CASE-` marker in capitals, such as `CASE-FADED-IN`. Keep it self-contained, with no external scripts, stylesheets, fonts or images, so the test stays offline.
2. For a markup check, read the file and pass it to `scan_html` in a test in `tests/test_scan.py`. For a rendered check, add a test to `tests/test_render_browser.py` that renders `f"{fixture_site}/your-page.html"` with `render_page(chromium, ..., FAST)` and asserts which markers end up hidden, visible or revealed.
3. Check that the new test fails without your fix and passes with it.

`tests/fixture_site.py` serves the fixtures from 127.0.0.1. Files under `tests/fixtures/slow/` are held for a second before they're served, for pages that need to be parsed before a stylesheet arrives.

## Report a false positive or missed hidden text

Use the [false positive or missed hidden text](https://github.com/devfrankduah/loupe/issues/new?template=hidden-text.yml) form, and include:

- the page URL
- what you expected, such as "people can see this text" or "this text is hidden and loupe didn't flag it"
- what loupe actually reported, copied from its output
- the loupe version from `loupe --version`, and the command you ran, including whether it used `--render`

Pages change, so the smallest HTML that reproduces it helps a lot.

## Secrets

Never commit `.env`, and never put a Bright Data token or Browser API endpoint in code, tests, results, screenshots, issues or pull requests. The endpoint carries a password. `.gitignore` excludes `.env` and GitHub's push protection is on for this repo, but neither catches a value pasted into a test or a comment. If you commit one by mistake, revoke it in the Bright Data control panel first, then say so in a [private report](https://github.com/devfrankduah/loupe/security/advisories/new).

## Re-record the demo GIF

`scripts/record-demo.sh` records `docs/demo.gif` with asciinema and agg (`brew install asciinema agg`). It runs the README's quickstart scan of the blender test page on the local backend, with the Bright Data values removed from the command, so it needs no account. The recording stays in a temporary folder and is deleted either way. If `.env` holds Bright Data values, the script checks the recording for the token and every part of the endpoint before converting it, and stops if it finds one. Never commit a `.cast` file.

## Pull requests

Keep each pull request to one change, and fill in the checklist in the template. Files in `results/` come from real runs: add new ones as new dated files, run with `--quote-chars 80`, and don't edit existing ones by hand or commit the fetched pages themselves.
