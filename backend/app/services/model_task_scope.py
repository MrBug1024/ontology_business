"""Bind a compiler task board to the server's validated authoring route."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


_SPECIFIC_SCOPES = {
    "ontology": "ontology", "mapping": "mapping",
    "capabilities": "capabilities", "workflow": "workflows",
}


def bind_request_scope(payload: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    """Task scheduling scope does not replace resource dependency validation."""
    task_id = _SPECIFIC_SCOPES.get(routing.get("scope"))
    if task_id is None:
        return payload
    result = deepcopy(payload)
    generation = dict(result.get("generation") or {})
    generation["requested_task_ids"] = [task_id]
    result["generation"] = generation
    # Any previous board was built before the server attached this scope.
    result.pop("tasks", None)
    return result
