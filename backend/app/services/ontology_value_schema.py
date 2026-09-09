"""Project authored property constraints into executable value contracts."""
from __future__ import annotations

from typing import Any


CONSTRAINT_KEYWORDS = {
    "const": "const", "minimum": "minimum", "maximum": "maximum",
    "exclusive_minimum": "exclusiveMinimum", "exclusive_maximum": "exclusiveMaximum",
    "min_length": "minLength", "max_length": "maxLength", "format": "format",
}


def property_schema(prop: Any, *, constrained: bool = True) -> dict[str, Any]:
    kind = str(getattr(prop, "data_type", "") or "").lower()
    types = {"float": "number", "text": "string", "date": "string", "datetime": "string"}
    kind = types.get(kind, kind)
    schema: dict[str, Any] = {}
    if kind in {"string", "integer", "number", "boolean"} or (constrained and kind in {"object", "array", "null"}):
        schema["type"] = kind
    elif constrained and kind == "json":
        schema["type"] = ["object", "array"]
    original_kind = str(getattr(prop, "data_type", "") or "").lower()
    if original_kind in {"date", "datetime"}:
        schema["format"] = "date" if original_kind == "date" else "date-time"
    if bool(getattr(prop, "is_enum", False)) and getattr(prop, "enum_values", None):
        schema["enum"] = list(prop.enum_values)
    if constrained:
        constraints = getattr(prop, "constraints", {}) or {}
        for source, target in CONSTRAINT_KEYWORDS.items():
            if source in constraints:
                schema[target] = constraints[source]
        if "pattern" in constraints:
            # Instance validation uses fullmatch, whereas JSON Schema patterns
            # normally match substrings. Keep both consumers equivalent.
            schema["pattern"] = "^(?:" + constraints["pattern"] + ")(?![\\s\\S])"
        if not getattr(prop, "is_required", False) and not getattr(prop, "is_key", False) and schema:
            return {"anyOf": [schema, {"type": "null"}]}
    return schema


def entity_schema(entity: Any, *, partial: bool = False) -> dict[str, Any]:
    properties = list(getattr(entity, "properties", []) or [])
    return {
        "type": "object",
        "properties": {str(prop.api_name or prop.name): property_schema(prop) for prop in properties},
        "required": [] if partial else [str(prop.api_name or prop.name) for prop in properties
                                       if prop.is_required or prop.is_key],
        "additionalProperties": False,
    }
