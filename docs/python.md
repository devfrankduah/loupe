# Use loupe from Python

`scan()` runs loupe's checks on a page from Python and returns what the page hides from people. Install loupe from this repository as the README's quickstart does. The package is `loupe-scan`, imported as `loupe_scan`. It's typed, and it ships a `py.typed` marker, so mypy and other type checkers read its annotations.

```
from loupe_scan import scan

result = scan("https://devfrankduah.github.io/loupe/test-page/")
print(result.verdict)
```

It runs the same checks as the `loupe` command, with the same backends and robots.txt rules, and reads credentials from the same environment variables.

## Check a page before handing it to a model

An agent that reads web pages can ask loupe first, and leave out a page that hides instructions from the person using the agent. If loupe can't check a page, because the site blocked it or robots.txt keeps loupe out, `scan()` raises `ScanError`, so a page that wasn't checked can't pass for a clean one.

```python
from loupe_scan import ScanError, scan


def safe_to_hand_over(url: str) -> bool:
    """True when loupe scanned url and found no hidden text worded like an instruction to an AI."""
    try:
        result = scan(url)
    except ScanError as error:
        # loupe couldn't check the page, so don't pass it on unchecked.
        print(f"{url} wasn't checked ({error.kind.value}): {error}")
        return False
    for item in result.instruction_like:
        reach = {True: "reaches", False: "doesn't reach", None: "wasn't compared with"}[item.in_markdown]
        print(f"{url} hides {item.kind.value} text that {reach} the markdown: {item.text[:80]!r}")
    return not result.fails("instructions")
```

Then, wherever the agent fetches a page for the model:

```
if safe_to_hand_over(url):
    messages.append({"role": "user", "content": page_markdown})
```

`scan()` fetches the page itself, so it checks what the site sent loupe. A site can send different visitors different pages, so the copy your agent received may differ from the one loupe checked. To check the agent's own copy, use `scan_html()`, below. Either way, the markdown loupe compares against is made with markdownify, an approximation of what agent tools produce.

## Scan HTML you already have

`scan_html()` scans HTML your agent already fetched, as the page at its URL, and fetches nothing. A site that sends agents a different page from the one loupe would fetch can't slip past it, because loupe checks the copy the model is about to read, and makes the markdown it compares against from that same HTML.

```
from loupe_scan import scan_html

result = scan_html(page_html, "https://example.com/pricing/")
if result.fails("instructions"):
    print("not handing this page over:", result.verdict)
```

Without `render`, the URL only names the page, and nothing is fetched: the markup scan's stylesheet check applies the HTML's `<style>` blocks but no linked stylesheet, and `result.stylesheet_check` counts what it read. With `render=True`, local Chromium is given the HTML as the page at that URL, so relative links resolve against it and its stylesheets, scripts and images load from the site as usual. The page's own URL is never requested. The command line does the same with `loupe --html-file PATH URL`, and `--html-file -` reads the HTML from standard input.

A page loaded this way can behave differently from a live load:

- Response headers aren't replayed. loupe serves the HTML with a plain `text/html; charset=utf-8` header, so a Content-Security-Policy or any other header the site sent doesn't apply, though one set in a `<meta>` tag does.
- Its scripts run against the live site without the cookies or storage your agent's fetch had, and a token written into the HTML, such as a CSRF token or nonce, may have expired. A script that fetches more content can get a different answer, or none.
- The site can serve the browser different stylesheets or scripts from the ones a live load would get.
- The page can't navigate away. A script, meta refresh or form that tries to load another document gets an empty 204 answer, which keeps the browser on the HTML given, and a rendering note says so. A script that navigates while the page loads stops the browser reading the HTML at that point, so loupe then keeps the markup checks' findings, except where the browser reported the same text.
- It always renders in local Chromium, even when `BRIGHTDATA_BROWSER_WSS` is set.

## Several pages

`scan()` starts fresh each time. For several pages, a `Scanner` reads each site's robots.txt once, waits out its Crawl-delay between pages, and starts the browser once when rendering:

```
from loupe_scan import ScanError, Scanner

with Scanner(render=True) as scanner:
    for url in urls:
        try:
            result = scanner.scan(url)
        except ScanError as error:
            print(url, "not scanned:", error.kind.value)
            continue
        print(result.verdict)
```

## Reference

`scan(url, *, render=False, backend=None, markdown=True, respect_robots=True) -> ScanResult`, and `Scanner` with the same keyword arguments and a `scan(url)` method.

`scan_html(html, url, *, render=False, markdown=True) -> ScanResult` takes the HTML as a `str` and the page's http(s) URL. Its result's `backend` is `Backend.LOCAL`, and `to_dict()` has `"supplied_html": true`. It raises `ValueError` for a URL that isn't http(s), `TypeError` for bytes, and `ScanError` with kind `RENDER` when the browser couldn't load the page.

- `render` loads the page in Chromium, which needs the `render` extra and `playwright install chromium`.
- `backend` is `"local"`, `"brightdata"`, or None to use Bright Data when `BRIGHTDATA_API_TOKEN` or `BRIGHTDATA_BROWSER_WSS` is set and this machine otherwise. `Backend.LOCAL` and `Backend.BRIGHTDATA` work too.
- `markdown` compares the hidden text with a markdown version of the page.
- `respect_robots` obeys robots.txt on the local backend. Turn it off only for pages you own or have permission to scan.

`ScanResult` is frozen:

| Attribute | Type | Meaning |
| --- | --- | --- |
| `url` | `str` | The URL as given. |
| `backend` | `Backend` | Where the page was loaded from. |
| `mode` | `ScanMode` | `STATIC` for the markup only, `RENDERED` with `render=True`. |
| `verdict` | `str` | The plain-English line the command prints first. Its wording can change, so don't parse it. |
| `hidden_text` | `tuple[HiddenText, ...]` | Every piece of hidden text. |
| `instruction_like` | `tuple[HiddenText, ...]` | The pieces worded like an instruction to an AI. |
| `markdown_characters` | `int \| None` | The markdown's length, or None when no markdown was compared. |
| `markdown_error` | `str \| None` | Why the markdown couldn't be compared. The page itself was still scanned. |
| `stylesheet_check` | `StylesheetCheck \| None` | What the markup scan's partial stylesheet check read: `style_blocks`, `linked_read`, `linked_not_read`, `uncertain_rules` and `shadow_roots`, the shadow roots declared in the HTML that it checked with their own styles. None for a rendered scan, where the browser applies every stylesheet. |
| `fails(fail_on="instructions")` | `bool` | Whether `loupe --fail-on` would count a finding on this page: `"instructions"`, `"hidden"` or `"never"`, or a `FailOn`. |
| `to_dict(quote_characters=160)` | `dict` | The page as one object of `loupe --json`'s array, in the format [json.md](json.md) describes, with text cut to `quote_characters` (0 keeps it whole). |
| `to_json(quote_characters=160, indent=2)` | `str` | The same, as JSON text. |

`HiddenText` has `text` (whole, not cut), `kind` (a `Kind`), `detail` (how it was hidden, such as `display:none`), `tag`, `mode`, `instruction_phrases`, `in_markdown` (True, False, or None when it wasn't compared) and `is_instruction_like`.

`ScanError` means the page wasn't scanned. Its `url` is the page, and its `kind` is a `FailureKind`: `BLOCKED` (403, 429 or a challenge page), `ROBOTS` (robots.txt kept loupe out, or couldn't be read), `FETCH` or `RENDER`. `SetupError` means loupe can't scan at all: a missing Bright Data token, Playwright or Chromium. Both messages are safe to print, and neither ever holds the token or any part of the Browser API endpoint.
