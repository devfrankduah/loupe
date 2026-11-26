"""Whether a URL someone gave loupe is one it can fetch, checked the same way wherever a URL comes in."""

from __future__ import annotations

from urllib.parse import urlsplit


def url_problem(url: str) -> str | None:
    """Why url isn't a page loupe can fetch, as a sentence that names it, or None for an http(s) URL with a host.

    A space or a control character anywhere in it makes it invalid. A browser refuses one in the host, and one in the
    path most often means two URLs ran together, which, fetched as one, would ask a site for a page that isn't there.
    Only URLs a person or an agent gives are checked: a page's own links and redirects are a browser's to resolve.
    """
    if any(character.isspace() or not character.isprintable() for character in url):
        return (
            f"{url!r} isn't a valid URL: it has a space or a control character in it. Write a space in a path as %20, "
            "and give each URL on its own"
        )
    try:
        parts = urlsplit(url)
        # Reading the port is what checks that it's a number in range.
        _ = parts.port
    except ValueError:
        return f"{url!r} isn't a valid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{url!r} isn't an http(s) URL with a host"
    return None
