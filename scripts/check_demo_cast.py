"""Check a demo recording for secrets and pick its playback speed.

Usage: python -I scripts/check_demo_cast.py RECORDING.cast

Exits with status 1 if the recording holds BRIGHTDATA_API_TOKEN or any part
of BRIGHTDATA_BROWSER_WSS, in the file as written or in the text it shows.
Otherwise prints the playback speed that keeps the GIF to about 19 seconds:
11 seconds of playback, then the output held for 8 seconds so it can be read.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

from loupe.render import EndpointRedactor, _endpoint_pieces

PLAYBACK_SECONDS = 11


def shown_text(events: list[list[object]]) -> str:
    return "".join(str(event[2]) for event in events if len(event) == 3 and event[1] == "o")


def holds_secret(texts: list[str], endpoint: str, token: str) -> bool:
    redact = EndpointRedactor(endpoint)
    long_pieces = [piece.lower() for piece in _endpoint_pieces(endpoint) if len(piece) >= 8]
    for text in texts:
        lowered = text.lower()
        if redact(text) != text or token in text or any(piece in lowered for piece in long_pieces):
            return True
    return False


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    endpoint = os.environ.get("BRIGHTDATA_BROWSER_WSS", "").strip()
    token = os.environ.get("BRIGHTDATA_API_TOKEN", "").strip()
    if not endpoint or not token:
        print("Load .env first: both BRIGHTDATA_API_TOKEN and BRIGHTDATA_BROWSER_WSS are needed.", file=sys.stderr)
        return 2
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    lines = raw.splitlines()
    events = [json.loads(line) for line in lines[1:] if line.strip()]
    shown = shown_text(events)
    if holds_secret([raw, shown], endpoint, token):
        print("The recording contains the token or part of the endpoint.", file=sys.stderr)
        return 1
    duration = sum(float(event[0]) for event in events)
    transfer = next((line.strip() for line in shown.splitlines() if line.strip().startswith("transfer:")), "no transfer line")
    print(f"recorded {duration:.1f} s; {transfer}", file=sys.stderr)
    print(max(1.0, math.ceil(duration / PLAYBACK_SECONDS * 2) / 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
