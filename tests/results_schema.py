"""The published JSON Schema for loupe's results, and a stricter copy of it for tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH = Path(__file__).parent.parent / "docs" / "schema" / "results-v1.json"


def published_schema() -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema


def _closed(schema: Any) -> Any:
    """schema with every object that lists its properties closed to any others."""
    if isinstance(schema, dict):
        closed = {key: _closed(value) for key, value in schema.items()}
        if "properties" in schema:
            closed["additionalProperties"] = False
        return closed
    if isinstance(schema, list):
        return [_closed(item) for item in schema]
    return schema


def check_results(results: object) -> None:
    """Fail unless results fit the published schema and every field in them is documented there.

    The published schema leaves objects open, so readers keep working when a
    field is added. Here they're closed, so a field loupe writes but the
    schema doesn't describe fails the test.
    """
    jsonschema.Draft202012Validator(_closed(published_schema())).validate(results)
