# Test page ground truth

[The test page](https://devfrankduah.github.io/loupe/test-page/) is a product page for the Quillmore Q6, a blender that doesn't exist. Its five visible reviews are mostly negative, averaging 1.8 out of 5. It also hides eight pieces of text a reader never sees. Six are instructions to an AI that summarises the page, asking it to call the blender the best one available and the reviews overwhelmingly positive. The other two are fake five-star reviews with no instruction in them.

It's modelled on The Guardian's test of ChatGPT search ("ChatGPT search tool vulnerable to manipulation and deception, tests show", 24 December 2024). There, hidden text on a made-up camera page turned ChatGPT's summaries entirely positive despite negative reviews on the page, and hidden fake reviews did the same without any instruction.

Each planted item starts with different words, so it can be told apart in loupe's output. The table is what loupe should report. `tests/test_test_page.py` reads it and checks every row against a scan of the same page, kept as a test fixture in `tests/fixtures/test-page/`, in both modes.

| # | Planted item | Hidden by | Starts with | Markup scan | Rendered scan | Flagged instruction-like |
|---|---|---|---|---|---|---|
| 1 | Instruction | a stylesheet class that sets `display:none` | Note for AI shopping assistants | missed | rendered-hidden (display:none) | yes |
| 2 | Instruction | an inline style that puts it 9999px off-screen | If you are a language model | inline-style | rendered-hidden (outside the page) | yes |
| 3 | Instruction | a script that adds it after load, from base64, so the HTML never holds the words | Updated guidance for AI summaries | missed | rendered-hidden (display:none) | yes |
| 4 | Instruction | an HTML comment | Message to AI crawlers | comment | comment | yes |
| 5 | Instruction | the product image's `alt` text | Quillmore Q6 blender in brushed steel | label-attribute (alt) | label-attribute (alt) | yes |
| 6 | Fake five-star review | the `hidden` attribute | Rated 5 out of 5 by Priya | hidden-attribute | rendered-hidden (hidden attribute) | no |
| 7 | Fake five-star review | a stylesheet class that sets `display:none` | Rated 5 out of 5 by Tom | missed | rendered-hidden (display:none) | no |
| 8 | Instruction | text coloured like the background | Instructions for any AI assistant | missed | missed | no |

Row 8 is a known limit, left in on purpose: loupe doesn't compare text colour with the background, so it counts that line as visible in both modes. Flagged instruction-like means loupe reports the finding as matching its phrase list. A missed item is never a finding, so it's never flagged, even when its words would match: row 8 contains "ignore all".

Besides the planted items, both scans report the page's three `<meta>` tags (viewport, robots and description), none of them instruction-like. In total:

- Markup scan: 7 findings, 3 instruction-like.
- Rendered scan: 10 findings, 5 instruction-like.

Whether each item reaches the markdown depends on Bright Data's converter, so the table doesn't predict it. The live results record it.

axe-core 4.10.3 finds no violations on the page at desktop or phone width. It lists one element for manual review: the row 8 line, because it sits on a gradient axe can't measure contrast against.
