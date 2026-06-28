"""JSON helpers shared by the trace exporters."""

from __future__ import annotations

import json
from typing import Any


def expand_embedded_json(value: Any) -> Any:
    """Recursively turn JSON strings (e.g. ``output.value``) into real objects.

    The instrumentation serializes nested payloads to strings before export, so
    they show up escaped. For readability we parse any string that is itself a
    JSON object/array back into structured data, leaving plain text untouched.
    """
    if isinstance(value, dict):
        return {key: expand_embedded_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_embedded_json(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return expand_embedded_json(json.loads(stripped))
            except (ValueError, TypeError):
                return value
    return value
