"""Turn a page's HTML into markdown on this machine, for the local backend.

Agent tools each convert pages with a converter of their own, so this is an
approximation of what an agent is given, not a copy of any one tool's
output. It uses markdownify (MIT licence), which like most converters keeps
the text of elements CSS hides, image alt text and link and image titles,
and drops comments, scripts and styles.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Final

from markdownify import markdownify

from loupe_scan.errors import PageFetchError

CONVERTER: Final[str] = "markdownify"
CONVERTER_LICENSE: Final[str] = "MIT"


class ConversionError(PageFetchError):
    """The HTML couldn't be turned into markdown. The message is safe to print."""


@dataclass(frozen=True)
class MarkdownSource:
    """Where a page's markdown came from."""

    name: str
    version: str | None
    license: str | None
    # True when the markdown was made here and only approximates what agent tools produce.
    approximation: bool

    @property
    def description(self) -> str:
        if not self.approximation:
            return f"from {self.name}"
        made_by = self.name if self.version is None else f"{self.name} {self.version}"
        return f"converted here by {made_by}, an approximation of what agent tools produce"

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "version": self.version, "license": self.license, "approximation": self.approximation}


BRIGHT_DATA_MARKDOWN: Final[MarkdownSource] = MarkdownSource(
    name="Bright Data's Fetch API", version=None, license=None, approximation=False
)


@functools.cache
def local_markdown_source() -> MarkdownSource:
    try:
        installed: str | None = version(CONVERTER)
    except PackageNotFoundError:
        installed = None
    return MarkdownSource(name=CONVERTER, version=installed, license=CONVERTER_LICENSE, approximation=True)


def html_to_markdown(url: str, html: str) -> str:
    """The markdown for a page's HTML. Raises ConversionError if the converter fails."""
    try:
        return markdownify(html, heading_style="ATX")
    except RecursionError:
        raise ConversionError(f"Converting {url} to markdown failed: its HTML is nested too deeply") from None
    except Exception as error:
        # The converter is third-party code running on untrusted HTML. Any
        # failure in it should cost this page its markdown comparison, not
        # stop the scan.
        raise ConversionError(f"Converting {url} to markdown failed ({type(error).__name__})") from None
