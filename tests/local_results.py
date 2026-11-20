"""Run loupe on the local backend against the fixture server and read back its JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loupe_scan import cli
from loupe_scan.scan import Finding, Kind, ScanMode


def scan_locally(url: str, json_path: Path, *extra: str) -> dict[str, Any]:
    """The one page loupe reports for url, scanned with no credentials and quotes kept whole."""
    assert cli.main([url, "--quote-chars", "0", "--json", str(json_path), *extra]) == cli.EXIT_OK
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["backend"] == "local"
    (page,) = report["pages"]
    assert page["backend"] == "local"
    assert page["markdown_source"]["approximation"] is True
    return dict(page)


def findings_of(page: dict[str, Any]) -> list[Finding]:
    return [
        Finding(
            kind=Kind(item["kind"]),
            detail=item["detail"],
            tag=item["tag"],
            text=item["text"],
            instruction_phrases=tuple(item["instruction_phrases"]),
            mode=ScanMode(item["mode"]),
        )
        for item in page["findings"]
    ]


def traced_of(page: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["text"], item["landed"]) for item in page["rendering"]["static_comparison"]]
