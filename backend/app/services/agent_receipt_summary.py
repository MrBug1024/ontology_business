"""Bounded scalar summaries selected by a governed output schema."""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
from typing import Any


MAX_SUMMARY_BYTES = 2048
MAX_SUMMARY_FIELDS = 20


def output_summary(output: Any, schema: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(output, Mapping) or not isinstance(schema, Mapping):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    summary: dict[str, Any] = {}
    for key in sorted(key for key in properties if isinstance(key, str)):
        field = properties[key]
        if key not in output or len(key) > 100 or not isinstance(field, Mapping):
            continue
        value = output[key]
        kind = field.get("type")
        allowed = (
            (kind == "boolean" and isinstance(value, bool))
            or (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (
                kind == "number"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (isinstance(value, int) or math.isfinite(value))
            )
            or (
                kind == "string"
                and isinstance(value, str)
                and len(value.encode("utf-8")) <= 128
                and isinstance(field.get("enum"), (list, tuple))
                and value in field["enum"]
            )
        )
        if not allowed:
            continue
        candidate = {**summary, key: value}
        encoded = json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) <= MAX_SUMMARY_BYTES:
            summary = candidate
        if len(summary) == MAX_SUMMARY_FIELDS:
            break
    return summary
