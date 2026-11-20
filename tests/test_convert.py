from __future__ import annotations

import pytest

from loupe import convert
from loupe.convert import BRIGHT_DATA_MARKDOWN, ConversionError, html_to_markdown, local_markdown_source
from loupe.survival import MarkdownIndex

PAGE = """<!doctype html><html><head><title>Lemon cake</title><meta name="description" content="Meta words here">
<style>.x { display: none }</style><script>var note = "script words here";</script></head>
<body><!-- comment words here --><h1>Lemon cake</h1><p hidden>Hidden attribute words</p>
<p style="display:none">Inline style words</p><img src="cake.jpg" alt="Alt text words here">
<a href="/more" title="Link title words">More cakes</a><button aria-label="Aria label words">x</button></body></html>"""


def test_the_converter_keeps_what_most_converters_keep_and_drops_the_rest() -> None:
    index = MarkdownIndex(html_to_markdown("https://a.test/", PAGE))
    kept = ("Hidden attribute words", "Inline style words", "Alt text words here", "Link title words")
    dropped = ("comment words here", "script words here", "Meta words here", "Aria label words")
    assert {text: index.contains(text) for text in kept} == dict.fromkeys(kept, True)
    assert {text: index.contains(text) for text in dropped} == dict.fromkeys(dropped, False)


def test_the_local_source_names_the_converter_its_version_and_licence() -> None:
    source = local_markdown_source()
    assert (source.name, source.license, source.approximation) == ("markdownify", "MIT", True)
    assert source.version is not None and source.version[0].isdigit()
    assert source.description == f"converted here by markdownify {source.version}, an approximation of what agent tools produce"
    assert BRIGHT_DATA_MARKDOWN.description == "from Bright Data's Fetch API"
    assert not BRIGHT_DATA_MARKDOWN.approximation


def test_deeply_nested_html_is_a_conversion_error_not_a_crash() -> None:
    nested = "<div>" * 5000 + "deep" + "</div>" * 5000
    with pytest.raises(ConversionError, match="nested too deeply"):
        html_to_markdown("https://a.test/deep", nested)


def test_any_converter_failure_is_reported_as_a_conversion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(html: str, **options: object) -> str:
        raise AttributeError("'NoneType' object has no attribute 'name'")

    monkeypatch.setattr(convert, "markdownify", broken)
    with pytest.raises(ConversionError) as caught:
        html_to_markdown("https://a.test/cake", "<p>x</p>")
    assert str(caught.value) == "Converting https://a.test/cake to markdown failed (AttributeError)"
