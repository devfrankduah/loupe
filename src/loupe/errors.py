"""Errors shared by every way loupe fetches a page."""

from __future__ import annotations


class PageFetchError(RuntimeError):
    """A page or its markdown couldn't be fetched. The message is safe to print."""


class BlockedError(PageFetchError):
    """The site turned the request away the way sites that block bots do: 403, 429 or a challenge page."""
