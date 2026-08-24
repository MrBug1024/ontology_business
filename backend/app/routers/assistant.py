"""全局 AI 助手：跨页面上下文、临时附件、草稿生成与确认应用。"""
from __future__ import annotations

import json
import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
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
    AssistantThread,
    BusinessScenario,
    BucketFile,
    DataMapping,
    DataSource,
    DocumentChunk,
    LLMConfig,
    MCPConfig,
    OntologyEntity,
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
    assistant_compilation_job_service,
    doc_parser,
    datasource_service,
    llm_service,
    mapping_refresh_service,
    ontology_service,
    permission_service,
    rag_service,
    runtime_connector_service,
    runtime_definition_service,
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
ASSISTANT_ATTACHMENT_TEXT_MAX_CHARS = 80_000
ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS = 80_000


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


def _scenario_context(scenario: BusinessScenario | None) -> str:
    if not scenario:
        return "当前未打开具体业务场景。"
    lines = [f"业务场景：{scenario.name}", f"场景说明：{scenario.description or '暂无'}"]
    if scenario.industry:
        lines.append(f"所属行业：{scenario.industry}")
    if scenario.entities:
        lines.append("已有本体实体：")
        for entity in scenario.entities[:30]:
            props = "、".join(p.name for p in entity.properties[:12]) or "暂无属性"
            lines.append(f"- {entity.name}：{props}")
    if scenario.relations:
        lines.append("已有关系：")
        for relation in scenario.relations[:30]:
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
            lines.append(f"- {relation.name}（{relation.relation_type}{suffix}）")
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
        changes = list(data.get("changes") or [])
        blocking = [
            item for item in (data.get("unresolved") or [])
            if item.get("blocking", True)
        ]
        coverage = data.get("coverage_summary") or {}
        title = "完整业务模型编译草稿"
        summary = (
            f"已从 {len(data.get('source_manifest') or [])} 份文档编译 "
            f"{len(changes)} 项变更，覆盖 {coverage.get('total', 0)} 个来源段落；"
            + (
                f"仍有 {len(blocking)} 个阻塞项，当前不可应用。"
                if blocking
                else "引用、冲突和来源覆盖已通过预检，可在确认后原子应用。"
            )
        )
    else:
        raise ValueError("不支持的助手草稿类型")
    return {
        "proposal_id": uuid.uuid4().hex,
        "kind": kind,
        "title": title,
        "summary": summary,
        "payload": data,
        "changes": changes,
        "base_snapshot": snapshot,
        "requires_confirmation": True,
        "status": "pending",
    }


def _find_saved_proposal(db: Session, thread_id: str, proposal_id: str) -> tuple[AssistantThread, AssistantMessage, dict[str, Any]]:
    thread = _thread(db, thread_id)
    messages = db.execute(
        select(AssistantMessage).where(
            AssistantMessage.thread_id == thread.id,
            AssistantMessage.role == "assistant",
        ).order_by(AssistantMessage.created_at.desc())
    ).scalars().all()
    for message in messages:
        proposal = message.proposal if isinstance(message.proposal, dict) else {}
        if proposal.get("proposal_id") == proposal_id:
            if _has_invalid_historic_rag_source(db, thread, message):
                raise HTTPException(409, "变更草稿引用的资料已不在当前访问范围，请重新生成")
            return thread, message, proposal
    raise HTTPException(404, "变更草稿不存在或已过期，请重新生成")


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
        return result
    if isinstance(value, list):
        return [_public_recovery_proposal(item) for item in value]
    return value


def _matching_compilation_proposal_message(
    db: Session,
    job: AssistantCompilationJob,
) -> tuple[AssistantThread | None, AssistantMessage | None]:
    result = job.result if isinstance(job.result, dict) else {}
    proposal_id = str(result.get("proposal_id") or "")
    if not proposal_id:
        return None, None
    rows = db.execute(
        select(AssistantThread, AssistantMessage)
        .join(AssistantMessage, AssistantMessage.thread_id == AssistantThread.id)
        .where(
            AssistantThread.tenant_id == job.tenant_id,
            AssistantThread.created_by_user_id == job.created_by_user_id,
            AssistantThread.scenario_id == job.scenario_id,
            AssistantMessage.role == "assistant",
        )
        .order_by(AssistantMessage.created_at.desc())
    ).all()
    for thread, message in rows:
        proposal = message.proposal if isinstance(message.proposal, dict) else {}
        if proposal.get("proposal_id") == proposal_id and proposal == result:
            return thread, message
    return None, None


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
        if not thread or not message or message.thread_id != thread.id or message.role != "assistant":
            raise RuntimeError("编译任务恢复占位消息不存在或不属于当前会话")
        message.context = {
            **context,
            "status": "processing",
            "compilation_job_id": job_id,
        }
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
        )
    finally:
        progress_db.close()


def _fail_compilation_job(
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    error: BaseException | str,
) -> None:
    failure_db = SessionLocal()
    failure_db.info["tenant_id"] = tenant_id
    failure_db.info["user_id"] = user_id
    try:
        job = failure_db.get(AssistantCompilationJob, job_id)
        if not job:
            return
        # Never let a late transport/persistence exception replace the
        # server-owned message of an already successful terminal job.
        if job.status == "succeeded":
            return
        public_error = assistant_compilation_job_service.public_compilation_error(
            error
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
        assistant_compilation_job_service.mark_failed(
            failure_db,
            job_id,
            error=error,
            commit=False,
        )
        failure_db.commit()
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
            )
        ).scalars().first()
        if not job:
            raise RuntimeError("编译任务不存在")
        if job.status == "succeeded":
            return dict(job.result or {})
        if job.status != "running":
            raise RuntimeError("非运行中的编译任务不能标记成功")
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
        if current_baseline != job.scenario_baseline:
            changed = assistant_compilation_job_service.CompilationBaselineChanged(
                "业务场景基线在编译期间发生变化"
            )
            assistant_compilation_job_service.mark_failed(
                finish_db,
                job.id,
                error=changed,
                commit=False,
            )
            finish_db.commit()
            raise changed
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
        proposal = _build_proposal("scenario_model", data, scenario)
        _save_message(
            finish_db,
            thread,
            "assistant",
            reply,
            {
                **context,
                "status": "success",
                "compilation_job_id": job.id,
            },
            sources,
            proposal,
            thinking,
            message_id=assistant_message_id,
        )
        job.thread_id = thread.id
        job.message_id = assistant_message_id
        assistant_compilation_job_service.mark_succeeded(
            finish_db,
            job.id,
            result=proposal,
            commit=False,
        )
        finish_db.commit()
        return proposal
    except Exception:
        finish_db.rollback()
        raise
    finally:
        finish_db.close()


def _attachment_context(attachments: list[AssistantAttachment]) -> tuple[str, list[dict[str, Any]]]:
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
        if item.status == "parsed" and item.parsed_text:
            part = f"【附件：{item.filename}】\n{parsed_text}"
        elif item.error:
            part = f"【附件：{item.filename}】解析失败：{item.error}"
        else:
            continue
        projected = included_chars + (len(parsed_text) if parsed_text else len(part))
        if projected > ASSISTANT_ATTACHMENT_CONTEXT_MAX_CHARS:
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
    return result


def _intent(message: str, mode: str, draft_kind: str = "auto") -> str:
    # Explicit modes take precedence over words in the prompt.  In particular,
    # an ``explain`` request that happens to mention “创建/映射/执行” must never
    # be routed into a proposal generator, and chat-level apply/execute never
    # cross their dedicated governance boundaries.
    if mode == "explain":
        return "explain"
    if mode == "apply":
        return "apply_guidance"
    if mode == "execute":
        return "execute_guidance"
    if mode == "draft" and draft_kind != "auto":
        return draft_kind
    text = message.lower()
    if any(k in text for k in ("创建场景", "新建场景", "建立场景", "业务场景草稿")):
        return "scenario"
    ontology_requested = any(k in text for k in ("本体", "实体", "关系", "建模", "数据模型", "对象类型"))
    explicit_ontology_requested = any(
        k in text
        for k in (
            "本体建模",
            "本体模型",
            "建立本体",
            "构建本体",
            "创建本体",
            "生成本体",
            "设计本体",
        )
    )
    mapping_requested = any(k in text for k in ("数据映射", "字段映射", "映射草稿", "列映射"))
    # An ontology brief commonly asks the model to preserve keys for a later
    # data-mapping step.  That supporting phrase must not turn the whole draft
    # into a mapping proposal.  A mapping-only request still routes normally.
    if mapping_requested and not ontology_requested:
        return "mapping"
    # 复合实施文档通常会在本体建模要求中同时提到规则、事件和工作流；
    # 这些下游章节不应把整份附件误送到单工作流生成器。
    if explicit_ontology_requested:
        return "ontology"
    if any(k in text for k in ("工作流", "流程", "编排", "审批流", "自动化")):
        return "workflow"
    if mode == "draft" or ontology_requested:
        return "ontology"
    return "chat"


def _mode_safety_context(mode: str) -> str:
    if mode == "explain":
        return "\n当前是解释模式：只读分析已授权上下文，不生成变更清单，不应用变更，不触发执行。"
    if mode == "draft":
        return "\n当前是草稿模式：最多生成待审阅变更清单，确认前不得写入正式数据。"
    if mode == "apply":
        return "\n当前是应用引导模式：聊天不能写入，只能引导用户在已保存的提案卡片显式确认。"
    if mode == "execute":
        return "\n当前是执行引导模式：聊天不能触发副作用，只能说明影响、权限、预演和审批入口。"
    return "\n当前是兼容问答模式：回答问题或生成待确认草稿，但不得直接应用或执行。"


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
    _purge_expired_attachments(db)
    consumed_at = datetime.now(timezone.utc)
    for attachment in attachments:
        attachment.thread_id = thread_id
        attachment.consumed_at = consumed_at
    db.flush()
    return attachments


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
    ).scalars().all()
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
    return AssistantCompilationJobResultOut(
        job_id=job.id,
        thread_id=job.thread_id,
        scenario_id=job.scenario_id,
        proposal=_public_recovery_proposal(job.result),
        proposal_thread_id=proposal_thread.id if proposal_thread else None,
        proposal_message_id=proposal_message.id if proposal_message else None,
        apply_ready=bool(proposal_thread and proposal_message),
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
    if not thread:
        thread = AssistantThread(
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

    attachments = _safe_attachment_ids(db, payload.attachment_ids, thread_id=thread.id)
    attachment_text, sources = _attachment_context(attachments)
    rag_context, rag_sources = _authorized_rag_context(db, scenario, payload.message)
    sources = [*sources, *rag_sources]
    context = {
        "page": payload.page,
        "path": payload.path,
        "scenario_id": payload.scenario_id,
        "selection": payload.selection,
        "mode": payload.mode,
        "draft_kind": payload.draft_kind,
        "llm_config_id": payload.llm_config_id,
        "skill_ids": payload.skill_ids,
        "mcp_ids": payload.mcp_ids,
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()
    thread_id = thread.id
    assistant_message_id = uuid.uuid4().hex
    intent = _intent(payload.message, payload.mode, payload.draft_kind)
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
                + _scenario_context(scenario)
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
            assistant_context = {
                **context,
                "evidence": evidence,
                "action_preview": action_preview,
            }
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
        compilation_job_id = ""
        owns_compilation_job = False
        try:
            scenario = _scenario(db, payload.scenario_id)
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

            if intent == "apply_guidance":
                reply = _fallback_reply(intent, scenario)
                yield progress({
                    "id": "governance",
                    "title": "确认安全边界",
                    "detail": "已提供变更确认或执行预演的受控入口说明。",
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
                yield progress({"id": "scenario", "title": "生成业务场景草稿", "detail": "场景名称、目标与边界已整理完成。", "status": "done"})
                yield _sse("proposal", proposal)
                yield _sse("token", reply)
            elif intent == "scenario_model" and scenario:
                yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "正在逐段识别对象、关系、能力、规则、事件、流程和映射。", "status": "running"})
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
                compilation_settings = get_settings()
                identity = assistant_compilation_job_service.build_compilation_identity(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    scenario_id=scenario.id,
                    message=compiler_message,
                    attachments=attachments,
                    llm=llm,
                    compiler_version=scenario_model_compiler.COMPILER_VERSION,
                    scenario_baseline=baseline,
                    mapping_context_fingerprint=prepared_context["fingerprint"],
                    execution_policy={
                        "llm_call_budget": compilation_settings.scenario_model_max_llm_calls,
                        "request_timeout": compilation_settings.scenario_model_llm_timeout,
                    },
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
                    blocking = sum(
                        1 for item in (data.get("unresolved") or [])
                        if item.get("blocking", True)
                    )
                    reply = (
                        "已重放相同输入此前完成的完整业务模型；没有再次调用模型。"
                        if not blocking
                        else f"已重放相同输入此前完成的完整业务模型，其中仍有 {blocking} 个阻塞项；没有再次调用模型，也不会写入。"
                    )
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "已重放同一执行指纹的复合变更清单。", "status": "done"})
                    yield _sse("proposal", proposal)
                    yield _sse("token", reply)
                elif not owns_compilation_job and job.status == "failed":
                    saved_status = "error"
                    public_failure = _public_compilation_progress(job)
                    reply = (
                        "相同输入此前已编译失败，系统没有自动再次调用模型。"
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
                        "相同输入的完整业务模型正在由已有任务编译；"
                        "本次请求没有启动第二套模型调用。完成后再次提交相同请求即可直接重放结果。"
                    )
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "已连接到持久任务状态；已有任务仍在运行。", "status": "running"})
                    yield _sse("token", reply)
                else:
                    job_id = job.id

                    def record_compilation_call(
                        used: int,
                        total: int,
                        phase: str,
                    ) -> None:
                        # A provider progress commit must never accidentally
                        # commit compiler-created ontology rows.  Compilation
                        # is proposal-only; any pending mutation is a contract
                        # violation and aborts the job before the next call.
                        if db.new or db.dirty or db.deleted:
                            db.rollback()
                            raise RuntimeError(
                                "完整业务模型编译器产生了未授权数据库变更；"
                                "任务已中止且正式模型保持零写入"
                            )
                        _record_compilation_progress(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            job_id=job_id,
                            used=used,
                            budget=total,
                            phase=phase,
                        )

                    budget = scenario_model_compiler.LLMCallBudget(
                        job.llm_call_budget,
                        on_consume=record_compilation_call,
                    )
                    try:
                        data = scenario_model_compiler.compile_scenario_model(
                            db,
                            scenario,
                            message=compiler_message,
                            documents=compiler_documents,
                            llm=llm,
                            call_budget=budget,
                            prepared_context=prepared_context,
                        )
                        # End the read transaction and discard any direct or
                        # accidental pending compiler mutation before saving
                        # only the replay artifact in the job ledger.
                        if db.new or db.dirty or db.deleted:
                            db.rollback()
                            raise RuntimeError(
                                "完整业务模型编译器产生了未授权数据库变更；"
                                "任务已中止且正式模型保持零写入"
                            )
                        db.rollback()
                        blocking = sum(
                            1 for item in (data.get("unresolved") or [])
                            if item.get("blocking", True)
                        )
                        reply = (
                            "完整业务模型已编译并通过来源覆盖与引用预检，请核对后原子应用。"
                            if not blocking
                            else f"完整业务模型已编译，但还有 {blocking} 个阻塞项；当前不会写入，请先按清单补充资料后重新编译。"
                        )
                        proposal = _finalize_compilation_success(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            job_id=job_id,
                            thread_id=thread_id,
                            assistant_message_id=assistant_message_id,
                            scenario_id=scenario.id,
                            data=data,
                            reply=reply,
                            context=context,
                            sources=sources,
                            thinking=thinking,
                        )
                        owns_compilation_job = False
                    except Exception as exc:
                        db.rollback()
                        _fail_compilation_job(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            job_id=job_id,
                            error=exc,
                        )
                        owns_compilation_job = False
                        raise
                    yield progress({"id": "scenario-model", "title": "编译完整业务模型", "detail": "复合变更清单、来源覆盖和待确认项已生成并持久化。", "status": "done"})
                    yield _sse("proposal", proposal)
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
                yield progress({"id": "ontology", "title": "生成本体草稿", "detail": "实体和关系建议已整理完成。", "status": "done"})
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
                yield progress({"id": "mapping", "title": "生成数据映射草稿", "detail": "字段引用和主键覆盖已校验。", "status": "done"})
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
                yield progress({"id": "workflow", "title": "编排工作流草稿", "detail": "节点和连线建议已整理完成。", "status": "done"})
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
                llm_used=bool(llm and intent not in ("apply_guidance", "execute_guidance")),
                preview=action_preview,
            )
            persist_result(reply, proposal, thinking, saved_status, evidence, action_preview)
            yield _sse("meta", {
                "thread_id": thread_id,
                "proposal": proposal,
                "questions": questions,
                "suggestions": suggestions,
                "sources": sources,
                "thinking": thinking,
                "evidence": evidence,
                "action_preview": action_preview,
            })
            yield _sse("done", {"thread_id": thread_id})
        except GeneratorExit:
            cancelled = True
            if owns_compilation_job and compilation_job_id:
                try:
                    db.rollback()
                    _fail_compilation_job(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=compilation_job_id,
                        error="客户端在模型调用开始前断开，编译任务已安全终止",
                    )
                    owns_compilation_job = False
                except Exception:
                    db.rollback()
            raise
        except Exception as exc:  # noqa: BLE001
            saved_status = "error"
            if owns_compilation_job and compilation_job_id:
                try:
                    db.rollback()
                    _fail_compilation_job(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=compilation_job_id,
                        error=exc,
                    )
                except Exception:
                    db.rollback()
            if intent == "scenario_model":
                public_error = (
                    assistant_compilation_job_service.public_compilation_error(
                        exc
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
                    "proposal": proposal,
                    "questions": questions,
                    "suggestions": suggestions,
                    "sources": sources,
                    "thinking": thinking,
                    "evidence": evidence,
                    "action_preview": action_preview,
                })
            except Exception:
                pass
        finally:
            db.close()
            if not cancelled:
                yield "data: [DONE]\n\n"

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
    if not thread:
        thread = AssistantThread(
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

    attachments = _safe_attachment_ids(db, payload.attachment_ids, thread_id=thread.id)
    attachment_text, sources = _attachment_context(attachments)
    rag_context, rag_sources = _authorized_rag_context(db, scenario, payload.message)
    sources = [*sources, *rag_sources]
    context = {
        "page": payload.page,
        "path": payload.path,
        "scenario_id": payload.scenario_id,
        "selection": payload.selection,
        "mode": payload.mode,
        "draft_kind": payload.draft_kind,
        "llm_config_id": payload.llm_config_id,
        "skill_ids": payload.skill_ids,
        "mcp_ids": payload.mcp_ids,
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()

    intent = _intent(payload.message, payload.mode, payload.draft_kind)
    reply = ""
    proposal: dict[str, Any] = {}
    questions: list[dict[str, Any]] = []
    action_preview: dict[str, Any] = {}
    error_uncertainties: list[str] = []
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
    suggestions = (
        ["创建业务场景草稿", "说明建模所需资料"]
        if not scenario
        else ["解释当前场景", "编译完整业务模型", "生成本体草稿", "生成数据映射草稿", "根据当前本体设计工作流"]
    )

    try:
        if intent == "apply_guidance":
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
            compilation_settings = get_settings()
            identity = assistant_compilation_job_service.build_compilation_identity(
                tenant_id=_tenant(db),
                user_id=_current_user_id(db),
                scenario_id=scenario.id,
                message=compiler_message,
                attachments=attachments,
                llm=llm,
                compiler_version=scenario_model_compiler.COMPILER_VERSION,
                scenario_baseline=baseline,
                mapping_context_fingerprint=prepared_context["fingerprint"],
                execution_policy={
                    "llm_call_budget": compilation_settings.scenario_model_max_llm_calls,
                    "request_timeout": compilation_settings.scenario_model_llm_timeout,
                },
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
                blocking = sum(
                    1 for item in (data.get("unresolved") or [])
                    if item.get("blocking", True)
                )
                reply = (
                    "已重放相同输入此前完成的完整业务模型；没有再次调用模型。"
                    if not blocking
                    else f"已重放相同输入此前完成的完整业务模型，其中仍有 {blocking} 个阻塞项；没有再次调用模型，也不会写入。"
                )
            elif not acquired and job.status == "failed":
                public_failure = _public_compilation_progress(job)
                reply = (
                    "相同输入此前已编译失败，系统没有自动再次调用模型。"
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
                    "相同输入的完整业务模型正在由已有任务编译；"
                    "本次请求没有启动第二套模型调用。完成后再次提交相同请求即可直接重放结果。"
                )
            else:
                job_id = job.id

                def record_sync_compilation_call(
                    used: int,
                    total: int,
                    phase: str,
                ) -> None:
                    if db.new or db.dirty or db.deleted:
                        db.rollback()
                        raise RuntimeError(
                            "完整业务模型编译器产生了未授权数据库变更；"
                            "任务已中止且正式模型保持零写入"
                        )
                    _record_compilation_progress(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=job_id,
                        used=used,
                        budget=total,
                        phase=phase,
                    )

                budget = scenario_model_compiler.LLMCallBudget(
                    job.llm_call_budget,
                    on_consume=record_sync_compilation_call,
                )
                try:
                    data = scenario_model_compiler.compile_scenario_model(
                        db,
                        scenario,
                        message=compiler_message,
                        documents=compiler_documents,
                        llm=llm,
                        call_budget=budget,
                        prepared_context=prepared_context,
                    )
                    if db.new or db.dirty or db.deleted:
                        db.rollback()
                        raise RuntimeError(
                            "完整业务模型编译器产生了未授权数据库变更；"
                            "任务已中止且正式模型保持零写入"
                        )
                    db.rollback()
                    blocking = sum(
                        1 for item in (data.get("unresolved") or [])
                        if item.get("blocking", True)
                    )
                    reply = (
                        "完整业务模型已编译并通过来源覆盖与引用预检，请核对后原子应用。"
                        if not blocking
                        else f"完整业务模型已编译，但还有 {blocking} 个阻塞项；当前不会写入，请先按清单补充资料后重新编译。"
                    )
                    proposal = _finalize_compilation_success(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=job_id,
                        thread_id=thread.id,
                        assistant_message_id=assistant_message_id,
                        scenario_id=scenario.id,
                        data=data,
                        reply=reply,
                        context=context,
                        sources=sources,
                        thinking=[],
                    )
                except Exception as exc:
                    db.rollback()
                    _fail_compilation_job(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        job_id=job_id,
                        error=exc,
                    )
                    raise
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
                            + _scenario_context(scenario)
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
        llm_used=bool(locals().get("llm")),
        preview=action_preview,
        uncertainties=error_uncertainties,
    )
    assistant_context = {
        **context,
        "evidence": evidence,
        "action_preview": action_preview,
    }
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
            status="success" if not questions or proposal else "needs_input",
            context=assistant_context,
            result={"intent": intent, "sources": sources, "proposal_kind": proposal.get("kind", ""), "evidence": evidence},
        )
    )
    db.commit()
    db.refresh(thread)
    return AssistantReplyOut(
        thread_id=thread.id,
        reply=reply,
        proposal=proposal,
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
    if saved_proposal.get("status") == "applied":
        return {
            "ok": True,
            "status": "replayed",
            "message": "该变更草稿已经应用过，已返回原应用结果",
            "data": saved_proposal.get("apply_result") or {},
        }

    kind = payload.kind
    data = saved_proposal.get("payload") or {}
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
        expected_snapshot = saved_proposal.get("base_snapshot") or {}
        if expected_snapshot and not _snapshot_matches(
            expected_snapshot, _scenario_snapshot(scenario)
        ):
            raise HTTPException(409, "场景在确认前已发生变化，请重新生成变更草稿")

    claim: AssistantProposalApplication | None = None
    try:
        claim, acquired = _claim_proposal_application(
            db,
            proposal_id=payload.proposal_id,
            thread_id=thread.id,
            message_id=proposal_message.id,
            kind=kind,
        )
        if not acquired:
            if claim.status == "applied":
                return {
                    "ok": True,
                    "status": "replayed",
                    "message": "该变更草稿已经应用过，已返回原应用结果",
                    "data": claim.result or {},
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
                "entities", "relations", "data_mappings", "actions",
                "function_definitions", "rules", "events", "workflows",
            )
            db.expire(scenario, relationship_names)
            if expected_snapshot and not _snapshot_matches(
                expected_snapshot, _scenario_snapshot(scenario)
            ):
                raise HTTPException(409, "场景在确认前已发生变化，请重新生成变更草稿")

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
        elif kind == "scenario_model":
            assert scenario is not None
            result = scenario_model_compiler.apply_scenario_model(db, scenario, data)
        else:  # Defensive guard for legacy rows bypassing current schema.
            raise PolicyViolation("不支持的变更草稿类型")
    except Exception:
        db.rollback()
        raise

    updated_proposal = dict(saved_proposal)
    updated_proposal["status"] = "applied"
    updated_proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    updated_proposal["apply_result"] = result
    proposal_message.proposal = updated_proposal
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
            context={"kind": kind, "proposal_id": payload.proposal_id, "confirmed": True},
            result=result,
        )
    )
    db.commit()
    message = "业务场景草稿已创建" if kind == "scenario" else "变更草稿已应用到场景草稿"
    return {"ok": True, "message": message, "data": result}
