"""Bind a compiler task board to the server's validated authoring route."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


_SPECIFIC_SCOPES = {
    "ontology": "ontology", "mapping": "mapping",
    "capabilities": "capabilities", "workflow": "workflows",
    "rules": "rules",
}

_TASK_ORDER = ('ontology', 'instances', 'mapping', 'capabilities', 'rules', 'workflows')


def requested_tasks(routing: dict[str, Any]) -> list[str]:
    tasks = routing.get('task_ids', [])
    if not isinstance(tasks, list) or len(tasks) > 6 or any(task not in _TASK_ORDER for task in tasks):
        raise ValueError('建模任务范围无效')
    if len(tasks) != len(set(tasks)):
        raise ValueError('建模任务不能重复')
    expected = _SPECIFIC_SCOPES.get(routing.get('scope'))
    if tasks and (expected and tasks != [expected] or not expected and routing.get('scope') != 'scenario_model'):
        raise ValueError('建模任务与明确主题不一致')
    return [task for task in _TASK_ORDER if task in tasks]


def bind_request_scope(payload: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    """Task scheduling scope does not replace resource dependency validation."""
    task_id = _SPECIFIC_SCOPES.get(routing.get("scope"))
    tasks = requested_tasks(routing)
    if not tasks and task_id is None:
        return payload
    result = deepcopy(payload)
    generation = dict(result.get("generation") or {})
    generation["requested_task_ids"] = tasks or [task_id]
    result["generation"] = generation
    # Any previous board was built before the server attached this scope.
    result.pop("tasks", None)
    return result
