"""Errors shared by every way loupe fetches or scans a page."""

from __future__ import annotations


class PageFetchError(RuntimeError):
    """A page or its markdown couldn't be fetched. The message is safe to print."""


class BlockedError(PageFetchError):
    """The site turned the request away the way sites that block bots do: 403, 429 or a challenge page."""


class SetupError(RuntimeError):
    """loupe can't scan at all: a token, Playwright or Chromium is missing, or a setting is wrong.

    The message is safe to print.
    """
