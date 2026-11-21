"""The fetch tool an agent reads pages through: plain in the raw condition, loupe's fetch_page in the guarded one."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Final, Protocol

from loupe_scan.convert import html_to_markdown
from loupe_scan.errors import PageFetchError
from loupe_scan.local_fetch import LocalFetcher
from loupe_scan.mcp_server import Mode, PageScanner, ServerSettings, fetch_page_text
from mcp.server.mcpserver.exceptions import ToolError

# Both conditions give the model a tool by this name, so it can't tell them apart by the name.
TOOL_NAME: Final[str] = "fetch_page"
MARKDOWN_DESCRIPTION: Final[str] = "Fetch a web page and return its content as markdown."
HTML_DESCRIPTION: Final[str] = "Fetch a web page and return its HTML."


class PageTool(Protocol):
    """A fetch tool for one condition: what an adapter offers its model, and runs when the model calls it."""

    @property
    def name(self) -> str: ...

    @property
    def description(self) -> str: ...

    def fetch(self, url: str) -> str:
        """The tool's result for url. A failure comes back as text, as a tool's error reaches a model."""
        ...


class RawOutput(str, Enum):
    MARKDOWN = "markdown"
    HTML = "html"


class RawTool:
    """Fetches the page and returns markdownify's markdown of it, or its HTML, as many agent fetch tools do."""

    name = TOOL_NAME

    def __init__(self, output: RawOutput = RawOutput.MARKDOWN, *, fetcher: LocalFetcher | None = None) -> None:
        self.output = output
        self.description = MARKDOWN_DESCRIPTION if output is RawOutput.MARKDOWN else HTML_DESCRIPTION
        self._fetcher = fetcher if fetcher is not None else LocalFetcher()

    def __repr__(self) -> str:
        return f"RawTool(output={self.output.value})"

    def fetch(self, url: str) -> str:
        try:
            html = self._fetcher.fetch_html(url)
            return html if self.output is RawOutput.HTML else html_to_markdown(url, html)
        except PageFetchError as error:
            return f"Error: {error}"


class GuardedTool:
    """loupe's fetch_page in strip mode: loupe's header, then the markdown with the hidden text loupe found taken out."""

    name = TOOL_NAME
    description = MARKDOWN_DESCRIPTION

    def __init__(self, *, render: bool = True, allow_private_addresses: bool = False) -> None:
        self.render = render
        settings = ServerSettings(mode=Mode.STRIP, allow_private_addresses=allow_private_addresses)
        # An empty environment, so the guarded condition always fetches from this machine.
        self._scanner = PageScanner(settings, environment={}, on_note=lambda message: None)

    def __repr__(self) -> str:
        return f"GuardedTool(render={self.render})"

    def fetch(self, url: str) -> str:
        try:
            return fetch_page_text(self._scanner, url, render=self.render)
        except ToolError as error:
            return f"Error: {error}"


class CachedTool:
    """Another tool, fetching each URL once per run, so a page isn't requested for every trial and every trial reads the same text."""

    def __init__(self, tool: PageTool) -> None:
        self._tool = tool
        self._results: dict[str, str] = {}
        self.fetches = 0

    def __repr__(self) -> str:
        return f"CachedTool({self._tool!r})"

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    def fetch(self, url: str) -> str:
        if url not in self._results:
            self.fetches += 1
            self._results[url] = self._tool.fetch(url)
        return self._results[url]


ToolFactory = Callable[[], PageTool]
