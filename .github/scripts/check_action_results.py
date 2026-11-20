"""Check the JSON the action wrote for the test pages. Usage: check_action_results.py NEVER RENDERED MISSING"""

import json
import sys

never, rendered, missing = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:4])
assert [(page["url"], page["scan_mode"], page["backend"]) for page in never] == [
    ("http://127.0.0.1:8000/test-page/", "static", "local"),
    ("http://127.0.0.1:8000/animated-page/", "static", "local"),
], never
assert [page["instruction_like_count"] for page in never] == [3, 0], never
(blender,) = rendered
assert (blender["scan_mode"], blender["instruction_like_count"]) == ("rendered", 5), blender
assert [page["error"]["kind"] for page in missing] == ["fetch", "fetch"], missing
assert all(page["schema_version"] == 1 for page in never + rendered + missing)
print("The action's results match the test pages.")
