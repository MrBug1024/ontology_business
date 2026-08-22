"""全局 AI 助手：跨页面上下文、临时附件、草稿生成与确认应用。"""
from __future__ import annotations

import json
import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import (
    AssistantAttachment,
    AssistantAuditLog,
    AssistantMessage,
    AssistantProposalApplication,
    AssistantThread,
    BusinessScenario,
    BucketFile,
    DataMapping,
    DataSource,
    DocumentChunk,
    LLMConfig,
    OntologyEntity,
    OntologyWorkflow,
)
from ..schemas import (
    AssistantAttachmentOut,
    AssistantChatRequest,
    AssistantMessageOut,
    AssistantProposalApplyRequest,
    AssistantReplyOut,
    AssistantThreadOut,
    DataMappingIn,
    Msg,
    ScenarioIn,
)
from ..services import (
    doc_parser,
    datasource_service,
    llm_service,
    mapping_refresh_service,
    ontology_service,
    permission_service,
    rag_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
    workflow_service,
)
from ..services.auth_service import get_tenant_db
from ..services.policies import PolicyViolation

router = APIRouter(prefix="/assistant", tags=["assistant"])


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
    return candidates[0] if candidates else None


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
            lines.append(f"- {relation.name}（{relation.relation_type}）")
    return "\n".join(lines)


def _scenario_revision(scenario: BusinessScenario) -> str:
    """Hash complete mutable definitions without persisting their raw secrets."""

    def fields(item: Any, names: tuple[str, ...]) -> dict[str, Any]:
        return {name: getattr(item, name, None) for name in names}

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
                    "id", "name", "data_type", "description", "is_key", "is_required",
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
            fields(item, ("id", "name", "namespace", "source_entity_id", "target_entity_id", "relation_type", "description"))
            for item in sorted(list(getattr(scenario, "relations", []) or []), key=lambda value: str(getattr(value, "id", "")))
        ],
        "mappings": [
            fields(item, ("id", "entity_id", "data_source_id", "data_source_binding_key", "data_source_binding_ref", "table_name", "column_map", "transform_rules"))
            for item in sorted(list(getattr(scenario, "data_mappings", []) or []), key=lambda value: str(getattr(value, "id", "")))
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
        existing_entities = set(snapshot["entity_names"])
        existing_relations = set(snapshot["relation_names"])
        for entity in data.get("entities") or []:
            name = str(entity.get("name") or "未命名实体").strip()
            exists = name in existing_entities
            changes.append(
                {
                    "operation": "skip" if exists else "add",
                    "resource": "entity",
                    "name": name,
                    "summary": "实体已存在，应用时跳过" if exists else f"新增实体，包含 {len(entity.get('properties') or [])} 个属性",
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
        summary = f"建议新增 {sum(1 for item in changes if item['operation'] == 'add' and item['resource'] == 'entity')} 个实体和 {sum(1 for item in changes if item['operation'] == 'add' and item['resource'] == 'relation')} 条关系。"
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


def _attachment_context(attachments: list[AssistantAttachment]) -> tuple[str, list[dict[str, Any]]]:
    if not attachments:
        return "", []
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    for item in attachments:
        sources.append({"id": item.id, "filename": item.filename, "status": item.status})
        if item.status == "parsed" and item.parsed_text:
            parts.append(f"【附件：{item.filename}】\n{item.parsed_text[:12000]}")
        elif item.error:
            parts.append(f"【附件：{item.filename}】解析失败：{item.error}")
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
    result.evidence = dict(context.get("evidence") or {})
    result.action_preview = dict(context.get("action_preview") or {})
    return result


def _intent(message: str, mode: str) -> str:
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
    text = message.lower()
    if any(k in text for k in ("创建场景", "新建场景", "建立场景", "业务场景草稿")):
        return "scenario"
    if any(k in text for k in ("数据映射", "字段映射", "映射草稿", "列映射")):
        return "mapping"
    if any(k in text for k in ("工作流", "流程", "编排", "审批流", "自动化")):
        return "workflow"
    if mode == "draft" or any(k in text for k in ("本体", "实体", "关系", "建模", "数据模型", "对象类型")):
        return "ontology"
    return "chat"


def _mode_safety_context(mode: str) -> str:
    if mode == "explain":
        return "\n当前是解释模式：只读分析已授权上下文，不生成 Change Set，不应用变更，不触发执行。"
    if mode == "draft":
        return "\n当前是草稿模式：最多生成待审阅 Change Set，确认前不得写入正式数据。"
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
        "workflow": ("workflow_dag_validation", "节点、连线和 Action 引用在应用边界重新校验"),
        "apply_guidance": ("explicit_confirmation", "聊天不写入，只有已保存提案的 confirm=true 可应用"),
        "execute_guidance": ("typed_action_only", "聊天只预演，真实副作用必须走类型化 Action 或任务审批"),
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
            "message": "Action 预演必须绑定业务场景，才能校验目标、权限和运行定义。",
            "options": [
                {"label": "打开业务场景", "value": "open_scenario", "impact": "进入已有场景后可选择 Action 并完成只读预演。", "recommended": True},
                {"label": "保持只读说明", "value": "explain_only", "impact": "仅解释执行流程，不创建预演日志，也不触发副作用。"},
            ],
        }
        return {}, question, "请先打开一个业务场景，我才能安全地解析并预演 Action；聊天不会直接触发任何操作。"

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
                    "label": "配置类型化 Action",
                    "value": "configure_action",
                    "impact": "先由 Builder 定义参数 Schema、权限、幂等和执行器，之后才能预演。",
                    "recommended": True,
                }
            ]
        question = {
            "id": "select-action",
            "title": "选择要预演的 Action",
            "message": "我不会从模糊文字猜测有副作用的目标。请选择一个当前有权读取的 Action。",
            "options": options,
        }
        return {}, question, "需要先确定一个明确的类型化 Action，聊天不会直接执行任何操作。"

    raw_params = selected.get("params", selected.get("parameters", {}))
    params = raw_params if isinstance(raw_params, dict) else {}
    schema = action.input_schema or {}
    required = list(schema.get("required") or []) if schema.get("type") == "object" else []
    missing = [str(name) for name in required if name not in params]
    if missing or not isinstance(raw_params, dict):
        question = {
            "id": "action-parameters",
            "title": "补充 Action 参数",
            "message": f"“{action.name}”还缺少必填参数：{'、'.join(missing) if missing else '参数必须是对象'}。",
            "options": [
                {"label": "填写必填参数", "value": "provide_params", "impact": "参数齐全后仅执行权限检查和 dry_run，不触发外部副作用。", "recommended": True},
                {"label": "查看参数定义", "value": "inspect_schema", "impact": "只查看参数 Schema 与影响说明，不创建预演日志。"},
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
        return analysis, question, f"已定位 Action“{action.name}”，补齐参数后才能完成权限检查和预演。"

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
        "execution_boundary": "真实执行必须从类型化 Action/任务入口重新确认；聊天永不设置 confirm=true。",
    }
    approval = "需要显式确认或审批" if runtime_action.requires_confirmation else "仍需从 Action 入口提交"
    return analysis, None, f"已完成 Action“{runtime_action.name}”的 dry_run；{approval}，本次没有触发外部副作用。"


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
            "聊天模式不会直接应用任何变更。请在会话中选择一个已保存的 Change Set 卡片，"
            "先核对变更范围、基线和权限，再点击显式确认；服务端只有在应用接口收到 "
            "confirm=true 时才会写入。"
        )
    if intent == "execute_guidance":
        return (
            "聊天模式不会直接触发 Action、工作流或外部副作用。请先在对应的 Action/任务界面"
            "核对目标对象、参数、影响范围、权限决策和预演结果；真正执行仍需走类型化 Action"
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
        attachment.parsed_text = str(parsed.get("text") or "")[:24000]
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
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()
    thread_id = thread.id
    assistant_message_id = uuid.uuid4().hex
    intent = _intent(payload.message, payload.mode)
    if intent == "execute_guidance":
        _save_message(
            db,
            thread,
            "assistant",
            "正在准备 Action 安全预演。",
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
        cancelled = False
        reply = ""
        proposal: dict[str, Any] = {}
        thinking: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        evidence: dict[str, Any] = {}
        action_preview: dict[str, Any] = {}
        suggestions = (
            ["创建业务场景草稿", "说明建模所需资料"]
            if not scenario
            else ["解释当前场景", "生成本体草稿", "生成数据映射草稿", "根据当前本体设计工作流"]
        )
        saved_status = "success"
        try:
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
                yield progress({"id": "action-preview", "title": "分析 Action", "detail": "正在核对目标、参数、影响和权限。", "status": "running"})
                action_preview, question, reply = _assistant_action_preview(
                    db,
                    scenario,
                    payload.message,
                    payload.selection,
                    assistant_message_id=assistant_message_id,
                )
                if question:
                    questions.append(question)
                done_event = progress({"id": "action-preview", "title": "分析 Action", "detail": "Action 分析完成；聊天未触发任何外部副作用。", "status": "done"})
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
            elif intent in ("ontology", "mapping", "workflow") and not scenario:
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
            raise
        except Exception as exc:  # noqa: BLE001
            saved_status = "error"
            error_message = f"这次助手任务没有完成：{exc}"
            questions.append({
                "id": "retry",
                "title": "需要补充或重试",
                "message": "请检查默认 LLM 配置、业务场景和附件解析状态后重试。",
                "options": [
                    {"label": "补充配置后重试", "value": "retry", "impact": "保留当前会话和附件，修正缺失配置后再次处理。", "recommended": True},
                    {"label": "仅保留说明", "value": "keep_read_only", "impact": "不生成或应用任何变更，只保留当前错误记录。"},
                ],
            })
            if not reply:
                reply = error_message
            yield _sse("progress", {"id": "error", "title": "处理未完成", "detail": "助手遇到问题，已保留当前会话。", "status": "error"})
            yield _sse("error", str(exc))
            try:
                evidence = _assistant_evidence(
                    intent,
                    proposal=proposal,
                    sources=sources,
                    llm_used=bool(llm),
                    preview=action_preview,
                    uncertainties=[str(exc)],
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
    }
    attachment_meta = [{"id": x.id, "filename": x.filename, "status": x.status} for x in attachments]
    user_message = _save_message(db, thread, "user", payload.message, context, attachment_meta)
    db.flush()

    intent = _intent(payload.message, payload.mode)
    reply = ""
    proposal: dict[str, Any] = {}
    questions: list[dict[str, Any]] = []
    action_preview: dict[str, Any] = {}
    assistant_message_id = uuid.uuid4().hex
    if intent == "execute_guidance":
        _save_message(
            db,
            thread,
            "assistant",
            "正在准备 Action 安全预演。",
            {**context, "status": "processing"},
            message_id=assistant_message_id,
        )
        db.flush()
    suggestions = (
        ["创建业务场景草稿", "说明建模所需资料"]
        if not scenario
        else ["解释当前场景", "生成本体草稿", "生成数据映射草稿", "根据当前本体设计工作流"]
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
        elif intent in ("ontology", "mapping", "workflow") and not scenario:
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
        reply = f"这次助手任务没有完成：{exc}"
        questions.append({
            "id": "retry",
            "title": "需要补充或重试",
            "message": "请检查默认 LLM 配置、业务场景和附件解析状态后重试。",
            "options": [
                {"label": "补充配置后重试", "value": "retry", "impact": "保留上下文并在修正后重新处理。", "recommended": True},
                {"label": "仅保留说明", "value": "keep_read_only", "impact": "不生成或应用任何变更。"},
            ],
        })

    evidence = _assistant_evidence(
        intent,
        proposal=proposal,
        sources=sources,
        llm_used=bool(locals().get("llm")),
        preview=action_preview,
        uncertainties=(
            [reply.removeprefix("这次助手任务没有完成：")]
            if reply.startswith("这次助手任务没有完成：")
            else []
        ),
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
                "rules", "events", "workflows",
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
