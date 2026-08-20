"""Agent 路由：CRUD + 对话（SSE 流式）。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Agent, BusinessScenario, Conversation, DataSource, LLMConfig, MCPConfig, Message, Skill
from ..schemas import (
    AgentIn,
    AgentOut,
    ChatRequest,
    ConversationOut,
    MessageOut,
    Msg,
)
from ..services import agent_engine, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/agents", tags=["agents"])


def _out(a: Agent, db: Session) -> AgentOut:
    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    llm = tenant_service.get_visible(db, LLMConfig, a.llm_config_id) if a.llm_config_id else None
    skills = db.execute(select(Skill).where(Skill.id.in_(a.skill_ids or []), tenant_service.visible_clause(Skill, db))).scalars().all()
    mcps = db.execute(select(MCPConfig).where(MCPConfig.id.in_(a.mcp_ids or []), tenant_service.visible_clause(MCPConfig, db))).scalars().all()
    dss = db.execute(select(DataSource).where(DataSource.id.in_(a.data_source_ids or []), tenant_service.visible_clause(DataSource, db))).scalars().all()
    return AgentOut(
        id=a.id,
        name=a.name,
        description=a.description,
        scenario_id=a.scenario_id,
        llm_config_id=a.llm_config_id,
        system_prompt=a.system_prompt,
        skill_ids=a.skill_ids or [],
        mcp_ids=a.mcp_ids or [],
        data_source_ids=a.data_source_ids or [],
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
        tenant_service.require_scenario(db, scenario_id)
    if payload.llm_config_id:
        tenant_service.require_visible(db, LLMConfig, payload.llm_config_id, "绑定的 LLM 配置不存在")

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
        invalid = [
            d.name for d in sources
            if not d.is_public and d.scenario_id not in (None, scenario_id)
        ]
        if invalid:
            raise HTTPException(400, f"数据源不属于当前业务场景: {', '.join(invalid)}")


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_tenant_db)):
    return [_out(a, db) for a in db.execute(
        select(Agent).where(Agent.tenant_id == tenant_service.current_tenant_id(db))
    ).scalars().all()]


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
    a = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    return _out(a, db)


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentIn, db: Session = Depends(get_tenant_db)):
    a = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    _validate_bindings(payload, db)
    for k, v in payload.model_dump().items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return _out(a, db)


@router.delete("/{agent_id}", response_model=Msg)
def delete_agent(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    db.delete(a)
    db.commit()
    return Msg(message="已删除")


# ── 对话 ──────────────────────────────────────
@router.get("/{agent_id}/conversations", response_model=list[ConversationOut])
def list_conversations(agent_id: str, db: Session = Depends(get_tenant_db)):
    tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    return list(
        db.execute(
            select(Conversation).where(Conversation.agent_id == agent_id).order_by(Conversation.created_at.desc())
        ).scalars().all()
    )


@router.post("/{agent_id}/conversations", response_model=ConversationOut)
def create_conversation(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    c = Conversation(agent_id=agent_id, title="新对话")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
def list_messages(conv_id: str, db: Session = Depends(get_tenant_db)):
    conversation = db.execute(
        select(Conversation).join(Agent).where(
            Conversation.id == conv_id,
            Agent.tenant_id == tenant_service.current_tenant_id(db),
        )
    ).scalars().first()
    if not conversation:
        raise HTTPException(404, "对话不存在")
    return list(db.execute(select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)).scalars().all())


@router.delete("/conversations/{conv_id}", response_model=Msg)
def delete_conversation(conv_id: str, db: Session = Depends(get_tenant_db)):
    c = db.execute(
        select(Conversation).join(Agent).where(
            Conversation.id == conv_id,
            Agent.tenant_id == tenant_service.current_tenant_id(db),
        )
    ).scalars().first()
    if not c:
        raise HTTPException(404, "对话不存在")
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{agent_id}/chat")
def chat(agent_id: str, payload: ChatRequest, db: Session = Depends(get_tenant_db)):
    a = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    llm = tenant_service.get_visible(db, LLMConfig, a.llm_config_id) if a.llm_config_id else None
    if not llm:
        llm = db.execute(
            select(LLMConfig).where(
                LLMConfig.is_default == True,  # noqa: E712
                tenant_service.visible_clause(LLMConfig, db),
            ).limit(1)
        ).scalars().first()
    if not llm:
        raise HTTPException(400, "请先为 Agent 配置 LLM（或设置默认 LLM）")

    # 会话
    conv = None
    if payload.conversation_id:
        conv = db.execute(
            select(Conversation).join(Agent).where(
                Conversation.id == payload.conversation_id,
                Agent.tenant_id == tenant_service.current_tenant_id(db),
            )
        ).scalars().first()
        if not conv:
            raise HTTPException(404, "对话不存在")
        if conv.agent_id != agent_id:
            raise HTTPException(400, "对话不属于当前 Agent")
    if not conv:
        conv = Conversation(agent_id=agent_id, title=payload.message[:50] or "新对话")
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

    def event_stream():
        assistant_content = ""
        tool_calls_log: list[dict[str, Any]] = []
        tool_results_log: list[dict[str, Any]] = []
        try:
            for ev in agent_engine.run_agent(
                db, a, llm, history, payload.message, scenario_name, ontology_summary
            ):
                etype = ev["type"]
                if etype == "token":
                    assistant_content += ev["data"]
                elif etype == "tool_call":
                    tool_calls_log.append(ev["data"])
                elif etype == "tool_result":
                    tool_results_log.append(ev["data"])
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
