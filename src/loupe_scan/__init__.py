"""Find text on web pages that human readers can't see but AI agents can.

    from loupe_scan import scan

    result = scan("https://example.com/")
    print(result.verdict)

docs/python.md describes the API.
"""

from loupe_scan._version import __version__
from loupe_scan.api import HiddenText, Scanner, ScanError, ScanResult, scan, scan_html
from loupe_scan.backend import Backend
from loupe_scan.errors import SetupError
from loupe_scan.report import FailOn, FailureKind
from loupe_scan.scan import Kind, ScanMode
from loupe_scan.stylesheets import StylesheetCheck

__all__ = [
    "Backend",
    "FailOn",
    "FailureKind",
    "HiddenText",
    "Kind",
    "ScanError",
    "ScanMode",
    "ScanResult",
    "Scanner",
    "SetupError",
    "StylesheetCheck",
    "__version__",
    "scan",
    "scan_html",
]
