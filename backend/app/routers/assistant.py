"""全局 AI 助手：跨页面上下文、临时附件、草稿生成与确认应用。"""
from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import SessionLocal
from ..models import (
    AssistantAttachment,
    AssistantAuditLog,
    AssistantMessage,
    AssistantThread,
    BusinessScenario,
    BucketFile,
    DataSource,
    DocumentChunk,
    LLMConfig,
    OntologyWorkflow,
)
from ..schemas import (
    AssistantAttachmentOut,
    AssistantChatRequest,
    AssistantMessageOut,
    AssistantProposalApplyRequest,
    AssistantReplyOut,
    AssistantThreadOut,
    Msg,
)
from ..services import (
    doc_parser,
    llm_service,
    ontology_service,
    permission_service,
    rag_service,
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


def _scenario_snapshot(scenario: BusinessScenario) -> dict[str, Any]:
    """生成提案的轻量基线，防止用户确认前场景已被其他操作改写。"""
    return {
        "entity_names": sorted(str(entity.name) for entity in scenario.entities),
        "relation_names": sorted(str(relation.name) for relation in scenario.relations),
        "workflow_names": sorted(str(workflow.name) for workflow in scenario.workflows),
    }


def _build_proposal(kind: str, data: dict[str, Any], scenario: BusinessScenario) -> dict[str, Any]:
    """将生成结果包装成可审计、可确认的 Change Set。"""
    snapshot = _scenario_snapshot(scenario)
    changes: list[dict[str, Any]] = []
    if kind == "ontology":
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
    else:
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
    return AssistantMessageOut.model_validate(message)


def _intent(message: str, mode: str) -> str:
    text = message.lower()
    if any(k in text for k in ("工作流", "流程", "编排", "审批流", "自动化")):
        return "workflow"
    if mode == "draft" or any(k in text for k in ("本体", "实体", "关系", "建模", "数据模型", "对象类型")):
        return "ontology"
    return "chat"


def _fallback_reply(intent: str, scenario: BusinessScenario | None) -> str:
    if intent == "ontology":
        return "我可以根据业务描述生成本体草稿。请先配置一个默认 LLM，并补充业务目标、核心对象、关键关系或上传业务资料。"
    if intent == "workflow":
        return "我可以根据业务描述生成工作流草稿。请先配置一个默认 LLM，并说明触发条件、判断规则、动作和最终结果。"
    if scenario:
        return f"当前上下文是「{scenario.name}」。我可以协助你查询本体、解释对象关系，或生成建模与流程草稿。请先配置默认 LLM 以启用自然语言推理。"
    return "我可以协助你理解平台、设计业务场景和生成本体草稿。打开一个业务场景或配置默认 LLM 后，我可以提供更具体的帮助。"


def _safe_attachment_ids(db: Session, ids: list[str]) -> list[AssistantAttachment]:
    if not ids:
        return []
    return list(
        db.execute(
            select(AssistantAttachment).where(
                AssistantAttachment.id.in_(ids),
                AssistantAttachment.tenant_id == _tenant(db),
                # Like threads, legacy attachment rows without a demonstrable
                # owner are fail-closed rather than shared across a tenant.
                AssistantAttachment.created_by_user_id == _current_user_id(db),
            )
        ).scalars().all()
    )


def _save_message(
    db: Session,
    thread: AssistantThread,
    role: str,
    content: str,
    context: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    proposal: dict[str, Any] | None = None,
    thinking: list[dict[str, Any]] | None = None,
) -> AssistantMessage:
    thread.updated_at = datetime.now(timezone.utc)
    message = AssistantMessage(
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

    attachments = _safe_attachment_ids(db, payload.attachment_ids)
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
    tenant_id = _tenant(db)
    user_id = str(db.info.get("user_id") or "")
    db.commit()

    intent = _intent(payload.message, payload.mode)
    llm = _llm(db)
    history = _history_messages(db, thread, user_message.id)
    llm_messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                "你是本体智能平台的全局 AI 助手。你必须区分事实、推测和待确认项；"
                "不直接修改数据，不绕过权限，不把 SQL 当作业务本体。回答简洁、可执行，"
                "必要时用问题卡片澄清。\n\n"
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

    def persist_result(reply: str, proposal: dict[str, Any], thinking: list[dict[str, Any]], status: str) -> None:
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
            _save_message(save_db, saved_thread, "assistant", reply, context, sources, proposal, thinking)
            save_db.add(
                AssistantAuditLog(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    scenario_id=payload.scenario_id,
                    thread_id=thread_id,
                    operation="propose" if proposal else "chat",
                    status=status,
                    context=context,
                    result={"intent": intent, "sources": sources, "proposal_kind": proposal.get("kind", "")},
                )
            )
            save_db.commit()
        finally:
            save_db.close()

    def event_stream():
        reply = ""
        proposal: dict[str, Any] = {}
        thinking: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        suggestions = ["解释当前场景", "生成本体草稿", "根据当前本体设计工作流"]
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

            if intent in ("ontology", "workflow") and not scenario:
                questions.append({
                    "id": "scenario",
                    "title": "需要一个业务场景",
                    "message": "请先打开或创建业务场景，我才能把草稿安全地放入对应的本体工作区。",
                })
                reply = "我可以继续协助，但需要先知道这次建模属于哪个业务场景。"
                yield progress({"id": "clarify", "title": "确认业务范围", "detail": "当前请求需要绑定到一个具体业务场景。", "status": "done"})
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

            persist_result(reply, proposal, thinking, saved_status)
            yield _sse("meta", {
                "thread_id": thread_id,
                "proposal": proposal,
                "questions": questions,
                "suggestions": suggestions,
                "sources": sources,
                "thinking": thinking,
            })
            yield _sse("done", {"thread_id": thread_id})
        except Exception as exc:  # noqa: BLE001
            saved_status = "error"
            error_message = f"这次助手任务没有完成：{exc}"
            questions.append({
                "id": "retry",
                "title": "需要补充或重试",
                "message": "请检查默认 LLM 配置、业务场景和附件解析状态后重试。",
            })
            if not reply:
                reply = error_message
            yield _sse("progress", {"id": "error", "title": "处理未完成", "detail": "助手遇到问题，已保留当前会话。", "status": "error"})
            yield _sse("error", str(exc))
            try:
                persist_result(reply, proposal, thinking, saved_status)
            except Exception:
                pass
        finally:
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

    attachments = _safe_attachment_ids(db, payload.attachment_ids)
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
    suggestions = ["解释当前场景", "生成本体草稿", "根据当前本体设计工作流"]

    try:
        if intent in ("ontology", "workflow") and not scenario:
            questions.append({
                "id": "scenario",
                "title": "需要一个业务场景",
                "message": "请先打开或创建业务场景，我才能把草稿安全地放入对应的本体工作区。",
            })
            reply = "我可以继续协助，但需要先知道这次建模属于哪个业务场景。"
        elif intent == "ontology" and scenario:
            description = payload.message
            if attachment_text:
                description += f"\n\n参考附件内容：\n{attachment_text}"
            if rag_context:
                description += f"\n\n已授权资料依据：\n{rag_context}"
            data = ontology_service.generate_ontology(db, scenario, description)
            proposal = _build_proposal("ontology", data, scenario)
            reply = "我已经根据当前场景和附件生成了本体草稿。请检查变更内容，确认后再应用到场景。"
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
        })

    _save_message(db, thread, "assistant", reply, context, sources, proposal)
    db.add(
        AssistantAuditLog(
            tenant_id=_tenant(db),
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=payload.scenario_id,
            thread_id=thread.id,
            operation="propose" if proposal else "chat",
            status="success" if not questions or proposal else "needs_input",
            context=context,
            result={"intent": intent, "sources": sources, "proposal_kind": proposal.get("kind", "")},
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
    )


@router.post("/proposals/apply")
def apply_proposal(payload: AssistantProposalApplyRequest, db: Session = Depends(get_tenant_db)):
    if not payload.confirm:
        raise HTTPException(409, "应用变更必须显式确认")
    scenario = _scenario(db, payload.scenario_id, writable=True)
    thread, proposal_message, saved_proposal = _find_saved_proposal(db, payload.thread_id, payload.proposal_id)
    if thread.scenario_id != scenario.id:
        raise HTTPException(409, "变更草稿与当前业务场景不一致")
    if saved_proposal.get("kind") != payload.kind:
        raise HTTPException(409, "变更草稿类型与请求不一致")
    if saved_proposal.get("status") == "applied":
        return {
            "ok": True,
            "status": "replayed",
            "message": "该变更草稿已经应用过，已返回原应用结果",
            "data": saved_proposal.get("apply_result") or {},
        }
    expected_snapshot = saved_proposal.get("base_snapshot") or {}
    if expected_snapshot and expected_snapshot != _scenario_snapshot(scenario):
        raise HTTPException(409, "场景在确认前已发生变化，请重新生成变更草稿")

    kind = payload.kind
    data = saved_proposal.get("payload") or {}
    result: dict[str, Any]

    try:
        if kind == "ontology":
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
        else:
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
    except Exception:
        db.rollback()
        raise

    updated_proposal = dict(saved_proposal)
    updated_proposal["status"] = "applied"
    updated_proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    updated_proposal["apply_result"] = result
    proposal_message.proposal = updated_proposal
    db.add(
        AssistantAuditLog(
            tenant_id=_tenant(db),
            user_id=str(db.info.get("user_id") or ""),
            scenario_id=scenario.id,
            thread_id=thread.id if thread else None,
            operation="apply_proposal",
            status="success",
            context={"kind": kind, "proposal_id": payload.proposal_id, "confirmed": True},
            result=result,
        )
    )
    db.commit()
    return {"ok": True, "message": "变更草稿已应用到场景草稿", "data": result}
