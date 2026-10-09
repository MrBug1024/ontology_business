"""Preserve existing business content when an AI proposal omits it."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


def _identity(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("key") or value.get("attribute") or value.get("name"):
            return str(value.get("key") or value.get("attribute") or value["name"])
        if value.get("source") and value.get("target"):
            return json.dumps([value["source"], value["target"], value.get("label", "")])
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def preserve_omitted(original: Any, proposed: Any) -> Any:
    """Blank/default proposal content is omission; explicit human edits still replace.

    AI can revise a non-empty value and add members. Removing known content is
    a separate, explicit replanning decision, never an accidental empty field.
    """
    if isinstance(original, dict) and isinstance(proposed, dict):
        return {
            key: preserve_omitted(value, proposed[key]) if key in proposed else deepcopy(value)
            for key, value in original.items()
        } | {key: deepcopy(value) for key, value in proposed.items() if key not in original}
    if isinstance(original, list) and isinstance(proposed, list):
        incoming = {_identity(value): value for value in proposed}
        retained = [preserve_omitted(value, incoming.pop(_identity(value)))
                    if _identity(value) in incoming else deepcopy(value) for value in original]
        return retained + [deepcopy(value) for value in incoming.values()]
    if proposed in (None, "") and original not in (None, ""):
        return deepcopy(original)
    return deepcopy(proposed)
