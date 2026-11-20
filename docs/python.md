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

loupe fetches the page itself, so it checks what the site sent loupe. A site can send different visitors different pages, so the copy your agent received may differ from the one loupe checked. The markdown loupe compares against is made with markdownify, an approximation of what agent tools produce.

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
| `fails(fail_on="instructions")` | `bool` | Whether `loupe --fail-on` would count a finding on this page: `"instructions"`, `"hidden"` or `"never"`, or a `FailOn`. |
| `to_dict(quote_characters=160)` | `dict` | The page as one object of `loupe --json`'s array, in the format [json.md](json.md) describes, with text cut to `quote_characters` (0 keeps it whole). |
| `to_json(quote_characters=160, indent=2)` | `str` | The same, as JSON text. |

`HiddenText` has `text` (whole, not cut), `kind` (a `Kind`), `detail` (how it was hidden, such as `display:none`), `tag`, `mode`, `instruction_phrases`, `in_markdown` (True, False, or None when it wasn't compared) and `is_instruction_like`.

`ScanError` means the page wasn't scanned. Its `url` is the page, and its `kind` is a `FailureKind`: `BLOCKED` (403, 429 or a challenge page), `ROBOTS` (robots.txt kept loupe out, or couldn't be read), `FETCH` or `RENDER`. `SetupError` means loupe can't scan at all: a missing Bright Data token, Playwright or Chromium. Both messages are safe to print, and neither ever holds the token or any part of the Browser API endpoint.
