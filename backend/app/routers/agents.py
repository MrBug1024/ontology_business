"""Agent 路由：CRUD + 对话（SSE 流式）。"""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import Agent, BusinessScenario, Conversation, LLMConfig, Message
from ..schemas import (
    AgentIn,
    AgentOut,
    ChatRequest,
    ConversationOut,
    MessageOut,
    Msg,
)
from ..services import agent_engine

router = APIRouter(prefix="/agents", tags=["agents"])


def _out(a: Agent, db: Session) -> AgentOut:
    scenario = db.get(BusinessScenario, a.scenario_id) if a.scenario_id else None
    llm = db.get(LLMConfig, a.llm_config_id) if a.llm_config_id else None
    from ..models import MCPConfig, Skill

    skills = db.execute(select(Skill).where(Skill.id.in_(a.skill_ids or []))).scalars().all()
    mcps = db.execute(select(MCPConfig).where(MCPConfig.id.in_(a.mcp_ids or []))).scalars().all()
    from ..models import DataSource

    dss = db.execute(select(DataSource).where(DataSource.id.in_(a.data_source_ids or []))).scalars().all()
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


@router.get("", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db)):
    return [_out(a, db) for a in db.execute(select(Agent)).scalars().all()]


@router.post("", response_model=AgentOut)
def create_agent(payload: AgentIn, db: Session = Depends(get_db)):
    a = Agent(**payload.model_dump())
    db.add(a)
    db.commit()
    db.refresh(a)
    return _out(a, db)


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    a = db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent 不存在")
    return _out(a, db)


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentIn, db: Session = Depends(get_db)):
    a = db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent 不存在")
    for k, v in payload.model_dump().items():
        setattr(a, k, v)
    db.commit()
    db.refresh(a)
    return _out(a, db)


@router.delete("/{agent_id}", response_model=Msg)
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    a = db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent 不存在")
    db.delete(a)
    db.commit()
    return Msg(message="已删除")


# ── 对话 ──────────────────────────────────────
@router.get("/{agent_id}/conversations", response_model=list[ConversationOut])
def list_conversations(agent_id: str, db: Session = Depends(get_db)):
    return list(
        db.execute(
            select(Conversation).where(Conversation.agent_id == agent_id).order_by(Conversation.created_at.desc())
        ).scalars().all()
    )


@router.post("/{agent_id}/conversations", response_model=ConversationOut)
def create_conversation(agent_id: str, db: Session = Depends(get_db)):
    a = db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent 不存在")
    c = Conversation(agent_id=agent_id, title="新对话")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


@router.get("/conversations/{conv_id}/messages", response_model=list[MessageOut])
def list_messages(conv_id: str, db: Session = Depends(get_db)):
    return list(db.execute(select(Message).where(Message.conversation_id == conv_id)).scalars().all())


@router.delete("/conversations/{conv_id}", response_model=Msg)
def delete_conversation(conv_id: str, db: Session = Depends(get_db)):
    c = db.get(Conversation, conv_id)
    if not c:
        raise HTTPException(404, "对话不存在")
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{agent_id}/chat")
def chat(agent_id: str, payload: ChatRequest, db: Session = Depends(get_db)):
    a = db.get(Agent, agent_id)
    if not a:
        raise HTTPException(404, "Agent 不存在")
    llm = db.get(LLMConfig, a.llm_config_id) if a.llm_config_id else None
    if not llm:
        llm = db.execute(select(LLMConfig).where(LLMConfig.is_default == True).limit(1)).scalars().first()  # noqa: E712
    if not llm:
        raise HTTPException(400, "请先为 Agent 配置 LLM（或设置默认 LLM）")

    # 会话
    conv = None
    if payload.conversation_id:
        conv = db.get(Conversation, payload.conversation_id)
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
        elif m.role == "assistant" and m.content:
            history.append({"role": "assistant", "content": m.content})

    # 场景 & 本体
    scenario = db.get(BusinessScenario, a.scenario_id) if a.scenario_id else None
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
