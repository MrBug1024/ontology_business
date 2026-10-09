"""Reject repair regressions in already validated nested business contracts."""
from __future__ import annotations

from typing import Any


PROPERTY_FIELDS = (
    "data_type", "is_required", "is_key", "is_title", "is_sensitive", "is_enum",
    "enum_values", "constraints", "default_value",
)


def _index(items: Any, identity: str) -> dict[str, dict]:
    return {str(item[identity]): item for item in items or []
            if isinstance(item, dict) and item.get(identity)}


def preserves_nested_contract(best: dict, evaluated: dict) -> bool:
    """Permit correcting a blocked entity, but never drop any existing property.

    Fields of an unblocked entity are protected. A blocked entity may correct
    invalid flags/types, while source requirements are checked independently
    by the compiler. Retaining a property name alone cannot hide regression.
    """
    blocked = {
        str(key) for issue in best.get("unresolved", [])
        if issue.get("blocking", True) is not False
        for key in issue.get("affected_change_keys", [])
    }
    updated = _index(evaluated.get("entities"), "key")
    for key, entity in _index(best.get("entities"), "key").items():
        replacement = updated.get(key)
        if replacement is None:
            return False
        before = _index(entity.get("properties"), "name")
        after = _index(replacement.get("properties"), "name")
        if not set(before).issubset(after):
            return False
        if key in blocked:
            continue
        for name, prop in before.items():
            for field in PROPERTY_FIELDS:
                if field in prop and prop[field] != after[name].get(field):
                    return False
    return True
