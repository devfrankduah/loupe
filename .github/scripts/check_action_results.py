"""Check the JSON the action wrote for the test pages. Usage: check_action_results.py NEVER RENDERED MISSING

The expected counts come from the test pages' ground truths, so the check
can't drift from the tables the test suite holds loupe to.
"""

import json
import re
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent.parent / "docs"


def instruction_like(page: str, mode: str) -> int:
    text = (DOCS / page / "ground-truth.md").read_text(encoding="utf-8")
    match = re.search(rf"^- {mode} scan: \d+ findings, (\d+) instruction-like\.$", text, re.MULTILINE)
    assert match is not None, f"{page}'s ground truth gives no {mode.lower()} scan total"
    return int(match.group(1))


never, rendered, missing = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:4])
assert [(page["url"], page["scan_mode"], page["backend"]) for page in never] == [
    ("http://127.0.0.1:8000/test-page/", "static", "local"),
    ("http://127.0.0.1:8000/animated-page/", "static", "local"),
], never
expected = [instruction_like("test-page", "Markup"), instruction_like("animated-page", "Markup")]
assert [page["instruction_like_count"] for page in never] == expected, (expected, never)
(blender,) = rendered
assert (blender["scan_mode"], blender["instruction_like_count"]) == ("rendered", instruction_like("test-page", "Rendered")), blender
assert [page["error"]["kind"] for page in missing] == ["fetch", "fetch"], missing
assert all(page["schema_version"] == 1 for page in never + rendered + missing)
print("The action's results match the test pages.")
