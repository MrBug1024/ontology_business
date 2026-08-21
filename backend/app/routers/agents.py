"""Agent 路由：CRUD + 对话（SSE 流式）。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Agent,
    BucketFile,
    BusinessScenario,
    Conversation,
    DataSource,
    DocumentChunk,
    LLMConfig,
    MCPConfig,
    Message,
    OntologyAction,
    OntologyEntity,
    OntologyRule,
    OntologyWorkflow,
    Skill,
)
from ..schemas import (
    AgentIn,
    AgentOut,
    ChatRequest,
    ConversationOut,
    MessageOut,
    Msg,
)
from ..services import agent_engine, llm_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/agents", tags=["agents"])


def _current_user_id(db: Session) -> str:
    return permission_service.require_principal(db).user_id


def _require_agent_access(db: Session, agent: Agent, *, writable: bool = False) -> Agent:
    """Agents inherit the ACL of their bound scenario.

    Agent rows are tenant-scoped, so tenant ownership alone is insufficient:
    a same-tenant user explicitly denied a scenario must not use its Agent as a
    side door to the scenario context, data sources or historic conversations.
    """
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id, writable=writable)
        permission_service.require_scenario_permission(
            db,
            scenario,
            "write" if writable else "read",
            message="没有该 Agent 所属业务场景的权限",
        )
    else:
        permission_service.require_tenant_permission(db, "write" if writable else "read")
    return agent


def _can_access_agent(db: Session, agent: Agent) -> bool:
    try:
        _require_agent_access(db, agent)
    except HTTPException:
        return False
    return True


def _agent(db: Session, agent_id: str, *, writable: bool = False) -> Agent:
    agent = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    return _require_agent_access(db, agent, writable=writable)


def _conversation(db: Session, conversation_id: str) -> Conversation:
    conversation = db.execute(
        select(Conversation)
        .join(Agent)
        .where(
            Conversation.id == conversation_id,
            Agent.tenant_id == tenant_service.current_tenant_id(db),
            # Legacy rows without an attributable creator are fail-closed.
            Conversation.created_by_user_id == _current_user_id(db),
        )
    ).scalars().first()
    if not conversation:
        raise HTTPException(404, "对话不存在")
    _require_agent_access(db, conversation.agent)
    return conversation


def _can_access_agent_data_source(db: Session, agent: Agent, source: DataSource) -> bool:
    """Keep legacy/bad bindings from leaking through Agent detail responses."""
    if source.scenario_id not in (None, agent.scenario_id):
        return False
    try:
        if source.scenario_id:
            scenario = tenant_service.require_scenario(db, source.scenario_id)
            permission_service.require_scenario_permission(db, scenario, "read")
        else:
            permission_service.require_tenant_permission(db, "read")
    except HTTPException:
        return False
    return True


def _agent_requires_tool_capability(
    db: Session,
    *,
    scenario_id: str | None,
    data_source_ids: list[str] | None,
    skill_ids: list[str] | None,
    mcp_ids: list[str] | None,
) -> bool:
    """Mirror AgentContext.build_tools before selecting a compatible LLM."""
    # Direct Skill/MCP side-effect tools are intentionally not part of the
    # Agent surface.  They therefore do not require a tool-capable model here.
    if data_source_ids:
        return True
    if not scenario_id:
        return False
    for model in (OntologyEntity, OntologyAction, OntologyRule, OntologyWorkflow):
        if db.execute(
            select(model.id).where(model.scenario_id == scenario_id).limit(1)
        ).scalar_one_or_none():
            return True
    return False


def _can_read_historic_citation(db: Session, agent: Agent, citation: object) -> bool:
    """Re-authorize persisted citations before returning a historic message.

    Citation text is persisted in the message for auditability, but persistence
    must never turn a formerly accessible source into a permanent disclosure.
    A revoked/deleted source, removed Agent binding, missing file or a replaced
    chunk makes the entire cited answer unavailable to the current requester.
    """
    if not isinstance(citation, dict):
        return False
    source_id = str(citation.get("data_source_id") or "")
    file_id = str(citation.get("file_id") or "")
    chunk_id = str(citation.get("chunk_id") or "")
    if not source_id or source_id not in set(agent.data_source_ids or []):
        return False
    source = tenant_service.get_visible(db, DataSource, source_id)
    if not source or source.type != "file_bucket" or not _can_access_agent_data_source(db, agent, source):
        return False
    bucket_file = db.get(BucketFile, file_id)
    if not bucket_file or bucket_file.data_source_id != source.id:
        return False
    if chunk_id:
        chunk = db.get(DocumentChunk, chunk_id)
        if not chunk or chunk.bucket_file_id != bucket_file.id or chunk.data_source_id != source.id:
            return False
        expected_hash = str(citation.get("content_hash") or "")
        if expected_hash and chunk.content_hash != expected_hash:
            return False
    else:
        # Full-document reads are versioned at file level rather than at an
        # individual chunk.  A reparse/reindex must invalidate the historic
        # excerpt just as surely as a revoked source does.
        expected_file_hash = str(
            citation.get("file_content_hash") or citation.get("content_hash") or ""
        )
        if expected_file_hash and bucket_file.indexed_content_hash != expected_file_hash:
            return False
    return True


def _has_legacy_uncited_document_read(message: Message) -> bool:
    """Old messages may have persisted full-document tool output without a citation."""
    if message.citations or not message.tool_results:
        return False
    for call in message.tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(call.get("name") or function.get("name") or "")
        if name == "read_document":
            return True
    return False


def _message_out(db: Session, message: Message, agent: Agent) -> MessageOut:
    citations = message.citations if isinstance(message.citations, list) else []
    if (
        (citations and not all(_can_read_historic_citation(db, agent, item) for item in citations))
        or _has_legacy_uncited_document_read(message)
    ):
        # Content and tool results can repeat the quoted source material, so
        # hiding only the citation card would still leak revoked information.
        return MessageOut(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,
            content="该历史回答引用的资料已不在当前访问范围，内容已隐藏。",
            tool_calls=[],
            tool_results=[],
            citations=[],
            created_at=message.created_at,
        )
    return MessageOut.model_validate(message)


def _out(a: Agent, db: Session) -> AgentOut:
    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    llm = tenant_service.get_visible(db, LLMConfig, a.llm_config_id) if a.llm_config_id else None
    skills = db.execute(select(Skill).where(Skill.id.in_(a.skill_ids or []), tenant_service.visible_clause(Skill, db))).scalars().all()
    mcps = db.execute(select(MCPConfig).where(MCPConfig.id.in_(a.mcp_ids or []), tenant_service.visible_clause(MCPConfig, db))).scalars().all()
    dss = db.execute(
        select(DataSource).where(
            DataSource.id.in_(a.data_source_ids or []),
            tenant_service.visible_clause(DataSource, db),
        )
    ).scalars().all()
    dss = [source for source in dss if _can_access_agent_data_source(db, a, source)]
    return AgentOut(
        id=a.id,
        name=a.name,
        description=a.description,
        scenario_id=a.scenario_id,
        llm_config_id=a.llm_config_id,
        system_prompt=a.system_prompt,
        skill_ids=a.skill_ids or [],
        mcp_ids=a.mcp_ids or [],
        data_source_ids=[source.id for source in dss],
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        created_at=a.created_at,
        updated_at=a.updated_at,
        scenario_name=scenario.name if scenario else "",
        llm_name=llm.name if llm else "",
        skill_names=[s.name for s in skills],
        mcp_names=[m.name for m in mcps],
        data_source_names=[d.name for d in dss],
    )


def _validate_bindings(payload: AgentIn, db: Session) -> None:
    """保证 Agent 只能绑定存在且属于当前场景的资源。"""
    scenario_id = payload.scenario_id
    if scenario_id:
        scenario = tenant_service.require_scenario(db, scenario_id, writable=True)
        permission_service.require_scenario_permission(db, scenario, "write")
    else:
        permission_service.require_tenant_permission(db, "write")
    if payload.llm_config_id:
        llm = tenant_service.require_visible(
            db, LLMConfig, payload.llm_config_id, "绑定的 LLM 配置不存在"
        )
        if not llm_service.supports_capability(llm, "chat"):
            raise HTTPException(400, "绑定的 LLM 配置未启用聊天能力或当前不可用")
        if _agent_requires_tool_capability(
            db,
            scenario_id=scenario_id,
            data_source_ids=payload.data_source_ids,
            skill_ids=payload.skill_ids,
            mcp_ids=payload.mcp_ids,
        ) and not llm_service.supports_capability(llm, "tool"):
            raise HTTPException(400, "该 Agent 需要工具调用，请绑定启用工具能力的 LLM 配置")

    skills = set(payload.skill_ids or [])
    if skills:
        found = set(db.scalars(select(Skill.id).where(Skill.id.in_(skills), tenant_service.visible_clause(Skill, db))).all())
        if found != skills:
            raise HTTPException(400, "绑定的技能中存在不存在或已删除的资源")
    mcps = set(payload.mcp_ids or [])
    if mcps:
        found = set(db.scalars(select(MCPConfig.id).where(MCPConfig.id.in_(mcps), tenant_service.visible_clause(MCPConfig, db))).all())
        if found != mcps:
            raise HTTPException(400, "绑定的 MCP 服务中存在不存在的资源")
    ds_ids = set(payload.data_source_ids or [])
    if ds_ids:
        sources = db.scalars(select(DataSource).where(DataSource.id.in_(ds_ids), tenant_service.visible_clause(DataSource, db))).all()
        if len(sources) != len(ds_ids):
            raise HTTPException(400, "绑定的数据源中存在不存在的资源")
        invalid: list[str] = []
        for source in sources:
            try:
                if source.scenario_id:
                    source_scenario = tenant_service.require_scenario(db, source.scenario_id)
                    permission_service.require_scenario_permission(db, source_scenario, "read")
                else:
                    permission_service.require_tenant_permission(db, "read")
            except HTTPException:
                invalid.append(source.name)
                continue
            if source.scenario_id is not None and source.scenario_id != scenario_id:
                invalid.append(source.name)
        if invalid:
            raise HTTPException(400, f"数据源不属于当前业务场景: {', '.join(invalid)}")


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_tenant_db)):
    return [
        _out(a, db)
        for a in db.execute(
            select(Agent).where(Agent.tenant_id == tenant_service.current_tenant_id(db))
        ).scalars().all()
        if _can_access_agent(db, a)
    ]


@router.post("", response_model=AgentOut)
def create_agent(payload: AgentIn, db: Session = Depends(get_tenant_db)):
    _validate_bindings(payload, db)
    a = Agent(tenant_id=tenant_service.current_tenant_id(db), **payload.model_dump())
    db.add(a)
    db.commit()
    db.refresh(a)
    return _out(a, db)


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id)
    return _out(a, db)


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentIn, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id, writable=True)
    _validate_bindings(payload, db)
    for k, v in payload.model_dump().items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return _out(a, db)


@router.delete("/{agent_id}", response_model=Msg)
def delete_agent(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id, writable=True)
    db.delete(a)
    db.commit()
    return Msg(message="已删除")


# ── 对话 ──────────────────────────────────────
@router.get("/{agent_id}/conversations", response_model=list[ConversationOut])
def list_conversations(agent_id: str, db: Session = Depends(get_tenant_db)):
    _agent(db, agent_id)
    return list(
        db.execute(
            select(Conversation)
            .where(
                Conversation.agent_id == agent_id,
                Conversation.created_by_user_id == _current_user_id(db),
            )
            .order_by(Conversation.created_at.desc())
        ).scalars().all()
    )


@router.post("/{agent_id}/conversations", response_model=ConversationOut)
def create_conversation(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id)
    c = Conversation(
        agent_id=agent_id,
        created_by_user_id=_current_user_id(db),
        title="新对话",
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
def list_messages(conv_id: str, db: Session = Depends(get_tenant_db)):
    conversation = _conversation(db, conv_id)
    return [
        _message_out(db, message, conversation.agent)
        for message in db.execute(
            select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
        ).scalars().all()
    ]


@router.delete("/conversations/{conv_id}", response_model=Msg)
def delete_conversation(conv_id: str, db: Session = Depends(get_tenant_db)):
    # A transcript belongs to its creator, not to the collaborative Agent
    # definition.  Read access to the Agent is enough to erase one's own
    # private context; scenario write permission is not required.
    c = _conversation(db, conv_id)
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{agent_id}/chat")
def chat(agent_id: str, payload: ChatRequest, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id)
    # Resolve a supplied transcript before model routing so an inaccessible
    # conversation is never masked by (or able to influence) LLM fallback.
    conv = None
    if payload.conversation_id:
        conv = _conversation(db, payload.conversation_id)
        if conv.agent_id != agent_id:
            raise HTTPException(400, "对话不属于当前 Agent")
    requires_tools = _agent_requires_tool_capability(
        db,
        scenario_id=a.scenario_id,
        data_source_ids=a.data_source_ids,
        skill_ids=a.skill_ids,
        mcp_ids=a.mcp_ids,
    )
    if a.llm_config_id:
        llm = tenant_service.get_visible(db, LLMConfig, a.llm_config_id)
        if not llm or not llm_service.supports_capability(llm, "chat"):
            raise HTTPException(409, "Agent 绑定的 LLM 不可用或未启用聊天能力，请重新配置")
        if requires_tools and not llm_service.supports_capability(llm, "tool"):
            raise HTTPException(409, "Agent 需要工具调用，但绑定的 LLM 未启用工具能力")
    else:
        candidates = llm_service.routable_configs(db, "tool" if requires_tools else "chat")
        llm = candidates[0] if candidates else None
    if not llm:
        raise HTTPException(400, "请先为 Agent 配置 LLM（或设置默认 LLM）")

    if not conv:
        conv = Conversation(
            agent_id=agent_id,
            created_by_user_id=_current_user_id(db),
            title=payload.message[:50] or "新对话",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    # 历史
    history_msgs = db.execute(
        select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
    ).scalars().all()
    history: list[dict[str, Any]] = []
    for m in history_msgs:
        if m.role == "user":
            history.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            if m.tool_calls and m.tool_results:
                calls = [
                    {
                        "id": call.get("id"),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": (
                                call.get("arguments", "")
                                if isinstance(call.get("arguments"), str)
                                else json.dumps(call.get("arguments", {}), ensure_ascii=False)
                            ),
                        },
                    }
                    for call in m.tool_calls
                    if call.get("id")
                ]
                result_map = {result.get("id"): result for result in m.tool_results if result.get("id")}
                if calls and all(call["id"] in result_map for call in calls):
                    history.append({"role": "assistant", "content": m.content, "tool_calls": calls})
                    for call in calls:
                        result = result_map[call["id"]]
                        history.append(
                            {
                                "role": "tool",
                                "tool_call_id": result["id"],
                                "name": result.get("name", ""),
                                "content": result.get("result", ""),
                            }
                        )
                    continue
            if m.content:
                history.append({"role": "assistant", "content": m.content})

    # 场景 & 本体
    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    scenario_name = scenario.name if scenario else ""
    ontology_summary = agent_engine.ontology_summary_for(scenario)

    # 保存用户消息
    db.add(Message(conversation_id=conv.id, role="user", content=payload.message))
    db.commit()

    conv_id = conv.id
    trace_context = {
        "correlation_id": uuid.uuid4().hex,
        "agent_id": a.id,
        "conversation_id": conv_id,
        "scenario_id": a.scenario_id or "",
        "user_id": str(db.info.get("user_id") or "") or None,
    }

    def event_stream():
        assistant_content = ""
        tool_calls_log: list[dict[str, Any]] = []
        tool_results_log: list[dict[str, Any]] = []
        citations_log: list[dict[str, Any]] = []
        try:
            for ev in agent_engine.run_agent(
                db,
                a,
                llm,
                history,
                payload.message,
                scenario_name,
                ontology_summary,
                trace_context=trace_context,
            ):
                etype = ev["type"]
                if etype == "token":
                    assistant_content += ev["data"]
                elif etype == "tool_call":
                    tool_calls_log.append(ev["data"])
                elif etype == "tool_result":
                    tool_results_log.append(ev["data"])
                elif etype == "citations":
                    # 引用由 AgentContext 在当前租户、绑定数据源范围内生成；作为
                    # 独立字段持久化，历史消息无需再从工具文本中反向解析。
                    citations_log = ev["data"] if isinstance(ev["data"], list) else []
                yield f"data: {json.dumps({'type': etype, 'data': ev['data']}, ensure_ascii=False)}\n\n"
            # 保存助手消息（用独立会话，避免请求作用域 db 在流式期间被关闭）
            save_db = SessionLocal()
            try:
                save_db.add(
                    Message(
                        conversation_id=conv_id,
                        role="assistant",
                        content=assistant_content,
                        tool_calls=tool_calls_log,
                        tool_results=tool_results_log,
                        citations=citations_log,
                    )
                )
                save_db.commit()
            finally:
                save_db.close()
        except Exception as exc:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'data': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
