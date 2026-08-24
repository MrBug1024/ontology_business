"""Structured Agent capability scopes and their runtime/readiness projection."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from . import capability_readiness_service, permission_service


CAPABILITY_CATEGORIES = ("functions", "actions", "rules", "events", "workflows")
KIND_BY_CATEGORY = {
    "functions": "function",
    "actions": "action",
    "rules": "rule",
    "events": "event",
    "workflows": "workflow",
}
LABEL_BY_CATEGORY = {
    "functions": "函数",
    "actions": "操作",
    "rules": "规则",
    "events": "事件",
    "workflows": "工作流",
}


class AgentCapabilityScopeError(ValueError):
    """The requested scope contains a resource outside the governed catalog."""


def explicit_empty_scope() -> dict[str, dict[str, Any]]:
    return {
        category: {"mode": "explicit", "selected_ids": []}
        for category in CAPABILITY_CATEGORIES
    }


def legacy_all_scope() -> dict[str, dict[str, Any]]:
    return {
        category: {"mode": "all", "selected_ids": []}
        for category in CAPABILITY_CATEGORIES
    }


def normalize_scope(
    raw_scope: Any,
    *,
    legacy_default: bool = False,
    allow_all: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return one complete, fail-closed persisted/API capability scope.

    A database NULL is returned as explicit-empty by this low-level normalizer.
    The two legacy compatibility call sites deliberately opt into
    :func:`legacy_all_scope`: runtime preserves the pre-scope Agent behaviour,
    while the Agent API resolves and freezes the current ACL-filtered ids.
    Missing categories and malformed values always stay explicit-empty.
    ``allow_all`` is reserved for validating a fresh API selection; persisted
    explicit scopes never dynamically inherit capabilities added later.
    """
    if hasattr(raw_scope, "model_dump"):
        raw_scope = raw_scope.model_dump()
    if raw_scope is None:
        return explicit_empty_scope()
    if not isinstance(raw_scope, dict):
        return explicit_empty_scope()
    normalized = explicit_empty_scope()
    for category in CAPABILITY_CATEGORIES:
        entry = raw_scope.get(category)
        if not isinstance(entry, dict):
            continue
        mode = "all" if allow_all and entry.get("mode") == "all" else "explicit"
        selected: list[str] = []
        if mode == "explicit" and isinstance(entry.get("selected_ids"), list):
            for value in entry["selected_ids"]:
                resource_id = str(value).strip() if isinstance(value, str) else ""
                if resource_id and len(resource_id) <= 32 and resource_id not in selected:
                    selected.append(resource_id)
        normalized[category] = {"mode": mode, "selected_ids": selected}
    return normalized


def scope_has_business_tools(scope: Any) -> bool:
    normalized = normalize_scope(scope, legacy_default=False, allow_all=True)
    return any(
        entry["mode"] == "all" or bool(entry["selected_ids"])
        for entry in normalized.values()
    )


def _rule_fields(rule: Any) -> set[str]:
    fields: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for field_key in ("field", "value_field"):
                if isinstance(value.get(field_key), str) and value[field_key].strip():
                    fields.add(value[field_key].strip())
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(getattr(rule, "condition", {}) or {})
    return fields


def rule_is_visible(
    db: Session,
    rule: Any,
    *,
    visible_action_ids: set[str],
) -> bool:
    """Prevent rules from leaking restricted Actions or sensitive properties."""
    if any(
        str(action_id) not in visible_action_ids
        for action_id in (getattr(rule, "trigger_action_ids", []) or [])
    ):
        return False
    entity = getattr(rule, "entity", None)
    if not entity:
        return True
    visible_fields = {
        str(prop.name)
        for prop in (getattr(entity, "properties", []) or [])
        if permission_service.can_read_property(db, prop)
    }
    return _rule_fields(rule).issubset(visible_fields)


def _workflow_references(workflow: Any) -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    key_by_kind = {"action": "action_id", "rule": "rule_id", "event": "event_id"}
    for item in [
        *(getattr(workflow, "steps", []) or []),
        *(getattr(workflow, "nodes", []) or []),
    ]:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("type") or "")
        key = key_by_kind.get(kind)
        data = item.get("data") if isinstance(item.get("data"), Mapping) else item
        resource_id = str(data.get(key) or "") if key else ""
        if kind and resource_id:
            references.append((kind, resource_id))
    return references


def workflow_is_visible(
    workflow: Any,
    *,
    all_resource_ids: dict[str, set[str]],
    visible_resource_ids: dict[str, set[str]],
) -> bool:
    """Do not expose a workflow as a side door to a hidden nested resource."""
    for kind, resource_id in _workflow_references(workflow):
        if (
            resource_id in all_resource_ids.get(kind, set())
            and resource_id not in visible_resource_ids.get(kind, set())
        ):
            return False
    return True


def visible_resources(
    db: Session,
    definition: Any,
) -> dict[str, list[Any]]:
    """Build the current ACL-filtered catalog from one resolved definition."""
    actions = [
        resource
        for resource in definition.actions.values()
        if permission_service.check_action(db, resource, "read").allowed
    ]
    visible_action_ids = {str(resource.id) for resource in actions}
    rules = [
        resource
        for resource in definition.rules.values()
        if rule_is_visible(db, resource, visible_action_ids=visible_action_ids)
    ]
    events = list(definition.events.values())
    visible_ids = {
        "action": visible_action_ids,
        "rule": {str(resource.id) for resource in rules},
        "event": {str(resource.id) for resource in events},
    }
    all_ids = {
        "action": {str(resource.id) for resource in definition.actions.values()},
        "rule": {str(resource.id) for resource in definition.rules.values()},
        "event": {str(resource.id) for resource in definition.events.values()},
    }
    resources = {
        "functions": list(definition.functions.values()),
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": [
            resource
            for resource in definition.workflows.values()
            if permission_service.check_workflow(db, resource, "read").allowed
            and workflow_is_visible(
                resource,
                all_resource_ids=all_ids,
                visible_resource_ids=visible_ids,
            )
        ],
    }
    return {
        category: sorted(
            resources[category],
            key=lambda resource: (str(getattr(resource, "name", "")), str(resource.id)),
        )
        for category in CAPABILITY_CATEGORIES
    }


def validate_scope(
    db: Session,
    scope: Any,
    *,
    definition: Any | None,
) -> dict[str, dict[str, Any]]:
    """Validate every explicit id against its category, scenario and current ACL."""
    normalized = normalize_scope(scope, legacy_default=False, allow_all=True)
    if definition is None:
        unsafe = [
            LABEL_BY_CATEGORY[category]
            for category, entry in normalized.items()
            if entry["mode"] == "all" or entry["selected_ids"]
        ]
        if unsafe:
            raise AgentCapabilityScopeError(
                "未绑定业务场景时不能授权业务能力：" + "、".join(unsafe)
            )
        return normalized
    catalog = visible_resources(db, definition)
    for category in CAPABILITY_CATEGORIES:
        entry = normalized[category]
        allowed_ids = {str(resource.id) for resource in catalog[category]}
        if entry["mode"] == "all":
            normalized[category] = {
                "mode": "explicit",
                "selected_ids": sorted(allowed_ids),
            }
            continue
        invalid = [resource_id for resource_id in entry["selected_ids"] if resource_id not in allowed_ids]
        if invalid:
            # Do not reveal whether an id exists in another scenario or is
            # merely hidden by ACL; both are outside this Agent's catalog.
            raise AgentCapabilityScopeError(
                f"{LABEL_BY_CATEGORY[category]}选择中包含不属于当前场景或无权读取的能力"
            )
    return normalized


def filter_resources(
    catalog: dict[str, list[Any]],
    scope: Any,
) -> dict[str, list[Any]]:
    normalized = normalize_scope(scope, legacy_default=False, allow_all=True)
    filtered: dict[str, list[Any]] = {}
    for category in CAPABILITY_CATEGORIES:
        entry = normalized[category]
        if entry["mode"] == "all":
            filtered[category] = list(catalog[category])
            continue
        selected = set(entry["selected_ids"])
        filtered[category] = [
            resource for resource in catalog[category] if str(resource.id) in selected
        ]
    return filtered


def _readiness_item(
    db: Session,
    category: str,
    resource: Any,
    definition: Any,
) -> dict[str, Any]:
    readiness = capability_readiness_service.capability_readiness(
        KIND_BY_CATEGORY[category],
        resource,
        definition=definition,
        db=db,
    )
    return {
        "id": str(resource.id),
        "name": str(getattr(resource, "name", "") or resource.id),
        "description": str(getattr(resource, "description", "") or "")[:240],
        **readiness.as_dict(),
    }


def catalog_summary(
    db: Session,
    definition: Any,
) -> dict[str, list[dict[str, Any]]]:
    catalog = visible_resources(db, definition)
    return {
        category: [
            _readiness_item(db, category, resource, definition)
            for resource in catalog[category]
        ]
        for category in CAPABILITY_CATEGORIES
    }


def capability_summary(
    db: Session,
    scope: Any,
    *,
    definition: Any | None,
    definition_error: str = "",
) -> dict[str, dict[str, Any]]:
    normalized = normalize_scope(scope, legacy_default=False)
    catalog = visible_resources(db, definition) if definition is not None else {
        category: [] for category in CAPABILITY_CATEGORIES
    }
    summaries: dict[str, dict[str, Any]] = {}
    for category in CAPABILITY_CATEGORIES:
        entry = normalized[category]
        by_id = {str(resource.id): resource for resource in catalog[category]}
        selected_ids = (
            list(by_id)
            if entry["mode"] == "all"
            else list(entry["selected_ids"])
        )
        items: list[dict[str, Any]] = []
        for resource_id in selected_ids:
            resource = by_id.get(resource_id)
            if resource is None:
                reason = definition_error or "所选能力不在当前运行定义中或当前账号已无读取权限"
                items.append({
                    "id": resource_id,
                    "name": f"已失效能力 {resource_id[:8]}",
                    "description": "",
                    "executable": False,
                    "blocked_reasons": [reason],
                })
            else:
                items.append(_readiness_item(db, category, resource, definition))
        blocked_reasons = list(dict.fromkeys(
            f"{item['name']}：{reason}"
            for item in items
            for reason in item["blocked_reasons"]
        ))
        if definition_error and entry["mode"] == "all" and not items:
            blocked_reasons.append(definition_error)
        summaries[category] = {
            "mode": entry["mode"],
            "available_count": len(catalog[category]),
            "selected_count": len(items),
            "executable_count": sum(1 for item in items if item["executable"]),
            "blocked_count": sum(1 for item in items if not item["executable"]),
            "blocked_reasons": blocked_reasons,
            "items": items,
        }
    return summaries
