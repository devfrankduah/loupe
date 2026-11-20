"""Where loupe gets pages from: this machine, or Bright Data's APIs.

The local backend needs no account: it loads pages itself. The Bright Data
backend fetches the HTML through Web Unlocker, renders through the Browser
API and gets the markdown from the Fetch API, which gets past sites that turn
away automated visitors.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from loupe_scan.fetch import TOKEN_ENVIRONMENT_VARIABLE
from loupe_scan.render import BROWSER_ENDPOINT_VARIABLE


class Backend(str, Enum):
    LOCAL = "local"
    BRIGHTDATA = "brightdata"

    @property
    def description(self) -> str:
        return "local" if self is Backend.LOCAL else "Bright Data"


def choose_backend(requested: Backend | None, environment: Mapping[str, str]) -> Backend:
    """The backend asked for, or else Bright Data when either of its credentials is set and local when neither is."""
    if requested is not None:
        return requested
    for name in (TOKEN_ENVIRONMENT_VARIABLE, BROWSER_ENDPOINT_VARIABLE):
        if environment.get(name, "").strip():
            return Backend.BRIGHTDATA
    return Backend.LOCAL
