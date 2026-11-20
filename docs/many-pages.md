# Scan many pages

Give loupe several URLs, or a file of them:

```
loupe https://devfrankduah.github.io/loupe/test-page/ https://devfrankduah.github.io/loupe/animated-page/
loupe --urls-file urls.txt
some-crawler | loupe --urls-file -
```

A URLs file has one URL per line. Blank lines and lines starting with `#` are skipped, and `-` reads the list from standard input. URLs given on the command line come first, then the file's. Each URL is scanned once, in the order given; repeats are left out, with a note on stderr saying how many.

After the last page, loupe prints a summary table:

```
Summary of 2 pages:
  result        hidden  instructions  instructions in markdown  url
  instructions       7             3                         2  https://devfrankduah.github.io/loupe/test-page/
  hidden text        8             0                         0  https://devfrankduah.github.io/loupe/animated-page/
loupe: exit 1: 1 page with hidden text worded like instructions to an AI (--fail-on instructions)
```

`result` is `instructions` when some hidden text is worded like an instruction to an AI, `hidden text` when there's hidden text but none of it is, and `clean` when there's none. A page that wasn't scanned shows why instead: `blocked`, `robots.txt`, `not fetched` or `not loaded`. `hidden` counts every piece of hidden text, `instructions` the pieces worded like instructions, and the next column how many of those reach the markdown (`-` when no markdown was compared). The last line is on stderr, and appears only when the exit code isn't 0.

With `--json`, loupe prints a JSON array with one object per page in the same order, and no table. [json.md](json.md) describes it. To keep the text output and save the JSON too, use `--json-output results.json`.

## Exit codes

One exit code covers the whole run. It's 2 if any page couldn't be scanned in full, 1 if every page was scanned and at least one had a finding `--fail-on` counts, and 0 otherwise. The README's [exit codes](https://github.com/devfrankduah/loupe#exit-codes) section has the details.

## How loupe treats each site

On the local backend loupe scans one page at a time, so it never has two requests open to a site at once. It reads each site's robots.txt once per run and obeys it. Between pages on the same site it waits out any Crawl-delay, up to 30 seconds. A site that asks for a longer delay gets its first page scanned, and every later page from it is reported as stopped by robots.txt, so scan the rest in later runs. `--ignore-robots` skips robots.txt and Crawl-delay, but not the one page at a time, and is only for pages you own or have permission to scan.

With `--render`, robots.txt and Crawl-delay are checked the same way before the browser loads each page. The browser then loads the page's images, styles and scripts the way any browser does, so one rendered page can mean several requests to the site at once.

On the Bright Data backend the requests go out through Bright Data's network. loupe doesn't read robots.txt there, and still scans one page at a time.

## From Python

A `Scanner` does the same per-site bookkeeping across pages: one robots.txt per site, Crawl-delay waits, and one browser when rendering. See [python.md](python.md#several-pages).
