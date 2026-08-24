"""One fail-closed readiness contract for Agent-visible runtime capabilities.

Authoring definitions remain discoverable even while incomplete.  A capability
is called executable only when its own runtime contract is usable in the
resolved environment; callers receive stable, human-readable blocking reasons
instead of inferring readiness from one flag such as ``enabled``.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    BusinessScenario,
    Skill,
)
from . import function_definition_service, runtime_connector_service
from .policies import PolicyViolation, validate_workflow_graph


_CONDITION_LEAF_OPS = {
    ">", ">=", "<", "<=", "==", "!=", "in", "not_in", "contains",
    "not_contains", "is_null", "is_not_null",
}
_ACTION_EXECUTORS = {"unbound", "sql", "skill", "mcp", "http", "script", "template"}


class CapabilityNotReady(PolicyViolation):
    """Raised when a caller attempts to execute an authoring-only capability."""


@dataclass(frozen=True)
class CapabilityReadiness:
    executable: bool
    blocked_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "blocked_reasons": list(self.blocked_reasons),
        }


def _dedupe(reasons: list[str]) -> CapabilityReadiness:
    normalized = tuple(dict.fromkeys(reason for reason in reasons if reason))
    return CapabilityReadiness(executable=not normalized, blocked_reasons=normalized)


def _validate_condition_node(value: Any, *, path: str) -> None:
    if not isinstance(value, Mapping) or not value:
        raise CapabilityNotReady(f"{path}必须是非空结构化规则条件")
    op = str(value.get("op") or "")
    if op in {"and", "or", "not"}:
        conditions = value.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            raise CapabilityNotReady(f"{path}.conditions 必须是非空数组")
        if op == "not" and len(conditions) != 1:
            raise CapabilityNotReady(f"{path} 的 not 必须且只能包含一个条件")
        for index, condition in enumerate(conditions):
            _validate_condition_node(condition, path=f"{path}.conditions[{index}]")
        return
    if op not in _CONDITION_LEAF_OPS:
        raise CapabilityNotReady(f"{path}包含不受支持的条件运算符")
    field = value.get("field")
    if not isinstance(field, str) or not field.strip():
        raise CapabilityNotReady(f"{path}.field 必须是非空字段名")
    has_value = "value" in value
    has_value_field = "value_field" in value
    if op in {"is_null", "is_not_null"}:
        if has_value_field:
            raise CapabilityNotReady(f"{path} 的空值判断不能使用 value_field")
        return
    if has_value == has_value_field:
        raise CapabilityNotReady(f"{path}必须且只能配置 value 或 value_field")
    if has_value_field and (
        not isinstance(value.get("value_field"), str)
        or not str(value.get("value_field") or "").strip()
    ):
        raise CapabilityNotReady(f"{path}.value_field 必须是非空字段名")


def normalize_structured_condition(value: Any, *, label: str) -> dict[str, Any] | None:
    """Parse an Action condition without interpreting natural-language prose."""
    if value in (None, ""):
        return None
    parsed: Any = value
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError as exc:
            raise CapabilityNotReady(
                f"{label}目前只是自然语言说明；请在条件表单中重新配置为可验证的结构化规则条件"
            ) from exc
    _validate_condition_node(parsed, path=label)
    return copy.deepcopy(dict(parsed))


def condition_fields(condition: Mapping[str, Any] | None) -> set[str]:
    if not condition:
        return set()
    fields: set[str] = set()
    field = condition.get("field")
    if isinstance(field, str) and field.strip():
        fields.add(field.strip())
    value_field = condition.get("value_field")
    if isinstance(value_field, str) and value_field.strip():
        fields.add(value_field.strip())
    for nested in condition.get("conditions") or []:
        if isinstance(nested, Mapping):
            fields.update(condition_fields(nested))
    return fields


def _function_readiness(function: Any) -> CapabilityReadiness:
    reasons: list[str] = []
    try:
        function_definition_service.normalize_definition({
            field: getattr(function, field, None)
            for field in (
                "name", "description", "input_schema", "output_schema", "tags",
                "visibility", "runtime_kind", "runtime_config",
            )
        })
    except Exception as exc:  # noqa: BLE001 - malformed legacy rows stay unavailable.
        reasons.append(f"函数运行契约无效：{exc}")
    if str(getattr(function, "runtime_kind", "contract") or "contract") == "contract":
        reasons.append("函数仅定义了契约，尚未绑定受治理运行类型")
    return _dedupe(reasons)


def _action_readiness(
    action: Any,
    *,
    definition: Any | None,
    db: Session | None,
) -> CapabilityReadiness:
    reasons: list[str] = []
    if not bool(getattr(action, "enabled", False)):
        reasons.append("操作已停用")
    executor_type = str(getattr(action, "executor_type", "") or "")
    config = getattr(action, "executor_config", {}) or {}
    if executor_type not in _ACTION_EXECUTORS:
        reasons.append("操作执行器类型不受支持")
    elif executor_type == "unbound":
        reasons.append("操作尚未绑定执行器")
    if not isinstance(config, Mapping):
        reasons.append("操作执行配置不是对象")
        config = {}
    if definition is not None and bool(getattr(definition, "is_frozen", False)) and executor_type in {
        "http", "skill", "script", "template",
    }:
        reasons.append(f"{executor_type} 操作不能在冻结发布环境执行")
    if executor_type == "sql":
        if not str(config.get("sql") or "").strip():
            reasons.append("SQL 操作缺少 SQL 模板")
        if db is not None and definition is not None:
            try:
                runtime_connector_service.resolve_connector(
                    db,
                    definition.scenario,
                    kind="data_source",
                    config=config,
                    environment=definition.environment,
                    release_id=definition.release_id,
                )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"SQL 操作的数据源未就绪：{exc}")
        elif not any(config.get(key) for key in (
            "data_source_id", "data_source_binding_key", "data_source_binding_ref",
        )):
            reasons.append("SQL 操作缺少数据源绑定")
    elif executor_type == "mcp":
        if not str(config.get("tool_name") or "").strip():
            reasons.append("MCP 操作缺少工具名称")
        if db is not None and definition is not None:
            try:
                runtime_connector_service.resolve_connector(
                    db,
                    definition.scenario,
                    kind="mcp",
                    config=config,
                    environment=definition.environment,
                    release_id=definition.release_id,
                )
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"MCP 操作的连接器未就绪：{exc}")
        elif not any(config.get(key) for key in (
            "mcp_id", "mcp_binding_key", "mcp_binding_ref",
        )):
            reasons.append("MCP 操作缺少连接器绑定")
    elif executor_type == "http":
        url = str(config.get("url") or "").strip()
        if not url or urlparse(url).scheme not in {"http", "https"}:
            reasons.append("HTTP 操作缺少有效目标地址")
    elif executor_type == "skill":
        skill_id = str(config.get("skill_id") or "").strip()
        if not skill_id:
            reasons.append("Skill 操作缺少受管理的 skill_id")
        elif db is not None:
            skill = db.get(Skill, skill_id)
            if not skill or not bool(skill.enabled):
                reasons.append("Skill 操作引用的能力不存在或已停用")
    elif executor_type == "script":
        if not get_settings().allow_unsafe_workflow_nodes:
            reasons.append("脚本操作未在受控环境中启用")
        if not str(config.get("script") or "").strip():
            reasons.append("脚本操作缺少脚本定义")
    elif executor_type == "template":
        template_id = str(config.get("template_id") or "").strip()
        template_file_id = str(config.get("template_file_id") or "").strip()
        target_id = str(config.get("target_data_source_id") or "").strip()
        if not target_id or not (template_id or template_file_id):
            reasons.append("模板操作缺少源模板或附件目标")
        elif template_id:
            try:
                pinned_version = int(config.get("template_version"))
            except (TypeError, ValueError):
                pinned_version = 0
            pinned_hash = str(config.get("template_sha256") or "").strip()
            if pinned_version < 1 or not pinned_hash:
                reasons.append("模板操作未固定不可变版本和哈希")
            elif db is not None:
                template = db.get(ArtifactTemplate, template_id)
                scenario = db.get(
                    BusinessScenario, str(getattr(action, "scenario_id", "") or "")
                )
                if (
                    not template
                    or not scenario
                    or template.tenant_id != scenario.tenant_id
                    or template.scenario_id not in (None, scenario.id)
                ):
                    reasons.append("模板操作引用的目录模板不存在或越过场景边界")
                else:
                    version = db.query(ArtifactTemplateVersion).filter_by(
                        template_id=template.id, version=pinned_version
                    ).first()
                    if (
                        not version
                        or version.content_sha256 != pinned_hash
                        or not db.get(BucketFile, version.bucket_file_id)
                    ):
                        reasons.append("模板操作固定的版本不存在或哈希不一致")
        elif db is not None and not db.get(BucketFile, template_file_id):
            reasons.append("模板操作引用的源模板不存在")

    parsed_conditions: dict[str, dict[str, Any] | None] = {}
    for field, label in (
        ("precondition", "操作前置条件"),
        ("postcondition", "操作后置条件"),
    ):
        try:
            parsed_conditions[field] = normalize_structured_condition(
                getattr(action, field, ""), label=label
            )
        except CapabilityNotReady as exc:
            reasons.append(str(exc))
            parsed_conditions[field] = None
    precondition = parsed_conditions.get("precondition")
    if precondition:
        declared = set(
            str(name)
            for name in ((getattr(action, "input_schema", {}) or {}).get("properties") or {})
        )
        unknown = sorted(condition_fields(precondition) - declared)
        if unknown:
            reasons.append("操作前置条件引用了输入 Schema 之外的字段：" + "、".join(unknown))
    if definition is not None:
        entity_id = str(getattr(action, "entity_id", "") or "")
        if not entity_id or entity_id not in definition.entities:
            reasons.append("操作引用的对象类型不在当前运行定义中")
    return _dedupe(reasons)


def _rule_readiness(
    rule: Any,
    *,
    definition: Any | None,
    db: Session | None,
) -> CapabilityReadiness:
    reasons: list[str] = []
    if not bool(getattr(rule, "enabled", False)):
        reasons.append("规则已停用")
    try:
        normalize_structured_condition(getattr(rule, "condition", {}) or {}, label="规则条件")
    except CapabilityNotReady as exc:
        reasons.append(str(exc))
    if definition is not None:
        entity_id = str(getattr(rule, "entity_id", "") or "")
        if entity_id and entity_id not in definition.entities:
            reasons.append("规则引用的对象类型不在当前运行定义中")
        for action_id in dict.fromkeys(str(item) for item in (getattr(rule, "trigger_action_ids", []) or [])):
            action = definition.actions.get(action_id)
            if action is None:
                reasons.append(f"规则触发的操作 {action_id} 不在当前运行定义中")
                continue
            action_status = _action_readiness(action, definition=definition, db=db)
            if not action_status.executable:
                reasons.append(
                    f"规则触发的操作“{getattr(action, 'name', action_id)}”未就绪："
                    + "；".join(action_status.blocked_reasons)
                )
    return _dedupe(reasons)


def _event_readiness(event: Any) -> CapabilityReadiness:
    reasons: list[str] = []
    if not bool(getattr(event, "enabled", False)):
        reasons.append("事件已停用")
    schema = getattr(event, "payload_schema", {}) or {}
    if schema:
        try:
            function_definition_service.normalize_schema(schema, label="事件载荷 Schema")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"事件载荷契约无效：{exc}")
    return _dedupe(reasons)


def _workflow_references(workflow: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    key_by_kind = {"action": "action_id", "rule": "rule_id", "event": "event_id"}
    for step in getattr(workflow, "steps", []) or []:
        if not isinstance(step, Mapping):
            continue
        kind = str(step.get("type") or "")
        key = key_by_kind.get(kind)
        if key:
            result.append((kind, str(step.get(key) or "")))
    for node in getattr(workflow, "nodes", []) or []:
        if not isinstance(node, Mapping):
            continue
        kind = str(node.get("type") or "")
        key = key_by_kind.get(kind)
        data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
        if key:
            result.append((kind, str(data.get(key) or "")))
    return result


def _workflow_readiness(
    workflow: Any,
    *,
    definition: Any | None,
    db: Session | None,
) -> CapabilityReadiness:
    reasons: list[str] = []
    if not bool(getattr(workflow, "enabled", False)):
        reasons.append("工作流已停用")
    if str(getattr(workflow, "status", "draft") or "draft") != "active":
        reasons.append("工作流尚未激活")
    nodes = list(getattr(workflow, "nodes", []) or [])
    steps = list(getattr(workflow, "steps", []) or [])
    if not nodes and not steps:
        reasons.append("工作流没有可执行节点")
    if nodes:
        try:
            validate_workflow_graph(nodes, list(getattr(workflow, "edges", []) or []))
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"工作流图无效：{exc}")
    if definition is not None:
        resources = {
            "action": definition.actions,
            "rule": definition.rules,
            "event": definition.events,
        }
        readiness = {
            "action": _action_readiness,
            "rule": _rule_readiness,
            "event": lambda item, **_kwargs: _event_readiness(item),
        }
        for kind, resource_id in _workflow_references(workflow):
            resource = resources[kind].get(resource_id) if resource_id else None
            if resource is None:
                reasons.append(f"工作流的 {kind} 节点缺少当前运行定义中的资源")
                continue
            status = readiness[kind](resource, definition=definition, db=db)
            if not status.executable:
                reasons.append(
                    f"工作流引用的 {kind} 能力未就绪："
                    + "；".join(status.blocked_reasons)
                )
        if str(getattr(workflow, "trigger_type", "manual") or "manual") == "event":
            event_id = str((getattr(workflow, "trigger_config", {}) or {}).get("event_id") or "")
            event = definition.events.get(event_id) if event_id else None
            if event is None:
                reasons.append("事件触发工作流缺少当前运行定义中的触发事件")
            else:
                event_status = _event_readiness(event)
                if not event_status.executable:
                    reasons.append("工作流触发事件未就绪：" + "；".join(event_status.blocked_reasons))
    return _dedupe(reasons)


def capability_readiness(
    kind: str,
    resource: Any,
    *,
    definition: Any | None = None,
    db: Session | None = None,
) -> CapabilityReadiness:
    handlers = {
        "function": lambda: _function_readiness(resource),
        "action": lambda: _action_readiness(resource, definition=definition, db=db),
        "rule": lambda: _rule_readiness(resource, definition=definition, db=db),
        "event": lambda: _event_readiness(resource),
        "workflow": lambda: _workflow_readiness(resource, definition=definition, db=db),
    }
    handler = handlers.get(str(kind))
    if handler is None:
        return _dedupe(["不受支持的能力类型"])
    return handler()


def require_executable(
    kind: str,
    resource: Any,
    *,
    definition: Any | None = None,
    db: Session | None = None,
) -> CapabilityReadiness:
    readiness = capability_readiness(kind, resource, definition=definition, db=db)
    if not readiness.executable:
        label = str(getattr(resource, "name", "") or getattr(resource, "id", "") or kind)
        raise CapabilityNotReady(
            f"能力“{label}”尚不可执行：" + "；".join(readiness.blocked_reasons)
        )
    return readiness
