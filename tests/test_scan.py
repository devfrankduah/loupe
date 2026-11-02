from __future__ import annotations

import pytest

from loupe.scan import Finding, Kind, inline_style_hiding_reasons, parse_inline_style, scan_html


def _finding_with_marker(findings: list[Finding], marker: str) -> Finding:
    matches = [finding for finding in findings if marker in finding.text]
    assert len(matches) == 1, f"expected one finding containing {marker}, got {matches}"
    return matches[0]


@pytest.mark.parametrize(
    ("marker", "kind", "detail"),
    [
        ("PLANTED-COMMENT", Kind.COMMENT, "<!-- -->"),
        ("PLANTED-HIDDEN-ATTRIBUTE", Kind.HIDDEN_ATTRIBUTE, "hidden"),
        ("PLANTED-ARIA-HIDDEN", Kind.ARIA_HIDDEN, 'aria-hidden="true"'),
        ("PLANTED-DISPLAY-NONE", Kind.INLINE_STYLE, "display:none"),
        ("PLANTED-VISIBILITY-HIDDEN", Kind.INLINE_STYLE, "visibility:hidden"),
        ("PLANTED-FONT-SIZE-ZERO", Kind.INLINE_STYLE, "font-size:0"),
        ("PLANTED-OPACITY-ZERO", Kind.INLINE_STYLE, "opacity:0"),
        ("PLANTED-OFF-SCREEN", Kind.INLINE_STYLE, "off-screen left:-9999px"),
        ("PLANTED-ALT", Kind.LABEL_ATTRIBUTE, "alt"),
        ("PLANTED-TITLE", Kind.LABEL_ATTRIBUTE, "title"),
        ("PLANTED-ARIA-LABEL", Kind.LABEL_ATTRIBUTE, "aria-label"),
        ("PLANTED-META", Kind.META, "name=description"),
        ("PLANTED-NOSCRIPT", Kind.NOSCRIPT, "<noscript>"),
    ],
)
def test_each_hiding_method_is_found_and_marked_instruction_like(
    planted_html: str, marker: str, kind: Kind, detail: str
) -> None:
    finding = _finding_with_marker(scan_html(planted_html), marker)
    assert finding.kind is kind
    assert finding.detail == detail
    assert finding.is_instruction_like


def test_visible_text_is_not_reported_even_when_it_reads_like_an_instruction(planted_html: str) -> None:
    findings = scan_html(planted_html)
    assert not any("three lemons" in finding.text for finding in findings)


def test_hidden_text_without_instruction_phrases_is_reported_but_not_flagged(planted_html: str) -> None:
    finding = _finding_with_marker(scan_html(planted_html), "Home Recipes Contact")
    assert finding.kind is Kind.INLINE_STYLE
    assert not finding.is_instruction_like


def test_meta_tags_without_a_name_are_ignored(planted_html: str) -> None:
    meta_details = [finding.detail for finding in scan_html(planted_html) if finding.kind is Kind.META]
    assert meta_details == ["name=viewport", "name=description"]


def test_meta_content_without_a_key_attribute_is_ignored() -> None:
    html = '<meta content="orphan value with no key"><meta property="og:title" content="Shared title">'
    assert [(finding.detail, finding.text) for finding in scan_html(html)] == [("property=og:title", "Shared title")]


def test_page_with_nothing_hidden_has_no_findings(visible_html: str) -> None:
    assert scan_html(visible_html) == []


def test_nested_hidden_elements_are_reported_once_on_the_outer_element() -> None:
    html = '<div hidden>outer <span style="display:none">inner</span><noscript>ns</noscript></div>'
    findings = scan_html(html)
    assert [(finding.kind, finding.text) for finding in findings] == [(Kind.HIDDEN_ATTRIBUTE, "outer inner ns")]


def test_script_and_style_contents_are_not_counted_as_hidden_text() -> None:
    html = "<div hidden><script>var you_are = 1;</script><style>p{}</style>label</div>"
    assert [finding.text for finding in scan_html(html)] == ["label"]


def test_hidden_elements_with_no_text_are_skipped() -> None:
    html = '<span aria-hidden="true"><svg></svg></span><div hidden>   </div><!--   -->'
    assert scan_html(html) == []


def test_aria_hidden_false_is_not_hidden() -> None:
    assert scan_html('<span aria-hidden="false">shown</span>') == []


def test_document_order_is_preserved() -> None:
    html = '<!-- first --><img alt="second"><p hidden>third</p>'
    assert [finding.text for finding in scan_html(html)] == ["first", "second", "third"]


def test_parse_inline_style_lowercases_and_later_declarations_win() -> None:
    style = "Display: BLOCK; color:red; display:none !important; broken; :empty"
    assert parse_inline_style(style) == {"display": "none", "color": "red"}


@pytest.mark.parametrize(
    ("style", "reasons"),
    [
        ("display:none", ("display:none",)),
        ("visibility:collapse", ("visibility:collapse",)),
        ("font-size:0", ("font-size:0",)),
        ("font-size:0.0em", ("font-size:0",)),
        ("opacity:0.0", ("opacity:0",)),
        ("opacity:0%", ("opacity:0",)),
        ("position:fixed; top:-2000px", ("off-screen top:-2000px",)),
        ("position:absolute; left:-100em", ("off-screen left:-100em",)),
        ("text-indent:-9999px", ("off-screen text-indent:-9999px",)),
        ("display:none; opacity:0", ("display:none", "opacity:0")),
        ("opacity:0.01", ()),
        ("font-size:1px", ()),
        ("left:-9999px", ()),
        ("position:relative; left:-9999px", ()),
        ("position:absolute; left:-999px", ()),
        ("position:absolute; left:calc(-100vw)", ()),
        ("", ()),
    ],
)
def test_inline_style_hiding_reasons(style: str, reasons: tuple[str, ...]) -> None:
    assert inline_style_hiding_reasons(style) == reasons
