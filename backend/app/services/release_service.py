"""P2 本体发布治理服务。

本模块把本体定义的变更限定在 ``分支 -> 不可变快照 -> 提案 -> 评审 -> 明确确认的
合并`` 闭环中。运行时对象、执行记录以及外部凭据均不属于治理快照：前两者不能因
发布而被静默删除，后者永不写入快照或 API 响应。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..models import (
    ActionExecutionLog,
    BusinessScenario,
    DataMapping,
    DataSource,
    EventEnvelope,
    FunctionDefinition,
    OntologyAction,
    OntologyBranch,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProposal,
    OntologyRelease,
    OntologyRelation,
    OntologyReview,
    OntologyRollback,
    OntologyRule,
    OntologySnapshot,
    OntologyWorkflow,
    OntologyProperty,
    RelationInstance,
    WorkflowRun,
)
from . import (
    connector_service,
    function_definition_service,
    ontology_service,
    permission_service,
)
from .policies import validate_workflow_graph


ENVIRONMENTS = {"dev", "staging", "prod"}
MERGEABLE_SNAPSHOT_KINDS = {"baseline", "merge", "rollback"}
ROLLBACKABLE_SNAPSHOT_KINDS = {"baseline", "merge", "rollback", "pre_merge", "pre_rollback"}
# These runs resolve the live workflow definition in the worker.  Publishing a
# changed definition while one is pending would make the persisted run execute a
# different plan from the one the caller originally approved.
NONTERMINAL_WORKFLOW_RUN_STATUSES = {"queued", "running", "awaiting_approval", "retry_waiting"}
_SECRET_MARKER_KEY = "__release_secret__"
_SECRET_MARKER = {_SECRET_MARKER_KEY: "preserve"}
_SECRET_KEY_PARTS = {
    "password",
    "passwd",
    "apikey",
    "api_key",
    "token",
    "secret",
    "access_token",
    "authorization",
    "credential",
    "credentials",
    "private_key",
    "client_secret",
    "access_key",
    "bearer",
    "connection_string",
    "connection_url",
    "database_url",
    "dsn",
}
_SECRET_STRING_PATTERNS = (
    # Common Authorization/header values, including nested config under an arbitrary key.
    re.compile(r"\b(?:bearer|basic)\s+[a-z0-9._~+/=-]{6,}", re.IGNORECASE),
    # Embedded ``key=value`` / JSON-like ``\"token\": \"...\"`` strings.
    re.compile(
        r"(?:api[_-]?key|access[_-]?token|token|client[_-]?secret|password|passwd|"
        r"secret|credential|authorization)\s*(?:=|:)\s*[^\s,;]+",
        re.IGNORECASE,
    ),
    # Credential-bearing database/HTTP connection URLs.  A redaction is safer than
    # trying to preserve the endpoint portion in an immutable review artifact.
    re.compile(r"://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----", re.IGNORECASE),
    # Widely used raw key/token forms when config does not label the value.
    re.compile(r"\b(?:sk|rk|pk)_[a-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[a-z0-9_-]{12,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{6,}\b", re.IGNORECASE),
)


class ReleaseValidationError(ValueError):
    """提交内容不满足可安全发布的本体快照约束。"""


class ReleaseConflictError(ReleaseValidationError):
    """并发/状态冲突；调用方需要重新基于最新分支创建提案。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def snapshot_hash(content: dict) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _secret_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    collapsed = normalized.replace("_", "")
    return (
        normalized in _SECRET_KEY_PARTS
        or collapsed in {"apikey", "accesstoken", "clientsecret", "privatekey"}
        or "password" in normalized
        or "token" in normalized
        or "secret" in normalized
        or "authorization" in normalized
        or "credential" in normalized
    )


def _is_marker(value: Any) -> bool:
    return isinstance(value, dict) and value == _SECRET_MARKER


def _looks_like_secret_string(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_STRING_PATTERNS)


def _contains_marker(value: Any) -> bool:
    if _is_marker(value):
        return True
    if isinstance(value, dict):
        return any(_contains_marker(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_marker(child) for child in value)
    return False


def _sanitize_secret_values(value: Any, *, key: str = "") -> Any:
    """递归替换可疑凭据，保证快照和响应中没有真实 secret。"""
    # Keep the marker idempotent.  A proposal is commonly built from a previously
    # returned snapshot, so re-sanitising must not turn it into nested markers.
    if _is_marker(value):
        return copy.deepcopy(_SECRET_MARKER)
    if _secret_key(key):
        return copy.deepcopy(_SECRET_MARKER)
    if isinstance(value, str) and _looks_like_secret_string(value):
        return copy.deepcopy(_SECRET_MARKER)
    if isinstance(value, dict):
        return {str(child_key): _sanitize_secret_values(child, key=str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_secret_values(child) for child in value]
    return copy.deepcopy(value)


def safe_snapshot_content(content: Any) -> dict:
    """Return an API-safe representation of persisted snapshot content.

    ``OntologySnapshot.content`` is written through :func:`normalize_snapshot_content`,
    but this extra defensive pass is deliberately performed at the API boundary too.
    It prevents a manually seeded/legacy row from ever exposing Action, MCP or data
    connector credentials when a snapshot is read back.
    """
    if not isinstance(content, dict):
        return {}
    sanitized = _sanitize_secret_values(content)
    return sanitized if isinstance(sanitized, dict) else {}


def _secret_subtree(value: Any) -> Any | None:
    """从旧配置中抽取需保留的凭据枝，不携带无关普通字段。"""
    if isinstance(value, str) and _looks_like_secret_string(value):
        return copy.deepcopy(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, child in value.items():
            if _secret_key(key):
                out[str(key)] = copy.deepcopy(child)
            else:
                nested = _secret_subtree(child)
                if nested not in (None, {}, []):
                    out[str(key)] = nested
        return out or None
    if isinstance(value, list):
        nested_items = [_secret_subtree(child) for child in value]
        if any(item not in (None, {}, []) for item in nested_items):
            return nested_items
    return None


def _preserve_secrets(existing: Any, incoming: Any, *, key: str = "") -> Any:
    """把占位或缺失的敏感字段从当前已保存配置中恢复。

    该规则在 merge 与 rollback 均生效：治理快照无法也不应该成为凭据备份，因此不论
    目标快照多旧，都只能保留当前受凭据管理保护的真实值，绝不能置空。
    """
    # Markers can originate from a sensitive *value* nested below a neutral key,
    # not only from a key called ``token``/``password``.  In both cases the old
    # live value is the only valid source of truth.
    if _is_marker(incoming):
        if existing is not None:
            return copy.deepcopy(existing)
        raise ReleaseValidationError("新资源的敏感配置不能通过发布快照设置")

    if _secret_key(key):
        if existing is not None and (_is_marker(incoming) or incoming is None or incoming == ""):
            return copy.deepcopy(existing)
        # 所有入库快照会先去敏；若调用路径传入了原值，也去敏语义为保留旧值。
        if existing is not None:
            return copy.deepcopy(existing)
        if _is_marker(incoming) or incoming is None or incoming == "":
            raise ReleaseValidationError("新资源的敏感配置不能通过发布快照设置")
        raise ReleaseValidationError("发布快照不能包含敏感配置")

    if isinstance(existing, dict) and isinstance(incoming, dict):
        result: dict[str, Any] = {str(name): copy.deepcopy(value) for name, value in incoming.items()}
        for name, old_value in existing.items():
            name = str(name)
            if _secret_key(name):
                result[name] = _preserve_secrets(old_value, incoming.get(name), key=name)
                continue
            if name in incoming:
                result[name] = _preserve_secrets(old_value, incoming[name], key=name)
                continue
            nested = _secret_subtree(old_value)
            if nested not in (None, {}, []):
                result[name] = nested
        return result
    if isinstance(existing, list) and isinstance(incoming, list):
        # 工作流 nodes/steps 等列表以索引保留同位敏感配置；长度变化时新节点不可携带
        # marker，旧节点未匹配到的 secret 不会被错误复制到新节点。
        result: list[Any] = []
        for index, value in enumerate(incoming):
            old_value = existing[index] if index < len(existing) else None
            result.append(_preserve_secrets(old_value, value, key=key))
        return result
    if existing is None and _contains_marker(incoming):
        raise ReleaseValidationError("新资源的敏感配置不能通过发布快照设置")
    return copy.deepcopy(incoming)


def _required_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 32:
        raise ReleaseValidationError(f"{label} 必须是 1-32 位稳定 id")
    return value.strip()


def _string(value: Any, label: str, *, default: str = "", maximum: int = 20_000) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ReleaseValidationError(f"{label} 必须是字符串")
    if len(value) > maximum:
        raise ReleaseValidationError(f"{label} 长度不能超过 {maximum}")
    return value


def _dict(value: Any, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReleaseValidationError(f"{label} 必须是对象")
    return copy.deepcopy(value)


def _list(value: Any, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ReleaseValidationError(f"{label} 必须是数组")
    return copy.deepcopy(value)


def _bool(value: Any, label: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ReleaseValidationError(f"{label} 必须是布尔值")
    return value


def _normalize_property(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("属性必须是对象")
    data_type = _string(raw.get("data_type"), "属性类型", default="string", maximum=50)
    try:
        constraints = ontology_service.normalize_property_constraints(
            data_type,
            _dict(raw.get("constraints"), "属性约束"),
        )
    except ValueError as exc:
        raise ReleaseValidationError(str(exc)) from exc
    default_probe = SimpleNamespace(
        name=str(raw.get("name") or ""),
        data_type=data_type,
        default_value=copy.deepcopy(raw.get("default_value", "")),
        constraints=constraints,
        is_required=_bool(raw.get("is_required"), "属性 is_required"),
        is_enum=_bool(raw.get("is_enum"), "属性 is_enum"),
        enum_values=_list(raw.get("enum_values"), "属性枚举值"),
    )
    try:
        default_value = ontology_service.normalize_property_default(default_probe)
    except ValueError as exc:
        raise ReleaseValidationError(str(exc)) from exc
    return {
        "id": _required_id(raw.get("id"), "属性 id"),
        "name": _string(raw.get("name"), "属性名称", maximum=200),
        "data_type": data_type,
        "description": _string(raw.get("description"), "属性说明"),
        "is_key": _bool(raw.get("is_key"), "属性 is_key"),
        "is_required": default_probe.is_required,
        "is_enum": default_probe.is_enum,
        "enum_values": default_probe.enum_values,
        "default_value": default_value,
        "constraints": constraints,
        "is_sensitive": _bool(raw.get("is_sensitive"), "属性 is_sensitive"),
    }


def _normalize_entity(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("实体必须是对象")
    properties = [_normalize_property(item) for item in _list(raw.get("properties"), "实体属性")]
    property_ids = [item["id"] for item in properties]
    if len(set(property_ids)) != len(property_ids):
        raise ReleaseValidationError("同一实体中不能重复属性 id")
    state_property = _string(
        raw.get("state_property"), "实体状态属性", maximum=200
    )
    if state_property:
        state_definition = next(
            (item for item in properties if item["name"] == state_property),
            None,
        )
        if not state_definition or not state_definition["is_enum"]:
            raise ReleaseValidationError("实体状态属性必须引用当前实体的枚举属性")
    try:
        namespace = ontology_service.validate_namespace(
            _string(raw.get("namespace"), "实体命名空间", default="default", maximum=180)
        )
    except ValueError as exc:
        raise ReleaseValidationError(str(exc)) from exc
    return {
        "id": _required_id(raw.get("id"), "实体 id"),
        "name": _string(raw.get("name"), "实体名称", maximum=200),
        "namespace": namespace,
        "description": _string(raw.get("description"), "实体说明"),
        "icon": _string(raw.get("icon"), "实体图标", default="box", maximum=50),
        "color": _string(raw.get("color"), "实体颜色", default="#4f46e5", maximum=20),
        "is_abstract": _bool(raw.get("is_abstract"), "实体 is_abstract"),
        "state_property": state_property,
        "properties": properties,
    }


def _normalize_relation(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("关系必须是对象")
    relation_type = _string(raw.get("relation_type"), "关系类型", default="1:N", maximum=10)
    if relation_type not in {"1:1", "1:N", "N:1", "N:M"}:
        raise ReleaseValidationError("关系类型必须为 1:1、1:N、N:1 或 N:M")
    try:
        namespace = ontology_service.validate_namespace(
            _string(raw.get("namespace"), "关系命名空间", default="default", maximum=180)
        )
    except ValueError as exc:
        raise ReleaseValidationError(str(exc)) from exc
    return {
        "id": _required_id(raw.get("id"), "关系 id"),
        "name": _string(raw.get("name"), "关系名称", maximum=200),
        "namespace": namespace,
        "source_entity_id": _required_id(raw.get("source_entity_id"), "关系源实体 id"),
        "target_entity_id": _required_id(raw.get("target_entity_id"), "关系目标实体 id"),
        "relation_type": relation_type,
        "description": _string(raw.get("description"), "关系说明"),
    }


def _normalize_mapping(raw: Any) -> dict:
    """Normalize the declarative part of a data mapping.

    Runtime health and import counters deliberately do not live in release
    snapshots.  A release can add or update the ontology-facing binding while the
    connector keeps its own operational history and credentials.
    """
    if not isinstance(raw, dict):
        raise ReleaseValidationError("数据映射必须是对象")
    normalized = {
        "id": _required_id(raw.get("id"), "数据映射 id"),
        "entity_id": _required_id(raw.get("entity_id"), "数据映射实体 id"),
        "data_source_id": _required_id(raw.get("data_source_id"), "数据映射数据源 id"),
        "data_source_binding_key": "",
        "data_source_binding_ref": {},
        "table_name": _string(raw.get("table_name"), "数据映射表名", maximum=300),
        "column_map": _sanitize_secret_values(_dict(raw.get("column_map"), "数据映射字段")),
        "transform_rules": _sanitize_secret_values(
            _dict(raw.get("transform_rules"), "数据映射转换规则")
        ),
    }
    try:
        binding = connector_service.runtime_binding_from_config(raw, "data_source")
    except connector_service.ConnectorBindingError as exc:
        raise ReleaseValidationError(f"数据映射运行时绑定配置无效：{exc}") from exc
    if binding is not None:
        key_field, ref_field = connector_service.runtime_binding_fields("data_source")
        normalized[key_field] = binding["binding_key"]
        # A mapping always executes a SELECT.  Record that requirement at the
        # declarative boundary so release and runtime both reject a healthy but
        # non-SQL target such as a file bucket.
        normalized[ref_field] = connector_service.with_required_capabilities(
            binding["reference"], "sql_read"
        )
    return normalized


def _normalize_function(raw: Any) -> dict:
    """Normalize a typed function plus its safe built-in runtime descriptor."""
    if not isinstance(raw, dict):
        raise ReleaseValidationError("函数定义必须是对象")
    try:
        declaration = function_definition_service.normalize_definition(
            {key: value for key, value in raw.items() if key != "id"}
        )
    except function_definition_service.FunctionDefinitionError as exc:
        raise ReleaseValidationError(f"函数定义无效：{exc}") from exc
    return {
        "id": _required_id(raw.get("id"), "函数定义 id"),
        **declaration,
    }


def _normalize_action(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("Action 必须是对象")
    executor_type = _string(raw.get("executor_type"), "Action 执行器类型", default="sql", maximum=30)
    if executor_type not in {"sql", "skill", "mcp", "http", "script"}:
        raise ReleaseValidationError("Action 执行器类型无效")
    access_scope = _string(raw.get("access_scope"), "Action 访问范围", default="tenant", maximum=20)
    if access_scope not in {"tenant", "restricted"}:
        raise ReleaseValidationError("Action 访问范围无效")
    return {
        "id": _required_id(raw.get("id"), "Action id"),
        "entity_id": _required_id(raw.get("entity_id"), "Action 实体 id"),
        "name": _string(raw.get("name"), "Action 名称", maximum=200),
        "description": _string(raw.get("description"), "Action 说明"),
        "input_schema": _sanitize_secret_values(
            _dict(raw.get("input_schema"), "Action 输入 schema")
        ),
        "executor_type": executor_type,
        "executor_config": _sanitize_secret_values(
            _dict(raw.get("executor_config"), "Action 执行配置")
        ),
        "precondition": _string(raw.get("precondition"), "Action 前置条件"),
        "postcondition": _string(raw.get("postcondition"), "Action 后置效果"),
        "enabled": _bool(raw.get("enabled"), "Action enabled", default=True),
        "requires_confirmation": _bool(
            raw.get("requires_confirmation"), "Action requires_confirmation", default=True
        ),
        "idempotency_required": _bool(
            raw.get("idempotency_required"), "Action idempotency_required", default=True
        ),
        "permission_scope": _string(
            raw.get("permission_scope"), "Action permission_scope", default="scenario", maximum=30
        ),
        "access_scope": access_scope,
    }


def _normalize_rule(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("规则必须是对象")
    entity_id = raw.get("entity_id")
    if entity_id is not None:
        entity_id = _required_id(entity_id, "规则实体 id")
    severity = _string(raw.get("severity"), "规则严重度", default="info", maximum=20)
    if severity not in {"info", "warning", "critical"}:
        raise ReleaseValidationError("规则严重度无效")
    trigger_action_ids = [_required_id(item, "规则触发 Action id") for item in _list(raw.get("trigger_action_ids"), "规则触发 Actions")]
    return {
        "id": _required_id(raw.get("id"), "规则 id"),
        "entity_id": entity_id,
        "name": _string(raw.get("name"), "规则名称", maximum=200),
        "description": _string(raw.get("description"), "规则说明"),
        "condition": _sanitize_secret_values(_dict(raw.get("condition"), "规则条件")),
        "action_on_match": _string(raw.get("action_on_match"), "规则命中动作"),
        "trigger_action_ids": trigger_action_ids,
        "severity": severity,
        "enabled": _bool(raw.get("enabled"), "规则 enabled", default=True),
    }


def _normalize_event(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("事件必须是对象")
    return {
        "id": _required_id(raw.get("id"), "事件 id"),
        "name": _string(raw.get("name"), "事件名称", maximum=200),
        "description": _string(raw.get("description"), "事件说明"),
        "payload_schema": _sanitize_secret_values(
            _dict(raw.get("payload_schema"), "事件 payload schema")
        ),
        "trigger_source": _string(raw.get("trigger_source"), "事件触发来源"),
        "enabled": _bool(raw.get("enabled"), "事件 enabled", default=True),
    }


def _normalize_workflow(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ReleaseValidationError("工作流必须是对象")
    trigger_type = _string(raw.get("trigger_type"), "工作流触发方式", default="manual", maximum=30)
    if trigger_type not in {"manual", "scheduled", "event"}:
        raise ReleaseValidationError("工作流触发方式无效")
    status = _string(raw.get("status"), "工作流状态", default="draft", maximum=20)
    if status not in {"draft", "active", "disabled"}:
        raise ReleaseValidationError("工作流状态无效")
    access_scope = _string(raw.get("access_scope"), "工作流访问范围", default="tenant", maximum=20)
    if access_scope not in {"tenant", "restricted"}:
        raise ReleaseValidationError("工作流访问范围无效")
    return {
        "id": _required_id(raw.get("id"), "工作流 id"),
        "name": _string(raw.get("name"), "工作流名称", maximum=200),
        "description": _string(raw.get("description"), "工作流说明"),
        "trigger_type": trigger_type,
        "trigger_config": _sanitize_secret_values(_dict(raw.get("trigger_config"), "工作流触发配置")),
        "steps": _sanitize_secret_values(_list(raw.get("steps"), "工作流步骤")),
        "nodes": _sanitize_secret_values(_list(raw.get("nodes"), "工作流节点")),
        "edges": _sanitize_secret_values(_list(raw.get("edges"), "工作流连线")),
        "status": status,
        "enabled": _bool(raw.get("enabled"), "工作流 enabled", default=True),
        "access_scope": access_scope,
    }


def _runtime_binding_requirements(
    mappings: list[dict], actions: list[dict], workflows: list[dict]
) -> list[dict[str, str]]:
    """Promote declarative runtime keys into release-gated dependencies.

    Package imports already add top-level ``connector_bindings``.  This scan
    closes the equivalent gap for an Action or DAG node configured manually:
    no staging/prod runtime key can bypass the publish-time binding check.
    """
    requirements: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for kind in ("data_source", "mcp", "llm"):
                try:
                    metadata = connector_service.runtime_binding_from_config(value, kind)
                except connector_service.ConnectorBindingError as exc:
                    raise ReleaseValidationError(str(exc)) from exc
                if metadata is None:
                    continue
                identity = (str(metadata["kind"]), str(metadata["binding_key"]))
                if identity not in seen:
                    seen.add(identity)
                    requirements.append(
                        {
                            "binding_key": identity[1],
                            "kind": identity[0],
                            # The requested target environment is applied by
                            # publish/runtime validation; this is a logical
                            # default retained for snapshot compatibility.
                            "environment": "dev",
                            "reference_label": f"运行时连接器：{identity[1]}",
                        }
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for mapping in mappings:
        visit(
            {
                "data_source_binding_key": mapping.get("data_source_binding_key"),
                "data_source_binding_ref": mapping.get("data_source_binding_ref"),
            }
        )
    for action in actions:
        visit(action.get("executor_config") or {})
    for workflow in workflows:
        visit(workflow.get("trigger_config") or {})
        visit(workflow.get("steps") or [])
        visit(workflow.get("nodes") or [])
    return requirements


def normalize_snapshot_content(content: Any) -> dict:
    """验证并规范化完整本体定义，同时在持久化前剥离所有敏感值。"""
    if not isinstance(content, dict):
        raise ReleaseValidationError("本体快照必须是对象")
    scenario_raw = content.get("scenario")
    if not isinstance(scenario_raw, dict):
        raise ReleaseValidationError("本体快照必须包含 scenario")
    entities = [_normalize_entity(item) for item in _list(content.get("entities"), "实体列表")]
    relations = [_normalize_relation(item) for item in _list(content.get("relations"), "关系列表")]
    # ``mappings`` was added after the first release-governance slice.  Keep
    # legacy snapshots readable: an omitted collection means “do not touch
    # mappings” when that snapshot is later applied, rather than “delete all”.
    mappings_present = "mappings" in content
    mappings = [
        _normalize_mapping(item)
        for item in _list(content.get("mappings") if mappings_present else [], "数据映射列表")
    ]
    # Functions were added after the initial release-governance rollout.  As
    # with mappings, an omitted legacy collection means "leave live functions
    # unchanged" during a merge, never "delete every function".
    functions_present = "functions" in content
    functions = [
        _normalize_function(item)
        for item in _list(content.get("functions") if functions_present else [], "函数定义列表")
    ]
    actions = [_normalize_action(item) for item in _list(content.get("actions"), "Action 列表")]
    rules = [_normalize_rule(item) for item in _list(content.get("rules"), "规则列表")]
    events = [_normalize_event(item) for item in _list(content.get("events"), "事件列表")]
    workflows = [_normalize_workflow(item) for item in _list(content.get("workflows"), "工作流列表")]
    runtime_requirements = _runtime_binding_requirements(mappings, actions, workflows)
    connector_bindings_present = "connector_bindings" in content or bool(runtime_requirements)
    raw_requirements = content.get("connector_bindings") if "connector_bindings" in content else []
    if raw_requirements is None:
        raw_requirements = []
    if isinstance(raw_requirements, list):
        existing_runtime_keys = {
            (str(item.get("kind") or ""), str(item.get("binding_key") or ""))
            for item in raw_requirements
            if isinstance(item, dict)
        }
        combined_requirements: Any = [
            *raw_requirements,
            *[
                item
                for item in runtime_requirements
                if (item["kind"], item["binding_key"]) not in existing_runtime_keys
            ],
        ]
    else:
        # Preserve the original invalid value so the shared normalizer returns
        # its usual useful validation error instead of silently discarding it.
        combined_requirements = raw_requirements
    try:
        connector_bindings = connector_service.normalize_snapshot_binding_requirements(
            combined_requirements if connector_bindings_present else None
        )
    except connector_service.ConnectorBindingError as exc:
        raise ReleaseValidationError(str(exc)) from exc

    entity_ids = [item["id"] for item in entities]
    if len(set(entity_ids)) != len(entity_ids):
        raise ReleaseValidationError("实体 id 不能重复")
    all_property_ids = [prop["id"] for entity in entities for prop in entity["properties"]]
    if len(set(all_property_ids)) != len(all_property_ids):
        raise ReleaseValidationError("属性 id 不能跨实体重复")
    for label, items in {
        "数据映射": mappings,
        "函数": functions,
        "关系": relations,
        "Action": actions,
        "规则": rules,
        "事件": events,
        "工作流": workflows,
    }.items():
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            raise ReleaseValidationError(f"{label} id 不能重复")

    entity_id_set = set(entity_ids)
    action_id_set = {item["id"] for item in actions}
    rule_id_set = {item["id"] for item in rules}
    event_id_set = {item["id"] for item in events}
    for relation in relations:
        if relation["source_entity_id"] not in entity_id_set or relation["target_entity_id"] not in entity_id_set:
            raise ReleaseValidationError("关系引用了不存在的实体")
    for mapping in mappings:
        if mapping["entity_id"] not in entity_id_set:
            raise ReleaseValidationError("数据映射引用了不存在的实体")
        entity_data = next(item for item in entities if item["id"] == mapping["entity_id"])
        try:
            mapping["transform_rules"] = ontology_service.normalize_transform_rules(
                SimpleNamespace(
                    properties=[
                        SimpleNamespace(name=prop["name"])
                        for prop in entity_data["properties"]
                    ]
                ),
                mapping.get("transform_rules"),
            )
        except ValueError as exc:
            raise ReleaseValidationError(str(exc)) from exc
    for action in actions:
        if action["entity_id"] not in entity_id_set:
            raise ReleaseValidationError("Action 引用了不存在的实体")
    for rule in rules:
        if rule["entity_id"] and rule["entity_id"] not in entity_id_set:
            raise ReleaseValidationError("规则引用了不存在的实体")
        if any(action_id not in action_id_set for action_id in rule["trigger_action_ids"]):
            raise ReleaseValidationError("规则引用了不存在的 Action")
    for workflow in workflows:
        _validate_workflow_references(workflow, action_id_set, rule_id_set, event_id_set)
        if workflow["nodes"]:
            try:
                validate_workflow_graph(workflow["nodes"], workflow["edges"])
            except Exception as exc:  # noqa: BLE001
                raise ReleaseValidationError(f"工作流图校验失败: {exc}") from exc

    try:
        scenario_namespace = ontology_service.validate_namespace(
            _string(
                scenario_raw.get("namespace"),
                "场景命名空间",
                default="default",
                maximum=180,
            )
        )
    except ValueError as exc:
        raise ReleaseValidationError(str(exc)) from exc
    normalized = {
        "schema_version": 1,
        "scenario": {
            "name": _string(scenario_raw.get("name"), "场景名称", maximum=200),
            "description": _string(scenario_raw.get("description"), "场景说明"),
            "industry": _string(scenario_raw.get("industry"), "场景行业", maximum=100),
            "namespace": scenario_namespace,
            "status": _string(scenario_raw.get("status"), "场景状态", default="draft", maximum=20),
        },
        "entities": entities,
        "relations": relations,
        "actions": actions,
        "rules": rules,
        "events": events,
        "workflows": workflows,
    }
    if mappings_present:
        normalized["mappings"] = mappings
    if functions_present:
        normalized["functions"] = functions
    if connector_bindings_present:
        normalized["connector_bindings"] = connector_bindings
    return normalized


def _validate_workflow_references(
    workflow: dict,
    action_ids: set[str],
    rule_ids: set[str],
    event_ids: set[str],
) -> None:
    refs = [(step.get("type"), step) for step in workflow["steps"] if isinstance(step, dict)]
    refs.extend(
        (node.get("type"), node.get("data") or node)
        for node in workflow["nodes"]
        if isinstance(node, dict)
    )
    for kind, data in refs:
        if not isinstance(data, dict):
            continue
        key, ids = {
            "action": ("action_id", action_ids),
            "rule": ("rule_id", rule_ids),
            "event": ("event_id", event_ids),
        }.get(str(kind), ("", set()))
        if key and data.get(key) and str(data[key]) not in ids:
            raise ReleaseValidationError(f"工作流引用了不存在的 {kind}")


def capture_snapshot_content(db: Session, scenario: BusinessScenario) -> dict:
    """捕获当前可发布的本体定义，且在内存中即去敏。"""
    entities = db.execute(
        select(OntologyEntity)
        .options(joinedload(OntologyEntity.properties))
        .where(OntologyEntity.scenario_id == scenario.id)
        .order_by(OntologyEntity.id.asc())
    ).scalars().unique().all()
    content = {
        "scenario": {
            "name": scenario.name,
            "description": scenario.description or "",
            "industry": scenario.industry or "",
            "namespace": scenario.namespace or "default",
            "status": scenario.status or "draft",
        },
        "entities": [
            {
                "id": entity.id,
                "name": entity.name,
                "namespace": entity.namespace or scenario.namespace or "default",
                "description": entity.description or "",
                "icon": entity.icon or "box",
                "color": entity.color or "#4f46e5",
                "is_abstract": bool(entity.is_abstract),
                "state_property": entity.state_property or "",
                "properties": [
                    {
                        "id": prop.id,
                        "name": prop.name,
                        "data_type": prop.data_type or "string",
                        "description": prop.description or "",
                        "is_key": bool(prop.is_key),
                        "is_required": bool(prop.is_required),
                        "is_enum": bool(prop.is_enum),
                        "enum_values": prop.enum_values or [],
                        "default_value": prop.default_value or "",
                        "constraints": copy.deepcopy(prop.constraints or {}),
                        "is_sensitive": bool(prop.is_sensitive),
                    }
                    for prop in sorted(entity.properties, key=lambda item: item.id)
                ],
            }
            for entity in entities
        ],
        "relations": [
            {
                "id": relation.id,
                "name": relation.name,
                "namespace": relation.namespace or scenario.namespace or "default",
                "source_entity_id": relation.source_entity_id,
                "target_entity_id": relation.target_entity_id,
                "relation_type": relation.relation_type or "1:N",
                "description": relation.description or "",
            }
            for relation in db.execute(
                select(OntologyRelation)
                .where(OntologyRelation.scenario_id == scenario.id)
                .order_by(OntologyRelation.id.asc())
            ).scalars().all()
        ],
        # A mapping definition is governed, while its health, counters and the
        # data-source credential are operational state.  Capture only the former.
        "mappings": [
            {
                "id": mapping.id,
                "entity_id": mapping.entity_id,
                "data_source_id": mapping.data_source_id,
                "data_source_binding_key": mapping.data_source_binding_key or "",
                "data_source_binding_ref": _sanitize_secret_values(
                    mapping.data_source_binding_ref or {}
                ),
                "table_name": mapping.table_name or "",
                "column_map": _sanitize_secret_values(mapping.column_map or {}),
                "transform_rules": _sanitize_secret_values(mapping.transform_rules or {}),
            }
            for mapping in db.execute(
                select(DataMapping)
                .where(DataMapping.scenario_id == scenario.id)
                .order_by(DataMapping.id.asc())
            ).scalars().all()
        ],
        # Function definitions are typed, declarative contracts.  There is no
        # executable implementation to capture or preserve in a release.
        "functions": [
            {
                "id": function.id,
                "name": function.name,
                "description": function.description or "",
                "input_schema": copy.deepcopy(function.input_schema or {}),
                "output_schema": copy.deepcopy(function.output_schema or {}),
                "tags": copy.deepcopy(function.tags or []),
                "visibility": function.visibility or "scenario",
                "runtime_kind": function.runtime_kind or "contract",
                "runtime_config": copy.deepcopy(function.runtime_config or {}),
            }
            for function in db.execute(
                select(FunctionDefinition)
                .where(FunctionDefinition.scenario_id == scenario.id)
                .order_by(FunctionDefinition.id.asc())
            ).scalars().all()
        ],
        "actions": [
            {
                "id": action.id,
                "entity_id": action.entity_id,
                "name": action.name,
                "description": action.description or "",
                "input_schema": action.input_schema or {},
                "executor_type": action.executor_type or "sql",
                "executor_config": _sanitize_secret_values(action.executor_config or {}),
                "precondition": action.precondition or "",
                "postcondition": action.postcondition or "",
                "enabled": bool(action.enabled),
                "requires_confirmation": bool(action.requires_confirmation),
                "idempotency_required": bool(action.idempotency_required),
                "permission_scope": action.permission_scope or "scenario",
                "access_scope": action.access_scope or "tenant",
            }
            for action in db.execute(
                select(OntologyAction)
                .where(OntologyAction.scenario_id == scenario.id)
                .order_by(OntologyAction.id.asc())
            ).scalars().all()
        ],
        "rules": [
            {
                "id": rule.id,
                "entity_id": rule.entity_id,
                "name": rule.name,
                "description": rule.description or "",
                "condition": _sanitize_secret_values(rule.condition or {}),
                "action_on_match": rule.action_on_match or "",
                "trigger_action_ids": rule.trigger_action_ids or [],
                "severity": rule.severity or "info",
                "enabled": bool(rule.enabled),
            }
            for rule in db.execute(
                select(OntologyRule)
                .where(OntologyRule.scenario_id == scenario.id)
                .order_by(OntologyRule.id.asc())
            ).scalars().all()
        ],
        "events": [
            {
                "id": event.id,
                "name": event.name,
                "description": event.description or "",
                "payload_schema": event.payload_schema or {},
                "trigger_source": event.trigger_source or "",
                "enabled": bool(event.enabled),
            }
            for event in db.execute(
                select(OntologyEvent)
                .where(OntologyEvent.scenario_id == scenario.id)
                .order_by(OntologyEvent.id.asc())
            ).scalars().all()
        ],
        "workflows": [
            {
                "id": workflow.id,
                "name": workflow.name,
                "description": workflow.description or "",
                "trigger_type": workflow.trigger_type or "manual",
                "trigger_config": _sanitize_secret_values(workflow.trigger_config or {}),
                "steps": _sanitize_secret_values(workflow.steps or []),
                "nodes": _sanitize_secret_values(workflow.nodes or []),
                "edges": _sanitize_secret_values(workflow.edges or []),
                "status": workflow.status or ("active" if workflow.enabled else "disabled"),
                "enabled": bool(workflow.enabled),
                "access_scope": workflow.access_scope or "tenant",
            }
            for workflow in db.execute(
                select(OntologyWorkflow)
                .where(OntologyWorkflow.scenario_id == scenario.id)
                .order_by(OntologyWorkflow.id.asc())
            ).scalars().all()
        ],
    }
    return normalize_snapshot_content(content)


def _scenario_for_read(db: Session, scenario_id: str) -> tuple[BusinessScenario, permission_service.Principal]:
    principal = permission_service.require_principal(db)
    scenario = db.get(BusinessScenario, scenario_id)
    if not scenario or scenario.tenant_id != principal.tenant_id:
        # 治理元数据不随 public 场景公开，避免泄露未合并架构和评审意见。
        raise HTTPException(status_code=404, detail="业务场景不存在")
    permission_service.require_scenario_permission(db, scenario, "read")
    return scenario, principal


def _scenario_for_manage(db: Session, scenario_id: str) -> tuple[BusinessScenario, permission_service.Principal]:
    scenario, principal = _scenario_for_read(db, scenario_id)
    permission_service.require_tenant_permission(db, "manage")
    return scenario, principal


def _branch_for_read(db: Session, branch_id: str) -> tuple[OntologyBranch, BusinessScenario, permission_service.Principal]:
    branch = db.get(OntologyBranch, branch_id)
    if not branch:
        raise HTTPException(status_code=404, detail="本体分支不存在")
    scenario, principal = _scenario_for_read(db, branch.scenario_id)
    if branch.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="本体分支不存在")
    return branch, scenario, principal


def _branch_for_manage(db: Session, branch_id: str) -> tuple[OntologyBranch, BusinessScenario, permission_service.Principal]:
    branch, scenario, principal = _branch_for_read(db, branch_id)
    permission_service.require_tenant_permission(db, "manage")
    return branch, scenario, principal


def _proposal_for_read(db: Session, proposal_id: str) -> tuple[OntologyProposal, BusinessScenario, permission_service.Principal]:
    proposal = db.execute(
        select(OntologyProposal)
        .options(joinedload(OntologyProposal.branch))
        .where(OntologyProposal.id == proposal_id)
    ).scalars().first()
    if not proposal:
        raise HTTPException(status_code=404, detail="本体提案不存在")
    scenario, principal = _scenario_for_read(db, proposal.scenario_id)
    if proposal.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="本体提案不存在")
    return proposal, scenario, principal


def _proposal_for_manage(db: Session, proposal_id: str) -> tuple[OntologyProposal, BusinessScenario, permission_service.Principal]:
    proposal, scenario, principal = _proposal_for_read(db, proposal_id)
    permission_service.require_tenant_permission(db, "manage")
    return proposal, scenario, principal


def _snapshot_for_scenario(db: Session, scenario: BusinessScenario, snapshot_id: str) -> OntologySnapshot:
    snapshot = db.get(OntologySnapshot, snapshot_id)
    if not snapshot or snapshot.scenario_id != scenario.id or snapshot.tenant_id != scenario.tenant_id:
        raise HTTPException(status_code=404, detail="本体快照不存在")
    return snapshot


def _lock_branch(db: Session, branch_id: str) -> OntologyBranch | None:
    """Refresh and row-lock a branch before a merge/rollback head transition.

    SQLite treats ``FOR UPDATE`` as a no-op, while PostgreSQL/MySQL serialize
    concurrent head updates.  ``populate_existing`` avoids relying on a stale
    relationship object that was loaded while authorising the proposal.
    """
    return db.execute(
        select(OntologyBranch)
        .where(OntologyBranch.id == branch_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).scalars().first()


def _create_snapshot(
    db: Session,
    scenario: BusinessScenario,
    *,
    branch_id: str | None,
    parent_snapshot_id: str | None,
    kind: str,
    content: dict,
    created_by_user_id: str | None,
) -> OntologySnapshot:
    normalized = normalize_snapshot_content(content)
    snapshot = OntologySnapshot(
        tenant_id=str(scenario.tenant_id),
        scenario_id=scenario.id,
        branch_id=branch_id,
        parent_snapshot_id=parent_snapshot_id,
        kind=kind,
        content=normalized,
        content_hash=snapshot_hash(normalized),
        created_by_user_id=created_by_user_id,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _definition_hash(content: Any) -> str:
    """Hash only live ontology definitions, not environment deployment metadata."""
    normalized = normalize_snapshot_content(content)
    normalized.pop("connector_bindings", None)
    return snapshot_hash(normalized)


def definition_hash(content: Any) -> str:
    """Public definition-only hash used by environment runtime validation."""
    return _definition_hash(content)


def live_definition_hash(db: Session, scenario: BusinessScenario) -> str:
    """Hash the current live definition without operational connector state."""
    return _definition_hash(capture_snapshot_content(db, scenario))


def _preserve_connector_requirements(captured: dict, source: Mapping[str, Any]) -> dict:
    """Carry immutable logical requirements through merge/rollback captures."""
    result = copy.deepcopy(captured)
    if "connector_bindings" in source:
        result["connector_bindings"] = copy.deepcopy(source.get("connector_bindings") or [])
    return result


def _assert_non_dev_runtime_bindings(content: Mapping[str, Any], *, environment: str) -> None:
    """Reject a non-dev release that would inevitably fail at runtime.

    Dev keeps the direct-ID/default-model compatibility path while users migrate
    legacy definitions.  Staging/prod runtime resolution is intentionally
    fail-closed, so publishing those definitions without logical binding keys
    would create a misleading "released" state.  Check only executable
    connector paths here; regular binding health/signature verification remains
    in ``_require_snapshot_connectors`` below.
    """
    if environment == "dev":
        return

    missing: list[str] = []
    prohibited_actions: list[str] = []

    def require_binding(config: Any, *, kind: str, label: str) -> None:
        try:
            metadata = connector_service.runtime_binding_from_config(config, kind)
        except connector_service.ConnectorBindingError as exc:
            raise ReleaseConflictError(
                f"{environment} 环境的 {label} 运行时绑定配置无效：{exc}"
            ) from exc
        if metadata is None:
            key_field, _ref_field = connector_service.runtime_binding_fields(kind)
            missing.append(f"{label} 缺少 {key_field}")

    mappings = content.get("mappings") or []
    for mapping in mappings if isinstance(mappings, list) else []:
        if not isinstance(mapping, Mapping):
            continue
        mapping_label = str(mapping.get("id") or "未命名映射")[:160]
        require_binding(
            {
                "data_source_binding_key": mapping.get("data_source_binding_key"),
                "data_source_binding_ref": mapping.get("data_source_binding_ref"),
            },
            kind="data_source",
            label=f"数据映射「{mapping_label}」",
        )

    actions = content.get("actions") or []
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, Mapping) or not bool(action.get("enabled", True)):
            continue
        action_label = str(action.get("name") or action.get("id") or "未命名 Action")[:200]
        executor_type = str(action.get("executor_type") or "")
        config = action.get("executor_config") or {}
        # Staging/prod must be reproducible from the released definition and
        # an auditable connector binding.  Direct HTTP, local skills and
        # scripts depend on host/process state outside that boundary, so they
        # are deliberately dev-only until they have an equivalent governed
        # connector implementation.
        if executor_type in {"http", "skill", "script"}:
            prohibited_actions.append(f"Action「{action_label}」使用 {executor_type}")
        elif executor_type == "sql":
            require_binding(config, kind="data_source", label=f"Action「{action_label}」")
        elif executor_type == "mcp":
            require_binding(config, kind="mcp", label=f"Action「{action_label}」")

    workflows = content.get("workflows") or []
    for workflow in workflows if isinstance(workflows, list) else []:
        if not isinstance(workflow, Mapping):
            continue
        if workflow.get("status") != "active" or not bool(workflow.get("enabled", True)):
            continue
        workflow_label = str(workflow.get("name") or workflow.get("id") or "未命名工作流")[:200]
        nodes = workflow.get("nodes") or []
        for node in nodes if isinstance(nodes, list) else []:
            if not isinstance(node, Mapping) or node.get("type") != "llm":
                continue
            node_data = node.get("data") if isinstance(node.get("data"), Mapping) else {}
            node_label = str(node_data.get("name") or node.get("id") or "未命名节点")[:160]
            require_binding(
                node_data,
                kind="llm",
                label=f"工作流「{workflow_label}」LLM 节点「{node_label}」",
            )

    if prohibited_actions:
        raise ReleaseConflictError(
            "非开发环境禁止 http、skill、script Action 执行器："
            + "；".join(prohibited_actions[:12])
        )
    if missing:
        raise ReleaseConflictError(
            "非开发环境发布必须配置运行时连接器绑定键：" + "；".join(missing[:12])
        )


def _require_snapshot_connectors(
    db: Session,
    scenario: BusinessScenario,
    content: Mapping[str, Any],
    *,
    environment: str | None = None,
) -> list[dict[str, Any]]:
    """Recheck persisted healthy bindings without making network calls.

    Snapshot records created before mappings gained runtime bindings may lack
    derived ``connector_bindings``.  Normalize an in-memory copy first so the
    publish audit uses the same declarative requirements as the runtime.
    """
    try:
        normalized = normalize_snapshot_content(dict(content))
        if environment and environment != "dev":
            # A legacy snapshot that intentionally omitted mappings means
            # "leave mappings untouched" during merge.  In a non-dev release
            # that would make definition-hash verification inevitably fail if
            # live mappings now exist, so require a fresh governed baseline.
            if "mappings" not in content and db.scalar(
                select(DataMapping.id)
                .where(DataMapping.scenario_id == scenario.id)
                .limit(1)
            ):
                raise ReleaseConflictError(
                    "该历史快照未声明数据映射；请先基于当前定义创建并合并新的快照后再发布"
                )
            _assert_non_dev_runtime_bindings(normalized, environment=environment)
        requirements = connector_service.normalize_snapshot_binding_requirements(
            normalized.get("connector_bindings")
        )
        audit: list[dict[str, Any]] = []
        if environment:
            if requirements:
                audit = connector_service.validate_snapshot_bindings(
                    db, scenario, normalized, environment=environment
                )
            _require_mapping_sql_bindings(
                db,
                scenario,
                normalized,
                environment=environment,
            )
            return audit
        for requirement_environment in sorted({item["environment"] for item in requirements}):
            scoped = {
                "connector_bindings": [
                    item for item in requirements if item["environment"] == requirement_environment
                ]
            }
            audit.extend(
                connector_service.validate_snapshot_bindings(
                    db, scenario, scoped, environment=requirement_environment
                )
            )
        # Merge-time validation has no selected deployment; snapshot runtime
        # requirements default to dev, matching the legacy-compatible path.
        _require_mapping_sql_bindings(db, scenario, normalized, environment="dev")
        return audit
    except connector_service.ConnectorBindingError as exc:
        raise ReleaseConflictError(f"连接器环境门禁未通过：{exc}") from exc


def _require_mapping_sql_bindings(
    db: Session,
    scenario: BusinessScenario,
    content: Mapping[str, Any],
    *,
    environment: str,
) -> None:
    """Verify mapping-specific adapter and SQL-read capability requirements."""
    mappings = content.get("mappings") or []
    for mapping in mappings if isinstance(mappings, list) else []:
        if not isinstance(mapping, Mapping):
            continue
        metadata = connector_service.runtime_binding_from_config(
            {
                "data_source_binding_key": mapping.get("data_source_binding_key"),
                "data_source_binding_ref": mapping.get("data_source_binding_ref"),
            },
            "data_source",
        )
        if metadata is None:
            continue
        connector_service.require_ready_binding(
            db,
            scenario,
            environment=environment,
            binding_key_value=str(metadata["binding_key"]),
            kind="data_source",
            reference=connector_service.with_required_capabilities(
                metadata["reference"], "sql_read"
            ),
        )


def _ensure_live_matches_head(db: Session, scenario: BusinessScenario, branch: OntologyBranch) -> None:
    if not branch.head_snapshot_id:
        raise ReleaseConflictError("分支没有可作为合并基线的 head 快照")
    head = _snapshot_for_scenario(db, scenario, branch.head_snapshot_id)
    live = capture_snapshot_content(db, scenario)
    if _definition_hash(live) != _definition_hash(head.content or {}):
        raise ReleaseConflictError("当前本体已偏离分支基线，请先创建新的分支和提案")


def _assert_id_scope(db: Session, model: type, resource_id: str, scenario_id: str, label: str) -> Any | None:
    item = db.get(model, resource_id)
    if item and getattr(item, "scenario_id", None) != scenario_id:
        raise ReleaseValidationError(f"{label} id 已被其他场景使用")
    return item


def _validate_mapping_sources(
    db: Session,
    scenario: BusinessScenario,
    content: dict,
) -> None:
    """Keep release snapshots from becoming a cross-tenant source-ID oracle.

    Portable packages resolve a source reference only after a same-tenant binding
    is found.  Direct snapshot editing must obey the same boundary rather than
    being able to smuggle an arbitrary ``data_source_id`` into a proposal.
    """
    for mapping in content.get("mappings", []):
        source = db.get(DataSource, mapping["data_source_id"])
        if not source:
            raise ReleaseValidationError("数据映射引用的数据源不存在")
        if source.tenant_id != scenario.tenant_id:
            raise ReleaseValidationError("数据映射不能引用其他租户的数据源")
        if source.scenario_id not in {None, scenario.id}:
            raise ReleaseValidationError("数据映射只能引用当前场景或租户级数据源")


def _workflow_reference_ids(workflow: dict) -> dict[str, set[str]]:
    """Return direct Action/Rule/Event references from a normalised workflow."""
    refs: dict[str, set[str]] = {"action": set(), "rule": set(), "event": set()}

    def collect(raw: Any) -> None:
        if not isinstance(raw, dict):
            return
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        # Accept both the documented typed form and legacy nodes that only carry
        # an ``action_id``/``rule_id``/``event_id`` inside data.
        sources = (raw, data)
        for resource_type in refs:
            value = next(
                (
                    source.get(f"{resource_type}_id")
                    for source in sources
                    if isinstance(source.get(f"{resource_type}_id"), str)
                    and source.get(f"{resource_type}_id").strip()
                ),
                None,
            )
            if value:
                refs[resource_type].add(str(value).strip())

    for item in workflow.get("steps") or []:
        collect(item)
    for item in workflow.get("nodes") or []:
        collect(item)
    trigger_config = workflow.get("trigger_config") or {}
    if workflow.get("trigger_type") == "event" and isinstance(trigger_config, dict):
        event_id = trigger_config.get("event_id")
        if isinstance(event_id, str) and event_id.strip():
            refs["event"].add(event_id.strip())
    return refs


def _ensure_active_workflow_definitions_unchanged(
    db: Session,
    scenario: BusinessScenario,
    desired_content: dict,
) -> None:
    """Fail closed when a pending run would observe a different live plan.

    A queued run stores only ``workflow_id`` and the worker resolves the workflow
    and referenced resources when it executes.  Therefore an unrelated release
    must not alter/delete the workflow, its direct Action/Rule/Event dependency,
    or Actions triggered by a referenced Rule until the run is terminal.
    """
    active_runs = db.execute(
        select(WorkflowRun).where(
            WorkflowRun.scenario_id == scenario.id,
            WorkflowRun.status.in_(NONTERMINAL_WORKFLOW_RUN_STATUSES),
        )
    ).scalars().all()
    # Frozen staging/prod runs resolve their own release snapshot and must not
    # prevent dev authoring from moving forward.  Live/dev runs still require
    # this historical mutation guard because they intentionally remain bound to
    # the mutable authoring definition.
    active_runs = [
        run
        for run in active_runs
        if (run.environment or "dev") == "dev"
        and not run.definition_snapshot_id
        and (run.definition_source or "live") == "live"
    ]
    if not active_runs:
        return

    live_content = capture_snapshot_content(db, scenario)
    live_by_type = {
        "workflow": {item["id"]: item for item in live_content["workflows"]},
        "action": {item["id"]: item for item in live_content["actions"]},
        "rule": {item["id"]: item for item in live_content["rules"]},
        "event": {item["id"]: item for item in live_content["events"]},
    }
    desired_by_type = {
        "workflow": {item["id"]: item for item in desired_content["workflows"]},
        "action": {item["id"]: item for item in desired_content["actions"]},
        "rule": {item["id"]: item for item in desired_content["rules"]},
        "event": {item["id"]: item for item in desired_content["events"]},
    }

    for run in active_runs:
        workflow = live_by_type["workflow"].get(run.workflow_id)
        desired_workflow = desired_by_type["workflow"].get(run.workflow_id)
        if not workflow or desired_workflow != workflow:
            raise ReleaseValidationError("不能变更或删除运行中的工作流定义")

        references = _workflow_reference_ids(workflow)
        for rule_id in tuple(references["rule"]):
            rule = live_by_type["rule"].get(rule_id)
            if rule:
                references["action"].update(
                    str(action_id).strip()
                    for action_id in rule.get("trigger_action_ids") or []
                    if isinstance(action_id, str) and action_id.strip()
                )
        for resource_type, resource_ids in references.items():
            for resource_id in resource_ids:
                live_resource = live_by_type[resource_type].get(resource_id)
                names = {"action": "Action", "rule": "规则", "event": "事件"}
                if not live_resource:
                    raise ReleaseValidationError(
                        f"运行中的工作流引用了不存在的{names[resource_type]}，拒绝发布"
                    )
                if desired_by_type[resource_type].get(resource_id) != live_resource:
                    raise ReleaseValidationError(
                        f"不能变更或删除运行中工作流引用的{names[resource_type]}"
                    )


_ACTIVE_RELEASE_RESOURCE_COLLECTIONS: dict[str, tuple[str, str]] = {
    "entity": ("entities", "实体"),
    "relation": ("relations", "关系"),
    "mapping": ("mappings", "数据映射"),
    "function": ("functions", "函数定义"),
    "action": ("actions", "Action"),
    "rule": ("rules", "规则"),
    "event": ("events", "事件"),
    "workflow": ("workflows", "工作流"),
}


def assert_scenario_deletion_allowed(
    db: Session,
    scenario: BusinessScenario,
) -> None:
    """Keep the scenario-level anchor while a non-dev release is active."""
    active_release = db.execute(
        select(OntologyRelease.id)
        .where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.status == "released",
            OntologyRelease.environment.in_(("staging", "prod")),
        )
        .with_for_update()
        .limit(1)
    ).scalar_one_or_none()
    if active_release is not None:
        raise ReleaseConflictError("不能删除仍被活动环境发布引用的业务场景")


def assert_resource_deletion_allowed(
    db: Session,
    scenario: BusinessScenario,
    *,
    kind: str,
    resource_id: str,
) -> None:
    """Fail closed before a live lookup anchor is removed from an active release.

    Staging/prod resolve immutable JSON, but their ordinary API routes first
    use the physical row to locate and authorize the resource.  The governed
    merge path already protects these anchors via ``_guard_safe_removals``;
    direct CRUD deletes must use the same deployment boundary instead of
    bypassing it.
    """
    normalized_kind = str(kind or "").strip().lower()
    collection, label = _ACTIVE_RELEASE_RESOURCE_COLLECTIONS.get(
        normalized_kind,
        ("", ""),
    )
    normalized_id = str(resource_id or "").strip()
    if not collection or not normalized_id:
        raise ReleaseValidationError("发布定义资源标识无效，拒绝删除")

    active_releases = db.execute(
        select(OntologyRelease)
        .where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.status == "released",
            OntologyRelease.environment.in_(("staging", "prod")),
        )
        .with_for_update()
    ).scalars().all()
    for release in active_releases:
        snapshot = db.get(OntologySnapshot, release.snapshot_id)
        if (
            not snapshot
            or release.tenant_id != scenario.tenant_id
            or snapshot.scenario_id != scenario.id
            or snapshot.tenant_id != scenario.tenant_id
        ):
            raise ReleaseConflictError("活动环境发布快照不可用，拒绝删除定义")
        try:
            # Historic snapshots that predate governed mappings cannot prove
            # that a live mapping is absent.  A non-dev deployment with such
            # evidence must not lose a potential runtime anchor by CRUD.
            if normalized_kind in {"mapping", "function"} and collection not in (snapshot.content or {}):
                raise ReleaseConflictError(f"活动环境发布快照缺少{label}，拒绝删除")
            snapshot_content = normalize_snapshot_content(snapshot.content or {})
        except ReleaseConflictError:
            raise
        except Exception as exc:  # noqa: BLE001 - corrupted deployment must fail closed.
            raise ReleaseConflictError("活动环境发布快照无效，拒绝删除定义") from exc
        if any(
            isinstance(item, dict) and str(item.get("id") or "") == normalized_id
            for item in snapshot_content.get(collection, [])
        ):
            raise ReleaseConflictError(f"不能删除仍被活动环境发布引用的{label}")


def _guard_safe_removals(
    db: Session,
    scenario: BusinessScenario,
    content: dict,
) -> None:
    _ensure_active_workflow_definitions_unchanged(db, scenario, content)
    desired_entities = {item["id"] for item in content["entities"]}
    desired_relations = {item["id"] for item in content["relations"]}
    # Legacy snapshots can omit functions.  In that case the apply path leaves
    # them untouched, so the removal guard must not pretend they are being
    # deleted merely because their collection is absent.
    functions_present = "functions" in content
    desired_functions = (
        {item["id"] for item in content["functions"]}
        if functions_present
        else {
            item.id
            for item in db.execute(
                select(FunctionDefinition).where(FunctionDefinition.scenario_id == scenario.id)
            ).scalars().all()
        }
    )
    desired_actions = {item["id"] for item in content["actions"]}
    desired_events = {item["id"] for item in content["events"]}
    desired_workflows = {item["id"] for item in content["workflows"]}

    # The physical rows remain stable lookup anchors for routes and foreign
    # keys, while staging/prod execute immutable DTOs from the release JSON.
    # Deleting an ID still referenced by an active environment release would
    # make that frozen deployment unreachable, so keep it until each affected
    # environment has moved to a new release/rollback target.
    released_ids: dict[str, set[str]] = {
        "entity": set(),
        "relation": set(),
        "function": set(),
        "action": set(),
        "rule": set(),
        "event": set(),
        "workflow": set(),
    }
    active_releases = db.execute(
        select(OntologyRelease).where(
            OntologyRelease.scenario_id == scenario.id,
            OntologyRelease.status == "released",
            OntologyRelease.environment.in_(("staging", "prod")),
        )
    ).scalars().all()
    for release in active_releases:
        snapshot = db.get(OntologySnapshot, release.snapshot_id)
        if not snapshot:
            raise ReleaseValidationError("活动环境发布快照不可用，拒绝删除定义")
        try:
            snapshot_content = normalize_snapshot_content(snapshot.content or {})
        except Exception as exc:  # noqa: BLE001 - corrupted deployment must fail closed.
            raise ReleaseValidationError("活动环境发布快照无效，拒绝删除定义") from exc
        for key, collection in (
            ("entity", "entities"),
            ("relation", "relations"),
            ("function", "functions"),
            ("action", "actions"),
            ("rule", "rules"),
            ("event", "events"),
            ("workflow", "workflows"),
        ):
            released_ids[key].update(
                str(item["id"])
                for item in snapshot_content.get(collection, [])
                if isinstance(item, dict) and item.get("id")
            )
    protected_deletions = {
        "实体": released_ids["entity"] - desired_entities,
        "关系": released_ids["relation"] - desired_relations,
        "函数": released_ids["function"] - desired_functions,
        "Action": released_ids["action"] - desired_actions,
        "规则": released_ids["rule"] - {item["id"] for item in content["rules"]},
        "事件": released_ids["event"] - desired_events,
        "工作流": released_ids["workflow"] - desired_workflows,
    }
    protected = next(
        (label for label, ids in protected_deletions.items() if ids), None
    )
    if protected:
        raise ReleaseValidationError(f"不能删除仍被活动环境发布引用的{protected}")

    for entity in db.execute(
        select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
    ).scalars().all():
        if entity.id in desired_entities:
            continue
        if db.execute(select(OntologyInstance.id).where(OntologyInstance.entity_id == entity.id).limit(1)).scalar_one_or_none():
            raise ReleaseValidationError("不能删除仍包含运行时对象的实体")
        if db.execute(select(DataMapping.id).where(DataMapping.entity_id == entity.id).limit(1)).scalar_one_or_none():
            raise ReleaseValidationError("不能删除仍被数据映射引用的实体")

    # ``DataMapping.column_map`` records property names (some early imports used
    # property IDs).  Keep either form stable; release snapshots intentionally do
    # not mutate mappings, so a delete/rename must be rejected rather than leave a
    # silently dangling ingestion binding.
    desired_properties_by_entity = {
        item["id"]: {prop["id"]: prop for prop in item["properties"]}
        for item in content["entities"]
    }
    for entity_id, desired_properties in desired_properties_by_entity.items():
        mappings = db.execute(
            select(DataMapping).where(DataMapping.entity_id == entity_id)
        ).scalars().all()
        if not mappings:
            continue
        mapped_property_keys = {
            str(key)
            for mapping in mappings
            for key in (mapping.column_map or {}).keys()
        }
        for prop in db.execute(
            select(OntologyProperty).where(OntologyProperty.entity_id == entity_id)
        ).scalars().all():
            if prop.id not in mapped_property_keys and prop.name not in mapped_property_keys:
                continue
            desired = desired_properties.get(prop.id)
            if not desired:
                raise ReleaseValidationError("不能删除仍被数据映射引用的属性")
            if desired["name"] != prop.name:
                raise ReleaseValidationError("不能重命名仍被数据映射引用的属性")
    for relation in db.execute(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
    ).scalars().all():
        if relation.id not in desired_relations and db.execute(
            select(RelationInstance.id).where(RelationInstance.relation_id == relation.id).limit(1)
        ).scalar_one_or_none():
            raise ReleaseValidationError("不能删除仍包含关系实例的关系")
    for action in db.execute(
        select(OntologyAction).where(OntologyAction.scenario_id == scenario.id)
    ).scalars().all():
        if action.id not in desired_actions and db.execute(
            select(ActionExecutionLog.id)
            .where(
                ActionExecutionLog.target_type == "action",
                ActionExecutionLog.target_id == action.id,
            )
            .limit(1)
        ).scalar_one_or_none():
            raise ReleaseValidationError("不能删除已有执行审计记录的 Action")
    for event in db.execute(
        select(OntologyEvent).where(OntologyEvent.scenario_id == scenario.id)
    ).scalars().all():
        if event.id not in desired_events and db.execute(
            select(EventEnvelope.id).where(EventEnvelope.event_id == event.id).limit(1)
        ).scalar_one_or_none():
            raise ReleaseValidationError("不能删除已有事件投递记录的事件")
    for workflow in db.execute(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ).scalars().all():
        if workflow.id not in desired_workflows and db.execute(
            select(WorkflowRun.id).where(WorkflowRun.workflow_id == workflow.id).limit(1)
        ).scalar_one_or_none():
            raise ReleaseValidationError("不能删除已有运行记录的工作流")


def _delete_missing(db: Session, scenario: BusinessScenario, content: dict) -> None:
    """按依赖由外向内移除无运行时引用的定义资源。"""
    desired = {
        OntologyWorkflow: {item["id"] for item in content["workflows"]},
        OntologyEvent: {item["id"] for item in content["events"]},
        OntologyRule: {item["id"] for item in content["rules"]},
        OntologyAction: {item["id"] for item in content["actions"]},
        OntologyRelation: {item["id"] for item in content["relations"]},
        OntologyEntity: {item["id"] for item in content["entities"]},
    }
    if "functions" in content:
        desired[FunctionDefinition] = {item["id"] for item in content["functions"]}
    for model, ids in desired.items():
        for item in db.execute(select(model).where(model.scenario_id == scenario.id)).scalars().all():
            if item.id not in ids:
                db.delete(item)
    db.flush()


def _apply_snapshot_content(db: Session, scenario: BusinessScenario, content: dict) -> None:
    """将已规范化快照应用到实时本体，调用方必须包在同一事务内。"""
    content = normalize_snapshot_content(content)
    _validate_mapping_sources(db, scenario, content)
    _guard_safe_removals(db, scenario, content)
    _delete_missing(db, scenario, content)

    scenario_data = content["scenario"]
    scenario.name = scenario_data["name"]
    scenario.description = scenario_data["description"]
    scenario.industry = scenario_data["industry"]
    scenario.namespace = scenario_data["namespace"]
    scenario.status = scenario_data["status"]

    for entity_data in content["entities"]:
        entity = _assert_id_scope(db, OntologyEntity, entity_data["id"], scenario.id, "实体")
        if not entity:
            entity = OntologyEntity(id=entity_data["id"], scenario_id=scenario.id)
            db.add(entity)
        for key in (
            "name",
            "namespace",
            "description",
            "icon",
            "color",
            "is_abstract",
            "state_property",
        ):
            setattr(entity, key, entity_data[key])
    db.flush()

    for entity_data in content["entities"]:
        entity = db.get(OntologyEntity, entity_data["id"])
        if not entity:
            raise ReleaseValidationError("实体应用失败")
        desired_properties = {item["id"] for item in entity_data["properties"]}
        for prop in db.execute(
            select(OntologyProperty).where(OntologyProperty.entity_id == entity.id)
        ).scalars().all():
            if prop.id not in desired_properties:
                db.delete(prop)
        for prop_data in entity_data["properties"]:
            prop = db.get(OntologyProperty, prop_data["id"])
            if prop and prop.entity_id != entity.id:
                raise ReleaseValidationError("属性 id 已被其他实体使用")
            if not prop:
                prop = OntologyProperty(id=prop_data["id"], entity_id=entity.id)
                db.add(prop)
            for key in (
                "name",
                "data_type",
                "description",
                "is_key",
                "is_required",
                "is_enum",
                "enum_values",
                "default_value",
                "constraints",
                "is_sensitive",
            ):
                setattr(prop, key, copy.deepcopy(prop_data[key]))
    db.flush()

    for relation_data in content["relations"]:
        relation = _assert_id_scope(db, OntologyRelation, relation_data["id"], scenario.id, "关系")
        if not relation:
            relation = OntologyRelation(id=relation_data["id"], scenario_id=scenario.id)
            db.add(relation)
        for key in (
            "name",
            "namespace",
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            "description",
        ):
            setattr(relation, key, relation_data[key])

    # Mapping removal is intentionally not performed as a side effect of a
    # release.  These bindings can have operational history and importing a
    # partial package must not silently sever an existing ingestion path.  A
    # governed proposal may add/update the declarative mapping, while explicit
    # connector retirement remains a separate operation.
    # Imported mapping work is definition-bound.  A merge that changes an
    # existing mapping must cancel its in-flight jobs in the same transaction;
    # the worker also revalidates before committing any rows it read earlier.
    from . import mapping_refresh_service

    for mapping_data in content.get("mappings", []):
        mapping = _assert_id_scope(db, DataMapping, mapping_data["id"], scenario.id, "数据映射")
        prior_fingerprint = mapping_refresh_service.mapping_fingerprint(mapping) if mapping else ""
        if not mapping:
            mapping = DataMapping(
                id=mapping_data["id"],
                scenario_id=scenario.id,
                entity_id=mapping_data["entity_id"],
                data_source_id=mapping_data["data_source_id"],
            )
            db.add(mapping)
        for key in (
            "entity_id",
            "data_source_id",
            "data_source_binding_key",
            "data_source_binding_ref",
            "table_name",
            "column_map",
            "transform_rules",
        ):
            setattr(mapping, key, copy.deepcopy(mapping_data[key]))
        if prior_fingerprint and mapping_refresh_service.mapping_fingerprint(mapping) != prior_fingerprint:
            mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                db,
                mapping.id,
                reason="映射定义已由治理发布更新",
            )

    for function_data in content.get("functions", []):
        function = _assert_id_scope(
            db,
            FunctionDefinition,
            function_data["id"],
            scenario.id,
            "函数定义",
        )
        if not function:
            function = FunctionDefinition(id=function_data["id"], scenario_id=scenario.id)
            db.add(function)
        for key in (
            "name", "description", "input_schema", "output_schema", "tags", "visibility",
            "runtime_kind", "runtime_config",
        ):
            setattr(function, key, copy.deepcopy(function_data[key]))

    for action_data in content["actions"]:
        action = _assert_id_scope(db, OntologyAction, action_data["id"], scenario.id, "Action")
        if not action:
            if _contains_marker(action_data["executor_config"]):
                raise ReleaseValidationError("新 Action 不能只包含凭据占位符")
            action = OntologyAction(id=action_data["id"], scenario_id=scenario.id, entity_id=action_data["entity_id"])
            db.add(action)
            old_config: dict = {}
        else:
            old_config = action.executor_config or {}
        for key in (
            "entity_id",
            "name",
            "description",
            "input_schema",
            "executor_type",
            "precondition",
            "postcondition",
            "enabled",
            "requires_confirmation",
            "idempotency_required",
            "permission_scope",
            "access_scope",
        ):
            setattr(action, key, copy.deepcopy(action_data[key]))
        action.executor_config = _preserve_secrets(old_config, action_data["executor_config"])

    for rule_data in content["rules"]:
        rule = _assert_id_scope(db, OntologyRule, rule_data["id"], scenario.id, "规则")
        if not rule:
            rule = OntologyRule(id=rule_data["id"], scenario_id=scenario.id)
            db.add(rule)
            old_condition: dict = {}
        else:
            old_condition = rule.condition or {}
        for key in (
            "entity_id",
            "name",
            "description",
            "action_on_match",
            "trigger_action_ids",
            "severity",
            "enabled",
        ):
            setattr(rule, key, copy.deepcopy(rule_data[key]))
        rule.condition = _preserve_secrets(old_condition, rule_data["condition"])

    for event_data in content["events"]:
        event = _assert_id_scope(db, OntologyEvent, event_data["id"], scenario.id, "事件")
        if not event:
            event = OntologyEvent(id=event_data["id"], scenario_id=scenario.id)
            db.add(event)
        for key in ("name", "description", "payload_schema", "trigger_source", "enabled"):
            setattr(event, key, copy.deepcopy(event_data[key]))

    for workflow_data in content["workflows"]:
        workflow = _assert_id_scope(db, OntologyWorkflow, workflow_data["id"], scenario.id, "工作流")
        if not workflow:
            if any(
                _contains_marker(workflow_data[key])
                for key in ("trigger_config", "steps", "nodes", "edges")
            ):
                raise ReleaseValidationError("新工作流不能只包含凭据占位符")
            workflow = OntologyWorkflow(id=workflow_data["id"], scenario_id=scenario.id)
            db.add(workflow)
            old_values: dict[str, Any] = {}
        else:
            old_values = {
                "trigger_config": workflow.trigger_config or {},
                "steps": workflow.steps or [],
                "nodes": workflow.nodes or [],
                "edges": workflow.edges or [],
            }
        for key in ("name", "description", "trigger_type", "status", "enabled", "access_scope"):
            setattr(workflow, key, copy.deepcopy(workflow_data[key]))
        for key in ("trigger_config", "steps", "nodes", "edges"):
            setattr(workflow, key, _preserve_secrets(old_values.get(key), workflow_data[key]))
    db.flush()


def create_branch(
    db: Session,
    scenario_id: str,
    *,
    name: str,
    description: str = "",
) -> OntologyBranch:
    scenario, principal = _scenario_for_manage(db, scenario_id)
    branch_name = _string(name, "分支名称", maximum=120).strip()
    if not branch_name:
        raise ReleaseValidationError("分支名称不能为空")
    if db.execute(
        select(OntologyBranch.id).where(
            OntologyBranch.scenario_id == scenario.id,
            OntologyBranch.name == branch_name,
        )
    ).scalar_one_or_none():
        raise ReleaseConflictError("同一场景已存在同名分支")
    try:
        branch = OntologyBranch(
            tenant_id=principal.tenant_id,
            scenario_id=scenario.id,
            name=branch_name,
            description=_string(description, "分支说明", maximum=4_000),
            status="active",
            created_by_user_id=principal.user_id,
        )
        db.add(branch)
        db.flush()
        baseline = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=None,
            kind="baseline",
            content=capture_snapshot_content(db, scenario),
            created_by_user_id=principal.user_id,
        )
        branch.base_snapshot_id = baseline.id
        branch.head_snapshot_id = baseline.id
        db.commit()
        db.refresh(branch)
        return branch
    except IntegrityError as exc:
        db.rollback()
        raise ReleaseConflictError("分支创建冲突") from exc
    except Exception:
        db.rollback()
        raise


def list_branches(db: Session, scenario_id: str) -> list[OntologyBranch]:
    scenario, _ = _scenario_for_read(db, scenario_id)
    return db.execute(
        select(OntologyBranch)
        .where(OntologyBranch.scenario_id == scenario.id)
        .order_by(OntologyBranch.created_at.desc())
    ).scalars().all()


def get_branch(db: Session, branch_id: str) -> OntologyBranch:
    branch, _, _ = _branch_for_read(db, branch_id)
    return branch


def get_snapshot(db: Session, snapshot_id: str) -> OntologySnapshot:
    snapshot = db.get(OntologySnapshot, snapshot_id)
    if not snapshot:
        raise HTTPException(status_code=404, detail="本体快照不存在")
    scenario, _ = _scenario_for_read(db, snapshot.scenario_id)
    if snapshot.tenant_id != scenario.tenant_id:
        raise HTTPException(status_code=404, detail="本体快照不存在")
    return snapshot


def create_proposal(
    db: Session,
    branch_id: str,
    *,
    title: str,
    description: str,
    content: dict,
    submit: bool = True,
    expected_base_snapshot_id: str | None = None,
) -> OntologyProposal:
    branch, scenario, principal = _branch_for_manage(db, branch_id)
    if branch.status != "active" or not branch.head_snapshot_id:
        raise ReleaseConflictError("分支不是可提交状态")
    if expected_base_snapshot_id and branch.head_snapshot_id != expected_base_snapshot_id:
        raise ReleaseConflictError("发布分支基线已变化，请刷新后重新创建提案")
    normalized = normalize_snapshot_content(content)
    _validate_mapping_sources(db, scenario, normalized)
    try:
        proposal_snapshot = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=branch.head_snapshot_id,
            kind="proposal",
            content=normalized,
            created_by_user_id=principal.user_id,
        )
        proposal = OntologyProposal(
            tenant_id=principal.tenant_id,
            scenario_id=scenario.id,
            branch_id=branch.id,
            base_snapshot_id=branch.head_snapshot_id,
            proposed_snapshot_id=proposal_snapshot.id,
            title=_string(title, "提案标题", maximum=240),
            description=_string(description, "提案说明", maximum=8_000),
            status="submitted" if submit else "draft",
            created_by_user_id=principal.user_id,
            submitted_at=_now() if submit else None,
        )
        db.add(proposal)
        db.commit()
        db.refresh(proposal)
        return proposal
    except Exception:
        db.rollback()
        raise


def list_proposals(
    db: Session,
    scenario_id: str,
    *,
    branch_id: str | None = None,
    status: str | None = None,
) -> list[OntologyProposal]:
    scenario, _ = _scenario_for_read(db, scenario_id)
    stmt = select(OntologyProposal).where(OntologyProposal.scenario_id == scenario.id)
    if branch_id:
        stmt = stmt.where(OntologyProposal.branch_id == branch_id)
    if status:
        stmt = stmt.where(OntologyProposal.status == status)
    return db.execute(stmt.order_by(OntologyProposal.created_at.desc())).scalars().all()


def get_proposal(db: Session, proposal_id: str) -> OntologyProposal:
    proposal, _, _ = _proposal_for_read(db, proposal_id)
    return proposal


def submit_proposal(db: Session, proposal_id: str) -> OntologyProposal:
    """Submit an existing draft without allowing silent content replacement."""
    proposal, scenario, principal = _proposal_for_manage(db, proposal_id)
    if proposal.status != "draft":
        raise ReleaseConflictError("只有草稿提案可以提交评审")
    if proposal.created_by_user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="只有提案创建者可以提交草稿")
    branch = db.get(OntologyBranch, proposal.branch_id)
    if (
        not branch
        or branch.scenario_id != scenario.id
        or branch.status != "active"
        or branch.head_snapshot_id != proposal.base_snapshot_id
    ):
        raise ReleaseConflictError("提案基线已过期，请基于当前分支重新创建")
    try:
        proposal.status = "submitted"
        proposal.submitted_at = _now()
        db.commit()
        db.refresh(proposal)
        return proposal
    except Exception:
        db.rollback()
        raise


def list_reviews(db: Session, proposal_id: str) -> list[OntologyReview]:
    """Return an authorised proposal's immutable review trail."""
    proposal, _, _ = _proposal_for_read(db, proposal_id)
    return db.execute(
        select(OntologyReview)
        .where(OntologyReview.proposal_id == proposal.id)
        .order_by(OntologyReview.created_at.asc())
    ).scalars().all()


def create_review(
    db: Session,
    proposal_id: str,
    *,
    decision: str,
    comment: str = "",
) -> OntologyReview:
    proposal, _, principal = _proposal_for_manage(db, proposal_id)
    if decision not in {"approve", "reject"}:
        raise ReleaseValidationError("评审决定必须为 approve 或 reject")
    if proposal.status != "submitted":
        raise ReleaseConflictError("只有已提交提案可以评审")
    if proposal.created_by_user_id == principal.user_id:
        raise HTTPException(status_code=403, detail="提案创建者不能评审自己的提案")
    try:
        review = OntologyReview(
            proposal_id=proposal.id,
            reviewer_user_id=principal.user_id,
            decision=decision,
            comment=_string(comment, "评审意见", maximum=8_000),
        )
        db.add(review)
        proposal.status = "approved" if decision == "approve" else "rejected"
        db.commit()
        db.refresh(review)
        return review
    except Exception:
        db.rollback()
        raise


def merge_proposal(
    db: Session,
    proposal_id: str,
    *,
    confirmed: bool,
    note: str = "",
) -> OntologyProposal:
    if confirmed is not True:
        raise ReleaseValidationError("合并必须显式 confirmed=true")
    proposal, scenario, principal = _proposal_for_manage(db, proposal_id)
    if proposal.status != "approved":
        raise ReleaseConflictError("只有已批准提案可以合并")
    try:
        branch = _lock_branch(db, proposal.branch_id)
        if not branch or branch.scenario_id != scenario.id or branch.status != "active":
            raise ReleaseConflictError("提案分支不可合并")
        if branch.head_snapshot_id != proposal.base_snapshot_id:
            raise ReleaseConflictError("提案基线已过期，请基于最新分支重新提交")
        proposed = _snapshot_for_scenario(db, scenario, proposal.proposed_snapshot_id)
        _ensure_live_matches_head(db, scenario, branch)
        # Imports may have been approved while a target connector was later
        # disabled or reconfigured.  Merge must recheck the package's declared
        # environment bindings before any live ontology rows are touched.
        _require_snapshot_connectors(db, scenario, proposed.content or {})
        head = _snapshot_for_scenario(db, scenario, branch.head_snapshot_id)
        pre_merge = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=branch.head_snapshot_id,
            kind="pre_merge",
            content=_preserve_connector_requirements(
                capture_snapshot_content(db, scenario), head.content or {}
            ),
            created_by_user_id=principal.user_id,
        )
        _apply_snapshot_content(db, scenario, proposed.content or {})
        merged = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=pre_merge.id,
            kind="merge",
            content=_preserve_connector_requirements(
                capture_snapshot_content(db, scenario), proposed.content or {}
            ),
            created_by_user_id=principal.user_id,
        )
        branch.head_snapshot_id = merged.id
        proposal.pre_merge_snapshot_id = pre_merge.id
        proposal.merged_snapshot_id = merged.id
        proposal.status = "merged"
        proposal.merged_at = _now()
        proposal.merged_by_user_id = principal.user_id
        if note:
            proposal.description = f"{proposal.description}\n\n合并说明：{_string(note, '合并说明', maximum=8_000)}".strip()
        db.commit()
        db.refresh(proposal)
        return proposal
    except Exception:
        db.rollback()
        raise


def _resolve_publish_snapshot(
    db: Session,
    scenario: BusinessScenario,
    *,
    branch_id: str | None,
    proposal_id: str | None,
    snapshot_id: str | None,
) -> tuple[OntologyBranch, OntologySnapshot, OntologyProposal | None]:
    supplied = sum(bool(value) for value in (branch_id, proposal_id, snapshot_id))
    if supplied > 1:
        raise ReleaseValidationError("发布目标只能指定 branch、proposal 或 snapshot 之一")
    proposal: OntologyProposal | None = None
    if proposal_id:
        proposal = db.get(OntologyProposal, proposal_id)
        if not proposal or proposal.scenario_id != scenario.id or proposal.status != "merged":
            raise ReleaseValidationError("只能发布已合并提案")
        branch = db.get(OntologyBranch, proposal.branch_id)
        snapshot = _snapshot_for_scenario(db, scenario, proposal.merged_snapshot_id or "")
    elif snapshot_id:
        snapshot = _snapshot_for_scenario(db, scenario, snapshot_id)
        if not snapshot.branch_id:
            raise ReleaseValidationError("快照不属于可发布分支")
        branch = db.get(OntologyBranch, snapshot.branch_id)
    else:
        if branch_id:
            branch = db.get(OntologyBranch, branch_id)
            if not branch or branch.scenario_id != scenario.id:
                raise HTTPException(status_code=404, detail="本体分支不存在")
        else:
            branch = db.execute(
                select(OntologyBranch)
                .where(OntologyBranch.scenario_id == scenario.id, OntologyBranch.status == "active")
                .order_by(OntologyBranch.updated_at.desc())
                .limit(1)
            ).scalars().first()
        if not branch or not branch.head_snapshot_id:
            raise ReleaseValidationError("没有可发布的分支快照")
        snapshot = _snapshot_for_scenario(db, scenario, branch.head_snapshot_id)
    if not branch or branch.tenant_id != scenario.tenant_id:
        raise ReleaseValidationError("发布分支无效")
    if snapshot.kind not in MERGEABLE_SNAPSHOT_KINDS or branch.head_snapshot_id != snapshot.id:
        raise ReleaseValidationError("只能发布分支当前已合并的快照")
    return branch, snapshot, proposal


def publish_snapshot(
    db: Session,
    scenario_id: str,
    *,
    environment: str,
    confirmed: bool,
    branch_id: str | None = None,
    proposal_id: str | None = None,
    snapshot_id: str | None = None,
    notes: str = "",
) -> OntologyRelease:
    if confirmed is not True:
        raise ReleaseValidationError("发布必须显式 confirmed=true")
    if environment not in ENVIRONMENTS:
        raise ReleaseValidationError("发布环境必须为 dev、staging 或 prod")
    scenario, principal = _scenario_for_manage(db, scenario_id)
    branch, snapshot, proposal = _resolve_publish_snapshot(
        db,
        scenario,
        branch_id=branch_id,
        proposal_id=proposal_id,
        snapshot_id=snapshot_id,
    )
    try:
        connector_audit = _require_snapshot_connectors(
            db, scenario, snapshot.content or {}, environment=environment
        )
        for old_release in db.execute(
            select(OntologyRelease).where(
                OntologyRelease.scenario_id == scenario.id,
                OntologyRelease.environment == environment,
                OntologyRelease.status == "released",
            )
        ).scalars().all():
            old_release.status = "superseded"
        release = OntologyRelease(
            tenant_id=principal.tenant_id,
            scenario_id=scenario.id,
            branch_id=branch.id,
            snapshot_id=snapshot.id,
            proposal_id=proposal.id if proposal else None,
            environment=environment,
            status="released",
            notes=_string(notes, "发布说明", maximum=8_000),
            connector_audit=connector_audit,
            created_by_user_id=principal.user_id,
        )
        db.add(release)
        db.commit()
        db.refresh(release)
        return release
    except Exception:
        db.rollback()
        raise


def list_releases(
    db: Session,
    scenario_id: str,
    *,
    environment: str | None = None,
) -> list[OntologyRelease]:
    scenario, _ = _scenario_for_read(db, scenario_id)
    if environment and environment not in ENVIRONMENTS:
        raise ReleaseValidationError("发布环境必须为 dev、staging 或 prod")
    stmt = select(OntologyRelease).where(OntologyRelease.scenario_id == scenario.id)
    if environment:
        stmt = stmt.where(OntologyRelease.environment == environment)
    return db.execute(stmt.order_by(OntologyRelease.created_at.desc())).scalars().all()


def rollback_snapshot(
    db: Session,
    scenario_id: str,
    *,
    target_snapshot_id: str,
    confirmed: bool,
    branch_id: str | None = None,
    environment: str | None = None,
    reason: str = "",
) -> OntologyRollback:
    if confirmed is not True:
        raise ReleaseValidationError("回滚必须显式 confirmed=true")
    if environment and environment not in ENVIRONMENTS:
        raise ReleaseValidationError("发布环境必须为 dev、staging 或 prod")
    scenario, principal = _scenario_for_manage(db, scenario_id)
    target = _snapshot_for_scenario(db, scenario, target_snapshot_id)
    if target.kind not in ROLLBACKABLE_SNAPSHOT_KINDS:
        raise ReleaseValidationError("不能回滚到未合并提案快照")
    resolved_branch_id = branch_id or target.branch_id
    if not resolved_branch_id:
        raise ReleaseValidationError("回滚目标缺少分支信息")
    branch = db.get(OntologyBranch, resolved_branch_id)
    if not branch or branch.scenario_id != scenario.id or branch.tenant_id != scenario.tenant_id:
        raise HTTPException(status_code=404, detail="本体分支不存在")
    if target.branch_id != branch.id:
        raise ReleaseValidationError("回滚目标必须属于当前分支")
    try:
        branch = _lock_branch(db, branch.id)
        if not branch or branch.scenario_id != scenario.id or branch.tenant_id != scenario.tenant_id:
            raise HTTPException(status_code=404, detail="本体分支不存在")
        if target.branch_id != branch.id:
            raise ReleaseValidationError("回滚目标必须属于当前分支")
        if not branch.head_snapshot_id:
            raise ReleaseConflictError("分支没有当前快照")
        # A staging/prod rollback is an environment deployment transition, not
        # a mutation of the shared dev authoring definition.  In particular it
        # must remain possible when dev has moved on from the branch head; the
        # selected immutable snapshot and its environment bindings are the
        # only inputs that need validation here.
        if environment in {"staging", "prod"}:
            connector_audit = _require_snapshot_connectors(
                db,
                scenario,
                target.content or {},
                environment=environment,
            )
            active_releases = db.execute(
                select(OntologyRelease)
                .where(
                    OntologyRelease.scenario_id == scenario.id,
                    OntologyRelease.environment == environment,
                    OntologyRelease.status == "released",
                )
                .order_by(OntologyRelease.created_at.desc())
            ).scalars().all()
            from_snapshot_id = (
                active_releases[0].snapshot_id
                if active_releases
                else branch.head_snapshot_id
            )
            rollback_reason = _string(reason, "回滚原因", maximum=8_000)
            rollback = OntologyRollback(
                tenant_id=principal.tenant_id,
                scenario_id=scenario.id,
                branch_id=branch.id,
                from_snapshot_id=from_snapshot_id,
                target_snapshot_id=target.id,
                # The environment now resolves the existing immutable target;
                # do not manufacture/apply a shared-live rollback snapshot.
                result_snapshot_id=target.id,
                environment=environment,
                reason=rollback_reason,
                connector_audit=connector_audit,
                created_by_user_id=principal.user_id,
            )
            db.add(rollback)
            for old_release in active_releases:
                old_release.status = "rolled_back"
            db.add(
                OntologyRelease(
                    tenant_id=principal.tenant_id,
                    scenario_id=scenario.id,
                    branch_id=branch.id,
                    snapshot_id=target.id,
                    environment=environment,
                    status="released",
                    notes=f"回滚：{rollback_reason}".strip(),
                    connector_audit=connector_audit,
                    created_by_user_id=principal.user_id,
                )
            )
            db.commit()
            db.refresh(rollback)
            return rollback
        _ensure_live_matches_head(db, scenario, branch)
        connector_audit = _require_snapshot_connectors(
            db,
            scenario,
            target.content or {},
            environment=environment or "dev",
        )
        head = _snapshot_for_scenario(db, scenario, branch.head_snapshot_id)
        before = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=branch.head_snapshot_id,
            kind="pre_rollback",
            content=_preserve_connector_requirements(
                capture_snapshot_content(db, scenario), head.content or {}
            ),
            created_by_user_id=principal.user_id,
        )
        _apply_snapshot_content(db, scenario, target.content or {})
        result = _create_snapshot(
            db,
            scenario,
            branch_id=branch.id,
            parent_snapshot_id=before.id,
            kind="rollback",
            content=_preserve_connector_requirements(
                capture_snapshot_content(db, scenario), target.content or {}
            ),
            created_by_user_id=principal.user_id,
        )
        branch.head_snapshot_id = result.id
        rollback = OntologyRollback(
            tenant_id=principal.tenant_id,
            scenario_id=scenario.id,
            branch_id=branch.id,
            from_snapshot_id=before.id,
            target_snapshot_id=target.id,
            result_snapshot_id=result.id,
            environment=environment,
            reason=_string(reason, "回滚原因", maximum=8_000),
            connector_audit=connector_audit,
            created_by_user_id=principal.user_id,
        )
        db.add(rollback)
        if environment:
            for old_release in db.execute(
                select(OntologyRelease).where(
                    OntologyRelease.scenario_id == scenario.id,
                    OntologyRelease.environment == environment,
                    OntologyRelease.status == "released",
                )
            ).scalars().all():
                old_release.status = "rolled_back"
            db.add(
                OntologyRelease(
                    tenant_id=principal.tenant_id,
                    scenario_id=scenario.id,
                    branch_id=branch.id,
                    snapshot_id=result.id,
                    environment=environment,
                    status="released",
                    notes=f"回滚：{rollback.reason}".strip(),
                    connector_audit=connector_audit,
                    created_by_user_id=principal.user_id,
                )
            )
        db.commit()
        db.refresh(rollback)
        return rollback
    except Exception:
        db.rollback()
        raise
