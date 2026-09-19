"""Portable optional arguments for OpenAI-compatible discovery tool callers."""
from __future__ import annotations


def portable_schema(value):
    definitions = value.get("$defs", {}) if isinstance(value, dict) else {}

    def expand(item, stack=()):
        if isinstance(item, list):
            return [expand(child, stack) for child in item]
        if not isinstance(item, dict):
            return item
        reference = item.get("$ref")
        if reference:
            name = reference.removeprefix("#/$defs/")
            if not reference.startswith("#/$defs/") or name not in definitions or name in stack:
                raise ValueError("Unsupported discovery tool schema reference")
            return expand({**definitions[name], **{key: child for key, child in item.items() if key != "$ref"}}, (*stack, name))
        return {key: expand(child, stack) for key, child in item.items() if key != "$defs"}

    return _optional_by_omission(expand(value))


def _optional_by_omission(value):
    """Represent optional nullable properties by omission, not an anyOf null.

    Some compatible endpoints consistently select the null branch even with
    an explicit ID. Required unions remain unchanged; server DTOs still
    validate every argument. This only narrows the advertised optional input.
    """
    if isinstance(value, list):
        return [_optional_by_omission(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _optional_by_omission(item) for key, item in value.items()}
    required = set(result.get("required", []))
    for name, schema in result.get("properties", {}).items():
        choices = schema.get("anyOf", [])
        non_null = [item for item in choices if item != {"type": "null"}]
        if name not in required and len(choices) == 2 and len(non_null) == 1:
            schema.pop("anyOf")
            schema.update(non_null[0])
            if schema.get("default") is None:
                schema.pop("default", None)
    return result
