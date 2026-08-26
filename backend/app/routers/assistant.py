"""全局 AI 助手：跨页面上下文、临时附件、草稿生成与确认应用。"""
from __future__ import annotations

import json
import hashlib
import copy
import math
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import (
    AssistantAttachment,
    AssistantAuditLog,
    AssistantCompilationJob,
    AssistantMessage,
    AssistantProposalApplication,
    AssistantRouteDecision,
    AssistantThread,
    BusinessScenario,
    BucketFile,
    DataMapping,
    DataSource,
    DocumentChunk,
    LLMConfig,
    MCPConfig,
    OntologyEntity,
    OntologyAction,
    OntologyEvent,
    OntologyRule,
    OntologyRelation,
    OntologyWorkflow,
    Skill,
)
from ..schemas import (
    AssistantAttachmentOut,
    AssistantChatRequest,
    AssistantCompilationJobResultOut,
    AssistantCompilationJobStatusOut,
    AssistantMessageOut,
    AssistantProposalApplyRequest,
    AssistantReplyOut,
    AssistantThreadOut,
    DataMappingIn,
    Msg,
    ScenarioIn,
)
from ..services import (
    assistant_orchestrator,
    assistant_compilation_job_service,
    doc_parser,
    datasource_service,
    llm_service,
    mapping_refresh_service,
    ontology_service,
    permission_service,
    rag_service,
    release_service,
    runtime_connector_service,
    runtime_definition_service,
    scenario_model_draft_service,
    scenario_model_compiler,
    tenant_service,
    workflow_service,
)
from ..services.auth_service import get_tenant_db
from ..services.policies import PolicyViolation

router = APIRouter(prefix="/assistant", tags=["assistant"])


# Temporary assistant attachments are fed directly to draft generators rather
# than indexed/retrieved in chunks.  Keep a generous, explicit single-request
# boundary and reject larger inputs instead of silently dropping the tail of a
# business document.  The ontology generator has a slightly larger envelope for
# the user's message and authorised RAG excerpts around this attachment body.
ASSISTANT_ATTACHMENT_TEXT_MAX_CHARS = 1_000_000
ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS = 80_000

# Compound compilation is proposal-only and each job has a durable single-
# flight claim. A small process-local pool lets the HTTP/SSE request return
# immediately while the existing status/result endpoints remain the recovery
# boundary. Production deployments can replace this executor with a queue
# worker without changing the job or proposal contract.
_COMPILATION_WORKER_COUNT = 4
_COMPILATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=_COMPILATION_WORKER_COUNT,
    thread_name_prefix="assistant-compilation",
)
_COMPILATION_SUBMISSION_SLOTS = threading.BoundedSemaphore(
    _COMPILATION_WORKER_COUNT
)
_COMPILATION_HEARTBEAT_SECONDS = max(
    1,
    assistant_compilation_job_service.DEFAULT_LEASE_SECONDS // 3,
)


def _durable_prepared_context(value: dict[str, Any]) -> dict[str, Any]:
    """Encode tuple-keyed schema columns into owner-private JSON storage."""
    columns = []
    for raw_key, raw_values in (value.get("columns_by_table") or {}).items():
        key = tuple(raw_key) if isinstance(raw_key, (list, tuple)) else ()
        if len(key) != 2:
            raise ValueError("编译上下文包含无效的数据源表键")
        columns.append({
            "data_source_id": str(key[0]),
            "table_name": str(key[1]),
            "columns": sorted(str(item) for item in (raw_values or [])),
        })
    columns.sort(key=lambda item: (item["data_source_id"], item["table_name"]))
    return {
        "mapping_catalog": copy.deepcopy(value.get("mapping_catalog") or []),
        "columns_by_table": columns,
        "working_drafts": copy.deepcopy(value.get("working_drafts") or []),
        "consumed_draft_revisions": copy.deepcopy(
            value.get("consumed_draft_revisions") or {}
        ),
        "fingerprint": str(value.get("fingerprint") or ""),
    }


def _restore_durable_prepared_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("持久化编译上下文格式无效")
    raw_columns = value.get("columns_by_table") or []
    if not isinstance(raw_columns, list):
        raise ValueError("持久化数据源列目录格式无效")
    columns: dict[tuple[str, str], list[str]] = {}
    for item in raw_columns:
        if not isinstance(item, dict):
            raise ValueError("持久化数据源列目录包含无效条目")
        source_id = str(item.get("data_source_id") or "")
        table_name = str(item.get("table_name") or "")
        raw_values = item.get("columns") or []
        if not source_id or not table_name or not isinstance(raw_values, list):
            raise ValueError("持久化数据源列目录缺少稳定身份")
        columns[(source_id, table_name)] = [str(column) for column in raw_values]
    return {
        "mapping_catalog": copy.deepcopy(value.get("mapping_catalog") or []),
        "columns_by_table": columns,
        "working_drafts": copy.deepcopy(value.get("working_drafts") or []),
        "consumed_draft_revisions": copy.deepcopy(
            value.get("consumed_draft_revisions") or {}
        ),
        "fingerprint": str(value.get("fingerprint") or ""),
    }


def _compilation_execution_input(
    *,
    compiler_message: str,
    compiler_documents: list[dict[str, str]],
    prepared_context: dict[str, Any],
    llm_config_id: str,
    context: dict[str, Any],
    sources: list[dict[str, Any]],
    execution_policy: dict[str, Any],
    recovery_issue: dict[str, str] | None = None,
) -> dict[str, Any]:
    value = {
        "version": 1,
        "compiler_message": str(compiler_message),
        "compiler_documents": copy.deepcopy(compiler_documents),
        "prepared_context": _durable_prepared_context(prepared_context),
        "llm_config_id": str(llm_config_id or ""),
        "context": copy.deepcopy(context),
        "sources": copy.deepcopy(sources),
        "execution_policy": copy.deepcopy(execution_policy),
    }
    if recovery_issue:
        value["recovery_issue"] = copy.deepcopy(recovery_issue)
    return assistant_compilation_job_service.normalize_execution_input(value)


def _load_compilation_execution_input(
    db: Session,
    job: AssistantCompilationJob,
    *,
    lease_token: str,
    lease_attempt: int,
) -> dict[str, Any]:
    """Load and validate the exact private input frozen with a running job."""
    value = assistant_compilation_job_service.load_leased_execution_input(
        db,
        job.id,
        token=lease_token,
        attempt=lease_attempt,
    )
    if int(value.get("version") or 0) != 1:
        raise ValueError("持久化编译任务缺少受支持的执行输入版本")
    compiler_message = value.get("compiler_message")
    compiler_documents = value.get("compiler_documents")
    context = value.get("context")
    sources = value.get("sources")
    execution_policy = value.get("execution_policy")
    recovery_issue = value.get("recovery_issue")
    if not isinstance(compiler_message, str):
        raise ValueError("持久化编译任务缺少原始用户描述")
    if not isinstance(compiler_documents, list) or not all(
        isinstance(item, dict) for item in compiler_documents
    ):
        raise ValueError("持久化编译任务附件格式无效")
    if not isinstance(context, dict) or not isinstance(sources, list):
        raise ValueError("持久化编译任务会话上下文格式无效")
    if not isinstance(execution_policy, dict):
        raise ValueError("持久化编译任务执行策略格式无效")
    if recovery_issue is not None and not isinstance(recovery_issue, dict):
        raise ValueError("持久化编译任务恢复问题格式无效")
    if set(execution_policy) != {
        "llm_call_budget", "request_timeout", "assistant_scope_key",
    }:
        raise ValueError("持久化编译任务执行策略字段无效")
    if (
        assistant_compilation_job_service.execution_policy_fingerprint(
            execution_policy
        )
        != str(job.execution_policy_fingerprint or "")
    ):
        raise ValueError("持久化编译任务执行策略与任务指纹不一致")
    prepared_context = _restore_durable_prepared_context(
        value.get("prepared_context")
    )
    if prepared_context["fingerprint"] != str(
        job.mapping_context_fingerprint or ""
    ):
        raise ValueError("持久化编译任务的冻结建模上下文与任务指纹不一致")
    if int(execution_policy.get("llm_call_budget") or 0) != int(
        job.llm_call_budget
    ):
        raise ValueError("持久化编译任务的模型调用预算与任务账本不一致")
    try:
        request_timeout = float(execution_policy.get("request_timeout"))
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化编译任务的请求超时无效") from exc
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("持久化编译任务的请求超时必须是有限正数")
    if not isinstance(execution_policy.get("assistant_scope_key"), str):
        raise ValueError("持久化编译任务缺少助手会话范围")
    execution_policy = {
        **execution_policy,
        "request_timeout": request_timeout,
    }
    result = {
        "compiler_message": compiler_message,
        "compiler_documents": copy.deepcopy(compiler_documents),
        "prepared_context": prepared_context,
        "llm_config_id": str(value.get("llm_config_id") or ""),
        "context": copy.deepcopy(context),
        "sources": copy.deepcopy(sources),
        "execution_policy": copy.deepcopy(execution_policy),
    }
    if recovery_issue is not None:
        result["recovery_issue"] = {
            "code": str(recovery_issue.get("code") or "")[:120],
            "message": str(recovery_issue.get("message") or "")[:2000],
        }
    return result


def _tenant(db: Session) -> str:
    return tenant_service.current_tenant_id(db)


def _current_user_id(db: Session) -> str:
    """Require a real organization principal before handling assistant state."""
    return permission_service.require_principal(db).user_id


def _thread(db: Session, thread_id: str) -> AssistantThread:
    thread = db.execute(
        select(AssistantThread).where(
            AssistantThread.id == thread_id,
            AssistantThread.tenant_id == _tenant(db),
            # Legacy NULL owner rows are intentionally inaccessible: guessing
            # ownership from tenant scope would reintroduce cross-user leaks.
            AssistantThread.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if not thread:
        raise HTTPException(404, "助手会话不存在")
    if thread.scenario_id:
        _scenario(db, thread.scenario_id)
    else:
        permission_service.require_tenant_permission(db, "read")
    return thread


def _context_scope(scenario_id: str | None, path: str = "") -> str:
    """生成稳定的会话范围；同一场景的不同页面必须使用不同会话。"""
    route_path = (path or "/").split("?", 1)[0].split("#", 1)[0] or "/"
    return f"scenario:{scenario_id or 'global'}|path:{route_path}"


def _assert_thread_scope(
    thread: AssistantThread,
    scenario_id: str | None,
    page: str = "",
    path: str = "",
) -> None:
    expected = _context_scope(scenario_id, path)
    if thread.scope_key != expected:
        raise HTTPException(409, "助手会话与当前页面或业务场景不一致，请切换到当前上下文的会话")


def _scenario(db: Session, scenario_id: str | None, writable: bool = False) -> BusinessScenario | None:
    if not scenario_id:
        return None
    scenario = tenant_service.require_scenario(db, scenario_id, writable=writable)
    permission_service.require_scenario_permission(
        db,
        scenario,
        "write" if writable else "read",
        message="没有当前业务场景的权限",
    )
    return scenario


def _llm(db: Session) -> LLMConfig | None:
    candidates = llm_service.routable_configs(db, "chat")
    selected_id = str(db.info.get("assistant_llm_config_id") or "")
    if selected_id:
        return next((candidate for candidate in candidates if candidate.id == selected_id), None)
    return candidates[0] if candidates else None


def _configure_assistant_runtime(db: Session, payload: AssistantChatRequest) -> None:
    """Resolve optional assistant capabilities once for this request.

    The selected model is still subject to the normal tenant visibility and
    routing checks.  Skills/MCP entries only become prompt context here; they
    never bypass the governed runtime tool registry.
    """
    if payload.llm_config_id:
        selected = next(
            (item for item in llm_service.routable_configs(db, "chat") if item.id == payload.llm_config_id),
            None,
        )
        if not selected:
            raise HTTPException(409, "所选 AI 模型不可用或未启用 chat 能力")
        db.info["assistant_llm_config_id"] = selected.id


def _assistant_capability_context(db: Session, payload: AssistantChatRequest) -> str:
    selected_skills = []
    if payload.skill_ids:
        selected_skills = db.execute(
            select(Skill)
            .where(
                Skill.id.in_(payload.skill_ids),
                Skill.enabled.is_(True),
                tenant_service.visible_clause(Skill, db),
            )
            .order_by(Skill.name)
        ).scalars().all()
    selected_mcps = []
    if payload.mcp_ids:
        selected_mcps = db.execute(
            select(MCPConfig)
            .where(
                MCPConfig.id.in_(payload.mcp_ids),
                MCPConfig.enabled.is_(True),
                tenant_service.visible_clause(MCPConfig, db),
            )
            .order_by(MCPConfig.name)
        ).scalars().all()
    lines = ["\n\n【本次助手能力配置】"]
    lines.append(
        "模型："
        + ("已按本次请求选择专用模型。" if payload.llm_config_id else "使用平台默认可用模型。")
    )
    lines.append(
        "技能："
        + ("、".join(item.name for item in selected_skills) if selected_skills else "未指定")
    )
    lines.append(
        "MCP："
        + ("、".join(item.name for item in selected_mcps) if selected_mcps else "未指定")
    )
    lines.append(
        "技能和 MCP 只能在本轮工具定义明确提供时调用；配置本身不会绕过权限、确认或平台受控执行流程。"
    )
    return "\n".join(lines)


def _scenario_context(db: Session, scenario: BusinessScenario | None) -> str:
    if not scenario:
        return "当前未打开具体业务场景。"

    def safe_value(value: Any) -> Any:
        return release_service.safe_snapshot_content({"value": value}).get("value")

    def short(value: Any, maximum: int = 500) -> str:
        sanitized = safe_value(value)
        if isinstance(sanitized, (dict, list)):
            text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(sanitized or "")
        return text if len(text) <= maximum else text[:maximum] + "…"

    def schema_fields(value: Any) -> str:
        sanitized = safe_value(value)
        schema = sanitized if isinstance(sanitized, dict) else {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        names = [short(name, 120) for name in properties]
        return "、".join(names[:12]) + (f" 等 {len(names)} 项" if len(names) > 12 else "") if names else "无字段"

    entities = list(getattr(scenario, "entities", []) or [])
    relations = list(getattr(scenario, "relations", []) or [])
    entity_names = {str(item.id): short(item.name, 200) for item in entities}
    visible_properties = {
        str(entity.id): [
            prop for prop in list(entity.properties)
            if permission_service.can_read_property(db, prop)
        ]
        for entity in entities
    }
    visible_property_names = {
        entity_id: {str(prop.name) for prop in properties}
        for entity_id, properties in visible_properties.items()
    }
    actions = [
        item for item in list(getattr(scenario, "actions", []) or [])
        if permission_service.check_action(db, item, "read").allowed
    ]
    action_names = {str(item.id): short(item.name, 200) for item in actions}
    rule_names = {str(item.id): short(item.name, 200) for item in list(getattr(scenario, "rules", []) or [])}
    event_names = {str(item.id): short(item.name, 200) for item in list(getattr(scenario, "events", []) or [])}
    lines = [
        f"业务场景：{short(scenario.name, 200)}",
        f"场景说明：{short(scenario.description, 1_000) or '暂无'}",
    ]
    if scenario.industry:
        lines.append(f"所属行业：{short(scenario.industry, 200)}")
    lines.append(
        "当前资源统计："
        f"对象 {len(entities)}，关系 {len(relations)}，"
        f"函数 {len(list(getattr(scenario, 'function_definitions', []) or []))}，"
        f"操作 {len(action_names)}，规则 {len(rule_names)}，事件 {len(event_names)}，"
        f"工作流 {len([item for item in list(getattr(scenario, 'workflows', []) or []) if permission_service.check_workflow(db, item, 'read').allowed])}，"
        f"对象映射 {len(list(getattr(scenario, 'data_mappings', []) or []))}，"
        f"关系映射 {len(list(getattr(scenario, 'relation_data_mappings', []) or []))}。"
    )
    if entities:
        lines.append("已有本体实体：")
        for entity in entities[:40]:
            props = "、".join(
                f"{short(prop.name, 200)}({short(prop.data_type, 80)}{'/主键' if prop.is_key else ''}{'/标题' if prop.is_title else ''})"
                for prop in visible_properties.get(str(entity.id), [])[:20]
            ) or "暂无属性"
            state_property = (
                short(entity.state_property, 200)
                if str(entity.state_property or "")
                in visible_property_names.get(str(entity.id), set())
                else ""
            )
            lines.append(
                f"- {short(entity.name, 200)}{'（抽象）' if entity.is_abstract else ''}：{props}；"
                f"状态属性：{state_property or '无或已受限'}"
            )
    if relations:
        lines.append("已有关系：")
        for relation in relations[:40]:
            constraints = ontology_service.normalize_relation_constraints(
                relation.constraints or {}, relation_type=relation.relation_type
            )
            axiom_labels = [
                label for key, label in (
                    ("symmetric", "对称"), ("transitive", "传递"),
                    ("irreflexive", "反自反"), ("asymmetric", "非对称"),
                    ("antisymmetric", "反对称"), ("acyclic", "无环"),
                ) if constraints.get(key)
            ]
            suffix = f"；约束：{'、'.join(axiom_labels)}" if axiom_labels else ""
            lines.append(
                f"- {short(relation.name, 200)}：{entity_names.get(str(relation.source_entity_id), '未知对象')}"
                f" → {entity_names.get(str(relation.target_entity_id), '未知对象')}"
                f"（{short(relation.relation_type, 80)}{suffix}）"
            )
    functions = list(getattr(scenario, "function_definitions", []) or [])
    if functions:
        lines.append("已有函数：")
        for item in functions[:30]:
            lines.append(
                f"- {short(item.name, 200)}：{short(item.description, 500) or '暂无说明'}；"
                f"运行方式 {short(item.runtime_kind, 80)}；"
                f"输入 {schema_fields(item.input_schema)}；输出 {schema_fields(item.output_schema)}"
            )
    if actions:
        lines.append("已有操作：")
        for item in actions[:30]:
            lines.append(
                f"- {short(item.name, 200)}：对象 {entity_names.get(str(item.entity_id), '未知对象')}；"
                f"执行器 {short(item.executor_type, 80)}；{'已启用' if item.enabled else '已停用'}；"
                f"输入 {schema_fields(item.input_schema)}；前置 {short(item.precondition, 240) or '无'}；"
                f"后置 {short(item.postcondition, 240) or '无'}"
            )
    rules = list(getattr(scenario, "rules", []) or [])
    if rules:
        lines.append("已有规则：")
        for item in rules[:30]:
            lines.append(
                f"- {short(item.name, 200)}：对象 {entity_names.get(str(item.entity_id), '场景级')}；"
                f"级别 {short(item.severity, 80)}；{'已启用' if item.enabled else '已停用'}；"
                f"条件 {short(item.condition)}；命中结果 {short(item.action_on_match, 240) or '无'}；"
                f"触发操作 {'、'.join(action_names.get(str(value), '受限或未知操作') for value in (item.trigger_action_ids or [])) or '无'}"
            )
    events = list(getattr(scenario, "events", []) or [])
    if events:
        lines.append("已有事件：")
        for item in events[:30]:
            lines.append(
                f"- {short(item.name, 200)}：{'已启用' if item.enabled else '已停用'}；"
                f"载荷 {schema_fields(item.payload_schema)}；触发来源 {short(item.trigger_source, 240) or '无'}"
            )
    workflows = [
        item for item in list(getattr(scenario, "workflows", []) or [])
        if permission_service.check_workflow(db, item, "read").allowed
    ]
    if workflows:
        lines.append("已有工作流：")
        for item in workflows[:30]:
            referenced: list[str] = []
            for node in item.nodes or []:
                data = node.get("data") if isinstance(node, dict) and isinstance(node.get("data"), dict) else {}
                node_type = str(node.get("type") or "") if isinstance(node, dict) else ""
                resource_id = str(data.get(f"{node_type}_id") or "")
                names = {"action": action_names, "rule": rule_names, "event": event_names}.get(node_type, {})
                if resource_id:
                    referenced.append(names.get(resource_id, "受限或未知引用"))
            lines.append(
                f"- {short(item.name, 200)}：{short(item.trigger_type, 80)} 触发；"
                f"状态 {short(item.status, 80)}；"
                f"{'已启用' if item.enabled else '已停用'}；{len(item.nodes or [])} 个节点；"
                f"引用 {'、'.join(dict.fromkeys(referenced)) or '无'}"
            )
    tenant_id = tenant_service.current_tenant_id(db)
    data_sources = [
        item for item in list(getattr(scenario, "data_sources", []) or [])
        if item.tenant_id == tenant_id or item.is_public
    ]
    visible_source_ids = {str(item.id) for item in data_sources}
    if data_sources:
        lines.append("当前场景数据源（不含连接密钥）：")
        for item in data_sources[:20]:
            lines.append(
                f"- {short(item.name, 200)}：{short(item.type, 80)}；"
                f"状态 {short(item.status, 80)}"
            )
    mappings = [
        item for item in list(getattr(scenario, "data_mappings", []) or [])
        if str(item.data_source_id) in visible_source_ids
    ]
    if mappings:
        lines.append("已有对象数据映射：")
        for item in mappings[:40]:
            source = getattr(item, "data_source", None)
            safe_column_map = {
                str(property_name): column_name
                for property_name, column_name in (item.column_map or {}).items()
                if str(property_name) in visible_property_names.get(str(item.entity_id), set())
            }
            lines.append(
                f"- {entity_names.get(str(item.entity_id), '未知对象')} ← "
                f"{short(getattr(source, 'name', ''), 200) or '未知数据源'}."
                f"{short(item.table_name, 300)}；字段 {short(safe_column_map, 700)}；"
                f"状态 {short(item.status, 80)}"
            )
    relation_mappings = [
        item for item in list(getattr(scenario, "relation_data_mappings", []) or [])
        if not item.data_source_id or str(item.data_source_id) in visible_source_ids
    ]
    if relation_mappings:
        relation_names = {str(item.id): str(item.name) for item in relations}
        lines.append("已有关系数据映射：")
        for item in relation_mappings[:40]:
            lines.append(
                f"- {short(relation_names.get(str(item.relation_id), '未知关系'), 200)}："
                f"{short(item.mode, 80)}；表 {short(item.table_name, 300) or '沿用对象映射'}；"
                f"状态 {short(item.status, 80)}"
            )
    return "\n".join(lines)


def _scenario_revision(scenario: BusinessScenario) -> str:
    """Hash complete mutable definitions without persisting their raw secrets."""

    def stable(value: Any) -> Any:
        if isinstance(value, datetime):
            normalized = (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )
            return normalized.isoformat()
        if isinstance(value, dict):
            return {str(key): stable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [stable(item) for item in value]
        return value

    def fields(item: Any, names: tuple[str, ...]) -> dict[str, Any]:
        return {name: stable(getattr(item, name, None)) for name in names}

    entities = []
    for entity in sorted(
        list(getattr(scenario, "entities", []) or []),
        key=lambda item: (str(getattr(item, "id", "")), str(getattr(item, "name", ""))),
    ):
        entity_data = fields(
            entity,
            ("id", "name", "namespace", "description", "icon", "color", "is_abstract", "state_property"),
        )
        entity_data["properties"] = [
            fields(
                prop,
                (
                    "id", "name", "data_type", "description", "is_key", "is_title", "is_required",
                    "is_enum", "enum_values", "default_value", "constraints", "is_sensitive",
                ),
            )
            for prop in sorted(
                list(getattr(entity, "properties", []) or []),
                key=lambda item: (str(getattr(item, "id", "")), str(getattr(item, "name", ""))),
            )
        ]
        entities.append(entity_data)
    definition = {
        "scenario": fields(
            scenario,
            ("id", "name", "description", "industry", "namespace", "status", "updated_at"),
        ),
        "entities": entities,
        "relations": [
            fields(item, ("id", "name", "namespace", "source_entity_id", "target_entity_id", "relation_type", "constraints", "description"))
            for item in sorted(list(getattr(scenario, "relations", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "data_sources": [
            # connector_revision is the governed, non-secret marker for every
            # runtime-relevant configuration change.  Do not copy credentials
            # into a proposal/job fingerprint.
            fields(item, ("id", "name", "type", "connector_revision", "status"))
            for item in sorted(list(getattr(scenario, "data_sources", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "mappings": [
            fields(item, ("id", "entity_id", "data_source_id", "data_source_binding_key", "data_source_binding_ref", "table_name", "column_map", "transform_rules"))
            for item in sorted(list(getattr(scenario, "data_mappings", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "relation_mappings": [
            fields(item, ("id", "relation_id", "source_mapping_id", "target_mapping_id", "mode", "data_source_id", "data_source_binding_key", "data_source_binding_ref", "table_name", "foreign_key_column", "source_key_column", "target_key_column"))
            for item in sorted(list(getattr(scenario, "relation_data_mappings", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "functions": [
            fields(item, ("id", "name", "description", "input_schema", "output_schema", "tags", "visibility", "runtime_kind", "runtime_config"))
            for item in sorted(list(getattr(scenario, "function_definitions", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "actions": [
            fields(item, ("id", "entity_id", "name", "description", "input_schema", "executor_type", "executor_config", "precondition", "postcondition", "enabled", "requires_confirmation", "idempotency_required", "permission_scope", "access_scope"))
            for item in sorted(list(getattr(scenario, "actions", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "rules": [
            fields(item, ("id", "entity_id", "name", "description", "condition", "action_on_match", "trigger_action_ids", "severity", "enabled"))
            for item in sorted(list(getattr(scenario, "rules", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "events": [
            fields(item, ("id", "name", "description", "payload_schema", "trigger_source", "enabled"))
            for item in sorted(list(getattr(scenario, "events", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "workflows": [
            fields(item, ("id", "name", "description", "trigger_type", "trigger_config", "steps", "nodes", "edges", "status", "enabled", "access_scope"))
            for item in sorted(list(getattr(scenario, "workflows", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
    }
    canonical = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _scenario_snapshot(scenario: BusinessScenario) -> dict[str, Any]:
    """Generate a complete definition fingerprint plus display-level diff keys."""
    return {
        "revision": _scenario_revision(scenario),
        "entity_names": sorted(str(entity.name) for entity in getattr(scenario, "entities", []) or []),
        "relation_names": sorted(str(relation.name) for relation in getattr(scenario, "relations", []) or []),
        "workflow_names": sorted(str(workflow.name) for workflow in getattr(scenario, "workflows", []) or []),
        "function_names": sorted(str(item.name) for item in getattr(scenario, "function_definitions", []) or []),
        "mapping_keys": sorted(
            f"{mapping.entity_id}:{mapping.data_source_id}:{mapping.table_name}"
            for mapping in getattr(scenario, "data_mappings", [])
        ),
    }


def _snapshot_matches(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    """Require a complete server-generated definition revision."""
    if expected.get("revision"):
        return expected["revision"] == current.get("revision")
    # Historic name-only snapshots cannot prove that properties, transforms,
    # Action definitions or workflow nodes stayed unchanged.  Fail closed and
    # require regeneration rather than applying against an unverifiable base.
    return False


def _claim_proposal_application(
    db: Session,
    *,
    proposal_id: str,
    thread_id: str,
    message_id: str,
    kind: str,
) -> tuple[AssistantProposalApplication, bool]:
    """Atomically own a proposal application or return its committed replay."""
    tenant_id = _tenant(db)
    claim = AssistantProposalApplication(
        proposal_id=proposal_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        message_id=message_id,
        kind=kind,
        status="applying",
        applied_by_user_id=_current_user_id(db),
    )
    db.add(claim)
    try:
        db.flush()
        return claim, True
    except IntegrityError:
        db.rollback()
        existing = db.get(AssistantProposalApplication, proposal_id)
        if (
            existing
            and existing.tenant_id == tenant_id
            and existing.thread_id == thread_id
            and existing.message_id == message_id
            and existing.kind == kind
        ):
            return existing, False
        raise HTTPException(409, "变更草稿的应用身份发生冲突，请重新生成")


def _build_proposal(
    kind: str,
    data: dict[str, Any],
    scenario: BusinessScenario | None = None,
) -> dict[str, Any]:
    """将生成结果包装成可审计、可确认的 Change Set。"""
    snapshot = _scenario_snapshot(scenario) if scenario else {"scenario_id": None}
    changes: list[dict[str, Any]] = []
    if kind == "scenario":
        name = str(data.get("name") or "未命名业务场景").strip()
        changes.append(
            {
                "operation": "add",
                "resource": "scenario",
                "name": name,
                "summary": "新增草稿业务场景；附件仍只保留在助手临时上下文",
            }
        )
        title = "业务场景创建草稿"
        summary = f"建议创建草稿场景“{name}”，确认前不会写入正式场景。"
    elif kind == "ontology":
        if not scenario:
            raise ValueError("本体草稿必须绑定业务场景")
        existing_entity_map = {
            str(entity.name): entity
            for entity in (getattr(scenario, "entities", None) or [])
        }
        existing_entities = set(existing_entity_map)
        existing_relations = set(snapshot["relation_names"])
        for entity in data.get("entities") or []:
            name = str(entity.get("name") or "未命名实体").strip()
            exists = name in existing_entities
            existing_property_names = {
                str(prop.name)
                for prop in (
                    getattr(existing_entity_map.get(name), "properties", None) or []
                )
            }
            generated_properties = entity.get("properties") or []
            missing_properties = [
                prop
                for prop in generated_properties
                if str(prop.get("name") or "").strip() not in existing_property_names
            ]
            operation = "add"
            if exists:
                operation = "update" if missing_properties else "skip"
            changes.append(
                {
                    "operation": operation,
                    "resource": "entity",
                    "name": name,
                    "summary": (
                        f"对象类型已存在，补充 {len(missing_properties)} 个新属性"
                        if exists and missing_properties
                        else "对象类型和属性均已存在，应用时跳过"
                        if exists
                        else f"新增对象类型，包含 {len(generated_properties)} 个属性"
                    ),
                }
            )
            for prop in missing_properties if exists else generated_properties:
                changes.append(
                    {
                        "operation": "add",
                        "resource": "property",
                        "name": f"{name}.{str(prop.get('name') or '未命名属性')}",
                        "summary": "向已有对象类型补充属性" if exists else "随对象类型新增属性",
                    }
                )
            existing_entities.add(name)
        for relation in data.get("relations") or []:
            name = str(relation.get("name") or "未命名关系").strip()
            exists = name in existing_relations
            changes.append(
                {
                    "operation": "skip" if exists else "add",
                    "resource": "relation",
                    "name": name,
                    "summary": "关系已存在，应用时跳过" if exists else f"新增关系：{relation.get('source', '')} → {relation.get('target', '')}",
                }
            )
            existing_relations.add(name)
        title = "本体建模变更草稿"
        summary = (
            f"建议新增 {sum(1 for item in changes if item['operation'] == 'add' and item['resource'] == 'entity')} 个对象类型，"
            f"更新 {sum(1 for item in changes if item['operation'] == 'update' and item['resource'] == 'entity')} 个已有对象类型，"
            f"并新增 {sum(1 for item in changes if item['operation'] == 'add' and item['resource'] == 'relation')} 条关系。"
        )
    elif kind == "mapping":
        if not scenario:
            raise ValueError("数据映射草稿必须绑定业务场景")
        mapping_key = (
            f"{data.get('entity_id', '')}:{data.get('data_source_id', '')}:"
            f"{data.get('table_name', '')}"
        )
        exists = mapping_key in set(snapshot.get("mapping_keys") or [])
        changes.append(
            {
                "operation": "update" if exists else "add",
                "resource": "mapping",
                "name": f"{data.get('entity_name') or data.get('entity_id')} ← {data.get('table_name') or '未选择表'}",
                "summary": (
                    f"{'更新' if exists else '新增'}数据映射，"
                    f"覆盖 {len(data.get('column_map') or {})} 个本体属性"
                ),
            }
        )
        for property_name, source_column in (data.get("column_map") or {}).items():
            changes.append(
                {
                    "operation": "update" if exists else "add",
                    "resource": "mapping_field",
                    "name": str(property_name),
                    "summary": f"映射到源字段 {source_column}",
                }
            )
        title = "数据映射变更草稿"
        summary = (
            f"建议将“{data.get('data_source_name') or data.get('data_source_id')}”的"
            f"“{data.get('table_name') or '未选择表'}”映射到"
            f"“{data.get('entity_name') or data.get('entity_id')}”。"
        )
    elif kind == "workflow":
        if not scenario:
            raise ValueError("工作流草稿必须绑定业务场景")
        workflow_name = str(data.get("name") or "AI 生成工作流").strip()
        changes.append(
            {
                "operation": "add",
                "resource": "workflow",
                "name": workflow_name,
                "summary": "新增一个草稿状态的工作流，不会立即执行",
            }
        )
        for node in data.get("nodes") or []:
            node_name = str(node.get("name") or node.get("label") or node.get("id") or "未命名节点")
            changes.append(
                {
                    "operation": "add",
                    "resource": "workflow_node",
                    "name": node_name,
                    "summary": f"新增 {node.get('type') or '业务'} 节点",
                }
            )
        for edge in data.get("edges") or []:
            changes.append(
                {
                    "operation": "add",
                    "resource": "workflow_edge",
                    "name": f"{edge.get('source', '')} → {edge.get('target', '')}",
                    "summary": edge.get("label") or "新增流程连线",
                }
            )
        title = "工作流编排变更草稿"
        summary = f"建议生成 {len(data.get('nodes') or [])} 个节点和 {len(data.get('edges') or [])} 条连线。"
    elif kind == "scenario_model":
        if not scenario:
            raise ValueError("复合业务模型必须绑定业务场景")
        # One compiler result becomes a resumable task board.  The original
        # resource/change payload remains intact so legacy clients can still
        # use the atomic whole-proposal path when they omit task_id.
        data = _refresh_model_task_states(
            scenario_model_compiler.attach_model_task_plan(data)
        )
        changes = list(data.get("changes") or [])
        blocking = [
            item for item in (data.get("unresolved") or [])
            if item.get("blocking", True)
        ]
        coverage = data.get("coverage_summary") or {}
        title = "完整业务模型任务清单"
        summary = (
            f"已从 {len(data.get('source_manifest') or [])} 份文档编译 "
            f"{len(changes)} 项变更，拆成 {len(data.get('tasks') or [])} 个可独立确认的任务，"
            f"覆盖 {coverage.get('total', 0)} 个来源段落；"
            + (
                f"仍有 {len(blocking)} 个阻塞项；阻塞只关联对应任务，安全部分仍可应用，"
                "问题和解决建议会留在当前会话。"
                if blocking
                else "引用、冲突和来源覆盖已通过预检，可按任务逐项应用。"
            )
        )
    else:
        raise ValueError("不支持的助手草稿类型")
    proposal_id = uuid.uuid4().hex
    proposal_status = "pending"
    requires_confirmation = True
    if kind == "scenario_model":
        data["run_id"] = proposal_id
        execution_status = str(data.get("execution_status") or "")
        proposal_status = (
            execution_status
            if execution_status in {
                "completed",
                "completed_with_gaps",
                "completed_no_changes",
            }
            else "in_progress"
        )
        requires_confirmation = bool(data.get("current_task_id"))
    return {
        "proposal_id": proposal_id,
        "kind": kind,
        "title": title,
        "summary": summary,
        "payload": data,
        "changes": changes,
        "base_snapshot": snapshot,
        "requires_confirmation": requires_confirmation,
        "status": proposal_status,
    }


def _find_saved_proposal(db: Session, thread_id: str, proposal_id: str) -> tuple[AssistantThread, AssistantMessage, dict[str, Any]]:
    thread = _thread(db, thread_id)
    messages = db.execute(
        select(AssistantMessage).where(
            AssistantMessage.thread_id == thread.id,
            AssistantMessage.role == "assistant",
        )
        .order_by(AssistantMessage.created_at.desc())
        .execution_options(populate_existing=True)
        .with_for_update()
    ).scalars().all()
    for message in messages:
        proposal = message.proposal if isinstance(message.proposal, dict) else {}
        if proposal.get("proposal_id") == proposal_id:
            if _has_invalid_historic_rag_source(db, thread, message):
                raise HTTPException(409, "变更草稿引用的资料已不在当前访问范围，请重新生成")
            context = message.context if isinstance(message.context, dict) else {}
            compilation_job_id = str(context.get("compilation_job_id") or "")
            if compilation_job_id:
                job = _scoped_compilation_job_for_message(
                    db,
                    message,
                    compilation_job_id,
                )
                if (
                    job is not None
                    and job.status == "succeeded"
                    and isinstance(job.result, dict)
                    and job.result.get("proposal_id") == proposal_id
                ):
                    canonical_thread, canonical_message = (
                        _matching_compilation_proposal_message(db, job)
                    )
                    if canonical_thread is None or canonical_message is None:
                        raise HTTPException(409, "编译结果的权威草稿不存在或不一致，请重新生成")
                    if _has_invalid_historic_rag_source(
                        db,
                        canonical_thread,
                        canonical_message,
                    ):
                        raise HTTPException(409, "变更草稿引用的资料已不在当前访问范围，请重新生成")
                    thread = canonical_thread
                    message = canonical_message
                    proposal = (
                        message.proposal
                        if isinstance(message.proposal, dict)
                        else {}
                    )
            # Applying owns its write only after the unique claim below.
            # Avoid taking a SQLite write lock for staging before another
            # committed claimant can be replayed.
            proposal, upgraded = _upgrade_saved_scenario_model_plan(
                db,
                message,
                materialize=False,
                persist=False,
            )
            return thread, message, proposal
    raise HTTPException(404, "变更草稿不存在或已过期，请重新生成")


def _proposal_application_key(proposal_id: str, task_id: str | None) -> str:
    """Give each resumable task its own replay/concurrency identity."""
    if not task_id:
        return proposal_id
    return hashlib.sha256(f"{proposal_id}:{task_id}".encode("utf-8")).hexdigest()


def _scenario_model_resource_ids(
    scenario: BusinessScenario,
    proposal_payload: dict[str, Any],
) -> dict[str, str]:
    """Resolve generated references only after their prerequisite task exists."""
    collections = {
        "entities": getattr(scenario, "entities", []) or [],
        "relations": getattr(scenario, "relations", []) or [],
        "actions": getattr(scenario, "actions", []) or [],
        "rules": getattr(scenario, "rules", []) or [],
        "events": getattr(scenario, "events", []) or [],
        "workflows": getattr(scenario, "workflows", []) or [],
    }
    by_name = {
        (section, str(item.name)): str(item.id)
        for section, items in collections.items()
        for item in items
        if str(getattr(item, "name", "") or "")
    }
    result: dict[str, str] = {}
    for section, items in collections.items():
        for item in proposal_payload.get(section) or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key:
                continue
            existing_id = str(item.get("existing_id") or "")
            if existing_id and any(str(candidate.id) == existing_id for candidate in items):
                result[key] = existing_id
                continue
            name = str(item.get("name") or "")
            if name and (section, name) in by_name:
                result[key] = by_name[(section, name)]
    return result


def _rewrite_persisted_task_references(
    value: Any,
    resource_ids: dict[str, str],
) -> Any:
    """Rewrite only generated references whose prerequisite task was applied."""
    if isinstance(value, dict):
        if value.get("kind") == "generated":
            key = str(value.get("key") or "")
            resolved = resource_ids.get(key)
            if resolved:
                return {
                    "kind": "existing",
                    "id": resolved,
                    "display_name": str(value.get("display_name") or ""),
                }
        return {
            str(key): _rewrite_persisted_task_references(child, resource_ids)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_persisted_task_references(child, resource_ids) for child in value]
    return value


_MODEL_TASK_APPLIED_STATUSES = {"applied", "partially_applied"}
_MODEL_TASK_DRAFT_ONLY_STATUSES = {
    "deferred",
    "drafted_with_gaps",
    # Historic proposals used ``skipped``.  Treat it as a completed draft
    # decision so an older row can never recreate the dependency deadlock.
    "skipped",
}
_MODEL_TASK_TERMINAL_STATUSES = (
    _MODEL_TASK_APPLIED_STATUSES
    | _MODEL_TASK_DRAFT_ONLY_STATUSES
    | {"empty"}
)
_SCENARIO_MODEL_RESOURCE_SECTIONS = (
    "entities",
    "relations",
    "instances",
    "functions",
    "actions",
    "rules",
    "events",
    "workflows",
    "mappings",
    "relation_mappings",
    "conceptual_mappings",
)


def _safe_nonnegative_int(value: Any, default: int = 0) -> int:
    """Parse persisted lifecycle counters without trusting historic JSON."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(parsed, 0)


def _model_task_fields_are_well_formed(tasks: list[dict[str, Any]]) -> bool:
    """Reject scalar/list drift before task advancement touches persisted JSON."""
    counter_fields = (
        "order",
        "change_count",
        "output_count",
        "draft_output_count",
        "draft_candidate_count",
        "issue_count",
        "safe_change_count",
        "compiled_safe_change_count",
        "blocked_issue_count",
        "compiled_blocked_issue_count",
    )
    for task in tasks:
        dependencies = task.get("depends_on", [])
        waiting_for = task.get("waiting_for", [])
        issues = task.get("issues", [])
        change_keys = task.get("change_keys", [])
        if dependencies is None:
            dependencies = []
        if waiting_for is None:
            waiting_for = []
        if issues is None:
            issues = []
        if change_keys is None:
            change_keys = []
        if (
            not isinstance(dependencies, list)
            or not all(isinstance(value, str) for value in dependencies)
            or not isinstance(waiting_for, list)
            or not all(isinstance(value, str) for value in waiting_for)
            or not isinstance(issues, list)
            or not all(isinstance(value, dict) for value in issues)
            or not isinstance(change_keys, list)
            or not all(isinstance(value, str) for value in change_keys)
        ):
            return False
        for field in counter_fields:
            if field not in task or task.get(field) is None:
                continue
            value = task.get(field)
            if isinstance(value, bool):
                return False
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return False
            if field != "order" and parsed < 0:
                return False
    return True


_DATA_SOURCE_ISSUE_CODES = frozenset({
    "MISSING_DATA_SOURCE",
    "MAPPING_DEFERRED_NO_DATA_SOURCE",
    "DATA_SOURCE_NOT_CONFIGURED",
    "DATA_SOURCE_UNAVAILABLE",
    "MISSING_MAPPING_TABLE",
    "UNINSPECTED_RELATION_MAPPING_TABLE",
    "MISSING_RELATION_MAPPING_TABLE",
})


def _effective_model_issue_code(raw: dict[str, Any]) -> str:
    code = str(raw.get("code") or "DOCUMENT_AMBIGUITY").strip().upper()
    if code == "DOCUMENT_REPORTED_ISSUE":
        reported = str(raw.get("reported_code") or "").strip().upper()
        if reported:
            return reported
    return code or "DOCUMENT_AMBIGUITY"


def _model_issue_cause(code: str, message: str) -> str:
    normalized = str(code or "").strip().upper()
    text = str(message or "").casefold()
    if normalized in _DATA_SOURCE_ISSUE_CODES or (
        normalized == "MISSING_REFERENCE"
        and any(token in text for token in ("数据源", "物理表", "数据表", "data source"))
    ):
        return "data_source_dependency"
    return normalized.casefold() or "document_ambiguity"


def _aggregate_model_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for issue in issues:
        code = str(issue.get("effective_code") or issue.get("code") or "")
        cause = _model_issue_cause(code, str(issue.get("message") or ""))
        group = groups.get(cause)
        if group is None:
            data_source = cause == "data_source_dependency"
            group = {
                "cause": cause,
                "code": "DATA_SOURCE_DEPENDENCY" if data_source else code,
                "message": (
                    "数据源、物理表或字段尚未接入或绑定。"
                    if data_source
                    else str(issue.get("message") or "存在待补全信息")[:500]
                ),
                "count": 0,
                "blocking_count": 0,
                "affected_count": 0,
                "source_count": 0,
                "resolution_hint": (
                    "接入并检查数据源后，把现有逻辑映射绑定到真实表和字段；无需重建其他草稿。"
                    if data_source
                    else str(issue.get("resolution_hint") or "")[:500]
                ),
                "requires_followup": False,
                "_affected": set(),
                "_sources": set(),
            }
            groups[cause] = group
        group["count"] += 1
        if issue.get("blocking", True) is not False:
            group["blocking_count"] += 1
        group["requires_followup"] = bool(
            group["requires_followup"]
            or issue.get("blocking", True) is not False
            or cause == "data_source_dependency"
            or any(token in code.upper() for token in ("MISSING", "DEFERRED", "UNAVAILABLE"))
        )
        group["_affected"].update(
            str(value) for value in (issue.get("affected_change_keys") or []) if str(value)
        )
        group["_sources"].update(
            str(value) for value in (issue.get("source_refs") or []) if str(value)
        )
        if not group["resolution_hint"] and issue.get("resolution_hint"):
            group["resolution_hint"] = str(issue.get("resolution_hint"))[:500]

    result: list[dict[str, Any]] = []
    for group in groups.values():
        affected = group.pop("_affected")
        sources = group.pop("_sources")
        group["affected_count"] = len(affected) or int(group["count"])
        group["source_count"] = len(sources)
        result.append(group)
    return sorted(
        result,
        key=lambda item: (
            not bool(item.get("blocking_count")),
            -int(item.get("count") or 0),
            str(item.get("cause") or ""),
        ),
    )


def _model_task_execution_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the durable, user-facing summary for one modelling run."""
    raw_tasks = payload.get("tasks")
    tasks = [
        item for item in (raw_tasks if isinstance(raw_tasks, list) else [])
        if isinstance(item, dict)
    ]
    statuses = [str(item.get("status") or "pending") for item in tasks]
    processed = sum(status in _MODEL_TASK_TERMINAL_STATUSES for status in statuses)
    current = next(
        (item for item in tasks if item.get("status") in {"ready", "blocked"}),
        None,
    )

    unresolved = payload.get("unresolved")
    raw_issues = [
        copy.deepcopy(item)
        for item in (unresolved if isinstance(unresolved, list) else [])
        if isinstance(item, dict)
    ]
    for task in tasks:
        task_issues = task.get("issues")
        for issue in (task_issues if isinstance(task_issues, list) else []):
            if isinstance(issue, dict):
                raw_issues.append(copy.deepcopy(issue))
    issues: list[dict[str, Any]] = []
    seen_issues: set[tuple[str, str, tuple[str, ...]]] = set()
    for raw in raw_issues:
        raw_source_refs = raw.get("source_refs")
        issue = {
            "code": str(raw.get("code") or "DOCUMENT_AMBIGUITY")[:100],
            "reported_code": str(raw.get("reported_code") or "")[:100],
            "effective_code": _effective_model_issue_code(raw)[:100],
            "message": str(raw.get("message") or "存在待补全信息")[:500],
            "blocking": raw.get("blocking", True) is not False,
            "source_refs": [
                str(value)
                for value in (
                    raw_source_refs if isinstance(raw_source_refs, list) else []
                )
            ][:20],
            "resolution_hint": str(raw.get("resolution_hint") or "")[:500],
            "affected_change_keys": [
                str(value)
                for value in (
                    raw.get("affected_change_keys")
                    if isinstance(raw.get("affected_change_keys"), list)
                    else []
                )
            ][:100],
        }
        identity = (
            issue["effective_code"].casefold(),
            issue["message"],
            tuple(issue["source_refs"]),
        )
        if identity in seen_issues:
            continue
        seen_issues.add(identity)
        issues.append(issue)

    issue_groups = _aggregate_model_issues(issues)
    final = bool(tasks) and processed == len(tasks)
    blocking_issue_count = sum(item["blocking"] for item in issues)
    followup_issue_count = sum(
        int(item.get("count") or 0)
        for item in issue_groups
        if item.get("requires_followup")
    )
    applied_count = statuses.count("applied")
    partial_count = statuses.count("partially_applied")
    draft_only_count = sum(
        status in _MODEL_TASK_DRAFT_ONLY_STATUSES for status in statuses
    )
    empty_count = statuses.count("empty")
    formal_write_task_count = applied_count + partial_count
    has_gaps = bool(
        blocking_issue_count
        or followup_issue_count
        or partial_count
        or draft_only_count
    )
    status = (
        "completed_with_gaps"
        if final and has_gaps
        else "completed_no_changes"
        if final and formal_write_task_count == 0
        else "completed"
        if final
        else "waiting_for_confirmation"
        if current
        else "running"
    )
    if final and formal_write_task_count == 0:
        detail_parts = ["没有正式定义写入当前场景"]
        if draft_only_count:
            detail_parts.append(
                f"{draft_only_count} 项任务的候选保留为停用、不可发布的待校验草稿"
            )
        if empty_count:
            detail_parts.append(f"{empty_count} 项任务没有产生此类变更")
        if issue_groups:
            detail_parts.append(
                f"仍有 {len(issue_groups)} 类问题或说明（共 {len(issues)} 项，已按根因合并）"
            )
        message = f"全部 {len(tasks)} 项任务均已推进；" + "；".join(detail_parts) + "。"
    elif final and has_gaps:
        message = (
            f"全部 {len(tasks)} 项任务均已完成本轮确认；"
            f"其中 {applied_count} 项任务的正式定义已写入、{partial_count} 项仅写入了安全部分、"
            f"{draft_only_count} 项没有可安全写入的正式定义，候选仍是停用且不可发布的待校验草稿；"
            f"仍有 {len(issue_groups)} 类问题或说明（共 {len(issues)} 项，已按根因合并）。"
        )
    elif final:
        message = (
            f"全部 {len(tasks)} 项任务均已推进；"
            f"{applied_count} 项任务的正式定义已写入当前场景"
            + (f"，{empty_count} 项任务没有产生此类变更" if empty_count else "")
            + "。"
        )
    elif current:
        message = (
            f"计划仍在执行：已推进 {processed}/{len(tasks)} 项，当前停留在"
            f"「{current.get('title') or '当前任务'}」等待确认；确认前不会结束本计划。"
        )
    else:
        message = f"计划仍在执行：已推进 {processed}/{len(tasks)} 项，正在准备下一任务。"

    resolution_hints: list[str] = []
    for issue in issue_groups:
        hint = str(issue.get("resolution_hint") or "").strip()
        if hint and hint not in resolution_hints:
            resolution_hints.append(hint)
    return {
        "final": final,
        "status": status,
        "message": message,
        "total_task_count": len(tasks),
        "completed_task_count": processed,
        "applied_task_count": applied_count,
        "partially_applied_task_count": partial_count,
        "draft_only_task_count": draft_only_count,
        "empty_task_count": empty_count,
        "current_task_id": str(current.get("id") or "") if current else "",
        "current_task_title": str(current.get("title") or "") if current else "",
        "remaining_issue_count": len(issues),
        "remaining_issue_group_count": len(issue_groups),
        "blocking_issue_count": blocking_issue_count,
        "issue_groups": issue_groups,
        "remaining_issues": issues[:10],
        "resolution_hints": resolution_hints[:8],
    }


def _model_run_context_status(summary: Any) -> str:
    if isinstance(summary, dict) and summary.get("final"):
        formal_write_task_count = (
            _safe_nonnegative_int(summary.get("applied_task_count"))
            + _safe_nonnegative_int(summary.get("partially_applied_task_count"))
        )
        if formal_write_task_count == 0:
            return "no_changes"
    return (
        "success"
        if isinstance(summary, dict) and summary.get("final")
        else "waiting_confirmation"
    )


def _refresh_model_task_states(
    payload: dict[str, Any],
    *,
    applied_task_id: str = "",
    applied_status: str = "",
) -> dict[str, Any]:
    """Advance exactly one durable task without letting gaps deadlock the run.

    Every non-empty generated task remains current until the user confirms it.
    Confirmation may persist formal resources, inactive working resources, or
    both; validation gaps never silently skip a task or end the plan.
    """
    result = copy.deepcopy(payload)
    plan_list_fields = (
        "entities",
        "relations",
        "instances",
        "functions",
        "actions",
        "rules",
        "events",
        "workflows",
        "mappings",
        "relation_mappings",
        "conceptual_mappings",
        "draft_candidates",
        "changes",
        "unresolved",
        "coverage",
    )
    malformed_plan_source = any(
        key in result
        and result.get(key) is not None
        and (
            not isinstance(result.get(key), list)
            or not all(isinstance(item, dict) for item in result.get(key))
        )
        for key in plan_list_fields
    )
    plan_source = copy.deepcopy(result)
    for key in plan_list_fields:
        raw_values = plan_source.get(key)
        plan_source[key] = (
            [item for item in raw_values if isinstance(item, dict)]
            if isinstance(raw_values, list)
            else []
        )
    generated_tasks = scenario_model_compiler.build_model_task_plan(plan_source)
    original_tasks = result.get("tasks") or generated_tasks
    malformed_task_shape = not isinstance(original_tasks, list) or not all(
        isinstance(item, dict) for item in original_tasks
    )

    def task_order(item: dict[str, Any]) -> int:
        try:
            return int(item.get("order") or 0)
        except (TypeError, ValueError):
            return 0

    tasks = sorted(
        generated_tasks
        if malformed_task_shape
        else [copy.deepcopy(item) for item in original_tasks],
        key=task_order,
    )
    now = datetime.now(timezone.utc).isoformat()

    def finish_malformed_plan(code: str, message: str) -> dict[str, Any]:
        issue = {
            "code": code,
            "message": message,
            "blocking": True,
            "source_refs": [],
            "resolution_hint": (
                "现有建模草稿和问题清单已经保留；请基于当前场景重新编译任务计划后继续优化。"
            ),
        }
        raw_unresolved = result.get("unresolved")
        unresolved = [
            copy.deepcopy(value)
            for value in (
                raw_unresolved if isinstance(raw_unresolved, list) else []
            )
            if isinstance(value, dict)
        ]
        if not any(str(value.get("code") or "") == code for value in unresolved):
            unresolved.append(copy.deepcopy(issue))
        result["unresolved"] = unresolved
        for task in tasks:
            status = str(task.get("status") or "pending")
            if status not in _MODEL_TASK_TERMINAL_STATUSES:
                existing_issues = task.get("issues")
                task["status"] = "drafted_with_gaps"
                task["completed_at"] = task.get("completed_at") or now
                task["issues"] = [
                    *[
                        copy.deepcopy(value)
                        for value in (
                            existing_issues
                            if isinstance(existing_issues, list)
                            else []
                        )
                        if isinstance(value, dict)
                        and str(value.get("code") or "") != code
                    ],
                    copy.deepcopy(issue),
                ]
                task["safe_change_count"] = 0
                task["apply_result"] = task.get("apply_result") or {
                    "kind": "scenario_model",
                    "task_id": str(task.get("id") or ""),
                    "task_status": "drafted_with_gaps",
                    "draft_preserved": True,
                    "remaining_blockers": task["issues"],
                }
        result["tasks"] = tasks
        summary = _model_task_execution_summary(result)
        result["execution_summary"] = summary
        result["execution_status"] = summary["status"]
        result["current_task_id"] = ""
        result["execution_revision"] = (
            _safe_nonnegative_int(result.get("execution_revision")) + 1
        )
        result["next_action"] = {
            "type": "refine_model",
            "requires_confirmation": False,
            "message": issue["resolution_hint"],
        }
        return result

    task_ids = [str(item.get("id") or "") for item in tasks]
    normalized_task_ids = [value.strip() for value in task_ids]
    if (
        malformed_task_shape
        or malformed_plan_source
        or not _model_task_fields_are_well_formed(tasks)
        or any(
            isinstance(item.get("order", 0), bool)
            or not str(item.get("order", 0) or "0").strip().lstrip("-").isdigit()
            for item in tasks
        )
        or any(
            not value
            or len(value) > 100
            or value != original
            for original, value in zip(task_ids, normalized_task_ids)
        )
        or len(set(normalized_task_ids)) != len(normalized_task_ids)
    ):
        return finish_malformed_plan(
            "INVALID_TASK_PLAN",
            "建模任务计划包含空白、重复或无效的任务标识，无法安全地逐项写入。",
        )
    if applied_task_id:
        current = next((item for item in tasks if item.get("id") == applied_task_id), None)
        if current is not None:
            current["status"] = applied_status or "applied"
            current["applied_at"] = now
            current["completed_at"] = now

    task_by_id = {str(item.get("id") or ""): item for item in tasks}
    active_task_id = ""
    for item in tasks:
        status = str(item.get("status") or "pending")
        if status in _MODEL_TASK_TERMINAL_STATUSES:
            # Normalize the historic wording while retaining replay metadata.
            if status == "skipped":
                item["status"] = "deferred"
            continue

        base_issues = [
            copy.deepcopy(issue)
            for issue in (item.get("issues") or [])
            if isinstance(issue, dict)
            and str(issue.get("code") or "").upper()
            != "PREREQUISITE_DRAFT_ONLY"
        ]
        compiled_safe_count = _safe_nonnegative_int(
            item.get(
                "compiled_safe_change_count",
                item.get("safe_change_count", 0),
            )
        )
        item["compiled_safe_change_count"] = compiled_safe_count
        item["compiled_blocked_issue_count"] = _safe_nonnegative_int(
            item.get(
                "compiled_blocked_issue_count",
                sum(issue.get("blocking", True) is not False for issue in base_issues),
            )
        )
        item["issues"] = base_issues
        item["issue_count"] = max(
            _safe_nonnegative_int(item.get("issue_count")),
            len(base_issues),
        )
        item["safe_change_count"] = compiled_safe_count
        item["blocked_issue_count"] = max(
            item["compiled_blocked_issue_count"],
            sum(issue.get("blocking", True) is not False for issue in base_issues),
        )
        item.pop("waiting_for", None)

        output_count = _safe_nonnegative_int(
            item.get("output_count", item.get("change_count", 0))
        )
        draft_output_count = _safe_nonnegative_int(
            item.get("draft_output_count", 0)
        )

        # A task is empty only when it produced neither formal nor staging
        # resources. Draft-only instances and logical mappings are real task
        # output and must remain visible in the plan.
        if output_count <= 0:
            item["status"] = "empty"
            item["completed_at"] = item.get("completed_at") or now
            continue

        if (
            _safe_nonnegative_int(item.get("change_count")) <= 0
            and draft_output_count <= 0
            and not base_issues
        ):
            # The provider described only formal resources that already match
            # the scenario. There is nothing new for the user to confirm.
            item["status"] = "empty"
            item["completed_at"] = item.get("completed_at") or now
            continue

        dependencies = [str(value) for value in (item.get("depends_on") or [])]
        unfinished_dependencies = [
            dependency
            for dependency in dependencies
            if str((task_by_id.get(dependency) or {}).get("status") or "pending")
            not in _MODEL_TASK_TERMINAL_STATUSES
        ]
        if unfinished_dependencies:
            item["status"] = "waiting"
            item["waiting_for"] = unfinished_dependencies
            continue

        if active_task_id:
            item["status"] = "waiting"
            item["waiting_for"] = [active_task_id]
            continue
        item["status"] = (
            "blocked"
            if _safe_nonnegative_int(item.get("blocked_issue_count")) > 0
            else "ready"
        )
        active_task_id = str(item.get("id") or "")

    if not active_task_id and any(
        str(item.get("status") or "pending") not in _MODEL_TASK_TERMINAL_STATUSES
        for item in tasks
    ):
        return finish_malformed_plan(
            "INVALID_TASK_DEPENDENCY",
            "建模任务计划包含缺失、自引用或循环依赖，无法确定安全的下一项任务。",
        )

    result["tasks"] = tasks
    summary = _model_task_execution_summary(result)
    result["execution_summary"] = summary
    result["execution_status"] = summary["status"]
    result["current_task_id"] = summary["current_task_id"]
    result["execution_revision"] = (
        _safe_nonnegative_int(result.get("execution_revision")) + 1
    )
    current_task = next(
        (
            item for item in tasks
            if str(item.get("id") or "") == summary["current_task_id"]
        ),
        None,
    )
    if current_task is not None:
        safe_count = _safe_nonnegative_int(current_task.get("safe_change_count"))
        output_count = _safe_nonnegative_int(current_task.get("output_count"))
        result["next_action"] = {
            "type": "confirm_task",
            "task_id": str(current_task.get("id") or ""),
            "task_title": str(current_task.get("title") or ""),
            "requires_confirmation": True,
            "can_apply": output_count > 0,
            "can_apply_partial": (
                current_task.get("status") == "blocked" and safe_count > 0
            ),
            "can_defer": True,
        }
    elif summary["final"]:
        result["next_action"] = {
            "type": "refine_model",
            "requires_confirmation": False,
            "message": (
                "本轮没有正式定义写入；可补充资料或调整要求后重新建模。"
                if summary["status"] == "completed_no_changes"
                else "可基于已写入模型和保留草稿继续补充资料或修正定义。"
            ),
        }
    else:  # Defensive fallback; the malformed-plan branch above should own it.
        return finish_malformed_plan(
            "INVALID_TASK_STATE",
            "建模任务计划没有可继续执行的当前任务。",
        )
    return result


def _baseline_changed_compilation_data(
    data: dict[str, Any],
    *,
    expected_baseline: str,
    current_baseline: str,
) -> dict[str, Any]:
    """Make every compiled candidate review-only after its input baseline drifts."""
    result = copy.deepcopy(data)
    candidate_keys: set[str] = set()
    raw_candidates = result.get("draft_candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        candidate_payload = (
            raw_candidate.get("payload")
            if isinstance(raw_candidate.get("payload"), dict)
            else {}
        )
        resource_key = str(
            raw_candidate.get("resource_key")
            or candidate_payload.get("key")
            or candidate_payload.get("id")
            or candidate_payload.get("name")
            or ""
        ).strip()
        if resource_key:
            candidate_keys.add(resource_key)

    # Historic compiler results may predate the draft sidecar. Preserve their
    # formal-shaped objects as staging candidates before the public payload is
    # made inert below.
    for section in _SCENARIO_MODEL_RESOURCE_SECTIONS:
        values = result.get(section)
        for raw_resource in (values if isinstance(values, list) else []):
            if not isinstance(raw_resource, dict):
                continue
            resource_key = str(
                raw_resource.get("key")
                or raw_resource.get("id")
                or raw_resource.get("name")
                or ""
            ).strip()
            if resource_key:
                candidate_keys.add(resource_key)

    issue = {
        "code": "BASELINE_CHANGED_DURING_COMPILATION",
        "message": (
            "业务场景在本次编译期间发生变化；所有生成候选均已保留为惰性草稿，"
            "本轮不会写入任何正式模型定义。"
        ),
        "blocking": True,
        "source_refs": [],
        "affected_change_keys": sorted(candidate_keys),
        "resolution_hint": "请基于当前场景和已保存草稿继续编译或逐项修正。",
    }
    unresolved = [
        copy.deepcopy(value)
        for value in (
            result.get("unresolved")
            if isinstance(result.get("unresolved"), list)
            else []
        )
        if isinstance(value, dict)
        and str(value.get("code") or "")
        != "BASELINE_CHANGED_DURING_COMPILATION"
    ]
    result["unresolved"] = [*unresolved, copy.deepcopy(issue)]
    for raw_candidate in candidates:
        if not isinstance(raw_candidate, dict):
            continue
        candidate_issues = [
            copy.deepcopy(value)
            for value in (
                raw_candidate.get("validation_issues")
                if isinstance(raw_candidate.get("validation_issues"), list)
                else []
            )
            if isinstance(value, dict)
            and str(value.get("code") or "")
            != "BASELINE_CHANGED_DURING_COMPILATION"
        ]
        raw_candidate["validation_status"] = "needs_attention"
        raw_candidate["validation_issues"] = [
            *candidate_issues,
            copy.deepcopy(issue),
        ]
    result["baseline_guard"] = {
        "status": "changed_during_compilation",
        "expected_revision": str(expected_baseline or ""),
        "observed_revision": str(current_baseline or ""),
        "formal_change_count": 0,
    }
    return result


def _complete_baseline_changed_proposal(
    proposal: dict[str, Any],
    *,
    current_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Close a drifted run with preserved staging and no applicable changes."""
    result = copy.deepcopy(proposal)
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    now = datetime.now(timezone.utc).isoformat()
    baseline_issue = next(
        (
            copy.deepcopy(value)
            for value in (payload.get("unresolved") or [])
            if isinstance(value, dict)
            and str(value.get("code") or "")
            == "BASELINE_CHANGED_DURING_COMPILATION"
        ),
        {
            "code": "BASELINE_CHANGED_DURING_COMPILATION",
            "message": "业务场景在编译期间发生变化；生成内容仅保留为惰性草稿。",
            "blocking": True,
            "source_refs": [],
            "resolution_hint": "请基于当前场景和已保存草稿继续编译。",
        },
    )
    candidate_task_ids = {
        str(value.get("task_id") or "")
        for value in (
            payload.get("draft_candidates")
            if isinstance(payload.get("draft_candidates"), list)
            else []
        )
        if isinstance(value, dict) and str(value.get("task_id") or "")
    }
    for section in _SCENARIO_MODEL_RESOURCE_SECTIONS:
        payload[section] = []
    payload["changes"] = []
    tasks = payload.get("tasks")
    for task in (tasks if isinstance(tasks, list) else []):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        status = str(task.get("status") or "pending")
        if status == "empty" and task_id not in candidate_task_ids:
            continue
        task_issues = [
            copy.deepcopy(value)
            for value in (
                task.get("issues") if isinstance(task.get("issues"), list) else []
            )
            if isinstance(value, dict)
            and str(value.get("code") or "")
            != "BASELINE_CHANGED_DURING_COMPILATION"
        ]
        task["status"] = "drafted_with_gaps"
        task["completed_at"] = task.get("completed_at") or now
        task["safe_change_keys"] = []
        task["safe_change_count"] = 0
        task["compiled_safe_change_count"] = 0
        task["issues"] = [*task_issues, copy.deepcopy(baseline_issue)]
        task["blocked_issue_count"] = sum(
            value.get("blocking", True) is not False for value in task["issues"]
        )
        task["apply_result"] = {
            "kind": "scenario_model",
            "task_id": task_id,
            "task_status": "drafted_with_gaps",
            "draft_preserved": True,
            "safe_change_count": 0,
            "applied_change_keys": [],
            "remaining_blockers": task["issues"],
        }
    summary = _model_task_execution_summary(payload)
    payload["execution_summary"] = summary
    payload["execution_status"] = "completed_with_gaps"
    payload["current_task_id"] = ""
    payload["execution_revision"] = (
        _safe_nonnegative_int(payload.get("execution_revision")) + 1
    )
    payload["next_action"] = {
        "type": "refine_model",
        "requires_confirmation": False,
        "message": str(baseline_issue.get("resolution_hint") or ""),
    }
    result["payload"] = payload
    result["changes"] = []
    result["base_snapshot"] = copy.deepcopy(current_snapshot)
    result["requires_confirmation"] = False
    result["status"] = "completed_with_gaps"
    result["run_revision"] = _safe_nonnegative_int(
        payload.get("execution_revision")
    )
    result["summary"] = (
        "场景基线在编译期间发生变化；所有候选已保留为不可运行、不可发布的"
        "场景草稿，本轮正式变更为 0 项。"
    )
    return result


def _is_inert_compilation_salvage(data: dict[str, Any]) -> bool:
    """Return whether a compiler result only contains blocked staging drafts."""
    salvage = data.get("draft_salvage")
    candidates = data.get("draft_candidates")
    if not isinstance(salvage, dict) or not isinstance(candidates, list) or not candidates:
        return False
    if _safe_nonnegative_int(salvage.get("formal_change_count")) != 0:
        return False
    if any(data.get(section) for section in _SCENARIO_MODEL_RESOURCE_SECTIONS):
        return False
    if any(
        isinstance(change, dict)
        and str(change.get("operation") or "") in {"add", "update", "delete"}
        for change in (data.get("changes") or [])
    ):
        return False
    return all(
        isinstance(candidate, dict)
        and candidate.get("enabled") is False
        and candidate.get("publishable") is False
        and str(candidate.get("validation_status") or "") == "blocked"
        for candidate in candidates
    )


def _complete_inert_salvage_proposal(
    proposal: dict[str, Any],
) -> dict[str, Any]:
    """Close a zero-write salvage run while retaining its editable drafts."""
    result = copy.deepcopy(proposal)
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    now = datetime.now(timezone.utc).isoformat()
    candidate_task_ids = {
        str(candidate.get("task_id") or "")
        for candidate in (payload.get("draft_candidates") or [])
        if isinstance(candidate, dict) and str(candidate.get("task_id") or "")
    }
    tasks = payload.get("tasks")
    for task in (tasks if isinstance(tasks, list) else []):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        has_draft_output = (
            task_id in candidate_task_ids
            or _safe_nonnegative_int(task.get("draft_output_count")) > 0
        )
        if not has_draft_output:
            if str(task.get("status") or "") not in _MODEL_TASK_TERMINAL_STATUSES:
                task["status"] = "empty"
                task["completed_at"] = task.get("completed_at") or now
            continue
        issues = [
            copy.deepcopy(issue)
            for issue in (task.get("issues") or [])
            if isinstance(issue, dict)
        ]
        task["status"] = "drafted_with_gaps"
        task["completed_at"] = task.get("completed_at") or now
        task["safe_change_keys"] = []
        task["safe_change_count"] = 0
        task["compiled_safe_change_count"] = 0
        task["apply_result"] = {
            "kind": "scenario_model",
            "task_id": task_id,
            "task_status": "drafted_with_gaps",
            "draft_preserved": True,
            "safe_change_count": 0,
            "applied_change_keys": [],
            "remaining_blockers": issues,
        }
        task.pop("waiting_for", None)
    payload["tasks"] = tasks if isinstance(tasks, list) else []
    summary = _model_task_execution_summary(payload)
    payload["execution_summary"] = summary
    payload["execution_status"] = summary["status"]
    payload["current_task_id"] = ""
    payload["execution_revision"] = (
        _safe_nonnegative_int(payload.get("execution_revision")) + 1
    )
    payload["next_action"] = {
        "type": "refine_model",
        "requires_confirmation": False,
        "message": (
            "可直接修改已保存的停用草稿；补充资料或恢复模型能力后，"
            "再基于这些草稿继续编译。"
        ),
    }
    result["payload"] = payload
    result["changes"] = []
    result["requires_confirmation"] = False
    result["status"] = summary["status"]
    result["run_revision"] = payload["execution_revision"]
    draft_candidates = [
        candidate
        for candidate in (payload.get("draft_candidates") or [])
        if isinstance(candidate, dict)
    ]
    candidate_kinds = {
        str(candidate.get("resource_kind") or "").strip()
        for candidate in draft_candidates
        if str(candidate.get("resource_kind") or "").strip()
    }
    candidate_summary = f"{len(draft_candidates)} 个候选"
    if candidate_kinds:
        candidate_summary += f"（{len(candidate_kinds)} 类）"
    result["summary"] = (
        f"本轮正式变更为 0 项；{candidate_summary}已保留为停用、不可发布的可编辑草稿，"
        "待补充资料或恢复模型能力后继续。"
    )
    return result


def _attach_draft_materialization(
    proposal: dict[str, Any],
    materialization: dict[str, Any],
) -> dict[str, Any]:
    """Expose scene draft rows on their tasks without making them runnable."""
    result = copy.deepcopy(proposal)
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    public_materialization = {
        "resource_count": _safe_nonnegative_int(
            materialization.get("resource_count")
        ),
        "issue_count": _safe_nonnegative_int(materialization.get("issue_count")),
        "by_kind": copy.deepcopy(materialization.get("by_kind") or {}),
        "by_status": copy.deepcopy(materialization.get("by_status") or {}),
    }
    payload["draft_materialization"] = public_materialization
    ids_by_task = materialization.get("resource_ids_by_task")
    task_summary = materialization.get("by_task")
    ids_by_task = ids_by_task if isinstance(ids_by_task, dict) else {}
    task_summary = task_summary if isinstance(task_summary, dict) else {}
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        result["payload"] = payload
        return result

    refresh_needed = False
    synthetic_codes = {
        "DRAFT_ONLY_RESOURCE", "STAGED_RESOURCE_REQUIRES_VALIDATION",
    }

    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        raw_task_issues = task.get("issues")
        clean_issues = [
            copy.deepcopy(value)
            for value in (
                raw_task_issues if isinstance(raw_task_issues, list) else []
            )
            if isinstance(value, dict)
            and str(value.get("code") or "") not in synthetic_codes
        ]
        if clean_issues != (
            raw_task_issues if isinstance(raw_task_issues, list) else []
        ):
            refresh_needed = True
        task["issues"] = clean_issues
        task["blocked_issue_count"] = sum(
            value.get("blocking", True) is not False for value in clean_issues
        )
        task["compiled_blocked_issue_count"] = task["blocked_issue_count"]
        draft_ids = [
            str(value)
            for value in (
                ids_by_task.get(task_id)
                if isinstance(ids_by_task.get(task_id), list)
                else []
            )
            if str(value)
        ]
        details = (
            task_summary.get(task_id)
            if isinstance(task_summary.get(task_id), dict)
            else {}
        )
        task["draft_candidate_count"] = len(draft_ids)
        task["draft_resource_ids"] = draft_ids
        task["draft_issue_count"] = _safe_nonnegative_int(
            details.get("issue_count")
        )
        task["draft_only_resource_count"] = _safe_nonnegative_int(
            details.get("draft_only_resource_count")
        )
        if not draft_ids:
            change_keys = task.get("change_keys")
            if isinstance(change_keys, list) and change_keys and all(
                str(value).startswith("draft_resource:") for value in change_keys
            ):
                task["change_keys"] = []
                task["change_count"] = 0
                task["safe_change_keys"] = []
                task["safe_change_count"] = 0
                task["compiled_safe_change_count"] = 0
                if str(task.get("status") or "") not in {
                    "applied", "partially_applied", "deferred", "skipped",
                    "drafted_with_gaps",
                }:
                    task["status"] = "empty"
                refresh_needed = True
            continue
        needs_attention = _safe_nonnegative_int(
            details.get("needs_attention_count")
        )
        formal_change_count = _safe_nonnegative_int(task.get("change_count"))
        draft_only_count = _safe_nonnegative_int(
            details.get("draft_only_resource_count")
        )
        if not (needs_attention or draft_only_count or formal_change_count <= 0):
            continue
        code = (
            "DRAFT_ONLY_RESOURCE"
            if draft_only_count
            else "STAGED_RESOURCE_REQUIRES_VALIDATION"
        )
        issue = {
            "code": code,
            "message": (
                "本任务包含只能保存在场景草稿层的对象实例或概念映射；"
                "它们已物化但保持停用且不可发布。"
                if draft_only_count
                else "本任务包含未通过校验的资源；具体定义和问题已保存在场景草稿层。"
            ),
            "blocking": True,
            "source_refs": [],
            "affected_change_keys": [f"draft_resource:{value}" for value in draft_ids],
            "resolution_hint": (
                "在场景建模页面修正这些具体草稿并重新校验；"
                "已保存的草稿不会因本轮结束或会话删除而丢失。"
            ),
        }
        raw_issues = task.get("issues")
        issues = [
            copy.deepcopy(value)
            for value in (raw_issues if isinstance(raw_issues, list) else [])
            if isinstance(value, dict)
        ]
        existing_issue = next(
            (value for value in issues if str(value.get("code") or "") == code),
            None,
        )
        if existing_issue is None:
            task["issues"] = [*issues, issue]
            refresh_needed = True
        else:
            task["issues"] = issues
        task["blocked_issue_count"] = sum(
            value.get("blocking", True) is not False for value in task["issues"]
        )
        task["compiled_blocked_issue_count"] = max(
            _safe_nonnegative_int(task.get("compiled_blocked_issue_count")),
            task["blocked_issue_count"],
        )
        if formal_change_count <= 0 and str(task.get("status") or "") == "empty":
            refresh_needed = True
            task["status"] = "pending"
            task["change_keys"] = [
                f"draft_resource:{value}" for value in draft_ids
            ]
            task["change_count"] = len(draft_ids)
            task["safe_change_keys"] = []
            task["safe_change_count"] = 0
            task["compiled_safe_change_count"] = 0

    if refresh_needed:
        payload = _refresh_model_task_states(payload)
    payload["draft_materialization"] = public_materialization
    result["payload"] = payload
    execution_status = str(payload.get("execution_status") or "")
    if str(result.get("status") or "") not in {"applied", "partially_applied"}:
        result["status"] = (
            execution_status
            if execution_status in {
                "completed",
                "completed_with_gaps",
                "completed_no_changes",
            }
            else "in_progress"
        )
    result["requires_confirmation"] = bool(payload.get("current_task_id"))
    result["run_revision"] = _safe_nonnegative_int(
        payload.get("execution_revision")
    )
    return result


def _materialize_scenario_model_proposal(
    db: Session,
    scenario: BusinessScenario,
    proposal: dict[str, Any],
    *,
    source_thread_id: str,
    source_message_id: str,
    compilation_job_id: str = "",
    lineage_started_at: datetime | None = None,
    consumed_draft_revisions: dict[str, int] | None = None,
) -> dict[str, Any]:
    lineage = (
        proposal.get("draft_lineage")
        if isinstance(proposal.get("draft_lineage"), dict)
        else {}
    )
    if lineage_started_at is None:
        raw_started = lineage.get("started_at")
        if isinstance(raw_started, str):
            try:
                lineage_started_at = datetime.fromisoformat(raw_started)
            except ValueError:
                lineage_started_at = None
        if lineage_started_at is None and source_message_id:
            source_message = db.get(AssistantMessage, source_message_id)
            lineage_started_at = source_message.created_at if source_message else None
    if consumed_draft_revisions is None:
        raw_revisions = lineage.get("consumed_draft_revisions")
        consumed_draft_revisions = (
            raw_revisions if isinstance(raw_revisions, dict) else {}
        )
    materialization = scenario_model_draft_service.materialize_draft_resources(
        db,
        scenario,
        proposal,
        source_thread_id=source_thread_id,
        source_message_id=source_message_id,
        compilation_job_id=compilation_job_id,
        created_by_user_id=str(db.info.get("user_id") or "") or None,
        lineage_started_at=lineage_started_at,
        consumed_draft_revisions=consumed_draft_revisions,
    )
    return _attach_draft_materialization(proposal, materialization)


def _owned_compilation_job(db: Session, job_id: str) -> AssistantCompilationJob:
    """Resolve a job through both principal ownership and current scenario ACL."""
    job = db.execute(
        select(AssistantCompilationJob).where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.tenant_id == _tenant(db),
            AssistantCompilationJob.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if not job:
        # Keep missing and foreign jobs indistinguishable to prevent ID probing.
        raise HTTPException(404, "编译任务不存在")
    if job.thread_id:
        thread = _thread(db, job.thread_id)
        if thread.scenario_id != job.scenario_id:
            raise HTTPException(404, "编译任务不存在")
    elif job.scenario_id:
        _scenario(db, job.scenario_id)
    else:
        permission_service.require_tenant_permission(db, "read")
    return job


def _public_compilation_progress(job: AssistantCompilationJob) -> dict[str, Any]:
    raw = job.progress if isinstance(job.progress, dict) else {}
    result = {
        "phase": str(raw.get("phase") or job.status),
        "detail": str(raw.get("detail") or "")[:500],
        "calls_used": int(raw.get("calls_used", job.llm_calls_used) or 0),
        "call_budget": int(raw.get("call_budget", job.llm_call_budget) or 0),
        "steps": assistant_compilation_job_service.normalize_progress_steps(
            raw.get("steps")
        ),
        "current_step": str(raw.get("current_step") or "")[:80],
        "results": assistant_compilation_job_service.normalize_progress_results(
            raw.get("results")
        ),
    }
    if job.status == "failed":
        public_error = assistant_compilation_job_service.public_compilation_error(
            job.error or "编译失败"
        )
        # Treat historic progress JSON as untrusted.  Earlier deployments may
        # have stored provider/parser details in ``detail``.
        result["detail"] = public_error.message[:500]
        result["error_code"] = public_error.code[:100]
    return result


def _compilation_status_out(
    job: AssistantCompilationJob,
) -> AssistantCompilationJobStatusOut:
    progress = _public_compilation_progress(job)
    return AssistantCompilationJobStatusOut(
        id=job.id,
        thread_id=job.thread_id,
        scenario_id=job.scenario_id,
        status=job.status,
        progress=progress,
        llm_calls_used=job.llm_calls_used,
        llm_call_budget=job.llm_call_budget,
        result_ready=job.status == "succeeded" and bool(job.result),
        error_code=str(progress.get("error_code") or ""),
        error_message=(
            str(progress.get("detail") or "") if job.status == "failed" else ""
        ),
        started_at=job.started_at,
        completed_at=job.completed_at,
        updated_at=job.updated_at,
    )


def _public_model_issue_group_rows(raw_groups: Any) -> list[dict[str, Any]]:
    """Return one bounded public row per root cause, preserving aggregate scale."""
    merged: dict[str, dict[str, Any]] = {}
    for raw in raw_groups if isinstance(raw_groups, list) else []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "DOCUMENT_AMBIGUITY")[:100]
        cause = str(raw.get("cause") or "").strip() or _model_issue_cause(
            _effective_model_issue_code(raw),
            str(raw.get("message") or ""),
        )
        data_source = cause == "data_source_dependency"
        count = _safe_nonnegative_int(raw.get("count")) or 1
        blocking_count = (
            _safe_nonnegative_int(raw.get("blocking_count"))
            if "blocking_count" in raw
            else count if raw.get("blocking", True) is not False else 0
        )
        blocking_count = min(blocking_count, count)
        row = merged.get(cause)
        if row is None:
            row = {
                "code": "DATA_SOURCE_DEPENDENCY" if data_source else code,
                "cause": cause[:100],
                "message": (
                    "数据源、物理表或字段尚未接入或绑定。"
                    if data_source
                    else str(raw.get("message") or "存在待补全信息")[:500]
                ),
                "blocking": False,
                "count": 0,
                "blocking_count": 0,
                "affected_count": 0,
                "source_count": 0,
                "resolution_hint": (
                    "接入并检查数据源后，把现有逻辑映射绑定到真实表和字段；无需重建其他草稿。"
                    if data_source
                    else str(raw.get("resolution_hint") or "")[:500]
                ),
                "source_refs": [],
            }
            merged[cause] = row
        row["count"] += count
        row["blocking_count"] += blocking_count
        row["affected_count"] += (
            _safe_nonnegative_int(raw.get("affected_count")) or count
        )
        row["source_count"] += _safe_nonnegative_int(raw.get("source_count"))
        row["blocking"] = row["blocking_count"] > 0
        if not row["resolution_hint"] and raw.get("resolution_hint"):
            row["resolution_hint"] = str(raw.get("resolution_hint"))[:500]
    return sorted(
        merged.values(),
        key=lambda item: (
            not bool(item.get("blocking_count")),
            -int(item.get("count") or 0),
            str(item.get("cause") or ""),
        ),
    )


def _public_model_issue_rows_from_issues(raw_issues: Any) -> list[dict[str, Any]]:
    issues = [
        item for item in (raw_issues if isinstance(raw_issues, list) else [])
        if isinstance(item, dict)
    ]
    if any("count" in item or "cause" in item for item in issues):
        return _public_model_issue_group_rows(issues)
    summary = _model_task_execution_summary({"tasks": [], "unresolved": issues})
    return _public_model_issue_group_rows(summary.get("issue_groups") or [])


def _public_model_execution_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = copy.deepcopy(value)
    raw_groups = result.get("issue_groups")
    groups = (
        _public_model_issue_group_rows(raw_groups)
        if isinstance(raw_groups, list) and raw_groups
        else _public_model_issue_rows_from_issues(result.get("remaining_issues"))
    )
    result["issue_groups"] = groups
    result["remaining_issues"] = copy.deepcopy(groups)
    result["remaining_issue_group_count"] = len(groups)
    return result


def _public_recovery_proposal(value: Any) -> Any:
    """Remove execution identities from the display copy of a proposal.

    Confirmation ignores client proposal payloads and resolves the exact,
    unsanitized proposal from its server-owned assistant message.
    """
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if (
                normalized == "fingerprint"
                or "fingerprint" in normalized
                or normalized == "hash"
                or normalized.endswith("_hash")
                or normalized in {"api_key", "raw_provider_error"}
            ):
                continue
            result[str(key)] = _public_recovery_proposal(item)
        if isinstance(result.get("remaining_blockers"), list):
            result["remaining_blockers"] = _public_model_issue_rows_from_issues(
                result.get("remaining_blockers")
            )
        if isinstance(result.get("execution_summary"), dict):
            result["execution_summary"] = _public_model_execution_summary(
                result.get("execution_summary")
            )
        if result.get("kind") == "scenario_model" and isinstance(
            result.get("payload"), dict
        ):
            payload = result["payload"]
            summary = _model_task_execution_summary(payload)
            public_groups = _public_model_issue_group_rows(
                summary.get("issue_groups") or []
            )
            payload["unresolved"] = public_groups
            summary["remaining_issues"] = copy.deepcopy(public_groups)
            summary["issue_groups"] = copy.deepcopy(public_groups)
            summary["remaining_issue_group_count"] = len(public_groups)
            payload["execution_summary"] = summary
            for task in payload.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                task_summary = _model_task_execution_summary({
                    "tasks": [],
                    "unresolved": task.get("issues") or [],
                })
                task["issues"] = _public_model_issue_group_rows(
                    task_summary.get("issue_groups") or []
                )
        return result
    if isinstance(value, list):
        return [_public_recovery_proposal(item) for item in value]
    return value


def _model_lifecycle_is_consistent(payload: Any) -> bool:
    """Recognize only lifecycle snapshots that cannot strand the next action."""
    if not isinstance(payload, dict):
        return False
    tasks = payload.get("tasks")
    summary = payload.get("execution_summary")
    next_action = payload.get("next_action")
    if (
        not isinstance(tasks, list)
        or not tasks
        or not all(isinstance(item, dict) for item in tasks)
        or not _model_task_fields_are_well_formed(tasks)
        or not isinstance(summary, dict)
        or not isinstance(next_action, dict)
    ):
        return False

    task_ids = [str(item.get("id") or "").strip() for item in tasks]
    if any(not value for value in task_ids) or len(set(task_ids)) != len(task_ids):
        return False
    statuses = [str(item.get("status") or "pending") for item in tasks]
    all_terminal = all(
        status in _MODEL_TASK_TERMINAL_STATUSES for status in statuses
    )
    canonical_task_ids = [
        str(item.get("id") or "")
        for item in scenario_model_compiler.model_task_definitions()
    ]
    # Completed historic runs remain replayable, but an active legacy plan
    # must be upgraded so a missing stage (notably instances) cannot vanish.
    if not all_terminal and task_ids != canonical_task_ids:
        return False
    task_by_id = {task_ids[index]: task for index, task in enumerate(tasks)}
    for task_id, task in task_by_id.items():
        dependencies = [str(value) for value in (task.get("depends_on") or [])]
        if any(value not in task_by_id or value == task_id for value in dependencies):
            return False

    visiting: set[str] = set()
    visited: set[str] = set()

    def dependency_cycle(task_id: str) -> bool:
        if task_id in visiting:
            return True
        if task_id in visited:
            return False
        visiting.add(task_id)
        for dependency in task_by_id[task_id].get("depends_on") or []:
            if dependency_cycle(str(dependency)):
                return True
        visiting.remove(task_id)
        visited.add(task_id)
        return False

    if any(dependency_cycle(task_id) for task_id in task_ids):
        return False
    actionable = [
        task_ids[index]
        for index, status in enumerate(statuses)
        if status in {"ready", "blocked"}
    ]
    current_task_id = str(payload.get("current_task_id") or "")
    summary_current = str(summary.get("current_task_id") or "")
    action_type = str(next_action.get("type") or "")
    execution_status = str(payload.get("execution_status") or "")

    for task_id, task in task_by_id.items():
        status = str(task.get("status") or "pending")
        dependencies = [str(value) for value in (task.get("depends_on") or [])]
        if status in {"ready", "blocked"} and any(
            str(task_by_id[value].get("status") or "pending")
            not in _MODEL_TASK_APPLIED_STATUSES | {"empty"}
            for value in dependencies
        ):
            return False
        if status == "waiting":
            waiting_for = [str(value) for value in (task.get("waiting_for") or [])]
            if not waiting_for or any(value not in task_by_id for value in waiting_for):
                return False

    if summary.get("final") is True:
        return bool(
            all_terminal
            and not actionable
            and not current_task_id
            and not summary_current
            and execution_status in {
                "completed",
                "completed_with_gaps",
                "completed_no_changes",
            }
            and action_type == "refine_model"
            and next_action.get("requires_confirmation") is False
        )
    return bool(
        summary.get("final") is False
        and not all_terminal
        and len(actionable) == 1
        and current_task_id == actionable[0]
        and summary_current == current_task_id
        and execution_status == "waiting_for_confirmation"
        and action_type == "confirm_task"
        and str(next_action.get("task_id") or "") == current_task_id
        and next_action.get("requires_confirmation") is True
    )


def _proposal_run_metrics(value: Any) -> tuple[int, int, int, int, int]:
    """Return monotonic progress counters for one server-owned model run."""
    proposal = value if isinstance(value, dict) else {}
    payload = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    raw_tasks = payload.get("tasks")
    tasks = (
        [item for item in raw_tasks if isinstance(item, dict)]
        if isinstance(raw_tasks, list)
        else []
    )
    statuses = [str(item.get("status") or "pending") for item in tasks]
    revision = max(
        _safe_nonnegative_int(proposal.get("run_revision")),
        _safe_nonnegative_int(payload.get("execution_revision")),
    )
    completed = sum(status in _MODEL_TASK_TERMINAL_STATUSES for status in statuses)
    applied = sum(status in _MODEL_TASK_APPLIED_STATUSES for status in statuses)
    final = int(bool(tasks) and completed == len(tasks))
    return revision, completed, applied, final, len(tasks)


def _proposal_can_advance(
    current: Any,
    candidate: Any,
    *,
    allow_equal_progress: bool = False,
) -> bool:
    """Prevent subscriber clones or stale GETs from moving a run backwards."""
    if not isinstance(candidate, dict) or not candidate:
        return False
    current_dict = current if isinstance(current, dict) else {}
    candidate_id = str(candidate.get("proposal_id") or "")
    current_id = str(current_dict.get("proposal_id") or "")
    if current_id and candidate_id != current_id:
        return False
    if not current_dict:
        return True
    if candidate == current_dict:
        return False

    current_revision, current_completed, current_applied, current_final, current_total = (
        _proposal_run_metrics(current_dict)
    )
    candidate_revision, candidate_completed, candidate_applied, candidate_final, candidate_total = (
        _proposal_run_metrics(candidate)
    )
    if candidate_revision < current_revision:
        return False
    if candidate_completed < current_completed or candidate_applied < current_applied:
        return False
    if current_final and not candidate_final:
        return False
    def task_statuses(value: dict[str, Any]) -> dict[str, str]:
        value_payload = (
            value.get("payload")
            if isinstance(value.get("payload"), dict)
            else {}
        )
        raw_tasks = value_payload.get("tasks")
        tasks = raw_tasks if isinstance(raw_tasks, list) else []
        return {
            str(item.get("id") or ""): str(item.get("status") or "pending")
            for item in tasks
            if isinstance(item, dict) and str(item.get("id") or "")
        }

    current_statuses = task_statuses(current_dict)
    candidate_statuses = task_statuses(candidate)
    canonical_task_ids = {
        str(item.get("id") or "")
        for item in scenario_model_compiler.model_task_definitions()
    }
    canonical_contract_upgrade = bool(
        current_statuses
        and set(current_statuses) < canonical_task_ids
        and set(candidate_statuses) == canonical_task_ids
    )
    if (
        current_total
        and candidate_total != current_total
        and not canonical_contract_upgrade
    ):
        return False
    if current_statuses:
        if (
            set(candidate_statuses) != set(current_statuses)
            and not canonical_contract_upgrade
        ):
            return False
        # A durable task decision is immutable.  A stale message may not move
        # one terminal task back to pending while marking a sibling complete,
        # even when aggregate counters and revisions happen to match.
        if any(
            current_status in _MODEL_TASK_TERMINAL_STATUSES
            and candidate_statuses.get(task_id) != current_status
            for task_id, current_status in current_statuses.items()
        ):
            return False
        if candidate_revision == current_revision and any(
            candidate_statuses.get(task_id) in _MODEL_TASK_TERMINAL_STATUSES
            and candidate_statuses.get(task_id) != current_status
            for task_id, current_status in current_statuses.items()
        ):
            return False
    return bool(
        candidate_revision > current_revision
        or candidate_completed > current_completed
        or candidate_applied > current_applied
        or candidate_final > current_final
        or allow_equal_progress
    )


def _scoped_compilation_job_for_message(
    db: Session,
    message: AssistantMessage,
    job_id: str,
    *,
    lock: bool = False,
) -> AssistantCompilationJob | None:
    """Resolve a message's job pointer without trusting context across scopes."""
    stmt = (
        select(AssistantCompilationJob)
        .join(AssistantThread, AssistantThread.id == message.thread_id)
        .where(
            AssistantCompilationJob.id == job_id,
            AssistantCompilationJob.tenant_id == _tenant(db),
            AssistantCompilationJob.created_by_user_id == _current_user_id(db),
            AssistantThread.tenant_id == AssistantCompilationJob.tenant_id,
            AssistantThread.created_by_user_id
            == AssistantCompilationJob.created_by_user_id,
            AssistantThread.scenario_id == AssistantCompilationJob.scenario_id,
        )
    )
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    return db.execute(stmt).scalars().first()


def _sync_compilation_job_result(
    job: AssistantCompilationJob | None,
    proposal: dict[str, Any],
    *,
    canonical: bool = False,
) -> bool:
    """Advance a succeeded compilation result, never overwrite it backwards."""
    if job is None or job.status != "succeeded":
        return False
    if not _proposal_can_advance(
        job.result,
        proposal,
        allow_equal_progress=canonical,
    ):
        return False
    job.result = copy.deepcopy(proposal)
    return True


def _upgrade_saved_scenario_model_plan(
    db: Session,
    message: AssistantMessage,
    *,
    materialize: bool = True,
    persist: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Turn an unapplied pre-task-board proposal into a resumable plan.

    Existing users must not remain trapped on the historic atomic proposal
    card after this lifecycle upgrade.  Applied/partially-applied rows are left
    untouched because reconstructing their write history could offer duplicate
    writes; only still-pending drafts are upgraded from their complete stored
    compiler payload.
    """
    proposal = (
        copy.deepcopy(message.proposal)
        if isinstance(message.proposal, dict)
        else {}
    )
    payload = proposal.get("payload")
    if (
        proposal.get("kind") != "scenario_model"
        or not isinstance(payload, dict)
    ):
        return proposal, False

    context = dict(message.context) if isinstance(message.context, dict) else {}
    compilation_job_id = str(context.get("compilation_job_id") or "")
    job = (
        _scoped_compilation_job_for_message(
            db,
            message,
            compilation_job_id,
            lock=True,
        )
        if compilation_job_id
        else None
    )
    canonical = bool(
        job is not None
        and job.thread_id == message.thread_id
        and job.message_id == message.id
    )

    def persist_message_proposal(value: dict[str, Any]) -> None:
        if not persist:
            return
        selected_payload = (
            value.get("payload")
            if isinstance(value.get("payload"), dict)
            else {}
        )
        summary = (
            selected_payload.get("execution_summary")
            if isinstance(selected_payload.get("execution_summary"), dict)
            else {}
        )
        revision = max(
            _safe_nonnegative_int(value.get("run_revision")),
            _safe_nonnegative_int(selected_payload.get("execution_revision")),
        )
        message.proposal = copy.deepcopy(value)
        context.update({
            "status": (
                _model_run_context_status(summary)
                if summary.get("final")
                else "success"
                if str(value.get("status") or "")
                in {
                    "applied",
                    "partially_applied",
                    "completed",
                    "completed_with_gaps",
                    "completed_no_changes",
                }
                else "waiting_confirmation"
            ),
            "model_run_id": value.get("proposal_id"),
            "run_revision": revision,
        })
        # Always assign a detached object.  JSON columns do not observe a
        # second in-place mutation of the same dictionary during lazy repair.
        message.context = copy.deepcopy(context)

    changed = False
    if (
        job is not None
        and job.status == "succeeded"
        and isinstance(job.result, dict)
        and job.result.get("kind") == "scenario_model"
        and _proposal_can_advance(proposal, job.result)
    ):
        # A previous request may have advanced the durable ledger after this
        # ORM row was read.  Heal the message before deciding whether a lazy
        # lifecycle upgrade is still necessary.
        proposal = copy.deepcopy(job.result)
        payload = proposal.get("payload")
        persist_message_proposal(proposal)
        changed = True

    if not isinstance(payload, dict):
        return proposal, changed
    if not str(proposal.get("proposal_id") or "").strip():
        proposal["proposal_id"] = uuid.uuid4().hex
        payload = proposal.get("payload")
        changed = True

    proposal_thread = db.execute(
        select(AssistantThread).where(
            AssistantThread.id == message.thread_id,
            AssistantThread.tenant_id == _tenant(db),
            AssistantThread.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if materialize and proposal_thread and proposal_thread.scenario_id:
        proposal_scenario = _scenario(db, proposal_thread.scenario_id)
        if proposal_scenario is not None:
            materialized = _materialize_scenario_model_proposal(
                db,
                proposal_scenario,
                proposal,
                source_thread_id=proposal_thread.id,
                source_message_id=message.id,
                compilation_job_id=compilation_job_id,
            )
            if materialized != proposal:
                proposal = materialized
                payload = proposal.get("payload")
                persist_message_proposal(proposal)
                changed = True

    raw_tasks = payload.get("tasks")
    task_contract_migrated = False
    if isinstance(raw_tasks, list) and all(
        isinstance(item, dict) for item in raw_tasks
    ):
        existing_tasks = {
            str(item.get("id") or ""): copy.deepcopy(item)
            for item in raw_tasks
            if str(item.get("id") or "")
        }
        # Historic JSON may contain scalar drift in one of the compiler list
        # fields.  The lifecycle repair below knows how to close that shape as
        # a recoverable gap, but the canonical task builder must not receive
        # the malformed values first (it expects iterable issue/resource rows).
        canonical_source = copy.deepcopy(payload)
        for field in (
            *_SCENARIO_MODEL_RESOURCE_SECTIONS,
            "draft_candidates",
            "changes",
            "unresolved",
            "coverage",
        ):
            raw_values = canonical_source.get(field)
            canonical_source[field] = (
                [value for value in raw_values if isinstance(value, dict)]
                if isinstance(raw_values, list)
                else []
            )
        canonical_plan = scenario_model_compiler.build_model_task_plan(
            canonical_source
        )
        canonical_ids = [str(item.get("id") or "") for item in canonical_plan]
        existing_ids = [str(item.get("id") or "") for item in raw_tasks]
        legacy_run_is_terminal = bool(raw_tasks) and all(
            str(item.get("status") or "pending")
            in _MODEL_TASK_TERMINAL_STATUSES
            for item in raw_tasks
        )
        if existing_ids != canonical_ids and not legacy_run_is_terminal:
            canonical_fields = {
                "id", "order", "title", "description", "sections", "depends_on",
            }
            migrated_tasks: list[dict[str, Any]] = []
            for generated in canonical_plan:
                existing = existing_tasks.get(str(generated.get("id") or ""))
                merged = {
                    **generated,
                    **(existing or {}),
                    **{key: copy.deepcopy(generated[key]) for key in canonical_fields},
                }
                if (
                    existing is not None
                    and "output_count" not in existing
                    and _safe_nonnegative_int(generated.get("output_count")) <= 0
                    and _safe_nonnegative_int(existing.get("change_count")) > 0
                ):
                    # Transitional task boards sometimes retained only their
                    # task-level change ledger after the heavyweight compiler
                    # sections were compacted.  Do not let the newly generated
                    # zero output_count erase that genuine pending work.
                    merged["output_count"] = _safe_nonnegative_int(
                        existing.get("change_count")
                    )
                migrated_tasks.append(merged)
            payload = {**payload, "tasks": migrated_tasks}
            proposal = {**proposal, "payload": payload}
            task_contract_migrated = True
            changed = True

    has_complete_lifecycle = (
        not task_contract_migrated and _model_lifecycle_is_consistent(payload)
    )
    if (
        has_complete_lifecycle
        or str(proposal.get("status") or "pending")
        in {"applied", "partially_applied"}
    ):
        synced = (
            _sync_compilation_job_result(
                job,
                proposal,
                canonical=True,
            )
            if canonical and persist
            else False
        )
        return proposal, synced or changed

    upgraded_payload = _refresh_model_task_states(
        payload
        if isinstance(payload.get("tasks"), list) and payload.get("tasks")
        else scenario_model_compiler.attach_model_task_plan(payload)
    )
    proposal_id = str(proposal.get("proposal_id") or uuid.uuid4().hex)
    upgraded_payload["run_id"] = str(upgraded_payload.get("run_id") or proposal_id)
    execution_status = str(upgraded_payload.get("execution_status") or "")
    run_revision = _safe_nonnegative_int(
        upgraded_payload.get("execution_revision")
    )
    upgraded = {
        **proposal,
        "proposal_id": proposal_id,
        "payload": upgraded_payload,
        "status": (
            execution_status
            if execution_status in {
                "completed",
                "completed_with_gaps",
                "completed_no_changes",
            }
            else "in_progress"
        ),
        "requires_confirmation": bool(upgraded_payload.get("current_task_id")),
        "run_revision": run_revision,
    }
    if materialize and proposal_thread and proposal_thread.scenario_id:
        proposal_scenario = _scenario(db, proposal_thread.scenario_id)
        if proposal_scenario is not None:
            upgraded = _materialize_scenario_model_proposal(
                db,
                proposal_scenario,
                upgraded,
                source_thread_id=proposal_thread.id,
                source_message_id=message.id,
                compilation_job_id=compilation_job_id,
            )
    persist_message_proposal(upgraded)
    if canonical and persist:
        _sync_compilation_job_result(
            job,
            upgraded,
            canonical=True,
        )
    return upgraded, True


def _matching_compilation_proposal_message(
    db: Session,
    job: AssistantCompilationJob,
) -> tuple[AssistantThread | None, AssistantMessage | None]:
    """Return the one canonical proposal row bound by the compilation ledger."""
    result = job.result if isinstance(job.result, dict) else {}
    proposal_id = str(result.get("proposal_id") or "")
    if not proposal_id or not job.thread_id or not job.message_id:
        return None, None
    row = db.execute(
        select(AssistantThread, AssistantMessage)
        .join(AssistantMessage, AssistantMessage.thread_id == AssistantThread.id)
        .where(
            AssistantThread.id == job.thread_id,
            AssistantMessage.id == job.message_id,
            AssistantThread.tenant_id == job.tenant_id,
            AssistantThread.created_by_user_id == job.created_by_user_id,
            AssistantThread.scenario_id == job.scenario_id,
            AssistantMessage.role == "assistant",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).first()
    if row is None:
        return None, None
    thread, message = row
    proposal = message.proposal if isinstance(message.proposal, dict) else {}
    if proposal.get("proposal_id") != proposal_id:
        return None, None
    return thread, message


def _update_compilation_subscription_messages(
    db: Session,
    job: AssistantCompilationJob,
    *,
    status: str,
    content: str,
    canonical_message_id: str,
    model_run_id: str = "",
    error_code: str = "",
) -> None:
    """Advance every duplicate subscription without copying canonical drafts."""
    messages = db.execute(
        select(AssistantMessage)
        .join(AssistantThread, AssistantThread.id == AssistantMessage.thread_id)
        .where(
            AssistantThread.tenant_id == job.tenant_id,
            AssistantThread.created_by_user_id == job.created_by_user_id,
            AssistantThread.scenario_id == job.scenario_id,
            AssistantMessage.role == "assistant",
        )
    ).scalars().all()
    for message in messages:
        message_context = (
            message.context if isinstance(message.context, dict) else {}
        )
        if str(message_context.get("compilation_job_id") or "") != job.id:
            continue
        if message.id == canonical_message_id:
            continue
        next_context = {
            **message_context,
            "status": status,
            "compilation_job_id": job.id,
            "canonical_message_id": canonical_message_id,
        }
        if model_run_id:
            next_context["model_run_id"] = model_run_id
        else:
            next_context.pop("model_run_id", None)
        if error_code:
            next_context["error_code"] = error_code
        else:
            next_context.pop("error_code", None)
        message.context = next_context
        message.content = content
        message.proposal = {}


def _reconcile_terminal_compilation_subscriptions(
    db: Session,
    job: AssistantCompilationJob,
) -> bool:
    """Project a terminal job onto every non-canonical subscription message."""
    if job.status == "succeeded":
        execution_summary = (
            ((job.result or {}).get("payload") or {}).get("execution_summary")
            or {}
        )
        _update_compilation_subscription_messages(
            db,
            job,
            status=_model_run_context_status(execution_summary),
            content=(
                str(execution_summary.get("message") or "").strip()
                or "同一场景建模任务已经完成；请查看已连接的权威草稿。"
            ),
            canonical_message_id=str(job.message_id or ""),
            model_run_id=str((job.result or {}).get("proposal_id") or ""),
        )
        return True
    if job.status == "failed":
        public_failure = _public_compilation_progress(job)
        _update_compilation_subscription_messages(
            db,
            job,
            status="error",
            content=str(public_failure.get("detail") or "场景建模任务未完成。"),
            canonical_message_id=str(job.message_id or ""),
            error_code=str(public_failure.get("error_code") or ""),
        )
        return True
    return False


def _link_compilation_placeholder(
    *,
    tenant_id: str,
    user_id: str,
    thread_id: str,
    assistant_message_id: str,
    job_id: str,
    context: dict[str, Any],
) -> None:
    """Persist each duplicate request's recoverable subscription placeholder."""
    link_db = SessionLocal()
    link_db.info["tenant_id"] = tenant_id
    link_db.info["user_id"] = user_id
    try:
        thread = link_db.execute(
            select(AssistantThread).where(
                AssistantThread.id == thread_id,
                AssistantThread.tenant_id == tenant_id,
                AssistantThread.created_by_user_id == user_id,
            )
        ).scalars().first()
        message = link_db.get(AssistantMessage, assistant_message_id)
        job = link_db.get(AssistantCompilationJob, job_id)
        if (
            not thread
            or not message
            or message.thread_id != thread.id
            or message.role != "assistant"
            or not job
            or job.tenant_id != tenant_id
            or job.created_by_user_id != user_id
        ):
            raise RuntimeError("编译任务恢复占位消息不存在或不属于当前会话")
        message.context = {
            **context,
            "status": "processing",
            "compilation_job_id": job_id,
        }
        if message.id != str(job.message_id or ""):
            message.proposal = {}
        # Publish the subscription pointer first.  A worker that starts after
        # this commit must see it; if the worker already finished, the fresh
        # terminal read below repairs it immediately.
        link_db.commit()
        link_db.expire_all()
        job = link_db.execute(
            select(AssistantCompilationJob).where(
                AssistantCompilationJob.id == job_id,
                AssistantCompilationJob.tenant_id == tenant_id,
                AssistantCompilationJob.created_by_user_id == user_id,
            ).with_for_update()
        ).scalars().first()
        if job is not None and _reconcile_terminal_compilation_subscriptions(
            link_db, job
        ):
            link_db.commit()
    finally:
        link_db.close()


def _record_compilation_progress(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    used: int,
    budget: int,
    phase: str,
    lease_token: str = "",
    lease_attempt: int = 0,
) -> None:
    progress_db = SessionLocal()
    progress_db.info["tenant_id"] = tenant_id
    progress_db.info["user_id"] = user_id
    try:
        assistant_compilation_job_service.record_provider_call(
            progress_db,
            job_id,
            used=used,
            budget=budget,
            phase=phase,
            lease_token=lease_token or None,
            lease_attempt=lease_attempt if lease_token else None,
        )
    finally:
        progress_db.close()


def _record_compilation_stage(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    step_id: str,
    detail: str,
    status: str,
    result: str = "",
    lease_token: str = "",
    lease_attempt: int = 0,
) -> None:
    """Persist one completed task so clients can render incremental work."""
    progress_db = SessionLocal()
    progress_db.info["tenant_id"] = tenant_id
    progress_db.info["user_id"] = user_id
    titles = {
        "analyze": "分析业务资料",
        "plan": "制定建模任务",
        "ontology": "建立本体与业务能力",
        "mapping": "整理数据映射",
        "rules": "校验规则、事件与工作流",
        "review": "生成待审核变更清单",
        "result": "汇总执行结果",
    }
    try:
        assistant_compilation_job_service.record_progress(
            progress_db,
            job_id,
            step_id=step_id,
            title=titles.get(step_id, step_id),
            detail=detail,
            status=status,
            result=result,
            lease_token=lease_token or None,
            lease_attempt=lease_attempt if lease_token else None,
        )
    finally:
        progress_db.close()


def _fail_compilation_job(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    error: BaseException | str,
    lease_token: str = "",
    lease_attempt: int = 0,
) -> None:
    failure_db = SessionLocal()
    failure_db.info["tenant_id"] = tenant_id
    failure_db.info["user_id"] = user_id
    try:
        job = failure_db.get(AssistantCompilationJob, job_id)
        if not job:
            return
        # Never let a late worker replace the server-owned message of either
        # terminal outcome after another lease has already finished the job.
        if job.status in {"succeeded", "failed"}:
            return
        public_error = assistant_compilation_job_service.public_compilation_error(
            error
        )
        assistant_compilation_job_service.mark_failed(
            failure_db,
            job_id,
            error=error,
            commit=False,
            lease_token=lease_token or None,
            lease_attempt=lease_attempt if lease_token else None,
        )
        message = (
            failure_db.get(AssistantMessage, job.message_id)
            if job.message_id
            else None
        )
        if message and message.role == "assistant" and message.thread_id == job.thread_id:
            message.content = public_error.message
            message.context = {
                **(message.context if isinstance(message.context, dict) else {}),
                "status": "error",
                "compilation_job_id": job.id,
                "error_code": public_error.code,
            }
            message.proposal = {}
        _update_compilation_subscription_messages(
            failure_db,
            job,
            status="error",
            content=public_error.message,
            canonical_message_id=str(job.message_id or ""),
            error_code=public_error.code,
        )
        failure_db.commit()
    except Exception:
        failure_db.rollback()
        raise
    finally:
        failure_db.close()


def _finalize_compilation_success(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    thread_id: str,
    assistant_message_id: str,
    scenario_id: str,
    data: dict[str, Any],
    reply: str,
    context: dict[str, Any],
    sources: list[dict[str, Any]],
    thinking: list[dict[str, Any]],
    prepared_context: dict[str, Any] | None = None,
    lease_token: str = "",
    lease_attempt: int = 0,
) -> dict[str, Any]:
    """Atomically bind the recoverable message and terminal job result."""
    finish_db = SessionLocal()
    finish_db.info["tenant_id"] = tenant_id
    finish_db.info["user_id"] = user_id
    try:
        job = finish_db.execute(
            select(AssistantCompilationJob).where(
                AssistantCompilationJob.id == job_id,
                AssistantCompilationJob.tenant_id == tenant_id,
                AssistantCompilationJob.created_by_user_id == user_id,
                AssistantCompilationJob.scenario_id == scenario_id,
            ).with_for_update()
        ).scalars().first()
        if not job:
            raise RuntimeError("编译任务不存在")
        if job.status == "succeeded":
            return dict(job.result or {})
        if job.status != "running":
            raise RuntimeError("非运行中的编译任务不能标记成功")
        if lease_token:
            assistant_compilation_job_service.load_leased_execution_input(
                finish_db,
                job.id,
                token=lease_token,
                attempt=lease_attempt,
            )
        authorized_scenario = _scenario(finish_db, scenario_id)
        assert authorized_scenario is not None
        # Serialize with governed scenario writers where the database supports
        # row locks, then reload every definition collection used by the
        # baseline.  SQLite safely treats FOR UPDATE as a no-op; the later
        # proposal base_snapshot still protects confirmation on every backend.
        scenario = finish_db.execute(
            select(BusinessScenario)
            .where(
                BusinessScenario.id == scenario_id,
                BusinessScenario.tenant_id == tenant_id,
            )
            .with_for_update()
        ).scalars().first()
        if not scenario:
            raise RuntimeError("编译任务所属业务场景不存在")
        finish_db.expire(
            scenario,
            [
                "entities",
                "relations",
                "data_sources",
                "data_mappings",
                "relation_data_mappings",
                "function_definitions",
                "actions",
                "rules",
                "events",
                "workflows",
            ],
        )
        current_baseline = _scenario_revision(scenario)
        baseline_changed = current_baseline != job.scenario_baseline
        if baseline_changed:
            data = _baseline_changed_compilation_data(
                data,
                expected_baseline=str(job.scenario_baseline or ""),
                current_baseline=current_baseline,
            )
        thread = finish_db.execute(
            select(AssistantThread).where(
                AssistantThread.id == thread_id,
                AssistantThread.tenant_id == tenant_id,
                AssistantThread.created_by_user_id == user_id,
                AssistantThread.scenario_id == scenario_id,
            )
        ).scalars().first()
        if not thread:
            raise RuntimeError("编译任务所属助手会话不存在")
        inert_salvage = _is_inert_compilation_salvage(data)
        proposal = _build_proposal("scenario_model", data, scenario)
        consumed_revisions = (
            prepared_context.get("consumed_draft_revisions")
            if isinstance(prepared_context, dict)
            and isinstance(prepared_context.get("consumed_draft_revisions"), dict)
            else {}
        )
        proposal["draft_lineage"] = {
            "started_at": job.started_at.isoformat(),
            "consumed_draft_revisions": copy.deepcopy(consumed_revisions),
        }
        # Materialize every generated candidate, including invalid and
        # staging-only definitions, in the same transaction that publishes the
        # canonical proposal.  Nothing in runtime/release reads these rows.
        proposal = _materialize_scenario_model_proposal(
            finish_db,
            scenario,
            proposal,
            source_thread_id=thread.id,
            source_message_id=assistant_message_id,
            compilation_job_id=job.id,
            lineage_started_at=job.started_at,
            consumed_draft_revisions=consumed_revisions,
        )
        if baseline_changed:
            proposal = _complete_baseline_changed_proposal(
                proposal,
                current_snapshot=_scenario_snapshot(scenario),
            )
        elif inert_salvage:
            proposal = _complete_inert_salvage_proposal(proposal)
        execution_summary = (
            (proposal.get("payload") or {}).get("execution_summary") or {}
        )
        durable_reply = "\n\n".join(
            value
            for value in (
                reply.strip(),
                str(execution_summary.get("message") or "").strip(),
            )
            if value
        )
        message_status = _model_run_context_status(execution_summary)
        _save_message(
            finish_db,
            thread,
            "assistant",
            durable_reply,
            {
                **context,
                "status": message_status,
                "compilation_job_id": job.id,
                "model_run_id": proposal.get("proposal_id"),
            },
            sources,
            proposal,
            thinking,
            message_id=assistant_message_id,
        )
        _update_compilation_subscription_messages(
            finish_db,
            job,
            status=message_status,
            content=(
                str(execution_summary.get("message") or "").strip()
                or "同一场景建模任务已有新的权威草稿结果。"
            ),
            canonical_message_id=assistant_message_id,
            model_run_id=str(proposal.get("proposal_id") or ""),
        )
        job.thread_id = thread.id
        job.message_id = assistant_message_id
        # mark_succeeded performs a populate_existing read so lease-fenced
        # workers see the database's latest job state. Persist the canonical
        # message binding first; otherwise that refresh can replace these two
        # pending attributes with their pre-finalize values in autoflush=False
        # sessions.
        finish_db.flush()
        assistant_compilation_job_service.mark_succeeded(
            finish_db,
            job.id,
            result=proposal,
            result_summary=(
                str(execution_summary.get("message") or "")
                or f"已汇总 {len(data.get('changes') or [])} 项变更。"
            ),
            commit=False,
            lease_token=lease_token or None,
            lease_attempt=lease_attempt if lease_token else None,
        )
        finish_db.commit()
        return proposal
    except Exception:
        finish_db.rollback()
        raise
    finally:
        finish_db.close()


def _start_compilation_lease_heartbeat(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    lease_token: str,
    lease_attempt: int,
) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """Renew a provider-blocked worker independently of progress callbacks."""
    stop = threading.Event()
    lost = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(_COMPILATION_HEARTBEAT_SECONDS):
            heartbeat_db = SessionLocal()
            heartbeat_db.info["tenant_id"] = tenant_id
            heartbeat_db.info["user_id"] = user_id
            try:
                lease = assistant_compilation_job_service.renew_compilation_lease(
                    heartbeat_db,
                    job_id,
                    token=lease_token,
                    attempt=lease_attempt,
                )
                if lease is None:
                    lost.set()
                    return
            except Exception:  # noqa: BLE001 - final fenced writes remain authoritative.
                heartbeat_db.rollback()
            finally:
                heartbeat_db.close()

    thread = threading.Thread(
        target=heartbeat,
        name=f"assistant-compilation-heartbeat-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return stop, lost, thread


def _stop_compilation_lease_heartbeat(
    stop: threading.Event | None,
    thread: threading.Thread | None,
) -> None:
    if stop is not None:
        stop.set()
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=1)


def _raise_if_compilation_lease_lost(lost: threading.Event) -> None:
    if lost.is_set():
        raise assistant_compilation_job_service.CompilationLeaseLost(
            "编译任务租约已失效，旧执行者停止写入"
        )


def _unavailable_worker_draft(
    *,
    compiler_message: str,
    compiler_documents: list[dict[str, Any]],
    prepared_context: dict[str, Any],
    on_progress: Any,
    code: str,
    message: str,
) -> dict[str, Any]:
    try:
        source_bundle = scenario_model_compiler.prepare_source_bundle_preview(
            compiler_message,
            compiler_documents,
            prepared_context,
        )
    except ValueError:
        # This fallback must not call the failing preview path again. It keeps
        # only bounded source identities and a synthetic diagnostic paragraph;
        # no unavailable or oversized attachment body is copied into the job.
        source_bundle = _diagnostic_source_bundle(
            compiler_documents,
            public_message=message,
        )
    return scenario_model_compiler._unavailable_compilation_result(
        source_bundle=source_bundle,
        on_progress=on_progress,
        code=code,
        message=message,
    )


def _diagnostic_source_bundle(
    compiler_documents: list[dict[str, Any]],
    *,
    public_message: str,
) -> dict[str, Any]:
    """Build a bounded provenance ledger for source-input placeholders."""
    documents: list[dict[str, Any]] = []
    paragraphs: list[dict[str, str]] = []
    values = compiler_documents or [{
        "id": "blocked-compilation-input",
        "filename": "待补充业务资料",
    }]
    for index, document in enumerate(values, 1):
        source_id = f"blocked-source-{index}"
        filename = str(
            document.get("filename") or document.get("id") or f"待处理附件 {index}"
        )[:300]
        body = (
            f"来源“{filename}”当前不能作为完整业务正文参与编译。"
            f"{str(public_message or '来源输入需要处理。')[:500]}"
            "系统已建立停用占位草稿，不会推断不可读取的业务事实。"
        )
        paragraphs.append({
            "ref": f"{source_id}:p0001",
            "source_id": source_id,
            "source_kind": "unavailable_attachment",
            "text": body,
        })
        documents.append({
            "source_id": source_id,
            "filename": filename,
            "source_kind": "unavailable_attachment",
            "semantic_role": "baseline_document_unavailable",
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "characters": len(body),
            "paragraph_count": 1,
        })
    return {
        "documents": documents,
        "paragraphs": paragraphs,
        "total_characters": sum(len(item["text"]) for item in paragraphs),
    }


def _source_bundle_preview_with_recovery(
    *,
    compiler_message: str,
    compiler_documents: list[dict[str, Any]],
    prepared_context: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Keep invalid/empty/oversized sources inside the durable draft flow."""
    try:
        return (
            scenario_model_compiler.prepare_source_bundle_preview(
                compiler_message,
                compiler_documents,
                prepared_context,
            ),
            None,
        )
    except ValueError:
        recovery_issue = {
            "code": "SOURCE_INPUT_INVALID",
            "message": (
                "部分业务资料当前无法完整编译（可能尚未解析、正文为空、"
                "来源身份冲突或超过单次边界）；系统已按来源建立停用占位草稿。"
            ),
        }
        return (
            _diagnostic_source_bundle(
                compiler_documents,
                public_message=recovery_issue["message"],
            ),
            recovery_issue,
        )


def _legacy_compilation_execution_input(
    db: Session,
    job: AssistantCompilationJob,
) -> dict[str, Any]:
    """Give migrated running jobs an editable result instead of a retry loop."""
    latest_user = db.execute(
        select(AssistantMessage)
        .where(
            AssistantMessage.thread_id == job.thread_id,
            AssistantMessage.role == "user",
        )
        .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
    ).scalars().first()
    placeholder = db.get(AssistantMessage, job.message_id) if job.message_id else None
    scenario = db.get(BusinessScenario, job.scenario_id) if job.scenario_id else None
    if scenario is None:
        raise ValueError("持久化编译任务所属业务场景已不存在")
    return {
        "compiler_message": str(
            getattr(latest_user, "content", "")
            or "恢复未完成的场景建模任务"
        ),
        "compiler_documents": [],
        "prepared_context": scenario_model_compiler.prepare_compilation_context(
            db, scenario
        ),
        "llm_config_id": "",
        "context": copy.deepcopy(
            placeholder.context
            if placeholder and isinstance(placeholder.context, dict)
            else {}
        ),
        "sources": copy.deepcopy(
            placeholder.attachments
            if placeholder and isinstance(placeholder.attachments, list)
            else []
        ),
        "execution_policy": {
            "llm_call_budget": int(job.llm_call_budget),
            "request_timeout": get_settings().scenario_model_llm_timeout,
        },
        "recovery_issue": {
            "code": "COMPILATION_RESTART_INPUT_UNAVAILABLE",
            "message": (
                "该任务来自旧版本，缺少可验证的完整执行输入；系统已根据现有场景和原会话"
                "建立可编辑占位草稿，未重新猜测或写入正式模型。"
            ),
        },
    }


def _run_compilation_job_in_background(
    *,
    job_id: str,
    lease_token: str,
    lease_attempt: int,
    _slot_reserved: bool = True,
) -> None:
    """Resume one fenced job solely from its owner-private durable input."""
    worker_db = SessionLocal()
    tenant_id = ""
    user_id = ""
    heartbeat_stop: threading.Event | None = None
    heartbeat_lost = threading.Event()
    heartbeat_thread: threading.Thread | None = None
    try:
        job = worker_db.get(AssistantCompilationJob, job_id)
        if not job or job.status != "running":
            return
        tenant_id = str(job.tenant_id or "")
        user_id = str(job.created_by_user_id or "")
        scenario_id = str(job.scenario_id or "")
        thread_id = str(job.thread_id or "")
        assistant_message_id = str(job.message_id or "")
        if not all((tenant_id, user_id, scenario_id, thread_id, assistant_message_id)):
            raise ValueError("持久化编译任务缺少租户、用户、场景或会话身份")
        worker_db.info["tenant_id"] = tenant_id
        worker_db.info["user_id"] = user_id
        heartbeat_stop, heartbeat_lost, heartbeat_thread = (
            _start_compilation_lease_heartbeat(
                tenant_id=tenant_id,
                user_id=user_id,
                job_id=job_id,
                lease_token=lease_token,
                lease_attempt=lease_attempt,
            )
        )
        try:
            execution = _load_compilation_execution_input(
                worker_db,
                job,
                lease_token=lease_token,
                lease_attempt=lease_attempt,
            )
        except ValueError:
            execution = _legacy_compilation_execution_input(worker_db, job)

        compiler_message = execution["compiler_message"]
        compiler_documents = execution["compiler_documents"]
        prepared_context = execution["prepared_context"]
        context = execution["context"]
        sources = execution["sources"]
        execution_policy = execution["execution_policy"]
        llm_config_id = execution["llm_config_id"]
        if llm_config_id:
            worker_db.info["assistant_llm_config_id"] = llm_config_id
        scenario = _scenario(worker_db, scenario_id, writable=True)
        if not scenario:
            raise ValueError("完整业务模型编译需要一个仍可用的业务场景")
        llm = _llm(worker_db) if llm_config_id else None

        def record_compilation_call(used: int, total: int, phase: str) -> None:
            _raise_if_compilation_lease_lost(heartbeat_lost)
            if worker_db.new or worker_db.dirty or worker_db.deleted:
                worker_db.rollback()
                raise RuntimeError(
                    "完整业务模型编译器产生了未授权数据库变更"
                )
            _record_compilation_progress(
                tenant_id=tenant_id,
                user_id=user_id,
                job_id=job_id,
                used=used,
                budget=total,
                phase=phase,
                lease_token=lease_token,
                lease_attempt=lease_attempt,
            )

        def record_compilation_stage(
            step_id: str,
            detail: str,
            status: str,
            result: str,
        ) -> None:
            _raise_if_compilation_lease_lost(heartbeat_lost)
            _record_compilation_stage(
                tenant_id=tenant_id,
                user_id=user_id,
                job_id=job_id,
                step_id=step_id,
                detail=detail,
                status=status,
                result=result,
                lease_token=lease_token,
                lease_attempt=lease_attempt,
            )

        recovery_issue = execution.get("recovery_issue")
        if isinstance(recovery_issue, dict):
            data = _unavailable_worker_draft(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
                on_progress=record_compilation_stage,
                code=str(recovery_issue.get("code") or "COMPILATION_RESTART_INPUT_UNAVAILABLE"),
                message=str(recovery_issue.get("message") or "持久化任务输入不可用，已建立占位草稿。"),
            )
        elif job.compiler_version != scenario_model_compiler.COMPILER_VERSION:
            data = _unavailable_worker_draft(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
                on_progress=record_compilation_stage,
                code="COMPILER_VERSION_CHANGED_SINCE_QUEUED",
                message="任务排队期间编译器版本已变化；已保留来源绑定的占位草稿，正式模型保持不变。",
            )
        elif (
            assistant_compilation_job_service.llm_config_fingerprint(llm)
            != str(job.llm_config_fingerprint or "")
        ):
            data = _unavailable_worker_draft(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
                on_progress=record_compilation_stage,
                code="LLM_CONFIG_CHANGED_SINCE_QUEUED",
                message="任务排队期间 AI 模型配置已变化；已保留来源绑定的占位草稿，等待用户选择后继续。",
            )
        else:
            budget = scenario_model_compiler.LLMCallBudget(
                job.llm_call_budget,
                on_consume=record_compilation_call,
            )
            try:
                data = scenario_model_compiler.compile_scenario_model(
                    worker_db,
                    scenario,
                    message=compiler_message,
                    documents=compiler_documents,
                    llm=llm,
                    call_budget=budget,
                    prepared_context=prepared_context,
                    request_timeout=float(execution_policy["request_timeout"]),
                    on_progress=record_compilation_stage,
                )
                if worker_db.new or worker_db.dirty or worker_db.deleted:
                    raise RuntimeError("完整业务模型编译器产生了未授权数据库变更")
            except assistant_compilation_job_service.CompilationLeaseLost:
                raise
            except Exception:  # noqa: BLE001 - unexpected compiler failures still yield drafts.
                worker_db.rollback()
                data = _unavailable_worker_draft(
                    compiler_message=compiler_message,
                    compiler_documents=compiler_documents,
                    prepared_context=prepared_context,
                    on_progress=record_compilation_stage,
                    code="COMPILER_EXECUTION_INTERRUPTED",
                    message="结构化编译执行中断；系统已基于冻结来源建立分阶段占位草稿，正式模型保持不变。",
                )
        worker_db.rollback()
        _raise_if_compilation_lease_lost(heartbeat_lost)
        reply = (
            "已根据业务资料生成并持久化本轮完整业务模型的待审核草稿；"
            "这不代表正式定义已经应用。任务需要逐项确认，不能安全写入的候选保持停用，"
            "具体缺口在最终总结中按根因合并。"
        )
        final_thinking = [{
                "id": "scenario-model",
                "title": "编译完整业务模型",
                "detail": "复合变更清单、来源覆盖和待确认项已生成并持久化。",
                "status": "done",
        }]
        finalize_kwargs = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "job_id": job_id,
            "thread_id": thread_id,
            "assistant_message_id": assistant_message_id,
            "scenario_id": scenario_id,
            "context": context,
            "sources": sources,
            "thinking": final_thinking,
            "prepared_context": prepared_context,
            "lease_token": lease_token,
            "lease_attempt": lease_attempt,
        }
        try:
            _finalize_compilation_success(data=data, reply=reply, **finalize_kwargs)
        except assistant_compilation_job_service.CompilationLeaseLost:
            raise
        except Exception:  # noqa: BLE001 - salvage a persistable editing surface.
            data = _unavailable_worker_draft(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
                on_progress=record_compilation_stage,
                code="DRAFT_MATERIALIZATION_INTERRUPTED",
                message=(
                    "生成结果未能完整写入草稿区；系统已改为保存来源绑定的"
                    "分阶段占位草稿，正式模型保持不变。"
                ),
            )
            reply = (
                "模型结果写入草稿区时发生问题；已建立 6 类来源绑定占位草稿，"
                "可直接修改并继续任务。"
            )
            _finalize_compilation_success(data=data, reply=reply, **finalize_kwargs)
    except assistant_compilation_job_service.CompilationLeaseLost:
        worker_db.rollback()
    except Exception as exc:  # noqa: BLE001 - terminal job state is persisted.
        worker_db.rollback()
        if tenant_id:
            try:
                _fail_compilation_job(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    job_id=job_id,
                    error=exc,
                    lease_token=lease_token,
                    lease_attempt=lease_attempt,
                )
            except assistant_compilation_job_service.CompilationLeaseLost:
                pass
    finally:
        _stop_compilation_lease_heartbeat(heartbeat_stop, heartbeat_thread)
        worker_db.close()
        if _slot_reserved:
            _COMPILATION_SUBMISSION_SLOTS.release()


def _release_compilation_lease_safely(
    job_id: str,
    *,
    lease_token: str,
    lease_attempt: int,
) -> None:
    release_db = SessionLocal()
    try:
        assistant_compilation_job_service.release_compilation_lease(
            release_db,
            job_id,
            token=lease_token,
            attempt=lease_attempt,
        )
    finally:
        release_db.close()


def _submit_compilation_job(
    *,
    job_id: str,
    lease_token: str = "",
    lease_attempt: int = 0,
    _slot_reserved: bool = False,
) -> bool:
    """Fence and submit only work that has an actual executor slot."""
    slot_owned = bool(_slot_reserved)
    submitted = False
    if not _slot_reserved:
        slot_owned = _COMPILATION_SUBMISSION_SLOTS.acquire(blocking=False)
        if not slot_owned:
            if lease_token:
                _release_compilation_lease_safely(
                    job_id,
                    lease_token=lease_token,
                    lease_attempt=lease_attempt,
                )
            return False
    try:
        if not lease_token:
            lease_db = SessionLocal()
            try:
                job = lease_db.get(AssistantCompilationJob, job_id)
                if not job or job.status != "running":
                    return False
                lease = assistant_compilation_job_service.acquire_compilation_lease(
                    lease_db,
                    job_id,
                    tenant_id=str(job.tenant_id or ""),
                    created_by_user_id=(
                        str(job.created_by_user_id)
                        if job.created_by_user_id is not None
                        else None
                    ),
                )
            finally:
                lease_db.close()
            if lease is None:
                return False
            lease_token = lease.token
            lease_attempt = lease.attempt
        _COMPILATION_EXECUTOR.submit(
            _run_compilation_job_in_background,
            job_id=job_id,
            lease_token=lease_token,
            lease_attempt=lease_attempt,
            _slot_reserved=True,
        )
        submitted = True
        return True
    except Exception:
        if lease_token:
            _release_compilation_lease_safely(
                job_id,
                lease_token=lease_token,
                lease_attempt=lease_attempt,
            )
        raise
    finally:
        # Ownership of a successful reservation transfers to the worker.
        if slot_owned and not submitted:
            _COMPILATION_SUBMISSION_SLOTS.release()


def recover_expired_compilation_jobs(*, limit: int = 4) -> int:
    """Claim restartable jobs only when this process can execute them now."""
    submitted = 0
    for _ in range(max(1, min(int(limit), _COMPILATION_WORKER_COUNT))):
        if not _COMPILATION_SUBMISSION_SLOTS.acquire(blocking=False):
            break
        scan_db = SessionLocal()
        try:
            leases = assistant_compilation_job_service.claim_expired_running_jobs(
                scan_db,
                limit=1,
            )
        except Exception:
            _COMPILATION_SUBMISSION_SLOTS.release()
            raise
        finally:
            scan_db.close()
        if not leases:
            _COMPILATION_SUBMISSION_SLOTS.release()
            break
        lease = leases[0]
        try:
            queued = _submit_compilation_job(
                job_id=lease.job_id,
                lease_token=lease.token,
                lease_attempt=lease.attempt,
                _slot_reserved=True,
            )
        except Exception:
            raise
        if queued:
            submitted += 1
    return submitted


def _attachment_context(
    attachments: list[AssistantAttachment],
    *,
    include_text: bool = True,
    enforce_context_limit: bool = True,
) -> tuple[str, list[dict[str, Any]]]:
    if not attachments:
        return "", []
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    included_chars = 0
    for item in attachments:
        parsed_text = str(item.parsed_text or "")
        sources.append({
            "id": item.id,
            "filename": item.filename,
            "status": item.status,
            "characters": len(parsed_text),
            "truncated": False,
        })
        if not include_text:
            continue
        if item.status == "parsed" and item.parsed_text:
            part = f"【附件：{item.filename}】\n{parsed_text}"
        elif item.error:
            part = f"【附件：{item.filename}】解析失败：{item.error}"
        else:
            continue
        projected = included_chars + (len(parsed_text) if parsed_text else len(part))
        if (
            enforce_context_limit
            and projected > ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS
        ):
            raise HTTPException(
                413,
                "所选附件正文合计"
                f" {projected} 个字符，超过单次助手建模上下文"
                f" {ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS} 个字符的明确边界；"
                "系统不会静默截断文档，请拆分附件后分批生成并审阅本体草稿。",
            )
        parts.append(part)
        included_chars = projected
    return "\n\n".join(parts), sources


def _authorized_rag_context(
    db: Session,
    scenario: BusinessScenario | None,
    query: str,
) -> tuple[str, list[dict[str, Any]]]:
    """为全局助手补入当前场景可访问资料库的引用上下文。

    场景 ACL 已由 `_scenario` 强制校验；RAG 服务仍会重复校验资料库租户可见性，
    所以不会因助手路径绕过资料库或场景边界。
    """
    if not scenario or not (query or "").strip():
        return "", []
    source_ids = list(
        db.scalars(
            select(DataSource.id).where(
                DataSource.scenario_id == scenario.id,
                DataSource.type == "file_bucket",
            )
        ).all()
    )
    if not source_ids:
        return "", []
    results = rag_service.search(db, source_ids, query, top_k=5, max_chars=4_000)
    if not results:
        return "", []
    sources = [
        {
            # Keep a complete, versioned reference rather than only a display
            # label.  Assistant messages are durable history, so this metadata
            # is re-authorized before either showing the answer or feeding it
            # back into a later model turn.
            "id": f"rag:{item['citation_id']}:{item['chunk_id']}",
            "kind": "rag",
            "citation_id": item["citation_id"],
            "filename": f"{item['citation_id']} · {item['filename']}",
            "status": "cited",
            "data_source_id": item["data_source_id"],
            "file_id": item["file_id"],
            "chunk_id": item["chunk_id"],
            "content_hash": item.get("content_hash") or "",
            "file_content_hash": item.get("file_content_hash") or "",
            "index_version": item.get("index_version") or "",
            "char_start": item.get("char_start"),
            "char_end": item.get("char_end"),
        }
        for item in results
    ]
    return rag_service.build_context(results), sources


_HISTORICAL_RAG_REDACTION = "该历史回答引用的资料已不在当前访问范围，内容已隐藏。"


def _is_rag_source(source: object) -> bool:
    """Identify persisted RAG sources without mistaking local attachments for them.

    Older records did not carry ``kind``.  Treat their stable RAG-like fields as
    RAG too, so a legacy incomplete citation fails closed instead of becoming a
    permanent disclosure.
    """
    if not isinstance(source, dict):
        return False
    return bool(
        source.get("kind") == "rag"
        or str(source.get("id") or "").startswith("rag:")
        or source.get("data_source_id")
        or source.get("file_id")
        or source.get("chunk_id")
        or source.get("file_content_hash")
    )


def _current_rag_source(
    db: Session,
    thread: AssistantThread,
    source_meta: object,
) -> tuple[DataSource, BucketFile, DocumentChunk | None] | None:
    """Re-authorize one persisted assistant RAG citation.

    The source must still be visible to the request tenant, remain bound to the
    thread's scenario, pass the scenario ACL, and point to exactly the same
    indexed document version.  Any ambiguous/legacy reference is intentionally
    invalid; showing an answer is less important than keeping revoked material
    out of history and subsequent LLM prompts.
    """
    if not isinstance(source_meta, dict) or not thread.scenario_id:
        return None
    source_id = str(source_meta.get("data_source_id") or "")
    file_id = str(source_meta.get("file_id") or "")
    chunk_id = str(source_meta.get("chunk_id") or "")
    expected_file_hash = str(source_meta.get("file_content_hash") or "")
    if not source_id or not file_id or not expected_file_hash:
        return None

    source = tenant_service.get_visible(db, DataSource, source_id)
    if (
        not source
        # A global-assistant thread is tenant-owned.  A foreign resource being
        # temporarily public must not become durable private-thread context.
        or source.tenant_id != _tenant(db)
        or source.type != "file_bucket"
        or source.scenario_id != thread.scenario_id
    ):
        return None
    scenario = tenant_service.get_visible(db, BusinessScenario, thread.scenario_id)
    if not scenario or not permission_service.check_scenario(db, scenario, "read").allowed:
        return None

    bucket_file = db.get(BucketFile, file_id)
    if (
        not bucket_file
        or bucket_file.data_source_id != source.id
        or bucket_file.status != "parsed"
        # A stale parsed_text with an old index hash is also an altered file.
        or not rag_service._index_is_current(bucket_file)
        or bucket_file.indexed_content_hash != expected_file_hash
    ):
        return None

    if not chunk_id:
        return source, bucket_file, None
    chunk = db.get(DocumentChunk, chunk_id)
    if (
        not chunk
        or chunk.bucket_file_id != bucket_file.id
        or chunk.data_source_id != source.id
    ):
        return None
    expected_chunk_hash = str(source_meta.get("content_hash") or "")
    if expected_chunk_hash and chunk.content_hash != expected_chunk_hash:
        return None
    return source, bucket_file, chunk


def _has_invalid_historic_rag_source(
    db: Session,
    thread: AssistantThread,
    message: AssistantMessage,
) -> bool:
    sources = message.attachments if isinstance(message.attachments, list) else []
    rag_sources = [source for source in sources if _is_rag_source(source)]
    return bool(rag_sources) and any(
        _current_rag_source(db, thread, source) is None for source in rag_sources
    )


def _assistant_message_out(
    db: Session,
    thread: AssistantThread,
    message: AssistantMessage,
) -> AssistantMessageOut:
    """Serialize assistant history without turning old citations into a bypass."""
    if message.role == "assistant" and _has_invalid_historic_rag_source(db, thread, message):
        # Proposal/thinking can contain the same facts as the final answer; all
        # three need to disappear together with the cited source cards.
        return AssistantMessageOut(
            id=message.id,
            thread_id=message.thread_id,
            role=message.role,
            content=_HISTORICAL_RAG_REDACTION,
            context={},
            attachments=[],
            proposal={},
            thinking=[],
            created_at=message.created_at,
        )
    result = AssistantMessageOut.model_validate(message)
    context = message.context if isinstance(message.context, dict) else {}
    evidence = context.get("evidence") if isinstance(context.get("evidence"), dict) else {}
    uncertainties = evidence.get("uncertainties") if isinstance(evidence, dict) else []
    routing = context.get("routing") if isinstance(context.get("routing"), dict) else {}
    if (
        message.role == "assistant"
        and routing.get("source") == "model_fallback"
        and not (message.proposal if isinstance(message.proposal, dict) else {})
    ):
        # Historic route failures sometimes continued into unconstrained chat,
        # whose prose could contradict the persisted no-write route evidence.
        # Keep the raw row for audit, but expose the authoritative route result.
        public_notice = _route_fallback_public_notice()
        result.content = public_notice
        result.context = {**context, "status": "route_fallback"}
        result.evidence = {
            **evidence,
            "uncertainties": [public_notice],
        }
        result.proposal = {}
        result.action_preview = {}
        return result
    if (
        message.role == "assistant"
        and context.get("draft_kind") == "scenario_model"
        and not (message.proposal if isinstance(message.proposal, dict) else {})
        and isinstance(uncertainties, list)
        and uncertainties
    ):
        # Historic deployments echoed parser/provider details into failed
        # assistant messages.  Preserve the server-side row for diagnosis but
        # redact it whenever history is serialized after this upgrade.
        public_error = assistant_compilation_job_service.public_compilation_error(
            str(uncertainties[0] or message.content or "编译失败")
        )
        safe_evidence = {
            **evidence,
            "uncertainties": [public_error.message],
        }
        result.content = public_error.message
        result.context = {
            key: value
            for key, value in context.items()
            if key not in {"evidence", "raw_error", "provider_error"}
        }
        result.context["error_code"] = public_error.code
        result.evidence = safe_evidence
        result.action_preview = {}
        return result
    result.evidence = dict(evidence or {})
    result.action_preview = dict(context.get("action_preview") or {})
    result.proposal = _public_recovery_proposal(message.proposal or {})
    return result


def _assistant_planner_context(
    db: Session,
    scenario: BusinessScenario | None,
) -> str:
    if scenario is None:
        return "当前没有绑定业务场景。"
    visible_workflows = [
        item for item in list(getattr(scenario, "workflows", []) or [])
        if permission_service.check_workflow(db, item, "read").allowed
    ]
    visible_actions = [
        item for item in list(getattr(scenario, "actions", []) or [])
        if permission_service.check_action(db, item, "read").allowed
    ]
    return (
        f"当前场景：{scenario.name}。"
        f"正式资源统计：对象 {len(list(getattr(scenario, 'entities', []) or []))}，"
        f"关系 {len(list(getattr(scenario, 'relations', []) or []))}，"
        f"函数 {len(list(getattr(scenario, 'function_definitions', []) or []))}，"
        f"操作 {len(visible_actions)}，"
        f"规则 {len(list(getattr(scenario, 'rules', []) or []))}，"
        f"事件 {len(list(getattr(scenario, 'events', []) or []))}，"
        f"工作流 {len(visible_workflows)}。"
    )


def _request_route_plan(
    db: Session,
    scenario: BusinessScenario | None,
    thread: AssistantThread | None,
    payload: AssistantChatRequest,
    *,
    has_attachments: bool,
    request_id: str,
) -> assistant_orchestrator.AssistantRoutePlan:
    history = _history_messages(db, thread, "") if thread is not None else []
    active_draft_scopes = (
        scenario_model_draft_service.active_working_draft_scopes(db, scenario)
        if scenario
        else []
    )
    active_drafts = bool(active_draft_scopes)
    previous_trace = db.info.get("llm_trace_context")
    db.info["llm_trace_context"] = {
        **dict(previous_trace or {}),
        "correlation_id": request_id,
        "scenario_id": scenario.id if scenario else None,
    }
    try:
        return assistant_orchestrator.plan_assistant_request(
            llm=_llm(db),
            db=db,
            message=payload.message,
            history=history,
            page=payload.page,
            path=payload.path,
            mode=payload.mode,
            preferred_scope=payload.draft_kind,
            has_scenario=scenario is not None,
            has_attachments=has_attachments,
            has_active_model_drafts=active_drafts,
            active_draft_scopes=active_draft_scopes,
            context_summary=_assistant_planner_context(db, scenario),
        )
    finally:
        if previous_trace is None:
            db.info.pop("llm_trace_context", None)
        else:
            db.info["llm_trace_context"] = previous_trace


def _assistant_route_fingerprint(
    payload: AssistantChatRequest,
    *,
    scope_key: str,
) -> str:
    canonical = {
        "message": payload.message,
        "scope_key": scope_key,
        "scenario_id": payload.scenario_id or "",
        "page": payload.page,
        "selection": payload.selection,
        "attachment_ids": sorted(set(payload.attachment_ids)),
        "llm_config_id": payload.llm_config_id or "",
        "skill_ids": sorted(set(payload.skill_ids)),
        "mcp_ids": sorted(set(payload.mcp_ids)),
        "mode": payload.mode,
        "draft_kind": payload.draft_kind,
    }
    return hashlib.sha256(json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


def _route_plan_from_claim(claim: AssistantRouteDecision) -> assistant_orchestrator.AssistantRoutePlan:
    try:
        return assistant_orchestrator.AssistantRoutePlan.model_validate(claim.route_plan)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, "已保存的助手语义决策无效，请使用新的 request_id 重试") from exc


def _ensure_route_thread(
    db: Session,
    *,
    thread_id: str,
    scope_key: str,
    payload: AssistantChatRequest,
) -> None:
    existing = db.execute(
        select(AssistantThread).where(
            AssistantThread.id == thread_id,
            AssistantThread.tenant_id == _tenant(db),
            AssistantThread.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if existing is not None:
        _assert_thread_scope(existing, payload.scenario_id, payload.page, payload.path)
        return
    db.add(AssistantThread(
        id=thread_id,
        tenant_id=_tenant(db),
        created_by_user_id=_current_user_id(db),
        scenario_id=payload.scenario_id,
        scope_key=scope_key,
        title="新的助手任务",
    ))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(
            select(AssistantThread).where(
                AssistantThread.id == thread_id,
                AssistantThread.tenant_id == _tenant(db),
                AssistantThread.created_by_user_id == _current_user_id(db),
            )
        ).scalars().first()
        if existing is None:
            raise HTTPException(409, "助手会话初始化冲突，请使用新的 request_id 重试")
        _assert_thread_scope(existing, payload.scenario_id, payload.page, payload.path)


def _claimed_request_route_plan(
    db: Session,
    scenario: BusinessScenario | None,
    thread: AssistantThread | None,
    payload: AssistantChatRequest,
    *,
    scope_key: str,
    request_id: str,
    pending_thread_id: str,
) -> tuple[
    assistant_orchestrator.AssistantRoutePlan,
    str,
    list[AssistantAttachment],
]:
    """Single-flight one semantic decision and freeze it before generation."""
    fingerprint = _assistant_route_fingerprint(payload, scope_key=scope_key)
    tenant_id = _tenant(db)
    user_id = _current_user_id(db)
    candidate_thread_id = thread.id if thread is not None else pending_thread_id
    # Reject unavailable attachments before creating a durable routing claim.
    attachments = _safe_attachment_ids(
        db,
        payload.attachment_ids,
        thread_id=candidate_thread_id,
        consume=False,
    )
    wait_deadline = time.monotonic() + 22
    lease_token = uuid.uuid4().hex
    claim: AssistantRouteDecision | None = None
    owns_claim = False

    while True:
        claim = db.execute(
            select(AssistantRouteDecision).where(
                AssistantRouteDecision.tenant_id == tenant_id,
                AssistantRouteDecision.created_by_user_id == user_id,
                AssistantRouteDecision.request_id == request_id,
            )
        ).scalars().first()
        if claim is None:
            claim = AssistantRouteDecision(
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                request_id=request_id,
                request_fingerprint=fingerprint,
                scenario_id=payload.scenario_id,
                thread_id=candidate_thread_id,
                status="planning",
                route_plan={},
                lease_token=lease_token,
                lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=20),
            )
            db.add(claim)
            try:
                db.commit()
                owns_claim = True
                break
            except IntegrityError:
                db.rollback()
                continue

        if claim.request_fingerprint != fingerprint:
            raise HTTPException(409, "request_id 已用于不同的助手输入，请重新发送")
        if payload.thread_id and claim.thread_id != payload.thread_id:
            raise HTTPException(409, "request_id 已绑定到另一助手会话")
        if thread is not None and claim.thread_id != thread.id:
            raise HTTPException(409, "request_id 与当前助手会话不一致")
        candidate_thread_id = claim.thread_id
        attachments = _safe_attachment_ids(
            db,
            payload.attachment_ids,
            thread_id=candidate_thread_id,
            consume=False,
        )
        if claim.status == "decided":
            _ensure_route_thread(
                db,
                thread_id=candidate_thread_id,
                scope_key=scope_key,
                payload=payload,
            )
            return _route_plan_from_claim(claim), candidate_thread_id, attachments

        lease_expires_at = claim.lease_expires_at
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if lease_expires_at <= datetime.now(timezone.utc):
            lease_token = uuid.uuid4().hex
            takeover = db.execute(
                update(AssistantRouteDecision)
                .where(
                    AssistantRouteDecision.id == claim.id,
                    AssistantRouteDecision.status == "planning",
                    AssistantRouteDecision.lease_token == claim.lease_token,
                )
                .values(
                    lease_token=lease_token,
                    lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=20),
                )
            )
            db.commit()
            if takeover.rowcount == 1:
                claim = db.get(AssistantRouteDecision, claim.id)
                owns_claim = True
                break
            continue
        if time.monotonic() >= wait_deadline:
            db.rollback()
            raise HTTPException(409, "同一助手请求仍在进行语义规划，请稍后重试")
        db.rollback()
        time.sleep(0.05)

    assert claim is not None and owns_claim
    route_plan = _request_route_plan(
        db,
        scenario,
        thread,
        payload,
        has_attachments=bool(attachments),
        request_id=request_id,
    )
    persisted = db.execute(
        update(AssistantRouteDecision)
        .where(
            AssistantRouteDecision.id == claim.id,
            AssistantRouteDecision.status == "planning",
            AssistantRouteDecision.lease_token == lease_token,
        )
        .values(
            status="decided",
            route_plan=route_plan.model_dump(mode="json"),
            lease_expires_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    if persisted.rowcount != 1:
        replay = db.get(AssistantRouteDecision, claim.id)
        if replay is None or replay.status != "decided":
            raise HTTPException(409, "助手语义规划所有权已变化，请稍后重试")
        route_plan = _route_plan_from_claim(replay)
    _ensure_route_thread(
        db,
        thread_id=candidate_thread_id,
        scope_key=scope_key,
        payload=payload,
    )
    return route_plan, candidate_thread_id, attachments


def _mode_safety_context(mode: str) -> str:
    if mode == "explain":
        return "\n当前是解释模式：只读分析已授权上下文，不生成变更清单，不应用变更，不触发执行。"
    if mode == "draft":
        return "\n当前选择了建模范围偏好：只有本条语义明确要求建设时才生成待审阅变更；提问仍直接回答。"
    if mode == "apply":
        return "\n当前是应用引导模式：聊天不能写入，只能引导用户在已保存的提案卡片显式确认。"
    if mode == "execute":
        return "\n当前选择了安全预演偏好：明确要求预演时只检查影响和权限；普通问题仍直接回答。"
    return "\n当前是智能协助：按本条语义回答问题或准备待确认草稿，但不得直接应用或执行。"


def _route_fallback_public_notice() -> str:
    return (
        "这次语义规划没有完成，我无法安全判断你是在提问还是要求建设内容。"
        "本条已停止处理，没有调用回答或建模模型，也没有生成、应用或保存任何变更。"
        "请重新发送本条请求；如果仍然失败，请先检查当前默认模型的连接和结构化输出能力。"
    )


def _route_fallback_notice(
    route_plan: assistant_orchestrator.AssistantRoutePlan,
) -> str:
    return (
        _route_fallback_public_notice()
        if route_plan.source == "model_fallback"
        else ""
    )


def _read_only_chat_contract() -> str:
    return (
        "\n本轮进入普通问答分支，平台没有创建 proposal、编译任务或应用结果。"
        "可以回答和解释，但不得声称本轮已经创建、保存、写入、应用或执行了任何平台资源；"
        "若用户要求的是建设或变更，应明确说明本轮没有产生变更。"
    )


def _assistant_evidence(
    intent: str,
    *,
    proposal: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    llm_used: bool = False,
    preview: dict[str, Any] | None = None,
    uncertainties: list[str] | None = None,
) -> dict[str, Any]:
    rule_names = {
        "scenario": ("draft_only", "新场景固定进入 draft，附件不提升为正式数据源"),
        "ontology": ("ontology_validation", "实体、属性、关系和约束在应用边界重新校验"),
        "mapping": ("mapping_reference_validation", "实体、数据源、表、列、主键和必填字段必须真实存在"),
        "workflow": ("workflow_dag_validation", "节点、连线和操作引用在应用边界重新校验"),
        "scenario_model": ("compound_model_validation", "全文来源覆盖、跨资源引用和冲突通过后才允许同一事务应用"),
        "apply_guidance": ("explicit_confirmation", "聊天不会写入正式业务模型，只有已保存提案的 confirm=true 可应用"),
        "execute_guidance": ("typed_action_only", "聊天只预演，真实执行必须进入场景中已配置的操作或任务审批"),
        "capability_update_guidance": (
            "unsupported_capability_update_read_only",
            "已有函数、操作、规则和事件的修改或删除不进入只支持新增的复合编译器",
        ),
        "change_guidance": (
            "existing_definition_change_read_only",
            "已有正式定义的修改或删除必须进入专用编辑与确认流程，聊天不会生成替代资源",
        ),
        "explain": ("read_only", "解释模式只读取已授权上下文"),
        "chat": ("read_only", "问答不直接修改或执行平台资源"),
    }
    key, detail = rule_names.get(intent, rule_names["chat"])
    tools: list[dict[str, Any]] = []
    if llm_used:
        tools.append({"name": "llm", "status": "completed", "purpose": "生成或解释"})
    if sources:
        tools.append({"name": "authorized_retrieval", "status": "completed", "purpose": "读取已授权引用"})
    if proposal:
        tools.append({"name": "change_set_validator", "status": "completed", "purpose": "生成待确认变更集"})
    if preview:
        tools.append({"name": "action_preview", "status": "completed", "purpose": "参数、权限与副作用预演"})
    confidence = 0.9 if preview else 0.78 if proposal else 0.7 if llm_used else 1.0
    return {
        "rules_used": [{"id": key, "name": key, "result": detail}],
        "tools_called": tools,
        "confidence": confidence,
        "uncertainties": [str(item)[:500] for item in (uncertainties or [])],
    }


def _assistant_action_preview(
    db: Session,
    scenario: BusinessScenario | None,
    message: str,
    selection: dict[str, Any] | None,
    *,
    assistant_message_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Resolve and dry-run one governed Action; never confirms execution."""
    if not scenario:
        question = {
            "id": "execute-scenario",
            "title": "需要业务场景",
            "message": "操作预演必须绑定业务场景，才能校验目标、权限和运行定义。",
            "options": [
                {"label": "打开业务场景", "value": "open_scenario", "impact": "进入已有场景后可选择操作并完成只读预演。", "recommended": True},
                {"label": "保持只读说明", "value": "explain_only", "impact": "仅解释执行流程，不创建预演日志，也不触发副作用。"},
            ],
        }
        return {}, question, "请先打开一个业务场景，我才能安全地解析并预演操作；聊天不会直接触发任何操作。"

    readable_actions: list[Any] = []
    for action in list(getattr(scenario, "actions", []) or []):
        try:
            permission_service.require_action_permission(db, action, "read")
        except Exception:
            continue
        if bool(getattr(action, "enabled", False)):
            readable_actions.append(action)

    selected = dict(selection or {})
    selected_id = str(
        selected.get("action_id")
        or (selected.get("id") if selected.get("type") == "action" else "")
        or ""
    ).strip()
    selected_name = str(selected.get("action_name") or "").strip()
    action = next((item for item in readable_actions if item.id == selected_id), None)
    if action is None and selected_name:
        action = next((item for item in readable_actions if item.name == selected_name), None)
    if action is None:
        mentioned = [item for item in readable_actions if item.name and item.name in message]
        if len(mentioned) == 1:
            action = mentioned[0]
    if action is None:
        options = [
            {
                "label": item.name,
                "value": item.id,
                "impact": (
                    (item.postcondition or item.description or "将先校验参数、权限和影响范围")[:300]
                    + "；本步骤只预演，不执行副作用。"
                ),
                "recommended": index == 0,
            }
            for index, item in enumerate(readable_actions[:8])
        ]
        if not options:
            options = [
                {
                    "label": "配置类型化操作",
                    "value": "configure_action",
                    "impact": "先在场景的“操作”页定义输入字段、权限、重复提交保护和执行方式，之后才能预演。",
                    "recommended": True,
                }
            ]
        question = {
            "id": "select-action",
            "title": "选择要预演的操作",
            "message": "我不会从模糊文字猜测有副作用的目标。请选择一个当前有权读取的操作。",
            "options": options,
        }
        return {}, question, "需要先确定一个明确的已配置操作，聊天不会直接执行任何副作用。"

    raw_params = selected.get("params", selected.get("parameters", {}))
    params = raw_params if isinstance(raw_params, dict) else {}
    schema = action.input_schema or {}
    required = list(schema.get("required") or []) if schema.get("type") == "object" else []
    missing = [str(name) for name in required if name not in params]
    if missing or not isinstance(raw_params, dict):
        question = {
            "id": "action-parameters",
            "title": "补充操作参数",
            "message": f"“{action.name}”还缺少必填参数：{'、'.join(missing) if missing else '请按字段填写参数'}。",
            "options": [
                {"label": "填写必填参数", "value": "provide_params", "impact": "参数齐全后仅执行权限检查和预演，不触发外部副作用。", "recommended": True},
                {"label": "查看参数定义", "value": "inspect_schema", "impact": "只查看输入字段与影响说明，不创建预演日志。"},
            ],
        }
        analysis = {
            "target": {"id": action.id, "name": action.name, "entity_id": action.entity_id},
            "parameter_schema": schema,
            "parameters": params,
            "missing_parameters": missing,
            "impact": {
                "precondition": action.precondition or "",
                "postcondition": action.postcondition or action.description or "",
                "executor_type": action.executor_type,
                "side_effects_skipped": True,
            },
            "permission": {"checked": False, "reason": "参数不完整，尚未创建预演"},
            "preview": {},
            "requires_approval": bool(action.requires_confirmation),
        }
        return analysis, question, f"已定位操作“{action.name}”，补齐参数后才能完成权限检查和预演。"

    definition = runtime_definition_service.resolve_active(
        db,
        scenario,
        environment=runtime_connector_service.runtime_environment(),
    )
    runtime_action = runtime_definition_service.resolve_resource(
        definition, "action", action.id
    )
    permission_service.require_action_permission(db, runtime_action, "read")
    previous_lineage = db.info.get("action_lineage_context")
    db.info["action_lineage_context"] = {
        "correlation_id": uuid.uuid4().hex,
        "assistant_message_id": assistant_message_id,
    }
    try:
        preview = workflow_service.preview_action(
            db,
            runtime_action,
            params,
            runtime_environment=definition.environment,
            runtime_definition=definition,
        )
    finally:
        if previous_lineage is None:
            db.info.pop("action_lineage_context", None)
        else:
            db.info["action_lineage_context"] = previous_lineage
    analysis = {
        "target": {"id": runtime_action.id, "name": runtime_action.name, "entity_id": runtime_action.entity_id},
        "parameter_schema": runtime_action.input_schema or {},
        "parameters": (preview.get("result") or {}).get("plan", {}).get("parameters", params),
        "impact": {
            "precondition": runtime_action.precondition or "",
            "postcondition": runtime_action.postcondition or runtime_action.description or "",
            "executor_type": runtime_action.executor_type,
            "side_effects_skipped": True,
        },
        "permission": preview.get("permission") or {},
        "preview": preview,
        "requires_approval": bool(runtime_action.requires_confirmation),
        "execution_boundary": "真实执行必须从场景操作或任务入口重新确认；聊天不会跳过确认步骤。",
    }
    approval = "需要显式确认或审批" if runtime_action.requires_confirmation else "仍需从操作入口提交"
    return analysis, None, f"已完成操作“{runtime_action.name}”的预演；{approval}，本次没有触发外部副作用。"


def _generate_scenario_draft(db: Session, description: str) -> dict[str, Any]:
    """Generate a minimal global scenario draft without writing platform state."""
    llm = _llm(db)
    if not llm:
        raise ValueError("请先配置并启用一个默认 LLM")
    prompt = (
        "根据业务描述生成一个业务场景草稿。只输出 JSON，字段只能包含 "
        "name、description、industry。name 是简短稳定的业务域名称，"
        "description 描述目标、角色和边界，不得包含凭据或执行代码。\n\n"
        f"业务描述：\n{description[:24000]}"
    )
    response = llm_service.chat(
        llm,
        [
            {"role": "system", "content": "你只输出合法 JSON，不执行任何操作。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=1200,
        db=db,
    )
    raw = ontology_service._extract_json(response.get("content", ""))
    name = str(raw.get("name") or "").strip()[:200]
    if not name:
        raise ValueError("AI 未生成有效的业务场景名称")
    draft = ScenarioIn(
        name=name,
        description=str(raw.get("description") or "").strip()[:6000],
        industry=str(raw.get("industry") or "").strip()[:100],
        status="draft",
    )
    return draft.model_dump()


def _mapping_catalog(
    db: Session,
    scenario: BusinessScenario,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], set[str]]]:
    """Return credential-free schema candidates for a mapping draft."""
    sources = list(
        db.execute(
            select(DataSource).where(
                tenant_service.visible_clause(DataSource, db),
                or_(DataSource.scenario_id.is_(None), DataSource.scenario_id == scenario.id),
                DataSource.type != "file_bucket",
            )
            .order_by(DataSource.created_at, DataSource.id)
            .limit(10)
        ).scalars().all()
    )
    catalog: list[dict[str, Any]] = []
    columns_by_table: dict[tuple[str, str], set[str]] = {}
    for source in sources:
        try:
            tables = datasource_service.list_tables(source)[:40]
        except Exception:  # A broken connector is not a valid AI mapping candidate.
            continue
        safe_tables: list[dict[str, Any]] = []
        for table in tables:
            table_name = str(table.get("name") or "").strip()
            if not table_name:
                continue
            columns = [
                {
                    "name": str(column.get("name") or ""),
                    "type": str(column.get("type") or ""),
                    "pk": bool(column.get("pk")),
                }
                for column in (table.get("columns") or [])[:120]
                if str(column.get("name") or "").strip()
            ]
            columns_by_table[(source.id, table_name)] = {
                column["name"] for column in columns
            }
            safe_tables.append({"name": table_name, "columns": columns})
        if safe_tables:
            catalog.append(
                {
                    "data_source_id": source.id,
                    "data_source_name": source.name,
                    "type": source.type,
                    "tables": safe_tables,
                }
            )
    return catalog, columns_by_table


def _validate_mapping_draft(
    db: Session,
    scenario: BusinessScenario,
    data: dict[str, Any],
    *,
    catalog: list[dict[str, Any]] | None = None,
    columns_by_table: dict[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    """Revalidate all AI-selected references at generation and apply boundaries."""
    entity_id = str(data.get("entity_id") or "").strip()
    source_id = str(data.get("data_source_id") or "").strip()
    table_name = str(data.get("table_name") or "").strip()
    entity = db.get(OntologyEntity, entity_id)
    if not entity or entity.scenario_id != scenario.id:
        raise ValueError("数据映射草稿引用的实体不属于当前场景")
    source = tenant_service.get_visible(db, DataSource, source_id)
    if (
        not source
        or source.scenario_id not in (None, scenario.id)
        or source.type == "file_bucket"
    ):
        raise ValueError("数据映射草稿引用的数据源不可用或不属于当前场景")

    if catalog is None or columns_by_table is None:
        tables = datasource_service.list_tables(source)
        columns_by_table = {
            (source.id, str(table.get("name") or "")): {
                str(column.get("name") or "")
                for column in (table.get("columns") or [])
                if str(column.get("name") or "")
            }
            for table in tables
        }
    available_columns = columns_by_table.get((source.id, table_name))
    if available_columns is None:
        raise ValueError("数据映射草稿引用的源表不存在，请重新生成")

    raw_map = data.get("column_map") or {}
    if not isinstance(raw_map, dict):
        raise ValueError("字段映射必须是对象")
    properties = {prop.name: prop for prop in entity.properties}
    column_map: dict[str, str] = {}
    for property_name, source_column in raw_map.items():
        property_name = str(property_name).strip()
        source_column = str(source_column).strip()
        if property_name not in properties:
            raise ValueError(f"数据映射引用了不存在的属性“{property_name}”")
        if source_column not in available_columns:
            raise ValueError(f"数据映射引用了不存在的源字段“{source_column}”")
        column_map[property_name] = source_column
    key_properties = [prop.name for prop in entity.properties if prop.is_key]
    missing_keys = [name for name in key_properties if name not in column_map]
    missing_required = [
        prop.name
        for prop in entity.properties
        if prop.is_required and prop.name not in column_map
    ]
    if missing_keys:
        raise ValueError(f"数据映射必须覆盖主键属性：{'、'.join(missing_keys)}")
    if missing_required:
        raise ValueError(f"数据映射必须覆盖必填属性：{'、'.join(missing_required)}")
    if not column_map:
        raise ValueError("数据映射至少需要一个字段")

    # Assistant proposals never create connector bindings.  Environment
    # bindings remain an explicit governance step and cannot be smuggled in via
    # model output or attachment text.
    normalized = DataMappingIn(
        entity_id=entity.id,
        data_source_id=source.id,
        data_source_binding_key="",
        data_source_binding_ref={},
        table_name=table_name,
        column_map=column_map,
    ).model_dump()
    # Absence means “leave the governed transform definition unchanged” when
    # updating an existing mapping.  An explicit value, including {}, means
    # “replace it” and must pass the declarative allowlist first.
    if "transform_rules" in data:
        normalized["transform_rules"] = ontology_service.normalize_transform_rules(
            entity, data.get("transform_rules")
        )
    else:
        normalized.pop("transform_rules", None)
    return {
        **normalized,
        "entity_name": entity.name,
        "data_source_name": source.name,
    }


def _generate_mapping_draft(
    db: Session,
    scenario: BusinessScenario,
    description: str,
    selection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    llm = _llm(db)
    if not llm:
        raise ValueError("请先配置并启用一个默认 LLM")
    catalog, columns_by_table = _mapping_catalog(db, scenario)
    if not catalog:
        raise ValueError("当前场景没有可读取表结构的数据库数据源")
    entities = [
        {
            "entity_id": entity.id,
            "name": entity.name,
            "properties": [
                {
                    "name": prop.name,
                    "data_type": prop.data_type,
                    "is_key": bool(prop.is_key),
                    "is_title": bool(getattr(prop, "is_title", False)),
                    "is_required": bool(prop.is_required),
                }
                for prop in entity.properties
            ],
        }
        for entity in scenario.entities
    ]
    if not entities:
        raise ValueError("请先创建本体实体，再生成数据映射草稿")
    prompt = (
        "为现有本体实体和数据库表生成一条可执行的数据映射草稿。"
        "只输出 JSON，字段为 entity_id、data_source_id、table_name、column_map。"
        "column_map 的键必须是本体属性名，值必须是候选表中的真实列名；"
        "必须覆盖主键和所有必填属性，不得输出 SQL、连接配置、凭据或新资源。\n\n"
        f"当前选择：{json.dumps(selection or {}, ensure_ascii=False)}\n"
        f"本体实体：{json.dumps(entities, ensure_ascii=False)}\n"
        f"可用表结构：{json.dumps(catalog, ensure_ascii=False)}\n"
        f"用户说明：{description[:12000]}"
    )
    response = llm_service.chat(
        llm,
        [
            {"role": "system", "content": "你只选择给定 ID、表和列并输出合法 JSON。"},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=2200,
        db=db,
    )
    raw = ontology_service._extract_json(response.get("content", ""))
    return _validate_mapping_draft(
        db,
        scenario,
        raw,
        catalog=catalog,
        columns_by_table=columns_by_table,
    )


def _apply_mapping_draft(
    db: Session,
    scenario: BusinessScenario,
    data: dict[str, Any],
) -> tuple[DataMapping, str]:
    """Apply a revalidated mapping while preserving the stable import identity."""
    normalized = _validate_mapping_draft(db, scenario, data)
    entity = db.get(OntologyEntity, normalized["entity_id"])
    assert entity is not None
    old = list(
        db.execute(
            select(DataMapping)
            .where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.entity_id == entity.id,
            )
            .order_by(DataMapping.created_at, DataMapping.id)
        ).scalars().all()
    )
    key_property = next((prop.name for prop in entity.properties if prop.is_key), "")
    incoming_identity = (
        normalized["data_source_id"],
        normalized["table_name"],
        str(normalized["column_map"].get(key_property) or ""),
    )
    operation = "add"
    if old:
        current = old[0]
        current_identity = (
            current.data_source_id,
            current.table_name,
            str((current.column_map or {}).get(key_property) or ""),
        )
        if current_identity == incoming_identity:
            mapping = current
            before = mapping_refresh_service.mapping_fingerprint(mapping)
            mapping.column_map = normalized["column_map"]
            if "transform_rules" in normalized:
                mapping.transform_rules = normalized["transform_rules"]
            if mapping_refresh_service.mapping_fingerprint(mapping) != before:
                mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                    db,
                    mapping.id,
                    reason="助手确认的数据映射已更新，请重新提交刷新",
                )
                mapping_refresh_service.invalidate_mapping_runtime_state(mapping)
            duplicates = old[1:]
            operation = "update"
        else:
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=normalized["entity_id"],
                data_source_id=normalized["data_source_id"],
                data_source_binding_key="",
                data_source_binding_ref={},
                table_name=normalized["table_name"],
                column_map=normalized["column_map"],
                transform_rules=normalized.get("transform_rules") or {},
            )
            db.add(mapping)
            duplicates = old
        for duplicate in duplicates:
            mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                db,
                duplicate.id,
                reason="映射已被助手确认的新定义替换",
            )
            db.delete(duplicate)
    else:
        mapping = DataMapping(
            scenario_id=scenario.id,
            entity_id=normalized["entity_id"],
            data_source_id=normalized["data_source_id"],
            data_source_binding_key="",
            data_source_binding_ref={},
            table_name=normalized["table_name"],
            column_map=normalized["column_map"],
            transform_rules=normalized.get("transform_rules") or {},
        )
        db.add(mapping)
    db.flush()
    return mapping, operation


def _fallback_reply(intent: str, scenario: BusinessScenario | None) -> str:
    if intent == "apply_guidance":
        return (
            "聊天模式不会直接应用任何变更。请在会话中选择一个已保存的变更清单，"
            "先核对变更范围、基线和权限，再点击显式确认；服务端只有在应用接口收到 "
            "confirm=true 时才会写入。"
        )
    if intent == "execute_guidance":
        return (
            "聊天模式不会直接触发操作、工作流或外部副作用。请先在对应的操作或任务界面"
            "核对目标对象、参数、影响范围、权限决策和预演结果；真正执行仍需进入场景中已配置的操作"
            "或任务审批流程。"
        )
    if intent == "capability_update_guidance":
        return (
            "当前完整模型编译器只会新增函数、操作、规则和事件，"
            "不会覆盖或删除已有定义。为避免生成必然阻塞的变更清单，"
            "本次已统一降级为只读指导，没有启动完整模型编译、"
            "生成可应用提案或写入数据。"
            "请在场景建模页的对应资源编辑入口修改已有定义；"
            "若要创建新能力，请明确使用“新增”或“创建”后重新提交。"
        )
    if intent == "change_guidance":
        return (
            "我理解你希望修改或删除已有正式定义。聊天不会把这类请求伪装成一个新的资源，"
            "也不会直接覆盖现有配置。请在对应资源的编辑入口核对当前定义并提交；"
            "涉及正式变更时仍需通过原有权限和确认流程。"
        )
    if intent == "explain":
        if scenario:
            return (
                f"当前上下文是「{scenario.name}」。解释模式只读取已授权上下文，不会生成或应用"
                "变更；请配置默认 LLM 以启用自然语言解释。"
            )
        return "解释模式只读取已授权上下文，不会生成或应用变更。请配置默认 LLM 后继续提问。"
    if intent == "scenario":
        return "我可以根据业务描述和临时附件生成业务场景草稿。请先配置一个默认 LLM。"
    if intent == "mapping":
        return "我可以根据当前本体和数据库表结构生成字段映射草稿。请先准备实体、数据库数据源和默认 LLM。"
    if intent == "ontology":
        return "我可以根据业务描述生成本体草稿。请先配置一个默认 LLM，并补充业务目标、核心对象、关键关系或上传业务资料。"
    if intent == "workflow":
        return "我可以根据业务描述生成工作流草稿。请先配置一个默认 LLM，并说明触发条件、判断规则、动作和最终结果。"
    if intent == "scenario_model":
        return "我可以逐段编译完整业务文档，并生成带来源、冲突和引用校验的复合变更清单。请先配置默认 LLM。"
    if scenario:
        return f"当前上下文是「{scenario.name}」。我可以协助你查询本体、解释对象关系，或生成建模与流程草稿。请先配置默认 LLM 以启用自然语言推理。"
    return "我可以协助你理解平台、设计业务场景和生成本体草稿。打开一个业务场景或配置默认 LLM 后，我可以提供更具体的帮助。"


def _purge_expired_attachments(db: Session) -> None:
    expired = db.execute(
        select(AssistantAttachment).where(
            AssistantAttachment.tenant_id == _tenant(db),
            AssistantAttachment.created_by_user_id == _current_user_id(db),
            AssistantAttachment.expires_at <= datetime.now(timezone.utc),
        )
    ).scalars().all()
    for attachment in expired:
        db.delete(attachment)
    if expired:
        db.flush()


def _safe_attachment_ids(
    db: Session,
    ids: list[str],
    *,
    thread_id: str,
    consume: bool = True,
) -> list[AssistantAttachment]:
    if not ids:
        return []
    unique_ids = list(dict.fromkeys(ids))
    now = datetime.now(timezone.utc)
    owned = list(
        db.execute(
            select(AssistantAttachment).where(
                AssistantAttachment.id.in_(unique_ids),
                AssistantAttachment.tenant_id == _tenant(db),
                # Like threads, legacy attachment rows without a demonstrable
                # owner are fail-closed rather than shared across a tenant.
                AssistantAttachment.created_by_user_id == _current_user_id(db),
            )
        ).scalars().all()
    )
    # A non-empty attachment request is all-or-nothing.  Missing, foreign,
    # expired and cross-thread ids all receive the same response, preserving
    # non-enumerability while preventing a silent attachment-free AI answer.
    invalid_owned = False
    attachments: list[AssistantAttachment] = []
    for attachment in owned:
        expires_at = attachment.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if (
            expires_at is None
            or expires_at <= now
            or attachment.thread_id not in (None, thread_id)
        ):
            invalid_owned = True
            continue
        attachments.append(attachment)
    if invalid_owned or len(owned) != len(unique_ids):
        raise HTTPException(409, "附件不可用、已过期或无权访问，请重新上传")
    if consume:
        _consume_attachments(db, attachments, thread_id=thread_id)
    return attachments


def _consume_attachments(
    db: Session,
    attachments: list[AssistantAttachment],
    *,
    thread_id: str,
) -> None:
    if not attachments:
        return
    _purge_expired_attachments(db)
    consumed_at = datetime.now(timezone.utc)
    attachment_ids = [attachment.id for attachment in attachments]
    claimed = db.execute(
        update(AssistantAttachment)
        .where(
            AssistantAttachment.id.in_(attachment_ids),
            AssistantAttachment.tenant_id == _tenant(db),
            AssistantAttachment.created_by_user_id == _current_user_id(db),
            AssistantAttachment.expires_at > consumed_at,
            or_(
                AssistantAttachment.thread_id.is_(None),
                AssistantAttachment.thread_id == thread_id,
            ),
        )
        .values(thread_id=thread_id, consumed_at=consumed_at)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != len(attachment_ids):
        raise HTTPException(409, "附件已被另一条消息占用，请重新上传")
    for attachment in attachments:
        attachment.thread_id = thread_id
        attachment.consumed_at = consumed_at
    db.flush()


def _save_message(
    db: Session,
    thread: AssistantThread,
    role: str,
    content: str,
    context: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    proposal: dict[str, Any] | None = None,
    thinking: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
) -> AssistantMessage:
    thread.updated_at = datetime.now(timezone.utc)
    if message_id:
        existing = db.get(AssistantMessage, message_id)
        if existing:
            if existing.thread_id != thread.id or existing.role != role:
                raise ValueError("助手消息标识与当前会话不匹配")
            existing.content = content
            existing.context = context or {}
            existing.attachments = attachments or []
            existing.proposal = proposal or {}
            existing.thinking = thinking or []
            return existing
    message = AssistantMessage(
        **({"id": message_id} if message_id else {}),
        thread_id=thread.id,
        role=role,
        content=content,
        context=context or {},
        attachments=attachments or [],
        proposal=proposal or {},
        thinking=thinking or [],
    )
    db.add(message)
    return message


def _history_messages(
    db: Session,
    thread: AssistantThread,
    user_message_id: str,
) -> list[dict[str, str]]:
    """读取助手历史，排除本次刚保存的用户消息。"""
    history = db.execute(
        select(AssistantMessage)
        .where(
            AssistantMessage.thread_id == thread.id,
            AssistantMessage.id != user_message_id,
        )
        .order_by(AssistantMessage.created_at.desc())
        .limit(8)
    ).scalars().all()
    result: list[dict[str, str]] = []
    for item in reversed(history):
        if item.role not in ("user", "assistant") or not item.content:
            continue
        content = (
            _assistant_message_out(db, thread, item).content
            if item.role == "assistant"
            else item.content
        )
        result.append({"role": item.role, "content": content[:8000]})
    return result


def _sse(event_type: str, data: Any) -> str:
    return f"data: {json.dumps({'type': event_type, 'data': data}, ensure_ascii=False)}\n\n"


@router.get("/threads", response_model=list[AssistantThreadOut])
def list_threads(
    scenario_id: str | None = None,
    page: str = "",
    path: str = "",
    db: Session = Depends(get_tenant_db),
):
    _scenario(db, scenario_id)
    if not scenario_id:
        permission_service.require_tenant_permission(db, "read")
    stmt = select(AssistantThread).where(
        AssistantThread.tenant_id == _tenant(db),
        AssistantThread.created_by_user_id == _current_user_id(db),
    )
    stmt = stmt.where(AssistantThread.scope_key == _context_scope(scenario_id, path))
    return list(db.execute(stmt.order_by(AssistantThread.updated_at.desc())).scalars().all())


@router.post("/threads", response_model=AssistantThreadOut)
def create_thread(
    scenario_id: str | None = None,
    page: str = "",
    path: str = "",
    db: Session = Depends(get_tenant_db),
):
    _scenario(db, scenario_id)
    if not scenario_id:
        permission_service.require_tenant_permission(db, "read")
    thread = AssistantThread(
        tenant_id=_tenant(db),
        created_by_user_id=_current_user_id(db),
        scenario_id=scenario_id,
        scope_key=_context_scope(scenario_id, path),
        title="新的助手任务",
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


@router.get("/threads/{thread_id}/messages", response_model=list[AssistantMessageOut])
def list_thread_messages(
    thread_id: str,
    scenario_id: str | None = None,
    page: str = "",
    path: str = "",
    db: Session = Depends(get_tenant_db),
):
    thread = _thread(db, thread_id)
    _assert_thread_scope(thread, scenario_id, page, path)
    messages = db.execute(
        select(AssistantMessage)
        .where(AssistantMessage.thread_id == thread_id)
        .order_by(AssistantMessage.created_at)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).scalars().all()
    upgraded_any = False
    for message in messages:
        if (
            message.role == "assistant"
            and not _has_invalid_historic_rag_source(db, thread, message)
        ):
            _proposal, upgraded = _upgrade_saved_scenario_model_plan(db, message)
            upgraded_any = upgraded_any or upgraded
    linked_job_ids = {
        str(context.get("compilation_job_id") or "")
        for context in (
            message.context
            for message in messages
            if message.role == "assistant"
        )
        if isinstance(context, dict) and context.get("compilation_job_id")
    }
    if linked_job_ids:
        terminal_jobs = db.execute(
            select(AssistantCompilationJob).where(
                AssistantCompilationJob.id.in_(linked_job_ids),
                AssistantCompilationJob.tenant_id == _tenant(db),
                AssistantCompilationJob.created_by_user_id
                == _current_user_id(db),
                AssistantCompilationJob.scenario_id == thread.scenario_id,
                AssistantCompilationJob.status.in_({"succeeded", "failed"}),
            )
        ).scalars().all()
        for job in terminal_jobs:
            upgraded_any = (
                _reconcile_terminal_compilation_subscriptions(db, job)
                or upgraded_any
            )
    if upgraded_any:
        db.commit()
    return [_assistant_message_out(db, thread, message) for message in messages]


@router.get(
    "/threads/{thread_id}/compilation-jobs",
    response_model=list[AssistantCompilationJobStatusOut],
)
def list_thread_compilation_jobs(
    thread_id: str,
    response: Response,
    scenario_id: str | None = None,
    page: str = "",
    path: str = "",
    db: Session = Depends(get_tenant_db),
):
    thread = _thread(db, thread_id)
    _assert_thread_scope(thread, scenario_id, page, path)
    response.headers["Cache-Control"] = "no-store"
    linked_job_ids = {
        str(context.get("compilation_job_id"))
        for context in db.execute(
            select(AssistantMessage.context).where(
                AssistantMessage.thread_id == thread.id,
                AssistantMessage.role == "assistant",
            )
        ).scalars().all()
        if isinstance(context, dict) and context.get("compilation_job_id")
    }
    stmt = select(AssistantCompilationJob).where(
        AssistantCompilationJob.tenant_id == _tenant(db),
        AssistantCompilationJob.created_by_user_id == _current_user_id(db),
        AssistantCompilationJob.scenario_id == thread.scenario_id,
    )
    if linked_job_ids:
        stmt = stmt.where(
            or_(
                AssistantCompilationJob.thread_id == thread.id,
                AssistantCompilationJob.id.in_(linked_job_ids),
            )
        )
    else:
        stmt = stmt.where(AssistantCompilationJob.thread_id == thread.id)
    jobs = db.execute(
        stmt.order_by(AssistantCompilationJob.created_at.desc())
    ).scalars().all()
    return [_compilation_status_out(job) for job in jobs]


@router.get(
    "/compilation-jobs/{job_id}",
    response_model=AssistantCompilationJobStatusOut,
)
def get_compilation_job(
    job_id: str,
    response: Response,
    db: Session = Depends(get_tenant_db),
):
    response.headers["Cache-Control"] = "no-store"
    return _compilation_status_out(_owned_compilation_job(db, job_id))


@router.get(
    "/compilation-jobs/{job_id}/result",
    response_model=AssistantCompilationJobResultOut,
)
def get_compilation_job_result(
    job_id: str,
    response: Response,
    db: Session = Depends(get_tenant_db),
):
    response.headers["Cache-Control"] = "no-store"
    job = _owned_compilation_job(db, job_id)
    if job.status != "succeeded" or not isinstance(job.result, dict) or not job.result:
        if job.status == "failed":
            progress = _public_compilation_progress(job)
            raise HTTPException(
                409,
                {
                    "error_code": progress.get("error_code") or "compilation_failed",
                    "message": progress.get("detail") or "编译任务未成功完成",
                },
            )
        raise HTTPException(409, "编译任务尚未完成")
    proposal_thread, proposal_message = _matching_compilation_proposal_message(
        db, job
    )
    if proposal_thread is None or proposal_message is None:
        raise HTTPException(409, "编译结果的权威草稿不存在或不一致，请重新生成")
    if _has_invalid_historic_rag_source(db, proposal_thread, proposal_message):
        raise HTTPException(409, "编译结果引用的资料已不在当前访问范围，请重新生成")

    locked_job = _scoped_compilation_job_for_message(
        db,
        proposal_message,
        job.id,
        lock=True,
    )
    if (
        locked_job is None
        or locked_job.status != "succeeded"
        or locked_job.thread_id != proposal_thread.id
        or locked_job.message_id != proposal_message.id
        or not isinstance(locked_job.result, dict)
        or locked_job.result.get("proposal_id")
        != (proposal_message.proposal or {}).get("proposal_id")
    ):
        raise HTTPException(409, "编译结果的权威草稿不存在或不一致，请重新生成")

    changed = False
    canonical_proposal = (
        copy.deepcopy(proposal_message.proposal)
        if isinstance(proposal_message.proposal, dict)
        else {}
    )
    if _proposal_can_advance(canonical_proposal, locked_job.result):
        canonical_proposal = copy.deepcopy(locked_job.result)
        proposal_message.proposal = canonical_proposal
        payload = (
            canonical_proposal.get("payload")
            if isinstance(canonical_proposal.get("payload"), dict)
            else {}
        )
        execution_summary = (
            payload.get("execution_summary")
            if isinstance(payload.get("execution_summary"), dict)
            else {}
        )
        context = (
            dict(proposal_message.context)
            if isinstance(proposal_message.context, dict)
            else {}
        )
        context.update({
            "status": _model_run_context_status(execution_summary),
            "model_run_id": canonical_proposal.get("proposal_id"),
            "run_revision": _safe_nonnegative_int(
                canonical_proposal.get("run_revision")
            ),
        })
        proposal_message.context = context
        changed = True

    upgraded, upgrade_changed = _upgrade_saved_scenario_model_plan(
        db,
        proposal_message,
    )
    changed = changed or upgrade_changed
    changed = (
        _sync_compilation_job_result(locked_job, upgraded, canonical=True)
        or changed
    )
    if changed:
        db.commit()
    return AssistantCompilationJobResultOut(
        job_id=locked_job.id,
        thread_id=locked_job.thread_id,
        scenario_id=locked_job.scenario_id,
        proposal=_public_recovery_proposal(upgraded),
        proposal_thread_id=proposal_thread.id,
        proposal_message_id=proposal_message.id,
        proposal_scope_key=proposal_thread.scope_key,
        apply_ready=True,
    )


@router.delete("/threads/{thread_id}", response_model=Msg)
def delete_thread(
    thread_id: str,
    scenario_id: str | None = None,
    page: str = "",
    path: str = "",
    db: Session = Depends(get_tenant_db),
):
    thread = _thread(db, thread_id)
    _assert_thread_scope(thread, scenario_id, page, path)
    db.execute(
        delete(AssistantRouteDecision).where(
            AssistantRouteDecision.tenant_id == _tenant(db),
            AssistantRouteDecision.created_by_user_id == _current_user_id(db),
            AssistantRouteDecision.thread_id == thread.id,
        )
    )
    db.delete(thread)
    db.commit()
    return Msg(message="助手会话已删除")


@router.post("/attachments", response_model=AssistantAttachmentOut)
async def upload_attachment(file: UploadFile = File(...), db: Session = Depends(get_tenant_db)):
    settings = get_settings()
    _purge_expired_attachments(db)
    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, f"附件超过 {settings.max_upload_bytes // 1024 // 1024}MB 限制")

    filename = Path(file.filename or "附件").name[:500]
    suffix = Path(filename).suffix or ".bin"
    attachment = AssistantAttachment(
        tenant_id=_tenant(db),
        created_by_user_id=_current_user_id(db),
        filename=filename,
        mime=file.content_type or "application/octet-stream",
        size=len(content),
        content_hash=hashlib.sha256(content).hexdigest(),
        status="pending",
    )
    db.add(attachment)
    db.flush()

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            temp_path = tmp.name
        parsed = doc_parser.parse_file(temp_path, filename)
        attachment.status = "parsed" if parsed.get("status") == "success" else "error"
        parsed_text = str(parsed.get("text") or "")
        if attachment.status == "parsed" and len(parsed_text) > ASSISTANT_ATTACHMENT_TEXT_MAX_CHARS:
            raise HTTPException(
                413,
                f"附件“{filename}”解析出 {len(parsed_text)} 个字符，超过单份临时附件"
                f" {ASSISTANT_ATTACHMENT_TEXT_MAX_CHARS} 个字符的明确边界；"
                "系统不会静默截断文档，请拆分文件后重新上传。",
            )
        attachment.parsed_text = parsed_text
        attachment.error = "" if attachment.status == "parsed" else str(parsed.get("message") or "解析失败")
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    db.commit()
    db.refresh(attachment)
    return attachment


@router.delete("/attachments/{attachment_id}", response_model=Msg)
def delete_attachment(attachment_id: str, db: Session = Depends(get_tenant_db)):
    attachment = db.execute(
        select(AssistantAttachment).where(
            AssistantAttachment.id == attachment_id,
            AssistantAttachment.tenant_id == _tenant(db),
            AssistantAttachment.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if not attachment:
        raise HTTPException(404, "附件不存在")
    db.delete(attachment)
    db.commit()
    return Msg(message="附件已移除")


@router.post("/chat/stream")
def stream_chat(payload: AssistantChatRequest, db: Session = Depends(get_tenant_db)):
    """全局助手 SSE：流式回答，同时发送可展开的安全处理摘要。"""
    scenario = _scenario(db, payload.scenario_id)
    _configure_assistant_runtime(db, payload)
    capability_context = _assistant_capability_context(db, payload)
    thread = _thread(db, payload.thread_id) if payload.thread_id else None
    scope_key = _context_scope(payload.scenario_id, payload.path)
    if thread:
        _assert_thread_scope(thread, payload.scenario_id, payload.page, payload.path)
    pending_thread_id = thread.id if thread is not None else uuid.uuid4().hex
    # Idempotency belongs to one explicit send, not to the user's wording.
    effective_request_id = str(payload.request_id or uuid.uuid4().hex)
    route_plan, pending_thread_id, attachments = _claimed_request_route_plan(
        db,
        scenario,
        thread,
        payload,
        scope_key=scope_key,
        request_id=effective_request_id,
        pending_thread_id=pending_thread_id,
    )
    if thread is None:
        thread = db.execute(
            select(AssistantThread).where(
                AssistantThread.id == pending_thread_id,
                AssistantThread.tenant_id == _tenant(db),
                AssistantThread.created_by_user_id == _current_user_id(db),
            )
        ).scalars().first()
        if thread is not None:
            _assert_thread_scope(thread, payload.scenario_id, payload.page, payload.path)
    intent = route_plan.intent
    if intent == "scenario_model" and scenario:
        permission_service.require_scenario_permission(
            db, scenario, "write", message="完整场景建模需要当前场景的编辑权限"
        )
    if not thread:
        thread = AssistantThread(
            id=pending_thread_id,
            tenant_id=_tenant(db),
            created_by_user_id=_current_user_id(db),
            scenario_id=payload.scenario_id,
            scope_key=scope_key,
            title=payload.message[:80] or "新的助手任务",
        )
        db.add(thread)
        db.flush()
    elif thread.title == "新的助手任务":
        thread.title = payload.message[:80] or thread.title
    if attachments:
        _consume_attachments(db, attachments, thread_id=thread.id)
    attachment_text, sources = _attachment_context(
        attachments,
        include_text=intent != "scenario_model",
        enforce_context_limit=intent != "scenario_model",
    )
    rag_context, rag_sources = _authorized_rag_context(db, scenario, payload.message)
    sources = [*sources, *rag_sources]
    context = {
        "request_id": effective_request_id,
        "page": payload.page,
        "path": payload.path,
        "scenario_id": payload.scenario_id,
        "selection": payload.selection,
        "mode": payload.mode,
        "draft_kind": payload.draft_kind,
        "llm_config_id": payload.llm_config_id,
        "skill_ids": payload.skill_ids,
        "mcp_ids": payload.mcp_ids,
        "routing": route_plan.public_context(),
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()
    thread_id = thread.id
    assistant_message_id = uuid.uuid4().hex
    if intent in {"execute_guidance", "scenario_model"}:
        _save_message(
            db,
            thread,
            "assistant",
            (
                "正在准备操作安全预演。"
                if intent == "execute_guidance"
                else "正在编译完整业务模型；结果写入前将再次核对场景基线。"
            ),
            {**context, "status": "processing"},
            message_id=assistant_message_id,
        )
        db.flush()
    tenant_id = _tenant(db)
    user_id = str(db.info.get("user_id") or "")
    db.commit()

    llm = _llm(db)
    history = _history_messages(db, thread, user_message.id)
    llm_messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "你是本体智能平台的全局 AI 助手。你必须区分事实、推测和待确认项；"
                "不直接修改数据，不绕过权限，不把 SQL 当作业务本体。回答简洁、可执行，"
                "必要时用问题卡片澄清。\n\n"
                + _mode_safety_context(payload.mode)
                + (
                    f"\n语义规划边界：{route_plan.policy_note}"
                    if route_plan.policy_note
                    else ""
                )
                + _read_only_chat_contract()
                + _scenario_context(db, scenario)
                + (f"\n\n当前页面：{payload.page}（{payload.path}）" if payload.page else "")
                + (f"\n当前选择：{payload.selection}" if payload.selection else "")
                + capability_context
                + (f"\n\n{attachment_text}" if attachment_text else "")
                + (f"\n\n{rag_context}" if rag_context else "")
            ),
        },
        *history,
        {"role": "user", "content": payload.message},
    ]

    compilation_job_id = ""

    def persist_result(
        reply: str,
        proposal: dict[str, Any],
        thinking: list[dict[str, Any]],
        status: str,
        evidence: dict[str, Any],
        action_preview: dict[str, Any],
        *,
        write_audit: bool = True,
    ) -> None:
        save_db = SessionLocal()
        try:
            saved_thread = save_db.execute(
                select(AssistantThread).where(
                    AssistantThread.id == thread_id,
                    AssistantThread.tenant_id == tenant_id,
                )
            ).scalars().first()
            if not saved_thread:
                return
            existing_message = save_db.get(
                AssistantMessage, assistant_message_id
            )
            existing_context = (
                existing_message.context
                if existing_message is not None
                and isinstance(existing_message.context, dict)
                else {}
            )
            # Canonical compilation messages and duplicate subscriptions are
            # owned by the fenced worker/job ledger.  A late SSE finalizer or
            # exception handler must never replace their terminal pointer,
            # status, content, or proposal with request-local state.
            if compilation_job_id or existing_context.get("compilation_job_id"):
                return
            assistant_context = {
                **context,
                "evidence": evidence,
                "action_preview": action_preview,
            }
            if status == "route_fallback":
                assistant_context["status"] = "route_fallback"
            elif proposal.get("kind") == "scenario_model":
                run_summary = (
                    (proposal.get("payload") or {}).get("execution_summary") or {}
                )
                assistant_context["status"] = _model_run_context_status(run_summary)
            _save_message(
                save_db,
                saved_thread,
                "assistant",
                reply,
                assistant_context,
                sources,
                proposal,
                thinking,
                message_id=assistant_message_id,
            )
            if write_audit:
                save_db.add(
                    AssistantAuditLog(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        scenario_id=payload.scenario_id,
                        thread_id=thread_id,
                        operation="propose" if proposal else "chat",
                        status=status,
                        context=assistant_context,
                        result={"intent": intent, "sources": sources, "proposal_kind": proposal.get("kind", ""), "evidence": evidence},
                    )
                )
            save_db.commit()
        finally:
            save_db.close()

    def event_stream():
        nonlocal compilation_job_id
        # FastAPI may finalize ``get_tenant_db`` before an SSE generator starts
        # iterating.  Reusing ORM objects captured from the request therefore
        # leaves unloaded relationships detached (for example
        # ``scenario.function_definitions`` during a compound compilation).
        # Give the whole streamed turn one explicitly-owned session and reload
        # its security-scoped resources instead of relying on request-session
        # lifetime details.
        db = SessionLocal()
        db.info["tenant_id"] = tenant_id
        db.info["user_id"] = user_id
        if payload.llm_config_id:
            db.info["assistant_llm_config_id"] = payload.llm_config_id
        scenario: BusinessScenario | None = None
        llm: LLMConfig | None = None
        cancelled = False
        reply = ""
        proposal: dict[str, Any] = {}
        thinking: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {}
        action_preview: dict[str, Any] = {}
        suggestions: list[str] = []
        saved_status = "success"
        route_notice = _route_fallback_notice(route_plan)
        if route_notice:
            saved_status = "route_fallback"
        compilation_queued = False
        owns_compilation_job = False
        try:
            scenario = _scenario(
                db,
                payload.scenario_id,
                writable=intent == "scenario_model",
            )
            llm = _llm(db)
            suggestions = (
                ["创建业务场景草稿", "说明建模所需资料"]
                if not scenario
                else ["解释当前场景", "编译完整业务模型", "生成本体草稿", "生成数据映射草稿", "根据当前本体设计工作流"]
            )
            def progress(step: dict[str, Any]):
                existing = next((item for item in thinking if item["id"] == step["id"]), None)
                if existing:
                    existing.update(step)
                else:
                    thinking.append(step)
                return _sse("progress", step)

            yield progress({"id": "context", "title": "理解当前上下文", "detail": "正在读取当前页面、业务场景和选中对象。", "status": "running"})
            yield progress({"id": "context", "title": "理解当前上下文", "detail": "当前上下文已准备完成。", "status": "done"})

            if route_notice:
                reply = route_notice
                yield progress({
                    "id": "routing",
                    "title": "语义规划未完成",
                    "detail": "已停止本轮处理，未进入问答、建模或应用链路。",
                    "status": "error",
                })
                yield _sse("token", reply)
            elif intent in {"apply_guidance", "capability_update_guidance", "change_guidance"}:
                reply = _fallback_reply(intent, scenario)
                yield progress({
                    "id": "governance",
                    "title": "确认安全边界",
                    "detail": (
                        "已转为已有业务能力的只读修改指导，未生成可应用提案。"
                        if intent in {"capability_update_guidance", "change_guidance"}
                        else "已提供变更确认或执行预演的受控入口说明。"
                    ),
                    "status": "done",
                })
                yield _sse("token", reply)
            elif intent == "execute_guidance":
                yield progress({"id": "action-preview", "title": "分析操作", "detail": "正在核对目标、参数、影响和权限。", "status": "running"})
                action_preview, question, reply = _assistant_action_preview(
                    db,
                    scenario,
                    payload.message,
                    payload.selection,
                    assistant_message_id=assistant_message_id,
                )
                if question:
                    questions.append(question)
                done_event = progress({"id": "action-preview", "title": "分析操作", "detail": "操作分析完成；聊天未触发任何外部副作用。", "status": "done"})
                # Persist the preview card before exposing it over SSE.  A client
                # may navigate away immediately after receiving the event; the
                # durable parent message and Action log must already agree.
                preview_evidence = _assistant_evidence(
                    intent,
                    proposal=proposal,
                    sources=sources,
                    llm_used=False,
                    preview=action_preview,
                )
                persist_result(
                    reply,
                    proposal,
                    thinking,
                    "processing",
                    preview_evidence,
                    action_preview,
                    write_audit=False,
                )
                yield done_event
                yield _sse("action_preview", action_preview)
                yield _sse("token", reply)
            elif intent in ("ontology", "mapping", "workflow", "scenario_model") and not scenario:
                questions.append({
                    "id": "scenario",
                    "title": "需要一个业务场景",
                    "message": "请先打开或创建业务场景，我才能把草稿安全地放入对应的本体工作区。",
                    "options": [
                        {"label": "打开已有场景", "value": "open_scenario", "impact": "继续在已有场景中生成草稿，不创建新业务域。", "recommended": True},
                        {"label": "创建场景草稿", "value": "draft_scenario", "impact": "先回到全局工作台生成并确认一个新的草稿场景。"},
                    ],
                })
                reply = "我可以继续协助，但需要先知道这次建模属于哪个业务场景。"
                yield progress({"id": "clarify", "title": "确认业务范围", "detail": "当前请求需要绑定到一个具体业务场景。", "status": "done"})
                yield _sse("token", reply)
            elif intent == "scenario" and scenario:
                questions.append({
                    "id": "global-scenario",
                    "title": "请在全局工作台创建场景",
                    "message": "当前会话已绑定现有场景。请回到业务场景列表或工作台发起新场景草稿，避免上下文串写。",
                    "options": [
                        {"label": "继续当前场景", "value": "keep_current", "impact": "保留当前上下文，只解释或生成当前场景内的草稿。", "recommended": True},
                        {"label": "回到全局工作台", "value": "go_global", "impact": "开始新的全局会话，再创建独立场景草稿。"},
                    ],
                })
                reply = "当前助手会话已绑定现有业务场景，不能在这里创建另一个场景。"
                yield progress({"id": "clarify", "title": "确认业务范围", "detail": "新场景草稿必须从全局上下文创建。", "status": "done"})
                yield _sse("token", reply)
            elif intent == "scenario":
                yield progress({"id": "scenario", "title": "生成业务场景草稿", "detail": "正在整理业务目标、角色和边界。", "status": "running"})
                description = payload.message + (
                    f"\n\n参考附件内容：\n{attachment_text}" if attachment_text else ""
                )
                data = _generate_scenario_draft(db, description)
                proposal = _build_proposal("scenario", data)
                reply = "我已生成业务场景草稿。确认前不会创建场景，附件也不会进入正式数据源。"
                done_event = progress({"id": "scenario", "title": "生成业务场景草稿", "detail": "场景名称、目标与边界已整理完成。", "status": "done"})
                persist_result(
                    reply,
                    proposal,
                    thinking,
                    "processing",
                    _assistant_evidence(
                        intent,
                        proposal=proposal,
                        sources=sources,
                        llm_used=bool(llm),
                    ),
                    action_preview,
                    write_audit=False,
                )
                yield done_event
                yield _sse("proposal", proposal)
                yield _sse("token", reply)
            elif intent == "scenario_model" and scenario:
                baseline = _scenario_revision(scenario)
                compiler_message = (
                    assistant_compilation_job_service.normalize_message(
                        payload.message
                    )
                )
                compiler_documents = (
                    assistant_compilation_job_service.canonical_compiler_documents(
                        attachments
                    )
                )
                prepared_context = (
                    scenario_model_compiler.prepare_compilation_context(
                        db, scenario
                    )
                )
                (
                    source_bundle_preview,
                    source_recovery_issue,
                ) = _source_bundle_preview_with_recovery(
                    compiler_message=compiler_message,
                    compiler_documents=compiler_documents,
                    prepared_context=prepared_context,
                )
                plan = assistant_compilation_job_service.compilation_plan(
                    document_count=len(source_bundle_preview["documents"]),
                    source_count=len(source_bundle_preview["paragraphs"]),
                    total_characters=int(source_bundle_preview["total_characters"]),
                )
                for item in plan:
                    if item["id"] == "analyze":
                        item["status"] = "done"
                        item["detail"] = f"已读取 {len(source_bundle_preview['paragraphs'])} 个来源段落。"
                    elif item["id"] == "plan":
                        item["status"] = "done"
                        item["detail"] = "已拆解为资料分析、计划和 6 个连续建模任务。"
                yield progress({
                    "id": "analyze",
                    "title": "分析业务资料",
                    "detail": f"已读取 {len(source_bundle_preview['paragraphs'])} 个来源段落，正在建立可追溯来源。",
                    "status": "done",
                })
                yield progress({
                    "id": "plan",
                    "title": "制定建模任务",
                    "detail": "已拆解为本体、实例、映射、业务能力、规则事件和工作流任务。",
                    "status": "done",
                })
                yield progress({
                    "id": "ontology",
                    "title": "建立本体与业务能力",
                    "detail": "任务已排队，下一步将逐段识别对象、关系、函数和操作。",
                    "status": "running",
                })
                compilation_settings = get_settings()
                execution_policy = {
                    "llm_call_budget": compilation_settings.scenario_model_max_llm_calls,
                    "request_timeout": compilation_settings.scenario_model_llm_timeout,
                    "assistant_scope_key": scope_key,
                }
                identity = assistant_compilation_job_service.build_compilation_identity(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    scenario_id=scenario.id,
                    message=compiler_message,
                    attachments=attachments,
                    llm=llm,
                    compiler_version=scenario_model_compiler.COMPILER_VERSION,
                    scenario_baseline=baseline,
                    request_id=context["request_id"],
                    mapping_context_fingerprint=prepared_context["fingerprint"],
                    execution_policy=execution_policy,
                )
                execution_input = _compilation_execution_input(
                    compiler_message=compiler_message,
                    compiler_documents=compiler_documents,
                    prepared_context=prepared_context,
                    llm_config_id=str(getattr(llm, "id", "") or ""),
                    context=context,
                    sources=sources,
                    execution_policy=execution_policy,
                    recovery_issue=source_recovery_issue,
                )
                job, owns_compilation_job = (
                    assistant_compilation_job_service.claim_compilation(
                        db,
                        identity=identity,
                        tenant_id=tenant_id,
                        user_id=user_id,
                        scenario_id=scenario.id,
                        thread_id=thread_id,
                        message_id=assistant_message_id,
                        compiler_version=scenario_model_compiler.COMPILER_VERSION,
                        scenario_baseline=baseline,
                        llm_call_budget=compilation_settings.scenario_model_max_llm_calls,
                        plan=plan,
                        execution_input=execution_input,
                    )
                )
                compilation_job_id = job.id
                context["compilation_job_id"] = job.id
                _link_compilation_placeholder(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    thread_id=thread_id,
                    assistant_message_id=assistant_message_id,
                    job_id=job.id,
                    context=context,
                )
                yield _sse("compilation_job", {
                    "job_id": job.id,
                    "thread_id": thread_id,
                    "scenario_id": scenario.id,
                    "status": job.status,
                    "progress": _public_compilation_progress(job),
                    "llm_calls_used": job.llm_calls_used,
                    "llm_call_budget": job.llm_call_budget,
                    "replayed": not owns_compilation_job,
                })
                if not owns_compilation_job and job.status == "succeeded":
                    proposal = dict(job.result or {})
                    if proposal.get("kind") != "scenario_model":
                        raise RuntimeError("已完成编译任务缺少可重放的复合变更清单")
                    data = proposal.get("payload") or {}
                    run_summary = data.get("execution_summary") or {}
                    reply = "\n\n".join(value for value in (
                        "已恢复同一次发送此前完成的完整业务模型；没有重复调用模型。",
                        str(run_summary.get("message") or "").strip(),
                    ) if value)
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "已重放同一执行指纹的复合变更清单。", "status": "done"})
                    yield _sse("proposal", _public_recovery_proposal(proposal))
                    yield _sse("token", reply)
                elif not owns_compilation_job and job.status == "failed":
                    saved_status = "error"
                    public_failure = _public_compilation_progress(job)
                    reply = (
                        "同一次发送此前已编译失败，系统没有重复调用模型。"
                        f"{public_failure.get('detail') or '系统已保持零写入，请显式重试。'}"
                    )
                    questions.append({
                        "id": "failed-compilation",
                        "title": "编译任务已失败",
                        "message": "相同执行指纹不会自动重跑；请先改变导致失败的输入。",
                        "options": [
                            {"label": "修改输入后重试", "value": "revise_and_retry", "impact": "形成新的执行指纹和独立调用预算。", "recommended": True},
                            {"label": "保留失败记录", "value": "keep_failed", "impact": "不调用模型、不生成或应用任何变更。"},
                        ],
                    })
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "相同执行指纹已失败，未自动重跑。", "status": "error"})
                    yield _sse("token", reply)
                elif not owns_compilation_job:
                    saved_status = "processing"
                    reply = (
                        "同一次发送的完整业务模型正在由已有任务编译；"
                        "系统没有启动第二套模型调用，将继续恢复这一个任务。"
                    )
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "已连接到持久任务状态；已有任务仍在运行。", "status": "running"})
                    yield _sse("token", reply)
                else:
                    job_id = job.id
                    # The request now only claims and queues the durable job.
                    # The browser immediately receives a recoverable task id;
                    # the worker later persists the exact proposal and the
                    # existing poller loads it from the result endpoint.
                    saved_status = "processing"
                    reply = "我已先完成资料分析并列出建模计划，正在按任务顺序执行；每完成一项都会回传阶段结果，最终再生成待审核变更清单。"
                    # Persist the placeholder before the worker can finish;
                    # otherwise a fast test/mock provider could race the
                    # common stream finalizer and overwrite the proposal.
                    persist_result(
                        reply,
                        proposal,
                        thinking,
                        saved_status,
                        {},
                        {},
                        write_audit=False,
                    )
                    compilation_queued = True
                    _submit_compilation_job(
                        job_id=job_id,
                    )
                    owns_compilation_job = False
                    yield progress({"id": "ontology", "title": "建立本体与业务能力", "detail": "任务已进入后台，正在逐项执行并持续回传阶段结果。", "status": "running"})
                    yield _sse("token", reply)
            elif intent == "ontology" and scenario:
                yield progress({"id": "ontology", "title": "生成本体草稿", "detail": "正在整理实体、属性和关系建议。", "status": "running"})
                description = (
                    payload.message
                    + (f"\n\n参考附件内容：\n{attachment_text}" if attachment_text else "")
                    + (f"\n\n已授权资料依据：\n{rag_context}" if rag_context else "")
                )
                data = ontology_service.generate_ontology(db, scenario, description)
                proposal = _build_proposal("ontology", data, scenario)
                reply = "我已经根据当前场景和附件生成了本体草稿。请检查变更内容，确认后再应用到场景。"
                done_event = progress({"id": "ontology", "title": "生成本体草稿", "detail": "实体和关系建议已整理完成。", "status": "done"})
                persist_result(
                    reply,
                    proposal,
                    thinking,
                    "processing",
                    _assistant_evidence(
                        intent,
                        proposal=proposal,
                        sources=sources,
                        llm_used=bool(llm),
                    ),
                    action_preview,
                    write_audit=False,
                )
                yield done_event
                yield _sse("proposal", proposal)
                yield _sse("token", reply)
            elif intent == "mapping" and scenario:
                yield progress({"id": "mapping", "title": "生成数据映射草稿", "detail": "正在核对实体、数据源、表和字段。", "status": "running"})
                description = payload.message + (
                    f"\n\n参考附件内容：\n{attachment_text}" if attachment_text else ""
                )
                data = _generate_mapping_draft(db, scenario, description, payload.selection)
                proposal = _build_proposal("mapping", data, scenario)
                reply = "我已生成并校验数据映射草稿。确认后才会保存映射，刷新数据仍需单独提交。"
                done_event = progress({"id": "mapping", "title": "生成数据映射草稿", "detail": "字段引用和主键覆盖已校验。", "status": "done"})
                persist_result(
                    reply,
                    proposal,
                    thinking,
                    "processing",
                    _assistant_evidence(
                        intent,
                        proposal=proposal,
                        sources=sources,
                        llm_used=bool(llm),
                    ),
                    action_preview,
                    write_audit=False,
                )
                yield done_event
                yield _sse("proposal", proposal)
                yield _sse("token", reply)
            elif intent == "workflow" and scenario:
                yield progress({"id": "workflow", "title": "编排工作流草稿", "detail": "正在识别触发条件、节点和分支关系。", "status": "running"})
                description = (
                    payload.message
                    + (f"\n\n参考附件内容：\n{attachment_text}" if attachment_text else "")
                    + (f"\n\n已授权资料依据：\n{rag_context}" if rag_context else "")
                )
                data = workflow_service.generate_workflow(db, scenario, description)
                proposal = _build_proposal("workflow", data, scenario)
                reply = "我已经生成了工作流草稿。请先检查节点、分支和动作引用，确认后再保存。"
                done_event = progress({"id": "workflow", "title": "编排工作流草稿", "detail": "节点和连线建议已整理完成。", "status": "done"})
                persist_result(
                    reply,
                    proposal,
                    thinking,
                    "processing",
                    _assistant_evidence(
                        intent,
                        proposal=proposal,
                        sources=sources,
                        llm_used=bool(llm),
                    ),
                    action_preview,
                    write_audit=False,
                )
                yield done_event
                yield _sse("proposal", proposal)
                yield _sse("token", reply)
            elif llm:
                yield progress({"id": "response", "title": "生成回答", "detail": "正在根据当前上下文组织答案。", "status": "running"})
                for event in llm_service.chat_stream(llm, llm_messages, db=db):
                    if event["type"] == "token":
                        reply += event["content"]
                        yield _sse("token", event["content"])
                if not reply.strip():
                    reply = _fallback_reply(intent, scenario)
                    yield _sse("token", reply)
                yield progress({"id": "response", "title": "生成回答", "detail": "回答已生成。", "status": "done"})
            else:
                yield progress({"id": "response", "title": "生成回答", "detail": "当前未配置默认 LLM，已切换到平台引导回复。", "status": "done"})
                reply = _fallback_reply(intent, scenario)
                yield _sse("token", reply)

            evidence = _assistant_evidence(
                intent,
                proposal=proposal,
                sources=sources,
                llm_used=bool(
                    llm
                    and not route_notice
                    and not compilation_queued
                    and intent not in (
                        "apply_guidance",
                        "execute_guidance",
                        "capability_update_guidance",
                        "change_guidance",
                    )
                ),
                preview=action_preview,
                uncertainties=[route_notice] if route_notice else [],
            )
            if not compilation_queued:
                persist_result(reply, proposal, thinking, saved_status, evidence, action_preview)
            yield _sse("meta", {
                "thread_id": thread_id,
                "proposal": _public_recovery_proposal(proposal),
                "questions": questions,
                "suggestions": suggestions,
                "sources": sources,
                "thinking": thinking,
                "evidence": evidence,
                "action_preview": action_preview,
            })
            yield _sse("done", {"thread_id": thread_id})
            yield "data: [DONE]\n\n"
        except GeneratorExit:
            cancelled = True
            if owns_compilation_job and compilation_job_id:
                try:
                    db.rollback()
                    _submit_compilation_job(job_id=compilation_job_id)
                    owns_compilation_job = False
                except Exception:
                    db.rollback()
                    # The durable row remains running and the periodic recovery
                    # worker will reclaim it after any partial submit failure.
            raise
        except Exception as exc:  # noqa: BLE001
            saved_status = "error"
            compilation_will_resume = False
            if owns_compilation_job and compilation_job_id:
                try:
                    db.rollback()
                    _submit_compilation_job(job_id=compilation_job_id)
                    owns_compilation_job = False
                except Exception:
                    db.rollback()
                compilation_will_resume = True
            if compilation_will_resume:
                saved_status = "processing"
                reply = (
                    "当前连接处理发生中断，但持久建模任务没有结束；"
                    "后台将继续同一份冻结输入并保留阶段草稿。"
                )
                yield _sse("progress", {
                    "id": "recovery",
                    "title": "继续持久建模任务",
                    "detail": "连接异常只影响本次响应，任务本体仍在后台继续。",
                    "status": "running",
                })
                yield _sse("token", reply)
                yield _sse("meta", {
                    "thread_id": thread_id,
                    "proposal": {},
                    "questions": [],
                    "suggestions": suggestions,
                    "sources": sources,
                    "thinking": thinking,
                    "evidence": {},
                    "action_preview": {},
                })
                yield _sse("done", {"thread_id": thread_id})
                yield "data: [DONE]\n\n"
                return
            if intent == "scenario_model":
                public_error = (
                    assistant_compilation_job_service.public_compilation_error(
                        exc
                    )
                )
            elif isinstance(exc, workflow_service.WorkflowGenerationError):
                public_error = (
                    assistant_compilation_job_service.PublicCompilationError(
                        "workflow_generation_invalid",
                        str(exc),
                    )
                )
            else:
                public_error = (
                    assistant_compilation_job_service.PublicCompilationError(
                        "assistant_request_failed",
                        "这次助手请求未完成，系统未执行任何变更；服务端已保留诊断记录，请稍后重试。",
                    )
                )
            error_message = public_error.message
            questions.append({
                "id": "retry",
                "title": "任务未完成",
                "message": public_error.message,
                "options": [
                    {"label": "显式重试", "value": "retry", "impact": "保留当前会话；完整业务模型仍保持零写入。", "recommended": True},
                    {"label": "仅保留说明", "value": "keep_read_only", "impact": "不生成或应用任何变更，只保留当前错误记录。"},
                ],
            })
            if not reply:
                reply = error_message
            yield _sse("progress", {"id": "error", "title": "处理未完成", "detail": public_error.message, "status": "error", "error_code": public_error.code})
            yield _sse("error", public_error.message)
            try:
                evidence = _assistant_evidence(
                    intent,
                    proposal=proposal,
                    sources=sources,
                    llm_used=bool(llm),
                    preview=action_preview,
                    uncertainties=[public_error.message],
                )
                persist_result(reply, proposal, thinking, saved_status, evidence, action_preview)
                yield _sse("meta", {
                    "thread_id": thread_id,
                    "proposal": _public_recovery_proposal(proposal),
                    "questions": questions,
                    "suggestions": suggestions,
                    "sources": sources,
                    "thinking": thinking,
                    "evidence": evidence,
                    "action_preview": action_preview,
                })
            except Exception:
                pass
            yield "data: [DONE]\n\n"
        finally:
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/chat", response_model=AssistantReplyOut)
def chat(payload: AssistantChatRequest, db: Session = Depends(get_tenant_db)):
    scenario = _scenario(db, payload.scenario_id)
    _configure_assistant_runtime(db, payload)
    capability_context = _assistant_capability_context(db, payload)
    thread = _thread(db, payload.thread_id) if payload.thread_id else None
    scope_key = _context_scope(payload.scenario_id, payload.path)
    if thread:
        _assert_thread_scope(thread, payload.scenario_id, payload.page, payload.path)
    pending_thread_id = thread.id if thread is not None else uuid.uuid4().hex
    effective_request_id = str(payload.request_id or uuid.uuid4().hex)
    route_plan, pending_thread_id, attachments = _claimed_request_route_plan(
        db,
        scenario,
        thread,
        payload,
        scope_key=scope_key,
        request_id=effective_request_id,
        pending_thread_id=pending_thread_id,
    )
    if thread is None:
        thread = db.execute(
            select(AssistantThread).where(
                AssistantThread.id == pending_thread_id,
                AssistantThread.tenant_id == _tenant(db),
                AssistantThread.created_by_user_id == _current_user_id(db),
            )
        ).scalars().first()
        if thread is not None:
            _assert_thread_scope(thread, payload.scenario_id, payload.page, payload.path)
    intent = route_plan.intent
    if intent == "scenario_model" and scenario:
        permission_service.require_scenario_permission(
            db, scenario, "write", message="完整场景建模需要当前场景的编辑权限"
        )
    if not thread:
        thread = AssistantThread(
            id=pending_thread_id,
            tenant_id=_tenant(db),
            created_by_user_id=_current_user_id(db),
            scenario_id=payload.scenario_id,
            scope_key=scope_key,
            title=payload.message[:80] or "新的助手任务",
        )
        db.add(thread)
        db.flush()
    elif thread.title == "新的助手任务":
        thread.title = payload.message[:80] or thread.title
    if attachments:
        _consume_attachments(db, attachments, thread_id=thread.id)
    attachment_text, sources = _attachment_context(
        attachments,
        include_text=intent != "scenario_model",
        enforce_context_limit=intent != "scenario_model",
    )
    rag_context, rag_sources = _authorized_rag_context(db, scenario, payload.message)
    sources = [*sources, *rag_sources]
    context = {
        "request_id": effective_request_id,
        "page": payload.page,
        "path": payload.path,
        "scenario_id": payload.scenario_id,
        "selection": payload.selection,
        "mode": payload.mode,
        "draft_kind": payload.draft_kind,
        "llm_config_id": payload.llm_config_id,
        "skill_ids": payload.skill_ids,
        "mcp_ids": payload.mcp_ids,
        "routing": route_plan.public_context(),
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()

    reply = ""
    proposal: dict[str, Any] = {}
    questions: list[dict[str, Any]] = []
    action_preview: dict[str, Any] = {}
    error_uncertainties: list[str] = []
    route_notice = _route_fallback_notice(route_plan)
    if route_notice:
        error_uncertainties.append(route_notice)
    assistant_message_id = uuid.uuid4().hex
    tenant_id = _tenant(db)
    user_id = _current_user_id(db)
    if intent in {"execute_guidance", "scenario_model"}:
        _save_message(
            db,
            thread,
            "assistant",
            (
                "正在准备操作安全预演。"
                if intent == "execute_guidance"
                else "正在编译完整业务模型；结果写入前将再次核对场景基线。"
            ),
            {**context, "status": "processing"},
            message_id=assistant_message_id,
        )
        db.flush()
    # Match the streamed transport: persist the user's accepted send before
    # any downstream generation model runs, so SQLite does not hold a write
    # transaction while waiting on the provider.
    db.commit()
    suggestions = (
        ["创建业务场景草稿", "说明建模所需资料"]
        if not scenario
        else ["解释当前场景", "编译完整业务模型", "生成本体草稿", "生成数据映射草稿", "根据当前本体设计工作流"]
    )

    try:
        if route_notice:
            reply = route_notice
        elif intent in {"apply_guidance", "capability_update_guidance", "change_guidance"}:
            reply = _fallback_reply(intent, scenario)
        elif intent == "execute_guidance":
            action_preview, question, reply = _assistant_action_preview(
                db,
                scenario,
                payload.message,
                payload.selection,
                assistant_message_id=assistant_message_id,
            )
            if question:
                questions.append(question)
        elif intent in ("ontology", "mapping", "workflow", "scenario_model") and not scenario:
            questions.append({
                "id": "scenario",
                "title": "需要一个业务场景",
                "message": "请先打开或创建业务场景，我才能把草稿安全地放入对应的本体工作区。",
                "options": [
                    {"label": "打开已有场景", "value": "open_scenario", "impact": "继续在已有场景中生成草稿，不创建新业务域。", "recommended": True},
                    {"label": "创建场景草稿", "value": "draft_scenario", "impact": "先生成并确认一个新的草稿场景。"},
                ],
            })
            reply = "我可以继续协助，但需要先知道这次建模属于哪个业务场景。"
        elif intent == "scenario" and scenario:
            questions.append({
                "id": "global-scenario",
                "title": "请在全局工作台创建场景",
                "message": "当前会话已绑定现有场景。请回到业务场景列表或工作台发起新场景草稿，避免上下文串写。",
                "options": [
                    {"label": "继续当前场景", "value": "keep_current", "impact": "保留当前上下文，只处理当前场景。", "recommended": True},
                    {"label": "回到全局工作台", "value": "go_global", "impact": "启动独立全局会话创建新场景。"},
                ],
            })
            reply = "当前助手会话已绑定现有业务场景，不能在这里创建另一个场景。"
        elif intent == "scenario":
            description = payload.message
            if attachment_text:
                description += f"\n\n参考附件内容：\n{attachment_text}"
            data = _generate_scenario_draft(db, description)
            proposal = _build_proposal("scenario", data)
            reply = "我已生成业务场景草稿。确认前不会创建场景，附件也不会进入正式数据源。"
        elif intent == "scenario_model" and scenario:
            # Persist the conversation parent before the unique job insert.
            # A duplicate fingerprint intentionally rolls back its failed
            # insert; it must not roll back this request's thread/message too.
            db.commit()
            llm = _llm(db)
            baseline = _scenario_revision(scenario)
            compiler_message = (
                assistant_compilation_job_service.normalize_message(
                    payload.message
                )
            )
            compiler_documents = (
                assistant_compilation_job_service.canonical_compiler_documents(
                    attachments
                )
            )
            prepared_context = (
                scenario_model_compiler.prepare_compilation_context(db, scenario)
            )
            (
                source_bundle_preview,
                source_recovery_issue,
            ) = _source_bundle_preview_with_recovery(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
            )
            plan = assistant_compilation_job_service.compilation_plan(
                document_count=len(source_bundle_preview["documents"]),
                source_count=len(source_bundle_preview["paragraphs"]),
                total_characters=int(source_bundle_preview["total_characters"]),
            )
            for item in plan:
                if item["id"] in {"analyze", "plan"}:
                    item["status"] = "done"
            compilation_settings = get_settings()
            execution_policy = {
                "llm_call_budget": compilation_settings.scenario_model_max_llm_calls,
                "request_timeout": compilation_settings.scenario_model_llm_timeout,
                "assistant_scope_key": thread.scope_key,
            }
            identity = assistant_compilation_job_service.build_compilation_identity(
                tenant_id=_tenant(db),
                user_id=_current_user_id(db),
                scenario_id=scenario.id,
                message=compiler_message,
                attachments=attachments,
                llm=llm,
                compiler_version=scenario_model_compiler.COMPILER_VERSION,
                scenario_baseline=baseline,
                request_id=context["request_id"],
                mapping_context_fingerprint=prepared_context["fingerprint"],
                execution_policy=execution_policy,
            )
            execution_input = _compilation_execution_input(
                compiler_message=compiler_message,
                compiler_documents=compiler_documents,
                prepared_context=prepared_context,
                llm_config_id=str(getattr(llm, "id", "") or ""),
                context=context,
                sources=sources,
                execution_policy=execution_policy,
                recovery_issue=source_recovery_issue,
            )
            job, acquired = assistant_compilation_job_service.claim_compilation(
                db,
                identity=identity,
                tenant_id=_tenant(db),
                user_id=_current_user_id(db),
                scenario_id=scenario.id,
                thread_id=thread.id,
                message_id=assistant_message_id,
                compiler_version=scenario_model_compiler.COMPILER_VERSION,
                scenario_baseline=baseline,
                llm_call_budget=compilation_settings.scenario_model_max_llm_calls,
                plan=plan,
                execution_input=execution_input,
            )
            context["compilation_job_id"] = job.id
            _link_compilation_placeholder(
                tenant_id=tenant_id,
                user_id=user_id,
                thread_id=thread.id,
                assistant_message_id=assistant_message_id,
                job_id=job.id,
                context=context,
            )
            if not acquired and job.status == "succeeded":
                proposal = dict(job.result or {})
                if proposal.get("kind") != "scenario_model":
                    raise RuntimeError("已完成编译任务缺少可重放的复合变更清单")
                data = proposal.get("payload") or {}
                run_summary = data.get("execution_summary") or {}
                reply = "\n\n".join(value for value in (
                    "已恢复同一次发送此前完成的完整业务模型；没有重复调用模型。",
                    str(run_summary.get("message") or "").strip(),
                ) if value)
            elif not acquired and job.status == "failed":
                public_failure = _public_compilation_progress(job)
                reply = (
                    "同一次发送此前已编译失败，系统没有重复调用模型。"
                    f"{public_failure.get('detail') or '系统已保持零写入，请显式重试。'}"
                )
                questions.append({
                    "id": "failed-compilation",
                    "title": "编译任务已失败",
                    "message": "相同执行指纹不会自动重跑；请先改变导致失败的输入。",
                    "options": [
                        {"label": "修改输入后重试", "value": "revise_and_retry", "impact": "形成新的执行指纹和独立调用预算。", "recommended": True},
                        {"label": "保留失败记录", "value": "keep_failed", "impact": "不调用模型、不生成或应用任何变更。"},
                    ],
                })
            elif not acquired:
                reply = (
                    "同一次发送的完整业务模型正在由已有任务编译；"
                    "系统没有启动第二套模型调用，将继续恢复这一个任务。"
                )
            else:
                job_id = job.id
                sync_slot = _COMPILATION_SUBMISSION_SLOTS.acquire(blocking=False)
                if not sync_slot:
                    reply = (
                        "当前执行槽位已满，持久任务已经排队；后台会继续同一份冻结输入，"
                        "不会要求用户重新开始。"
                    )
                else:
                    try:
                        lease = assistant_compilation_job_service.acquire_compilation_lease(
                            db,
                            job_id,
                            tenant_id=tenant_id,
                            created_by_user_id=user_id,
                        )
                    except Exception:
                        _COMPILATION_SUBMISSION_SLOTS.release()
                        raise
                    if lease is None:
                        _COMPILATION_SUBMISSION_SLOTS.release()
                        reply = (
                            "任务已被后台恢复执行；当前请求不会启动第二套模型调用，"
                            "请继续在同一任务中查看阶段结果。"
                        )
                    else:
                        _run_compilation_job_in_background(
                            job_id=job_id,
                            lease_token=lease.token,
                            lease_attempt=lease.attempt,
                            _slot_reserved=True,
                        )
                    db.expire_all()
                    completed_job = db.get(AssistantCompilationJob, job_id)
                    if completed_job and completed_job.status == "succeeded":
                        proposal = copy.deepcopy(completed_job.result or {})
                        canonical_message = (
                            db.get(AssistantMessage, completed_job.message_id)
                            if completed_job.message_id
                            else None
                        )
                        reply = str(
                            getattr(canonical_message, "content", "")
                            or "完整业务模型草稿已经建立。"
                        )
                    elif completed_job and completed_job.status == "failed":
                        public_failure = _public_compilation_progress(completed_job)
                        reply = str(
                            public_failure.get("detail")
                            or "任务未能完成持久化；已保留任务记录。"
                        )
                    else:
                        reply = (
                            "任务执行权已被恢复 worker 接管；系统将继续同一个任务，"
                            "不会从头重复建模。"
                        )
        elif intent == "ontology" and scenario:
            description = payload.message
            if attachment_text:
                description += f"\n\n参考附件内容：\n{attachment_text}"
            if rag_context:
                description += f"\n\n已授权资料依据：\n{rag_context}"
            data = ontology_service.generate_ontology(db, scenario, description)
            proposal = _build_proposal("ontology", data, scenario)
            reply = "我已经根据当前场景和附件生成了本体草稿。请检查变更内容，确认后再应用到场景。"
        elif intent == "mapping" and scenario:
            description = payload.message
            if attachment_text:
                description += f"\n\n参考附件内容：\n{attachment_text}"
            data = _generate_mapping_draft(db, scenario, description, payload.selection)
            proposal = _build_proposal("mapping", data, scenario)
            reply = "我已生成并校验数据映射草稿。确认后才会保存映射，刷新数据仍需单独提交。"
        elif intent == "workflow" and scenario:
            description = payload.message
            if attachment_text:
                description += f"\n\n参考附件内容：\n{attachment_text}"
            if rag_context:
                description += f"\n\n已授权资料依据：\n{rag_context}"
            data = workflow_service.generate_workflow(db, scenario, description)
            proposal = _build_proposal("workflow", data, scenario)
            reply = "我已经生成了工作流草稿。请先检查节点、分支和动作引用，确认后再保存。"
        else:
            llm = _llm(db)
            if llm:
                messages: list[dict[str, str]] = [
                    {
                        "role": "system",
                        "content": (
                            "你是本体智能平台的全局 AI 助手。你必须区分事实、推测和待确认项；"
                            "不直接修改数据，不绕过权限，不把 SQL 当作业务本体。回答简洁、可执行，"
                            "必要时用问题卡片澄清。\n\n"
                            + _mode_safety_context(payload.mode)
                            + (
                                f"\n语义规划边界：{route_plan.policy_note}"
                                if route_plan.policy_note
                                else ""
                            )
                            + _read_only_chat_contract()
                            + _scenario_context(db, scenario)
                            + (f"\n\n当前页面：{payload.page}（{payload.path}）" if payload.page else "")
                            + (f"\n当前选择：{payload.selection}" if payload.selection else "")
                            + capability_context
                            + (f"\n\n{attachment_text}" if attachment_text else "")
                            + (f"\n\n{rag_context}" if rag_context else "")
                        ),
                    }
                ]
                messages.extend(_history_messages(db, thread, user_message.id))
                answer = llm_service.chat(
                    llm,
                    messages + [{"role": "user", "content": payload.message}],
                    db=db,
                )
                reply = answer.get("content", "").strip() or _fallback_reply(intent, scenario)
            else:
                reply = _fallback_reply(intent, scenario)
    except Exception as exc:  # noqa: BLE001
        if intent == "scenario_model":
            public_error = (
                assistant_compilation_job_service.public_compilation_error(exc)
            )
        elif isinstance(exc, workflow_service.WorkflowGenerationError):
            public_error = assistant_compilation_job_service.PublicCompilationError(
                "workflow_generation_invalid",
                str(exc),
            )
        else:
            public_error = assistant_compilation_job_service.PublicCompilationError(
                "assistant_request_failed",
                "这次助手请求未完成，系统未执行任何变更；服务端已保留诊断记录，请稍后重试。",
            )
        reply = public_error.message
        error_uncertainties = [public_error.message]
        questions.append({
            "id": "retry",
            "title": "任务未完成",
            "message": public_error.message,
            "options": [
                {"label": "显式重试", "value": "retry", "impact": "保留当前会话；完整业务模型仍保持零写入。", "recommended": True},
                {"label": "仅保留说明", "value": "keep_read_only", "impact": "不生成或应用任何变更。"},
            ],
        })

    evidence = _assistant_evidence(
        intent,
        proposal=proposal,
        sources=sources,
        llm_used=bool(
            locals().get("llm")
            and not route_notice
            and intent not in {
                "apply_guidance",
                "execute_guidance",
                "capability_update_guidance",
                "change_guidance",
            }
        ),
        preview=action_preview,
        uncertainties=error_uncertainties,
    )
    assistant_context = {
        **context,
        "evidence": evidence,
        "action_preview": action_preview,
    }
    if route_notice:
        assistant_context["status"] = "route_fallback"
    if proposal.get("kind") == "scenario_model":
        run_summary = (proposal.get("payload") or {}).get("execution_summary") or {}
        assistant_context.update({
            "status": _model_run_context_status(run_summary),
            "model_run_id": proposal.get("proposal_id"),
        })
    job_bound_message = (
        intent == "scenario_model"
        and bool(str(context.get("compilation_job_id") or ""))
    )
    if not job_bound_message:
        _save_message(
            db,
            thread,
            "assistant",
            reply,
            assistant_context,
            sources,
            proposal,
            message_id=assistant_message_id,
        )
    db.add(
        AssistantAuditLog(
            tenant_id=_tenant(db),
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=payload.scenario_id,
            thread_id=thread.id,
            operation="propose" if proposal else "chat",
            status=(
                "route_fallback"
                if route_notice
                else "success" if not questions or proposal else "needs_input"
            ),
            context=assistant_context,
            result={"intent": intent, "sources": sources, "proposal_kind": proposal.get("kind", ""), "evidence": evidence},
        )
    )
    db.commit()
    db.refresh(thread)
    return AssistantReplyOut(
        thread_id=thread.id,
        reply=reply,
        proposal=_public_recovery_proposal(proposal),
        questions=questions,
        suggestions=suggestions,
        sources=sources,
        evidence=evidence,
        action_preview=action_preview,
    )


@router.post("/proposals/apply")
def apply_proposal(payload: AssistantProposalApplyRequest, db: Session = Depends(get_tenant_db)):
    if not payload.confirm:
        raise HTTPException(409, "应用变更必须显式确认")
    thread, proposal_message, saved_proposal = _find_saved_proposal(db, payload.thread_id, payload.proposal_id)
    if saved_proposal.get("kind") != payload.kind:
        raise HTTPException(409, "变更草稿类型与请求不一致")
    kind = payload.kind
    task_id = str(payload.task_id or "").strip()
    if kind == "scenario_model" and not task_id:
        raise HTTPException(409, "完整场景建模计划必须指定当前任务，不能整体应用或重放")
    if saved_proposal.get("status") in {"applied", "partially_applied"} and not payload.task_id:
        return {
            "ok": True,
            "status": "replayed",
            "message": "该变更草稿已经应用过，已返回原应用结果",
            "data": _public_recovery_proposal(
                saved_proposal.get("apply_result") or {}
            ),
        }

    data = (
        saved_proposal.get("payload")
        if isinstance(saved_proposal.get("payload"), dict)
        else {}
    )
    defer_task = bool(task_id and payload.task_action in {"defer", "skip"})
    if task_id and kind != "scenario_model":
        raise HTTPException(409, "只有完整场景建模草稿支持按任务继续")
    result: dict[str, Any]
    scenario: BusinessScenario | None = None
    expected_snapshot: dict[str, Any] = {}
    if kind == "scenario":
        if thread.scenario_id is not None:
            raise HTTPException(409, "新场景草稿只能从全局助手会话应用")
        if payload.scenario_id:
            raise HTTPException(409, "创建新场景时不能指定既有业务场景")
        permission_service.require_tenant_permission(db, "write")
    else:
        if not payload.scenario_id:
            raise HTTPException(400, "该变更草稿必须指定业务场景")
        scenario = _scenario(db, payload.scenario_id, writable=True)
        if not scenario or thread.scenario_id != scenario.id:
            raise HTTPException(409, "变更草稿与当前业务场景不一致")
        if kind == "scenario_model" and task_id:
            saved_task = next(
                (
                    item for item in (data.get("tasks") or [])
                    if str(item.get("id") or "") == task_id
                ),
                None,
            )
            if saved_task is None:
                raise HTTPException(404, "建模任务不存在或已过期，请重新生成")
            saved_task_status = str(saved_task.get("status") or "pending")
            if saved_task_status in _MODEL_TASK_TERMINAL_STATUSES:
                existing_result = saved_task.get("apply_result") or {
                    "kind": "scenario_model",
                    "task_id": task_id,
                    "task_status": saved_task_status,
                }
                return {
                    "ok": True,
                    "status": "replayed",
                    "message": "该建模任务已经处理过，已返回原任务结果",
                    "data": _public_recovery_proposal(existing_result),
                    "proposal": _public_recovery_proposal(saved_proposal),
                    "task_update_text": str(existing_result.get("task_update_text") or ""),
                    "execution_summary": _public_model_execution_summary(
                        data.get("execution_summary") or {}
                    ),
                    "next_action": data.get("next_action") or {},
                }
        expected_snapshot = saved_proposal.get("base_snapshot") or {}
        if expected_snapshot and not defer_task and not _snapshot_matches(
            expected_snapshot, _scenario_snapshot(scenario)
        ):
            raise HTTPException(409, "场景在确认前已发生变化，请重新生成变更草稿")

    claim: AssistantProposalApplication | None = None
    try:
        claim, acquired = _claim_proposal_application(
            db,
            proposal_id=_proposal_application_key(payload.proposal_id, task_id or None),
            thread_id=thread.id,
            message_id=proposal_message.id,
            kind=kind,
        )
        if not acquired:
            # The unique-key conflict rolls back and expires every object read
            # before the winner committed.  Re-lock the canonical proposal so
            # replay never returns the stale pre-application task board.
            thread, proposal_message, saved_proposal = _find_saved_proposal(
                db,
                payload.thread_id,
                payload.proposal_id,
            )
            data = (
                saved_proposal.get("payload")
                if isinstance(saved_proposal.get("payload"), dict)
                else {}
            )
            if claim.status == "applied":
                claim_result = claim.result if isinstance(claim.result, dict) else {}
                return {
                    "ok": True,
                    "status": "replayed",
                    "message": "该变更草稿已经应用过，已返回原应用结果",
                    "data": _public_recovery_proposal(claim_result),
                    "proposal": _public_recovery_proposal(saved_proposal),
                    "task_update_text": str(claim_result.get("task_update_text") or ""),
                    "execution_summary": _public_model_execution_summary(
                        data.get("execution_summary") or {}
                    ),
                    "next_action": data.get("next_action") or {},
                }
            raise HTTPException(409, "该变更草稿正在应用，请稍后重试")

        if scenario is not None:
            # PostgreSQL/MySQL serialize definition writes on the scenario row.
            # SQLite ignores FOR UPDATE, but the preceding unique claim INSERT
            # already holds its single-writer lock until this transaction ends.
            locked = db.execute(
                select(BusinessScenario)
                .where(BusinessScenario.id == scenario.id)
                .with_for_update()
            ).scalars().first()
            if not locked:
                raise HTTPException(409, "业务场景已不存在，请重新生成变更草稿")
            scenario = locked
            relationship_names = (
                "entities", "relations", "data_mappings",
                "relation_data_mappings", "actions", "function_definitions",
                "rules", "events", "workflows",
            )
            db.expire(scenario, relationship_names)
            if expected_snapshot and not defer_task and not _snapshot_matches(
                expected_snapshot, _scenario_snapshot(scenario)
            ):
                raise HTTPException(409, "场景在确认前已发生变化，请重新生成变更草稿")

        if kind == "scenario_model" and scenario is not None:
            source_context = (
                proposal_message.context
                if isinstance(proposal_message.context, dict)
                else {}
            )
            saved_proposal = _materialize_scenario_model_proposal(
                db,
                scenario,
                saved_proposal,
                source_thread_id=proposal_message.thread_id,
                source_message_id=proposal_message.id,
                compilation_job_id=str(
                    source_context.get("compilation_job_id") or ""
                ),
            )
            data = (
                saved_proposal.get("payload")
                if isinstance(saved_proposal.get("payload"), dict)
                else data
            )

        if kind == "scenario":
            draft = ScenarioIn.model_validate(data)
            name = draft.name.strip()[:200]
            if not name:
                raise PolicyViolation("业务场景名称不能为空")
            duplicate = db.execute(
                select(BusinessScenario.id).where(
                    BusinessScenario.tenant_id == _tenant(db),
                    BusinessScenario.name == name,
                )
            ).first()
            if duplicate:
                raise PolicyViolation("同名业务场景已存在，请重新生成或修改草稿")
            scenario = BusinessScenario(
                tenant_id=_tenant(db),
                name=name,
                description=draft.description.strip()[:6000],
                industry=draft.industry.strip()[:100],
                # AI-created scenes always enter the governed lifecycle as a
                # draft, irrespective of any legacy/client payload field.
                status="draft",
            )
            db.add(scenario)
            db.flush()
            result = {"kind": kind, "scenario_id": scenario.id, "status": "draft"}
        elif kind == "ontology":
            assert scenario is not None
            entities = data.get("entities") or []
            relations = data.get("relations") or []
            if not entities:
                raise PolicyViolation("本体草稿没有实体，不能应用")
            applied = ontology_service.apply_generated_ontology(
                db,
                scenario,
                {"entities": entities, "relations": relations},
                commit=False,
            )
            result = {"kind": kind, **applied}
        elif kind == "mapping":
            assert scenario is not None
            mapping, operation = _apply_mapping_draft(db, scenario, data)
            result = {
                "kind": kind,
                "mapping_id": mapping.id,
                "operation": operation,
                "entity_id": mapping.entity_id,
                "data_source_id": mapping.data_source_id,
                "table_name": mapping.table_name,
                "field_count": len(mapping.column_map or {}),
                # Saving a definition does not read/import source rows.
                "refresh_required": True,
            }
        elif kind == "workflow":
            assert scenario is not None
            nodes = data.get("nodes") or []
            edges = data.get("edges") or []
            workflow_service.validate_workflow_definition(nodes, edges)
            workflow_service.canonicalize_workflow_references(
                db,
                scenario.id,
                steps=[],
                nodes=nodes,
            )
            workflow_service.validate_workflow_references(
                db,
                scenario.id,
                steps=[],
                nodes=nodes,
            )
            workflow = OntologyWorkflow(
                scenario_id=scenario.id,
                name=str(data.get("name") or "AI 生成工作流"),
                description=str(data.get("description") or ""),
                trigger_type="manual",
                steps=[],
                nodes=nodes,
                edges=edges,
                status="draft",
                enabled=False,
            )
            db.add(workflow)
            db.flush()
            result = {"kind": kind, "workflow_id": workflow.id, "nodes": len(nodes), "edges": len(edges)}
        elif kind == "scenario_model" and task_id:
            assert scenario is not None
            tasks = data.get("tasks") or scenario_model_compiler.build_model_task_plan(data)
            task = next((item for item in tasks if str(item.get("id") or "") == task_id), None)
            if task is None:
                raise HTTPException(404, "建模任务不存在或已过期，请重新生成")
            task_status = str(task.get("status") or "pending")
            if task_status in {
                "applied",
                "partially_applied",
                "deferred",
                "drafted_with_gaps",
                "skipped",
                "empty",
            }:
                existing_result = task.get("apply_result") or {
                    "kind": "scenario_model",
                    "task_id": task_id,
                    "task_status": task_status,
                }
                return {
                    "ok": True,
                    "status": "replayed",
                    "message": "该建模任务已经处理过，已返回原任务结果",
                    "data": _public_recovery_proposal(existing_result),
                    "proposal": _public_recovery_proposal(saved_proposal),
                    "task_update_text": str(existing_result.get("task_update_text") or ""),
                    "execution_summary": _public_model_execution_summary(
                        data.get("execution_summary") or {}
                    ),
                    "next_action": data.get("next_action") or {},
                }
            current_task_id = str(data.get("current_task_id") or "")
            if task_id != current_task_id:
                raise HTTPException(
                    409,
                    "只能处理计划当前停留的任务，请先完成或保留当前任务草稿",
                )
            if task_status == "waiting":
                waiting_for = "、".join(str(value) for value in (task.get("waiting_for") or task.get("depends_on") or []))
                raise HTTPException(409, f"该任务正在等待前置任务完成：{waiting_for or '前置任务'}")

            task_drafts = scenario_model_draft_service.task_drafts_for_apply(
                db,
                tenant_id=_tenant(db),
                scenario_id=scenario.id,
                proposal_id=str(saved_proposal.get("proposal_id") or ""),
                task_id=task_id,
                created_by_user_id=_current_user_id(db),
            )
            authoritative_ineligible_draft_statuses = {
                row.id: str(row.draft_status or "needs_attention")
                for row in task_drafts
                if row.draft_status
                not in scenario_model_draft_service.APPLYABLE_DRAFT_STATUSES
            }

            if defer_task:
                result = {
                    "kind": "scenario_model",
                    "task_id": task_id,
                    "task_status": "deferred",
                    "deferred": True,
                    "draft_preserved": True,
                    "remaining_blockers": task.get("issues") or [],
                }
            else:
                # Select only the current task.  References to resources from
                # completed prerequisite tasks are rewritten to their persisted
                # IDs; unresolved generated references remain a hard failure.
                apply_payload = scenario_model_compiler.task_payload_for_apply(data, task_id)
                resource_ids = _scenario_model_resource_ids(scenario, data)
                apply_payload = _rewrite_persisted_task_references(apply_payload, resource_ids)
                edited_drafts = [
                    row for row in task_drafts
                    if row.draft_status == "needs_validation"
                ]
                attention_drafts = [
                    row for row in task_drafts
                    if row.draft_status == "needs_attention"
                ]
                superseded_drafts = [
                    row for row in task_drafts if row.draft_status == "superseded"
                ]
                ineligible_drafts = [
                    row for row in task_drafts
                    if row.draft_status
                    not in scenario_model_draft_service.APPLYABLE_DRAFT_STATUSES
                ]
                apply_payload, draft_exclusion = (
                    scenario_model_draft_service.exclude_unvalidated_drafts_from_apply_payload(
                        apply_payload,
                        ineligible_drafts,
                    )
                )
                blocking = [
                    item for item in (apply_payload.get("unresolved") or [])
                    if item.get("blocking", True)
                ]
                partial_result: dict[str, Any] = {}
                if blocking:
                    partial_payload, partial_result = (
                        scenario_model_compiler.partial_scenario_model_payload(apply_payload)
                    )
                    # Confirmation is the write boundary.  A model may be
                    # incomplete, but that must never turn into a dead-end:
                    # formal resources that pass the ordinary validator are
                    # written now and every other candidate was already
                    # materialized as an inert, canvas-visible draft.
                    apply_payload = partial_payload
                safe_change_count = sum(
                    1 for item in (apply_payload.get("changes") or [])
                    if item.get("operation") in {"add", "update", "delete"}
                )
                active_task_drafts = [
                    row for row in task_drafts
                    if row.draft_status in scenario_model_draft_service.OPEN_DRAFT_STATUSES
                ]
                draft_preserved = bool(
                    ineligible_drafts
                    or any(
                        row.resource_kind not in scenario_model_draft_service.FORMAL_RESOURCE_KINDS
                        for row in active_task_drafts
                    )
                )
                remaining_blockers = list(
                    partial_result.get("unresolved", blocking)
                )
                if edited_drafts:
                    remaining_blockers.append({
                        "code": "EDITED_DRAFT_REQUIRES_REVALIDATION",
                        "message": "用户已修改本任务的场景草稿；系统没有用旧 proposal 覆盖或应用这些定义。",
                        "blocking": True,
                        "source_refs": [],
                        "resolution_hint": "请基于当前 working draft 重新运行校验/编译。",
                        "affected_change_keys": list(draft_exclusion.get("excluded_resource_keys") or []),
                    })
                if attention_drafts:
                    remaining_blockers.append({
                        "code": "STAGED_RESOURCE_REQUIRES_VALIDATION",
                        "message": "本任务包含待修正的场景草稿；这些定义未写入正式模型。",
                        "blocking": True,
                        "source_refs": [],
                        "resolution_hint": "请修正可见草稿并基于当前 working draft 继续编译。",
                        "affected_change_keys": list(draft_exclusion.get("excluded_resource_keys") or []),
                    })
                if superseded_drafts:
                    remaining_blockers.append({
                        "code": "PROPOSAL_RESOURCE_SUPERSEDED",
                        "message": "该 proposal 的部分资源已被更新 lineage 取代，本次旧定义未写入正式模型。",
                        "blocking": True,
                        "source_refs": [],
                        "resolution_hint": "请在最新活动草稿上继续优化或重新编译。",
                        "affected_change_keys": list(draft_exclusion.get("excluded_resource_keys") or []),
                    })
                if ineligible_drafts:
                    status_counts: dict[str, int] = {}
                    for row in ineligible_drafts:
                        status = str(row.draft_status or "needs_attention")
                        status_counts[status] = status_counts.get(status, 0) + 1
                    remaining_blockers.append({
                        "code": "STAGING_DRAFT_NOT_READY_FOR_APPLY",
                        "message": (
                            "只有 ready_for_review 状态的场景草稿可以写入正式模型；"
                            "其余草稿保持原状态并继续留在工作区。"
                        ),
                        "blocking": True,
                        "source_refs": [],
                        "resolution_hint": "请修正、重新校验或显式解决这些具体草稿后再继续。",
                        "affected_change_keys": list(
                            draft_exclusion.get("excluded_resource_keys") or []
                        ),
                        "draft_status_counts": status_counts,
                    })
                if safe_change_count <= 0 and task_drafts:
                    result = {
                        "kind": "scenario_model",
                        "task_id": task_id,
                        "task_status": "drafted_with_gaps",
                        "partial": False,
                        "draft_preserved": True,
                        "edited_draft_count": len(edited_drafts),
                        "ineligible_draft_count": len(ineligible_drafts),
                        "excluded_draft_ids": draft_exclusion.get(
                            "excluded_draft_ids", []
                        ),
                        "excluded_resource_keys": draft_exclusion.get(
                            "excluded_resource_keys", []
                        ),
                        "blocked_issue_count": len(remaining_blockers),
                        "safe_change_count": 0,
                        "remaining_blockers": remaining_blockers,
                        "applied_change_keys": [],
                    }
                elif safe_change_count <= 0:
                    result = {
                        "kind": "scenario_model",
                        "task_id": task_id,
                        "task_status": "drafted_with_gaps",
                        "partial": False,
                        "draft_preserved": False,
                        "blocked_issue_count": len(remaining_blockers),
                        "safe_change_count": 0,
                        "remaining_blockers": remaining_blockers or [{
                            "code": "NO_APPLICABLE_TASK_CHANGE",
                            "message": "本任务没有可安全写入的正式变更；任务问题已保留，计划继续推进。",
                            "blocking": True,
                            "source_refs": [],
                            "resolution_hint": "请在后续对话中补充资料或基于当前场景重新编译。",
                        }],
                        "applied_change_keys": [],
                        "excluded_resource_keys": draft_exclusion.get("excluded_resource_keys", []),
                    }
                else:
                    applied = scenario_model_compiler.apply_scenario_model(
                        db, scenario, apply_payload
                    )
                    actual_applied_change_keys = [
                        str(value)
                        for value in (applied.get("applied_change_keys") or [])
                        if str(value)
                    ]
                    has_gaps = bool(blocking or draft_preserved)
                    result = {
                        **applied,
                        "task_id": task_id,
                        "task_status": "partially_applied" if has_gaps else "applied",
                        "partial": has_gaps,
                        "draft_preserved": draft_preserved,
                        "edited_draft_count": len(edited_drafts),
                        "ineligible_draft_count": len(ineligible_drafts),
                        "excluded_draft_ids": draft_exclusion.get(
                            "excluded_draft_ids", []
                        ),
                        "excluded_resource_keys": draft_exclusion.get(
                            "excluded_resource_keys", []
                        ),
                        "blocked_issue_count": len(remaining_blockers),
                        "safe_change_count": partial_result.get(
                            "safe_change_count", safe_change_count
                        ),
                        "remaining_blockers": remaining_blockers,
                        "applied_change_keys": actual_applied_change_keys,
                    }
            updated_payload = _refresh_model_task_states(
                data,
                applied_task_id=task_id,
                applied_status=str(result.get("task_status") or "applied"),
            )
            task_after = next((item for item in updated_payload.get("tasks") or [] if item.get("id") == task_id), None)
            if task_after is not None:
                task_after["apply_result"] = result
            data = updated_payload
        elif kind == "scenario_model":
            assert scenario is not None
            blocking = [
                item for item in (data.get("unresolved") or [])
                if item.get("blocking", True)
            ]
            apply_payload = data
            partial_result: dict[str, Any] = {}
            if blocking:
                if not payload.allow_partial:
                    raise PolicyViolation(
                        f"复合业务模型仍有 {len(blocking)} 个阻塞项；请确认应用可用部分，或先重新编译"
                    )
                apply_payload, partial_result = (
                    scenario_model_compiler.partial_scenario_model_payload(data)
                )
                if not partial_result.get("safe_change_count"):
                    raise PolicyViolation(
                        "当前没有可独立应用且通过安全预检的变更；请先按阻塞项补充资料后重新编译"
                    )
            result = scenario_model_compiler.apply_scenario_model(
                db, scenario, apply_payload
            )
            if blocking:
                result = {
                    **result,
                    "partial": True,
                    "blocked_issue_count": len(blocking),
                    "safe_change_count": partial_result.get("safe_change_count", 0),
                    "blocked_change_count": partial_result.get("blocked_change_count", 0),
                    "blocked_change_keys": partial_result.get("blocked_change_keys", []),
                    "remaining_blockers": partial_result.get("unresolved", blocking),
                }
        else:  # Defensive guard for legacy rows bypassing current schema.
            raise PolicyViolation("不支持的变更草稿类型")
    except PolicyViolation as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    updated_proposal = dict(saved_proposal)
    task_update_text = ""
    if task_id:
        updated_proposal["payload"] = data
        execution_status = str(data.get("execution_status") or "")
        updated_proposal["status"] = (
            "applied"
            if execution_status == "completed"
            else execution_status
            if execution_status in {"completed_with_gaps", "completed_no_changes"}
            else "in_progress"
        )
        updated_proposal["requires_confirmation"] = bool(data.get("current_task_id"))
        updated_proposal["run_revision"] = _safe_nonnegative_int(
            data.get("execution_revision")
        )
        execution_summary = data.get("execution_summary") or {}
        next_action = data.get("next_action") or {}
        task_after = next(
            (
                item for item in (data.get("tasks") or [])
                if str(item.get("id") or "") == task_id
            ),
            {},
        )
        task_title = str(task_after.get("title") or task_id)
        if result.get("task_status") == "drafted_with_gaps":
            task_update_text = (
                f"「{task_title}」本轮没有可安全写入的正式变更；候选仍保留为"
                "停用、不可发布的待校验草稿，计划已继续推进，具体问题保留在助手会话中。"
            )
        elif result.get("deferred"):
            task_update_text = f"「{task_title}」已保留为可继续优化的草稿，本次未写入正式模型。"
        elif result.get("partial"):
            task_update_text = (
                f"「{task_title}」的安全部分已写入正式模型；其余候选仍保留为"
                "停用、不可发布的待校验草稿，问题汇总保留在助手会话中。"
            )
        else:
            task_update_text = f"「{task_title}」已应用到当前场景。"
        if execution_summary.get("final"):
            task_update_text += f"\n\n{execution_summary.get('message') or ''}"
            hints = [
                str(value).strip()
                for value in (execution_summary.get("resolution_hints") or [])
                if str(value).strip()
            ]
            if hints:
                task_update_text += "\n\n建议下一步：" + "；".join(hints[:5])
        elif next_action.get("type") == "confirm_task":
            task_update_text += (
                f" 下一步已停留在「{next_action.get('task_title') or '下一任务'}」"
                "等待确认，整个计划仍保持进行中。"
            )
        result = {
            **result,
            "execution_summary": execution_summary,
            "next_action": next_action,
            "run_revision": _safe_nonnegative_int(data.get("execution_revision")),
            "task_update_text": task_update_text,
        }
        if task_after:
            task_after["apply_result"] = copy.deepcopy(result)
    else:
        updated_proposal["status"] = (
            "partially_applied"
            if kind == "scenario_model" and result.get("partial")
            else "applied"
        )
    updated_proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    updated_proposal["apply_result"] = result
    if task_id and scenario is not None:
        applied_change_keys = result.get("applied_change_keys")
        excluded_resource_keys = result.get("excluded_resource_keys")
        scenario_model_draft_service.mark_task_outcome(
            db,
            tenant_id=_tenant(db),
            scenario_id=scenario.id,
            proposal_id=str(updated_proposal.get("proposal_id") or ""),
            task_id=task_id,
            created_by_user_id=_current_user_id(db),
            task_status=str(result.get("task_status") or "applied"),
            applied_change_keys=(
                applied_change_keys if isinstance(applied_change_keys, list) else []
            ),
            excluded_resource_keys=(
                excluded_resource_keys
                if isinstance(excluded_resource_keys, list)
                else []
            ),
        )
        # The staging row is the authority for whether a concrete resource may
        # enter governed tables. Applying a task may close ready_for_review
        # rows, but it must not reinterpret edited, conflicted, deferred, or
        # historical rows as applied merely because their old proposal had a
        # matching change key.
        for row in task_drafts:
            authoritative_status = authoritative_ineligible_draft_statuses.get(
                row.id
            )
            if authoritative_status is None:
                continue
            row.draft_status = authoritative_status
            row.enabled = False
            row.publishable = False
        proposal_context = (
            proposal_message.context
            if isinstance(proposal_message.context, dict)
            else {}
        )
        updated_proposal = _materialize_scenario_model_proposal(
            db,
            scenario,
            updated_proposal,
            source_thread_id=proposal_message.thread_id,
            source_message_id=proposal_message.id,
            compilation_job_id=str(proposal_context.get("compilation_job_id") or ""),
        )
        data = (
            updated_proposal.get("payload")
            if isinstance(updated_proposal.get("payload"), dict)
            else data
        )
    if task_id and scenario is not None and not defer_task:
        db.flush()
        db.expire(
            scenario,
            (
                "entities", "relations", "data_mappings",
                "relation_data_mappings", "actions", "function_definitions",
                "rules", "events", "workflows",
            ),
        )
        updated_proposal["base_snapshot"] = _scenario_snapshot(scenario)
    proposal_message.proposal = updated_proposal
    if task_update_text:
        existing_content = str(proposal_message.content or "").rstrip()
        proposal_message.content = (
            f"{existing_content}\n\n{task_update_text}"
            if existing_content
            else task_update_text
        )
        proposal_context = (
            dict(proposal_message.context)
            if isinstance(proposal_message.context, dict)
            else {}
        )
        proposal_context.update({
            "status": _model_run_context_status(
                data.get("execution_summary") or {}
            ),
            "model_run_id": updated_proposal.get("proposal_id"),
            "run_revision": _safe_nonnegative_int(data.get("execution_revision")),
        })
        proposal_message.context = proposal_context
        compilation_job_id = str(proposal_context.get("compilation_job_id") or "")
        if compilation_job_id:
            compilation_job = _scoped_compilation_job_for_message(
                db,
                proposal_message,
                compilation_job_id,
                lock=True,
            )
            canonical_compilation_message = bool(
                compilation_job is not None
                and compilation_job.thread_id == proposal_message.thread_id
                and compilation_job.message_id == proposal_message.id
            )
            # Recovery must return the latest run revision, not the proposal
            # snapshot that existed when compilation ended.
            _sync_compilation_job_result(
                compilation_job,
                updated_proposal,
                canonical=canonical_compilation_message,
            )
            if (
                compilation_job is not None
                and compilation_job.status == "succeeded"
                and canonical_compilation_message
            ):
                execution_summary = data.get("execution_summary") or {}
                _update_compilation_subscription_messages(
                    db,
                    compilation_job,
                    status=_model_run_context_status(execution_summary),
                    content=(
                        str(execution_summary.get("message") or "").strip()
                        or task_update_text
                        or "场景建模计划已推进；请查看权威草稿的下一项任务。"
                    ),
                    canonical_message_id=proposal_message.id,
                    model_run_id=str(updated_proposal.get("proposal_id") or ""),
                )
    assert claim is not None
    claim.status = "applied"
    claim.result = result
    claim.applied_at = datetime.now(timezone.utc)
    db.add(
        AssistantAuditLog(
            tenant_id=_tenant(db),
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=scenario.id if scenario else None,
            thread_id=thread.id if thread else None,
            operation="apply_proposal",
            status="success",
            context={
                "kind": kind,
                "proposal_id": payload.proposal_id,
                "confirmed": True,
                "task_id": task_id,
                "task_action": "defer" if defer_task else payload.task_action,
            },
            result=result,
        )
    )
    db.commit()
    message = (
        "业务场景草稿已创建"
        if kind == "scenario"
        else "本任务草稿已保留，计划已继续推进"
        if task_id and result.get("deferred")
        else "本任务已处理，计划已推进到下一状态"
        if task_id
        else "变更草稿已应用到场景草稿"
    )
    return {
        "ok": True,
        "message": message,
        "data": _public_recovery_proposal(result),
        "proposal": _public_recovery_proposal(updated_proposal) if task_id else None,
        "task_update_text": task_update_text,
        "execution_summary": (
            _public_model_execution_summary(data.get("execution_summary") or {})
            if task_id else {}
        ),
        "next_action": (data.get("next_action") or {}) if task_id else {},
    }
