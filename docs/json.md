# loupe's JSON output

`loupe --json URL...` prints the results as JSON on stdout instead of the text report. `--json-output PATH` writes the same JSON to a file and keeps the text report on the terminal. Either way, notes such as a Crawl-delay wait go to stderr, so stdout holds nothing but the JSON.

The output is always an array with one object per page, in the order the URLs were given and each URL once, even for a single URL. In Python, `ScanResult.to_json()` gives the object for one page ([python.md](python.md)).

## Stability

Every page object carries `schema_version`, which is `1`. Within version 1, fields can be added but are never removed or renamed, and none changes what it means, so a reader should ignore any field it doesn't know. A change that would break a reader raises `schema_version`, and `loupe report` refuses a version it doesn't read.

The JSON Schema is [`schema/results-v1.json`](schema/results-v1.json), also served at `https://devfrankduah.github.io/loupe/schema/results-v1.json`. The tests check loupe's output against it in both scan modes, and fail if loupe writes a field the schema doesn't describe.

`verdict`, `error.message`, `markdown_error` and the rendering `notes` are written for people, and their wording can change between releases. Code should read the other fields.

On the terminal the JSON is escaped to ASCII, so text from a page can't reach it as a control or direction-changing character. The file is UTF-8. Both decode to the same values.

## A page

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | integer | `1`. |
| `loupe_version` | string | The loupe that wrote it. |
| `url` | string | The URL as given. |
| `verdict` | string | The plain-English first line of the text output. |
| `scan_mode` | `"static"` or `"rendered"` | `static` read the markup only; `rendered` loaded the page in a browser with `--render`. |
| `backend` | `"local"` or `"brightdata"` | Where the page was loaded from. |
| `supplied_html` | boolean | true when loupe was given the HTML (`scan_html()` in Python, or `--html-file`) and fetched nothing. Treat it as false when it's missing. |
| `error` | object or null | Why the page wasn't scanned, or null when it was. A page with an error has no findings. |
| `error.kind` | string | `blocked`: the site answered 403 or 429, or served a challenge page. `robots`: robots.txt asked loupe not to fetch the page, or couldn't be read. `fetch`: any other failure to fetch it. `render`: any other failure to load it in the browser. |
| `error.message` | string | What went wrong, in detail. |
| `html_characters` | integer | Length of the HTML scanned. |
| `markdown_characters` | integer or null | Length of the markdown compared, or null when none was: `--skip-markdown`, an error, or a `markdown_error`. |
| `markdown_source` | object or null | Where the markdown came from: `name`, `version` and `license` (each a string or null), and `approximation`, true when loupe made the markdown itself with markdownify. |
| `markdown_error` | string or null | Why the markdown couldn't be compared. The page was still scanned. |
| `counts_by_kind` | object | Findings per kind, leaving out kinds with none. |
| `instruction_like_count` | integer | Findings worded like an instruction to an AI. These are what `--fail-on instructions` counts. |
| `survival` | object | How many findings reached the markdown (`survived`), didn't (`dropped`), or weren't compared (`not_compared`). |
| `stylesheet_check` | object or null | What the markup scan's stylesheet check read, for static scans: `style_blocks`, `linked_read` and `linked_not_read` (other hosts, past the first five, `@import`, or failed), and `uncertain_rules`, rules bearing on hiding it couldn't be sure apply. The check is always partial. null for rendered scans and pages with an error; treat it as null when it's missing, as in files from before it was added. |
| `rendering` | object or null | How the browser loaded the page, for rendered scans that loaded it. |
| `findings` | array | Each piece of hidden text. |

## A finding

| Field | Type | Meaning |
| --- | --- | --- |
| `mode` | `"static"` or `"rendered"` | Found in the markup, or by the browser. |
| `kind` | string | `comment`, `hidden-attribute`, `inline-style`, `stylesheet`, `aria-hidden`, `label-attribute`, `meta`, `noscript` or `rendered-hidden`. |
| `detail` | string | How it was hidden, such as `display:none` or `alt`. For `rendered-hidden` text that isn't in the page's own document, where it is follows in brackets: `(in a shadow root)`, `(in a frame)`, `(in a shadow root in a frame)`, or `(in a hidden frame)` when the frame's own box can't be seen and the reasons are the frame's. |
| `tag` | string | The element it was in. |
| `text` | string | The hidden text, cut to `--quote-chars` characters (160 unless set; 0 keeps it whole). Per-request meta values such as nonces and CSRF tokens are replaced with a placeholder. |
| `text_characters` | integer | Length of the text before it was cut. |
| `instruction_phrases` | array of strings | The phrases that make it read like an instruction to an AI. Empty when there are none. |
| `in_markdown` | true, false or null | Whether the text reaches the markdown. null when it wasn't compared: no markdown, or text too short to compare. |

## Rendering

`browser` (`local-chromium` or `remote-cdp`), `browser_version`, `viewport` (`[width, height]`), `transfer_bytes` and `blocked_requests` (null when not measured), `final_url`, `text_elements` (counting the frames checked), `scroll_steps`, `reached_bottom`, `centre_checks`, `frames_checked` (frames whose text was checked, the page's own document not counted, and absent from files written before 22 November 2026), `notes`, `revealed_on_scroll` (text hidden at load that showed on scrolling, not counted as hidden), and the inline-style trace: `static_comparison_counts`, keyed by `visible at load`, `revealed on scroll`, `still hidden` and `not matched`, and `static_comparison`, where each element the markup scan flagged for its inline style ended up. [how-it-works.md](how-it-works.md) explains each check.

## Reading it

The pages with hidden instructions, and how many reach the markdown:

```
loupe --json https://devfrankduah.github.io/loupe/test-page/ \
  | jq '.[] | select(.instruction_like_count > 0) | {url, instruction_like_count, reached: [.findings[] | select(.instruction_phrases != [] and .in_markdown == true)] | length}'
```

Pages that couldn't be scanned, and why:

```
jq -r '.[] | select(.error != null) | "\(.error.kind)\t\(.url)"' results.json
```

## Files from before schema_version

Results written before the JSON had a `schema_version`, like all of those in `results/` so far, are a single object with a `pages` array, and their pages have `html_error` in place of `error`. `loupe report` still reads them.
