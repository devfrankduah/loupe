from __future__ import annotations

import datetime
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from loupe import cli
from loupe.render import Landing
from loupe.scan import ScanMode
from loupe.site import (
    HIDING_METHODS,
    ReportFormatError,
    choose_example,
    group_by_method,
    load_results,
    render_report_page,
    run_date_from_filename,
    shorten_middle,
)

HOSTILE_TEXT = '<script>alert("x")</script><img src=x onerror="alert(1)">'


def _finding(kind: str, detail: str, text: str, in_markdown: bool | None, phrases: list[str] | None = None) -> dict[str, object]:
    return {
        "kind": kind,
        "detail": detail,
        "tag": "div",
        "text": text,
        "text_characters": len(text),
        "instruction_phrases": phrases or [],
        "in_markdown": in_markdown,
    }


def _page(url: str, findings: list[dict[str, object]], **extra: object) -> dict[str, object]:
    page: dict[str, object] = {"url": url, "html_error": None, "markdown_error": None, "findings": findings}
    page.update(extra)
    return page


def _results(*pages: dict[str, object]) -> str:
    return json.dumps({"loupe_version": "0.1.0", "pages": list(pages)})


class _ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attribute_names: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        for name, value in attrs:
            self.attribute_names.append(name)
            if name == "href" and value is not None:
                self.hrefs.append(value)


def _parse(page_html: str) -> _ElementCollector:
    collector = _ElementCollector()
    collector.feed(page_html)
    return collector


def test_text_from_fetched_pages_is_inert_in_the_output() -> None:
    hostile_page = _page(
        'https://evil.test/"><script>alert(2)</script>',
        [
            _finding("comment", HOSTILE_TEXT, HOSTILE_TEXT, False, ["you are"]),
            _finding("inline-style", "display:none", HOSTILE_TEXT, True, ["assistant"]),
        ],
        markdown_error=HOSTILE_TEXT,
    )
    page_html = render_report_page(load_results(_results(hostile_page)))
    parsed = _parse(page_html)

    assert "script" not in parsed.tags
    assert "img" not in parsed.tags
    assert not [name for name in parsed.attribute_names if name.startswith("on")]
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page_html


def test_only_http_urls_become_links() -> None:
    page_html = render_report_page(load_results(_results(_page("javascript:alert(1)", []))))
    assert not [href for href in _parse(page_html).hrefs if href.startswith("javascript:")]


def test_page_makes_no_external_requests() -> None:
    page_html = render_report_page(load_results(_results(_page("https://a.test/", []))))
    parsed = _parse(page_html)
    assert not {"script", "img", "link", "iframe"} & set(parsed.tags)
    assert "src" not in parsed.attribute_names
    assert "default-src 'none'" in page_html


def test_headline_counts_hidden_elements_and_comments() -> None:
    long_text = "a hidden sentence long enough to compare"
    results = _results(
        _page(
            "https://a.test/",
            [
                _finding("hidden-attribute", "hidden", long_text, True),
                _finding("inline-style", "display:none, opacity:0", long_text, True),
                _finding("inline-style", "opacity:0", long_text, True),
                _finding("inline-style", "display:none", long_text, False),
                _finding("comment", "<!-- -->", long_text, False),
                _finding("comment", "<!-- -->", "short", None),
            ],
        )
    )
    page_html = render_report_page(load_results(results))
    assert "Text marked hidden mostly reached the AI: 2 of 3. HTML comments never did: 0 of 1." in page_html


def test_example_domains_are_marked_as_the_control() -> None:
    results = _results(_page("https://example.com", []), _page("https://a.test/", []))
    assert render_report_page(load_results(results)).count('<li class="card control">') == 1


def test_retry_and_fetch_errors_are_explained_on_the_card() -> None:
    results = _results(
        _page("https://a.test/", [], markdown_retry={"first_error": "x", "retry_error": None,
                                                     "not_compared_because_text_was_cut": 34}),
        _page("https://b.test/", [], html_error="HTTP 500"),
    )
    page_html = render_report_page(load_results(results))
    assert "worked on a single retry. 34 longer findings" in page_html
    assert "The HTML could not be fetched: HTTP 500" in page_html


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        "[]",
        '{"pages": []}',
        '{"pages": [{"url": "https://a.test/"}]}',
        _results(_page("https://a.test/", [_finding("made-up", "x", "y", None)])),
        _results(_page("https://a.test/", [_finding("comment", "x", "y", None)], markdown_retry="yes")),
        _results(_page("https://a.test/", [], markdown_retry={"not_compared_because_text_was_cut": -1})),
        _results(_page("https://a.test/", [], markdown_retry={"not_compared_because_text_was_cut": "3"})),
    ],
)
def test_malformed_results_are_rejected(document: str) -> None:
    with pytest.raises(ReportFormatError):
        load_results(document)


def test_run_date_comes_from_an_iso_filename() -> None:
    assert run_date_from_filename(Path("results/2026-11-02.json")) == datetime.date(2026, 11, 2)
    assert run_date_from_filename(Path("results/2026-11-07-rendered.json")) == datetime.date(2026, 11, 7)
    assert run_date_from_filename(Path("results/2026-11-071.json")) is None
    assert run_date_from_filename(Path("results/latest.json")) is None


def test_report_command_writes_the_page(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_path = tmp_path / "2026-11-02.json"
    results_path.write_text(_results(_page("https://example.com", [])), encoding="utf-8")
    output_path = tmp_path / "docs" / "index.html"

    assert cli.main(["report", str(results_path), "-o", str(output_path)]) == cli.EXIT_OK
    assert "measured on 2 November 2026 across one page" in output_path.read_text(encoding="utf-8")


def test_report_command_rejects_a_bad_results_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    results_path = tmp_path / "bad.json"
    results_path.write_text("{}", encoding="utf-8")
    assert cli.main(["report", str(results_path), "-o", str(tmp_path / "out.html")]) == cli.EXIT_USAGE
    assert "missing 'pages'" in capsys.readouterr().err
    assert not (tmp_path / "out.html").exists()


def _example_html(page_html: str) -> str:
    start = page_html.index('<section class="example"')
    return page_html[start : page_html.index("</section>", start)]


def test_example_panel_shows_hostile_text_inert() -> None:
    results = _results(_page("https://evil.test/", [_finding("hidden-attribute", "hidden", HOSTILE_TEXT, True)]))
    panel = _example_html(render_report_page(load_results(results)))
    parsed = _parse(panel)

    assert "What a person sees" in panel and "What an AI reads" in panel
    assert "&lt;script&gt;" in panel
    assert not {"script", "img"} & set(parsed.tags)
    assert not [name for name in parsed.attribute_names if name.startswith("on")]


def test_example_prefers_a_complete_quote_over_a_longer_cut_one() -> None:
    cut = _finding("inline-style", "display:none", "x" * 80, True)
    cut["text_characters"] = 500
    complete = _finding("hidden-attribute", "hidden", "You can't perform that action at this time.", True)
    dropped = _finding("hidden-attribute", "hidden", "a complete sentence that never reached the markdown", False)
    faded = _finding("inline-style", "opacity:0", "a faded sentence that did reach the markdown", True)
    pages = load_results(_results(_page("https://a.test/", [cut, dropped, faded]), _page("https://b.test/", [complete])))

    chosen = choose_example(pages)
    assert chosen is not None
    page, finding = chosen
    assert (page.host, finding.text) == ("b.test", "You can't perform that action at this time.")


def test_without_a_surviving_hidden_element_the_panel_says_so() -> None:
    results = _results(_page("https://a.test/", [_finding("comment", "<!-- -->", "a comment long enough", False)]))
    assert "No text hidden with the hidden attribute or display:none reached the markdown" in render_report_page(
        load_results(results)
    )


def test_every_finding_is_counted_under_exactly_one_hiding_method() -> None:
    text = "long enough to be compared"
    findings = [
        _finding("hidden-attribute", "hidden", text, True),
        _finding("inline-style", "display:none, opacity:0", text, True),
        _finding("inline-style", "opacity:0", text, False),
        _finding("inline-style", "font-size:0", text, False),
        _finding("aria-hidden", 'aria-hidden="true"', text, None),
        _finding("label-attribute", "alt", text, True),
        _finding("meta", "name=description", text, False),
        _finding("comment", "<!-- -->", text, False),
        _finding("noscript", "<noscript>", text, True),
    ]
    page = load_results(_results(_page("https://a.test/", findings)))[0]
    groups = group_by_method(list(page.findings))

    assert sum(len(members) for _, members in groups) == len(findings)
    assert [len(members) for _, members in groups] == [2, 1, 1, 1, 1, 1, 1, 1]
    assert len(groups) == len(HIDING_METHODS)


def test_theme_toggle_works_without_javascript() -> None:
    page_html = render_report_page(load_results(_results(_page("https://a.test/", []))))
    parsed = _parse(page_html)
    assert '<input type="checkbox" id="theme"' in page_html
    assert '<label for="theme"' in page_html
    assert ":root:has(#theme:checked)" in page_html
    assert ".theme-toggle .when-dark { display: none; }" in page_html
    assert "script" not in parsed.tags


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("/wiki/Prompt_injection", 24, "/wiki/Prompt_injection"),
        ("/llmrisk/llm01-prompt-injection", 24, "/llmrisk/ll…pt-injection"),
        ("abcdefghij", 5, "ab…ij"),
    ],
)
def test_shorten_middle(text: str, limit: int, expected: str) -> None:
    result = shorten_middle(text, limit)
    assert result == expected
    assert len(result) <= limit


def test_shorten_middle_rejects_a_limit_too_small_for_both_ends() -> None:
    with pytest.raises(ValueError):
        shorten_middle("abcdef", 2)


def test_card_heading_shortens_the_path_keeps_the_full_url_and_stays_escaped() -> None:
    url = "https://genai.owasp.org/llmrisk/llm01-prompt-injection/<script>alert(1)</script>"
    page_html = render_report_page(load_results(_results(_page(url, []))))
    card = page_html[page_html.index('<li class="card">') :]

    assert '<span class="card-host">genai.owasp.org</span>' in card
    assert "…" in card[: card.index("</h3>")]
    assert 'title="https://genai.owasp.org/llmrisk/llm01-prompt-injection/&lt;script&gt;' in card
    assert "script" not in _parse(page_html).tags


def _rendering(**changes: object) -> dict[str, object]:
    rendering: dict[str, object] = {
        "browser": "local-chromium",
        "scroll_steps": 4,
        "reached_bottom": True,
        "notes": [],
        "revealed_on_scroll": [{"tag": "p", "text": "Bake for 45 minutes.", "text_characters": 20}],
        "static_comparison": [
            {"tag": "p", "detail": "opacity:0", "text": "Bake for 45 minutes.", "landed": "revealed on scroll"},
            {"tag": "span", "detail": "opacity:0", "text": "Lemon drizzle cake", "landed": "visible at load"},
            {"tag": "p", "detail": "display:none", "text": "An older method.", "landed": "still hidden"},
        ],
    }
    rendering.update(changes)
    return rendering


def _rendered_finding(text: str, detail: str = "display:none") -> dict[str, object]:
    return {**_finding("rendered-hidden", detail, text, None), "mode": "rendered"}


def _rendered_page(url: str, findings: list[dict[str, object]], **extra: object) -> dict[str, object]:
    extra.setdefault("rendering", _rendering())
    return _page(url, findings, scan_mode="rendered", **extra)


MARKUP_PAGE = _page(
    "https://cake.test/recipe",
    [
        _finding("inline-style", "opacity:0", "Bake for 45 minutes.", None),
        _finding("hidden-attribute", "hidden", "An older method.", None, ["you are"]),
        _finding("comment", "<!-- -->", "a note in the source", None),
    ],
)
RENDERED_PAGE = _rendered_page(
    "https://cake.test/recipe/",
    [_rendered_finding("An older method."), {**_finding("comment", "<!-- -->", "a note in the source", None), "mode": "static"}],
)


@pytest.mark.parametrize(
    "document",
    [
        _results(_page("https://a.test/", [{**_finding("comment", "<!-- -->", "a note", None), "mode": "rendered"}])),
        _results(_page("https://a.test/", [_finding("rendered-hidden", "opacity:0", "faded text", None)])),
        _results(_page("https://a.test/", [_rendered_finding("faded text")])),
        _results(_page("https://a.test/", [], scan_mode="rendered")),
        _results(_rendered_page("https://a.test/", [], rendering=_rendering(reached_bottom="yes"))),
        _results(
            _rendered_page(
                "https://a.test/",
                [],
                rendering=_rendering(static_comparison=[{"tag": "p", "detail": "x", "text": "y", "landed": "gone"}]),
            )
        ),
    ],
)
def test_inconsistent_rendered_results_are_rejected(document: str) -> None:
    with pytest.raises(ReportFormatError):
        load_results(document)


def test_rendered_results_load_with_their_rendering() -> None:
    (page,) = load_results(_results(RENDERED_PAGE))
    assert page.mode is ScanMode.RENDERED
    assert page.rendering is not None
    assert (page.rendering.scroll_steps, page.rendering.revealed_on_scroll) == (4, 1)
    assert [item.landed for item in page.rendering.comparison] == [
        Landing.REVEALED_ON_SCROLL,
        Landing.VISIBLE_AT_LOAD,
        Landing.STILL_HIDDEN,
    ]
    assert [finding.mode for finding in page.findings] == [ScanMode.RENDERED, ScanMode.STATIC]


def test_a_failed_render_needs_no_rendering_details() -> None:
    (page,) = load_results(_results(_page("https://a.test/", [], scan_mode="rendered", html_error="HTTP 500")))
    assert page.rendering is None


def _rendered_section(page_html: str) -> str:
    start = page_html.index('<section aria-labelledby="rendered-title">')
    return page_html[start : page_html.index("</section>", start)]


def test_markup_only_and_rendered_runs_appear_side_by_side() -> None:
    page_html = render_report_page(load_results(_results(MARKUP_PAGE, RENDERED_PAGE)))
    section = _rendered_section(page_html)

    assert section.count('<li class="pair">') == 1
    assert "<dt>Hidden by a style or the hidden attribute</dt><dd>2</dd>" in section
    assert "<dt>Never shown</dt><dd>1</dd>" in section
    assert "<dt>Shown only after scrolling</dt><dd>1</dd>" in section
    assert "Where the 3 inline-style guesses landed" in section
    for count, label in ((1, "showed as the page loaded"), (1, "showed after scrolling"), (1, "never showed")):
        assert f"<b>{count}</b> {label}" in section
    assert "<b>0</b> not found in the live page" in section
    assert section.count('<q class="page-text">') == 3
    # The trailing slash doesn't make the recipe two pages.
    assert "measured across one page" in page_html
    assert "the rendered scan above traces each of them" in page_html


def test_rendered_results_alone_render_without_the_markup_sections() -> None:
    page_html = render_report_page(load_results(_results(RENDERED_PAGE)))
    assert '<section class="example"' not in page_html
    assert 'id="result-title"' not in page_html
    assert "No markup-only scan of this page here." in _rendered_section(page_html)
    assert "<dt>Rendered scan</dt>" in page_html


def test_markup_only_results_carry_no_rendered_section_or_styles() -> None:
    page_html = render_report_page(load_results(_results(MARKUP_PAGE)))
    assert 'id="rendered-title"' not in page_html
    assert "--later" not in page_html
    assert "<dt>Rendered scan</dt>" not in page_html


def test_text_in_rendered_results_is_inert() -> None:
    hostile = _rendered_page(
        'https://evil.test/"><script>alert(3)</script>',
        [_rendered_finding(HOSTILE_TEXT, detail=HOSTILE_TEXT)],
        rendering=_rendering(
            notes=[HOSTILE_TEXT],
            static_comparison=[{"tag": HOSTILE_TEXT, "detail": HOSTILE_TEXT, "text": HOSTILE_TEXT, "landed": "still hidden"}],
        ),
    )
    failed = _page("https://broken.test/", [], scan_mode="rendered", html_error=HOSTILE_TEXT)
    page_html = render_report_page(load_results(_results(hostile, failed)))
    parsed = _parse(page_html)

    assert "script" not in parsed.tags and "img" not in parsed.tags
    assert not [name for name in parsed.attribute_names if name.startswith("on")]
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in _rendered_section(page_html)


def test_report_command_reads_several_results_files(tmp_path: Path) -> None:
    static_path = tmp_path / "2026-11-07.json"
    rendered_path = tmp_path / "2026-11-07-rendered.json"
    static_path.write_text(_results(MARKUP_PAGE), encoding="utf-8")
    rendered_path.write_text(_results(RENDERED_PAGE), encoding="utf-8")
    output_path = tmp_path / "page.html"

    assert cli.main(["report", str(static_path), str(rendered_path), "-o", str(output_path)]) == cli.EXIT_OK
    page_html = output_path.read_text(encoding="utf-8")
    assert 'id="rendered-title"' in page_html and 'id="result-title"' in page_html
    assert "measured on 7 November 2026 across one page" in page_html


@pytest.mark.parametrize(
    "document",
    [
        _results(_page("https://a.test/", [], scan_mode="sideways")),
        _results(_page("https://a.test/", [{**_finding("comment", "<!-- -->", "a note", None), "mode": 3}])),
    ],
)
def test_an_unknown_scan_mode_is_rejected(document: str) -> None:
    with pytest.raises(ReportFormatError, match="static"):
        load_results(document)


def test_static_results_load_with_or_without_a_mode() -> None:
    tagged = {**_finding("comment", "<!-- -->", "a note in the source", False), "mode": "static"}
    untagged = _finding("comment", "<!-- -->", "an older note", False)
    pages = load_results(_results(_page("https://a.test/", [tagged, untagged], scan_mode="static")))
    assert [finding.text for finding in pages[0].findings] == ["a note in the source", "an older note"]
