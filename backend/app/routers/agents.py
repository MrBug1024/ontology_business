"""Agent 路由：CRUD + 对话（SSE 流式）。"""
from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Agent,
    BucketFile,
    BusinessScenario,
    Conversation,
    DataMapping,
    DataSource,
    DocumentChunk,
    LLMConfig,
    Message,
    OntologyEntity,
)
from ..schemas import (
    AgentIn,
    AgentOut,
    AgentToolConfirmationRequest,
    ChatRequest,
    ConversationOut,
    MessageOut,
    Msg,
)
from ..services import (
    agent_capability_service,
    agent_confirmation_service,
    agent_engine,
    llm_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/agents", tags=["agents"])

_HISTORIC_MODEL_REPLAY_PLACEHOLDER = (
    "此前回答保留在会话记录中，但其数据快照未参与本轮推理。"
)


def _stream_error_content(content: str, error: object) -> str:
    """Match the browser's durable rendering for a failed SSE turn."""
    separator = "\n\n" if content else ""
    return f"{content}{separator}[错误] {error}"


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
    capability_scope: object,
) -> bool:
    """Mirror AgentContext.build_tools before selecting a compatible LLM."""
    # Direct Skill/MCP side-effect tools are intentionally not part of the
    # Agent surface.  They therefore do not require a tool-capable model here.
    if data_source_ids:
        return True
    if not scenario_id:
        return False
    # Ontology and mapped-data reads stay available even when the business
    # capability allow-list is empty.
    for model in (OntologyEntity, DataMapping):
        if db.execute(
            select(model.id).where(model.scenario_id == scenario_id).limit(1)
        ).scalar_one_or_none():
            return True
    return agent_capability_service.scope_has_business_tools(capability_scope)


def _agent_readiness_missing(
    db: Session,
    agent: Agent,
    *,
    runtime_context: agent_engine.AgentContext | None = None,
) -> list[str]:
    """Return business-facing prerequisites that still block Agent chat.

    CRUD deliberately accepts incomplete Agents as drafts.  The chat endpoint
    enforces the same minimum chain as the UI so it cannot be bypassed by a
    direct API call.
    """
    missing: list[str] = []
    if not agent.scenario_id:
        return ["业务场景", "对象类型", "数据源", "数据映射", "对话模型", "映射数据绑定"]

    has_entity = (
        bool(runtime_context.entities)
        if runtime_context is not None
        else bool(db.execute(
            select(OntologyEntity.id)
            .where(OntologyEntity.scenario_id == agent.scenario_id)
            .limit(1)
        ).scalar_one_or_none())
    )
    if not has_entity:
        missing.append("对象类型")

    has_source = (
        bool(runtime_context.data_sources)
        if runtime_context is not None
        else bool(db.execute(
            select(DataSource.id)
            .where(
                tenant_service.visible_clause(DataSource, db),
                or_(
                    DataSource.scenario_id.is_(None),
                    DataSource.scenario_id == agent.scenario_id,
                ),
            )
            .limit(1)
        ).scalar_one_or_none())
    )
    if not has_source:
        missing.append("数据源")

    if runtime_context is not None:
        definition_mappings = (
            runtime_context.runtime_definition.mappings
            if runtime_context.runtime_definition is not None else {}
        )
        has_mapping = bool(definition_mappings)
        has_bound_mapping = bool(runtime_context.mappings)
    else:
        mapped_source_ids = set(db.scalars(
            select(DataMapping.data_source_id).where(
                DataMapping.scenario_id == agent.scenario_id
            )
        ).all())
        has_mapping = bool(mapped_source_ids)
        has_bound_mapping = bool(mapped_source_ids.intersection(agent.data_source_ids or []))
    if not has_mapping:
        missing.append("数据映射")
    if not agent.llm_config_id:
        missing.append("对话模型")
    if not has_bound_mapping:
        missing.append("映射数据绑定")
    return missing


def _can_read_historic_citation(db: Session, agent: Agent, citation: object) -> bool:
    """Re-authorize persisted citations before replaying them to a model.

    The creator-owned UI transcript is an immutable answer-time record. A
    revoked/deleted source, removed Agent binding, missing file or replaced
    chunk only prevents its raw snapshot from being sent to a later model turn.
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
        if name in {"read_document", "search_documents"}:
            return True
    return False


def _tool_call_name_args(call: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(call, dict):
        return "", {}
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(call.get("name") or function.get("name") or "")
    raw_args = call.get("args", call.get("arguments", function.get("arguments", {})))
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            return name, {}
    return name, dict(raw_args) if isinstance(raw_args, dict) else {}


def _authorization_context(
    db: Session,
    agent: Agent,
    llm: LLMConfig | None = None,
) -> agent_engine.AgentContext | None:
    """Build the exact current runtime/ACL view used for historic replay."""
    try:
        return agent_engine.AgentContext(
            db,
            agent,
            llm or LLMConfig(name="历史权限校验"),
        )
    except Exception:  # noqa: BLE001 - missing release/binding must fail closed.
        return None


def _historic_tool_results_authorized(
    message: Message,
    context: agent_engine.AgentContext | None,
) -> bool:
    calls = [call for call in (message.tool_calls or []) if isinstance(call, dict)]
    results = [result for result in (message.tool_results or []) if isinstance(result, dict)]
    if not calls and not results:
        return True
    if not calls and results:
        # Earliest citation-backed rows persisted only the retrieved text. They
        # remain safe solely while every citation is independently current.
        return bool(message.citations) and all(
            not result.get("id") and not result.get("name") for result in results
        )
    if context is None or not calls or not results:
        return False
    by_id = {
        str(result.get("id") or ""): result
        for result in results
        if str(result.get("id") or "")
    }
    if len(by_id) != len(results):
        return False
    seen: set[str] = set()
    for call in calls:
        call_id = str(call.get("id") or "")
        if not call_id or call_id not in by_id:
            return False
        name, args = _tool_call_name_args(call)
        result = by_id[call_id]
        if not name or str(result.get("name") or name) != name:
            return False
        if not context.authorize_historic_tool_result(name, args, result.get("result")):
            return False
        seen.add(call_id)
    return seen == set(by_id)


def _message_out_without_tool_details(message: Message) -> MessageOut:
    """Build a model-replay view without stale raw tool payloads.

    This helper is deliberately *not* used by the message-list endpoint. The
    creator-owned transcript must preserve its answer-time snapshot, including
    tool cards, attachments and citations. Sending raw results to an external
    model on a later turn is a separate trust boundary and is re-authorized.
    """
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=message.role,
        content=_HISTORIC_MODEL_REPLAY_PLACEHOLDER,
        tool_calls=[],
        tool_results=[],
        citations=[],
        created_at=message.created_at,
    )


def _message_out_for_model_replay(
    db: Session,
    message: Message,
    agent: Agent,
    *,
    context: agent_engine.AgentContext | None = None,
) -> MessageOut:
    citations = message.citations if isinstance(message.citations, list) else []
    authorization_context = (
        context if context is not None else _authorization_context(db, agent)
    )
    runtime_source_ids = {
        source.id for source in authorization_context.data_sources
    } if authorization_context is not None else set()
    if (
        (
            citations
            and (
                authorization_context is None
                or not all(
                    isinstance(item, dict)
                    and str(item.get("data_source_id") or "") in runtime_source_ids
                    and _can_read_historic_citation(db, agent, item)
                    for item in citations
                )
            )
        )
        or _has_legacy_uncited_document_read(message)
        or not _historic_tool_results_authorized(
            message,
            authorization_context,
        )
    ):
        # The UI still receives the durable answer-time snapshot. The external
        # model receives only a neutral continuity marker here: the answer body
        # itself may repeat raw rows, so it must be withheld together with tool
        # arguments/results and citation excerpts after authorization fails.
        return _message_out_without_tool_details(message)
    return MessageOut.model_validate(message)


def _model_history(
    db: Session,
    conversation_id: str,
    agent: Agent,
    context: agent_engine.AgentContext,
    *,
    excluded_message_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Re-authorize transcript data with the same context used for this turn."""
    statement = select(Message).where(Message.conversation_id == conversation_id)
    excluded = {str(item) for item in (excluded_message_ids or set()) if str(item)}
    if excluded:
        statement = statement.where(Message.id.notin_(excluded))
    history_msgs = db.execute(
        statement.order_by(Message.created_at, Message.id)
    ).scalars().all()
    history: list[dict[str, Any]] = []
    for message in history_msgs:
        if message.role == "user":
            history.append({"role": "user", "content": message.content})
            continue
        if message.role != "assistant":
            continue
        safe_message = _message_out_for_model_replay(
            db,
            message,
            agent,
            context=context,
        )
        # Only replay raw tools when calls and results still form one fully
        # authorized chain. A stripped message contributes at most its neutral
        # continuity marker.
        if safe_message.tool_calls and safe_message.tool_results:
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
                for call in safe_message.tool_calls
                if call.get("id")
            ]
            result_map = {
                result.get("id"): result
                for result in safe_message.tool_results
                if result.get("id")
            }
            if calls and all(call["id"] in result_map for call in calls):
                history.append({
                    "role": "assistant",
                    "content": safe_message.content,
                    "tool_calls": calls,
                })
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
        if safe_message.content:
            history.append({"role": "assistant", "content": safe_message.content})
    return history


def _out(a: Agent, db: Session) -> AgentOut:
    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    llm = tenant_service.get_visible(db, LLMConfig, a.llm_config_id) if a.llm_config_id else None
    dss = db.execute(
        select(DataSource).where(
            DataSource.id.in_(a.data_source_ids or []),
            tenant_service.visible_clause(DataSource, db),
        )
    ).scalars().all()
    dss = [source for source in dss if _can_access_agent_data_source(db, a, source)]
    definition = None
    definition_error = ""
    if scenario is None:
        definition_error = "尚未绑定业务场景"
    else:
        try:
            definition = runtime_definition_service.resolve_active(
                db,
                scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
        except (runtime_definition_service.RuntimeDefinitionError, ValueError) as exc:
            definition_error = str(exc) or "当前环境运行定义不可用"
    if a.capability_scope is None and definition is not None:
        # Pre-scope Agents historically saw every capability that was visible
        # in their scenario.  Project that legacy grant as the *current*
        # explicit ids so the editor and runtime agree.  A later save persists
        # this frozen snapshot instead of silently clearing the Agent or
        # dynamically granting capabilities created in the future.
        try:
            capability_scope = agent_capability_service.validate_scope(
                db,
                agent_capability_service.legacy_all_scope(),
                definition=definition,
            )
        except agent_capability_service.AgentCapabilityScopeError as exc:
            capability_scope = agent_capability_service.explicit_empty_scope()
            definition_error = definition_error or str(exc)
    else:
        capability_scope = agent_capability_service.normalize_scope(
            a.capability_scope,
            legacy_default=False,
        )
    return AgentOut(
        id=a.id,
        name=a.name,
        description=a.description,
        scenario_id=a.scenario_id,
        llm_config_id=a.llm_config_id,
        system_prompt=a.system_prompt,
        data_source_ids=[source.id for source in dss],
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        created_at=a.created_at,
        updated_at=a.updated_at,
        scenario_name=scenario.name if scenario else "",
        llm_name=llm.name if llm else "",
        data_source_names=[d.name for d in dss],
        capability_scope=capability_scope,
        capability_scope_legacy=a.capability_scope is None,
        capability_summary=agent_capability_service.capability_summary(
            db,
            capability_scope,
            definition=definition,
            definition_error=definition_error,
        ),
    )


def _validate_bindings(
    payload: AgentIn,
    db: Session,
    *,
    capability_scope: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """保证 Agent 只能绑定存在且属于当前场景的资源。"""
    scenario_id = payload.scenario_id
    scenario = None
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
            capability_scope=capability_scope,
        ) and not llm_service.supports_capability(llm, "tool"):
            raise HTTPException(400, "该 Agent 需要工具调用，请绑定启用工具能力的 LLM 配置")

    needs_capability_definition = agent_capability_service.scope_has_business_tools(
        capability_scope
    )
    try:
        if scenario is None:
            capability_scope = agent_capability_service.validate_scope(
                db,
                capability_scope,
                definition=None,
            )
        elif needs_capability_definition:
            definition = runtime_definition_service.resolve_active(
                db,
                scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
            capability_scope = agent_capability_service.validate_scope(
                db,
                capability_scope,
                definition=definition,
            )
    except agent_capability_service.AgentCapabilityScopeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (runtime_definition_service.RuntimeDefinitionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc

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
    return capability_scope


@router.get("/capability-catalog/{scenario_id}")
def get_agent_capability_catalog(
    scenario_id: str,
    db: Session = Depends(get_tenant_db),
):
    """Return only capabilities readable by the current principal.

    The client uses this governed catalog to render labels and readiness; it
    never submits arbitrary JSON or treats a hidden resource id as selectable.
    """
    scenario = tenant_service.require_scenario(db, scenario_id)
    permission_service.require_scenario_permission(db, scenario, "read")
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
    except (runtime_definition_service.RuntimeDefinitionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return {
        "scenario_id": scenario.id,
        "environment": definition.environment,
        "definition_hash": definition.definition_hash,
        "categories": agent_capability_service.catalog_summary(db, definition),
    }


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
    capability_scope = agent_capability_service.normalize_scope(
        payload.capability_scope,
        legacy_default=False,
        allow_all=True,
    )
    capability_scope = _validate_bindings(
        payload, db, capability_scope=capability_scope
    )
    a = Agent(
        tenant_id=tenant_service.current_tenant_id(db),
        **payload.model_dump(exclude={"capability_scope"}),
        capability_scope=capability_scope,
    )
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
    scenario_changed = payload.scenario_id != a.scenario_id
    if payload.capability_scope is not None:
        capability_scope = agent_capability_service.normalize_scope(
            payload.capability_scope,
            legacy_default=False,
            allow_all=True,
        )
        stored_scope: dict[str, dict[str, Any]] | None = capability_scope
    elif scenario_changed:
        # A capability id is meaningful only inside its original scenario.
        # Switching scenarios without an explicit replacement always clears it.
        capability_scope = agent_capability_service.explicit_empty_scope()
        stored_scope = capability_scope
    else:
        # Preserve the behaviour of a pre-scope Agent during an unrelated
        # edit, then freeze the current ACL-filtered catalog to explicit ids.
        # This avoids both "edit => lose every capability" and future dynamic
        # grants.  Agents without a scenario remain explicitly empty.
        capability_scope = (
            agent_capability_service.legacy_all_scope()
            if a.capability_scope is None and a.scenario_id
            else agent_capability_service.normalize_scope(
                a.capability_scope,
                legacy_default=False,
            )
        )
        stored_scope = capability_scope
    capability_scope = _validate_bindings(
        payload, db, capability_scope=capability_scope
    )
    stored_scope = capability_scope
    for k, v in payload.model_dump(exclude={"capability_scope"}).items():
        setattr(a, k, v)
    a.capability_scope = stored_scope
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
    # ``_conversation`` is the authorization boundary: tenant, creator and
    # current Agent/scenario access must all pass. Do not reinterpret an
    # immutable transcript through today's mutable capability graph.
    return list(db.execute(
        select(Message).where(Message.conversation_id == conv_id).order_by(Message.created_at)
    ).scalars().all())


@router.delete("/conversations/{conv_id}", response_model=Msg)
def delete_conversation(conv_id: str, db: Session = Depends(get_tenant_db)):
    # A transcript belongs to its creator, not to the collaborative Agent
    # definition.  Read access to the Agent is enough to erase one's own
    # private context; scenario write permission is not required.
    c = _conversation(db, conv_id)
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{agent_id}/confirmations/{preview_log_id}")
def confirm_agent_tool_preview(
    agent_id: str,
    preview_log_id: str,
    payload: AgentToolConfirmationRequest,
    db: Session = Depends(get_tenant_db),
):
    """Confirm one durable event/workflow preview from this user's conversation."""
    agent = _agent(db, agent_id)
    conversation = _conversation(db, payload.conversation_id)
    if conversation.agent_id != agent.id:
        raise HTTPException(409, "预演对话不属于当前 Agent")
    try:
        return agent_confirmation_service.confirm_preview(
            db,
            preview_log_id,
            agent=agent,
            conversation=conversation,
            correlation_id=payload.correlation_id,
            expected_environment=payload.expected_environment,
            expected_definition_snapshot_id=payload.expected_definition_snapshot_id,
            expected_release_id=payload.expected_release_id,
            expected_definition_hash=payload.expected_definition_hash,
        )
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from exc
    except agent_confirmation_service.AgentConfirmationError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, f"确认失败：{exc}") from exc


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
    # Resolve exactly one definition before inspecting readiness or selecting a
    # model.  In staging/prod those decisions must use the active release and
    # environment-resolved connectors, never mutable live authoring rows.
    history_context = _authorization_context(db, a)
    if history_context is None:
        raise HTTPException(
            409,
            "Agent 当前运行定义、发布快照或环境连接器不完整，已阻止对话",
        )
    missing = _agent_readiness_missing(db, a, runtime_context=history_context)
    if missing:
        raise HTTPException(
            409,
            "Agent 尚未就绪，请先完成：" + "、".join(missing),
        )
    requires_tools = bool(history_context.build_tools())
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
    history_context.llm = llm

    if not conv:
        conv = Conversation(
            agent_id=agent_id,
            created_by_user_id=_current_user_id(db),
            title=payload.message[:50] or "新对话",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    # 场景 & 本体
    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    scenario_name = scenario.name if scenario else ""
    ontology_summary = agent_engine.ontology_summary_for(scenario, db=db)

    # 保存用户消息
    current_user_message = Message(
        conversation_id=conv.id,
        role="user",
        content=payload.message,
    )
    db.add(current_user_message)
    db.commit()
    current_user_message_id = str(current_user_message.id)

    conv_id = conv.id
    trace_context = {
        "correlation_id": uuid.uuid4().hex,
        # Preallocate the answer id so Action dry-runs emitted during the tool
        # loop can durably point at the AI answer before SSE persistence ends.
        "assistant_message_id": uuid.uuid4().hex,
        "agent_id": a.id,
        "conversation_id": conv_id,
        "scenario_id": a.scenario_id or "",
        "user_id": str(db.info.get("user_id") or "") or None,
    }
    stream_tenant_id = str(db.info.get("tenant_id") or "")
    stream_user_id = str(db.info.get("user_id") or "")
    stream_llm_id = str(llm.id)
    # Action tools may commit their dry-run audit row before the streaming turn
    # finishes.  Persist its parent answer first so SQLite/Postgres FK checks and
    # lineage never depend on a not-yet-created message id.
    db.add(
        Message(
            id=trace_context["assistant_message_id"],
            conversation_id=conv_id,
            role="assistant",
            content="正在准备受控工具调用。",
            stream_finalized=False,
        )
    )
    db.commit()

    def persist_answer(
        content: str,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        *,
        finalized: bool = False,
    ) -> None:
        save_db = SessionLocal()
        try:
            message = save_db.get(Message, trace_context["assistant_message_id"])
            if (
                not message
                or message.conversation_id != conv_id
                or message.role != "assistant"
            ):
                raise RuntimeError("Agent 回答占位消息不存在或上下文不匹配")
            message.content = content
            message.tool_calls = tool_calls
            message.tool_results = tool_results
            message.citations = citations
            if finalized:
                message.stream_finalized = True
            save_db.commit()
        finally:
            save_db.close()

    def event_stream():
        cancelled = False
        assistant_content = ""
        tool_calls_log: list[dict[str, Any]] = []
        tool_results_log: list[dict[str, Any]] = []
        citations_log: list[dict[str, Any]] = []
        stream_db: Session | None = None
        try:
            # FastAPI may close yield-based request dependencies before a
            # StreamingResponse body is consumed. Never carry request-bound ORM
            # objects into the SSE generator: commits above also expire them,
            # which otherwise makes relation/property lazy loads fail as
            # detached instances. Re-authorize and resolve the runtime using a
            # session owned for the entire stream instead.
            stream_db = SessionLocal()
            stream_db.info["tenant_id"] = stream_tenant_id
            stream_db.info["user_id"] = stream_user_id
            stream_agent = _agent(stream_db, agent_id)
            stream_context = _authorization_context(stream_db, stream_agent)
            if stream_context is None:
                raise RuntimeError("Agent 当前运行定义、发布快照或环境连接器不完整，已阻止对话")
            stream_missing = _agent_readiness_missing(
                stream_db,
                stream_agent,
                runtime_context=stream_context,
            )
            if stream_missing:
                raise RuntimeError("Agent 尚未就绪，请先完成：" + "、".join(stream_missing))
            stream_llm = tenant_service.get_visible(stream_db, LLMConfig, stream_llm_id)
            if not stream_llm or not llm_service.supports_capability(stream_llm, "chat"):
                raise RuntimeError("Agent 绑定的 LLM 不可用或未启用聊天能力，请重新配置")
            if (
                stream_context.build_tools()
                and not llm_service.supports_capability(stream_llm, "tool")
            ):
                raise RuntimeError("Agent 需要工具调用，但绑定的 LLM 未启用工具能力")
            stream_context.llm = stream_llm
            stream_conversation = _conversation(stream_db, conv_id)
            if stream_conversation.agent_id != stream_agent.id:
                raise RuntimeError("对话不属于当前 Agent")
            history = _model_history(
                stream_db,
                conv_id,
                stream_agent,
                stream_context,
                excluded_message_ids={
                    current_user_message_id,
                    str(trace_context["assistant_message_id"]),
                },
            )
            for ev in agent_engine.run_agent(
                stream_db,
                stream_agent,
                stream_llm,
                history,
                payload.message,
                scenario_name,
                ontology_summary,
                trace_context=trace_context,
                runtime_context=stream_context,
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
                if etype in {"tool_result", "citations"}:
                    # A tool result can contain a durable Action dry-run id.  Save
                    # it into the already-existing answer before the SSE event is
                    # visible so early client cancellation cannot break lineage.
                    stream_db.commit()
                    persist_answer(
                        assistant_content or "已完成受控工具预演，正在整理最终说明。",
                        tool_calls_log,
                        tool_results_log,
                        citations_log,
                    )
                yield f"data: {json.dumps({'type': etype, 'data': ev['data']}, ensure_ascii=False)}\n\n"
            # Complete the pre-persisted answer using an independent session;
            # the request-scoped session may be closed while SSE is streaming.
            persist_answer(
                assistant_content,
                tool_calls_log,
                tool_results_log,
                citations_log,
                finalized=True,
            )
        except GeneratorExit:
            cancelled = True
            # A cancelled browser stream still has a durable partial answer.
            # Mark it final before releasing any preview for confirmation.
            try:
                persist_answer(
                    assistant_content or "对话已停止。",
                    tool_calls_log,
                    tool_results_log,
                    citations_log,
                    finalized=True,
                )
            except Exception:  # noqa: BLE001 - preserve cancellation semantics.
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            error_data = str(exc)
            try:
                persist_answer(
                    _stream_error_content(assistant_content, error_data),
                    tool_calls_log,
                    tool_results_log,
                    citations_log,
                    finalized=True,
                )
            except Exception:  # noqa: BLE001 - preserve the original SSE error.
                pass
            yield f"data: {json.dumps({'type': 'error', 'data': error_data}, ensure_ascii=False)}\n\n"
        finally:
            if stream_db is not None:
                stream_db.close()
            if not cancelled:
                yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def invoke_agent_once(
    agent_id: str,
    *,
    message: str,
    conversation_id: str | None,
    db: Session,
) -> dict[str, Any]:
    """Run one durable Agent turn for a non-browser transport.

    The MCP adapter deliberately enters the same authorization context, model
    routing, history replay and ``agent_engine.run_agent`` loop as browser chat.
    Only the transport envelope differs: this function returns one structured
    result instead of yielding SSE frames.
    """
    a = _agent(db, agent_id)
    conv = None
    if conversation_id:
        conv = _conversation(db, conversation_id)
        if conv.agent_id != agent_id:
            raise HTTPException(400, "对话不属于当前 Agent")

    runtime_context = _authorization_context(db, a)
    if runtime_context is None:
        raise HTTPException(409, "Agent 当前运行定义、发布快照或环境连接器不完整，已阻止对话")
    missing = _agent_readiness_missing(db, a, runtime_context=runtime_context)
    if missing:
        raise HTTPException(409, "Agent 尚未就绪，请先完成：" + "、".join(missing))
    requires_tools = bool(runtime_context.build_tools())
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
    runtime_context.llm = llm

    if conv is None:
        conv = Conversation(
            agent_id=agent_id,
            created_by_user_id=_current_user_id(db),
            title=message[:50] or "新对话",
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    scenario = tenant_service.get_visible(db, BusinessScenario, a.scenario_id) if a.scenario_id else None
    scenario_name = scenario.name if scenario else ""
    ontology_summary = agent_engine.ontology_summary_for(scenario, db=db)
    user_message = Message(conversation_id=conv.id, role="user", content=message)
    assistant_message_id = uuid.uuid4().hex
    assistant_message = Message(
        id=assistant_message_id,
        conversation_id=conv.id,
        role="assistant",
        content="正在准备受控工具调用。",
        stream_finalized=False,
    )
    db.add_all([user_message, assistant_message])
    db.commit()

    trace_id = uuid.uuid4().hex
    trace_context = {
        "correlation_id": trace_id,
        "assistant_message_id": assistant_message_id,
        "agent_id": a.id,
        "conversation_id": conv.id,
        "scenario_id": a.scenario_id or "",
        "user_id": str(db.info.get("user_id") or "") or None,
    }
    history = _model_history(
        db,
        conv.id,
        a,
        runtime_context,
        excluded_message_ids={str(user_message.id), assistant_message_id},
    )
    content = ""
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    try:
        for event in agent_engine.run_agent(
            db,
            a,
            llm,
            history,
            message,
            scenario_name,
            ontology_summary,
            trace_context=trace_context,
            runtime_context=runtime_context,
        ):
            event_type = event["type"]
            data = event.get("data")
            if event_type == "token":
                content += str(data or "")
            elif event_type == "tool_call" and isinstance(data, dict):
                tool_calls.append(data)
            elif event_type == "tool_result" and isinstance(data, dict):
                tool_results.append(data)
                db.commit()
            elif event_type == "citations" and isinstance(data, list):
                citations = data
        assistant_message.content = content
        assistant_message.tool_calls = tool_calls
        assistant_message.tool_results = tool_results
        assistant_message.citations = citations
        assistant_message.stream_finalized = True
        db.commit()
    except Exception as exc:
        assistant_message.content = _stream_error_content(content, str(exc))
        assistant_message.tool_calls = tool_calls
        assistant_message.tool_results = tool_results
        assistant_message.citations = citations
        assistant_message.stream_finalized = True
        db.commit()
        raise

    definition = runtime_context.runtime_definition
    return {
        "answer": content,
        "conversation_id": conv.id,
        "trace_id": trace_id,
        "assistant_message_id": assistant_message_id,
        "citations": citations,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "runtime": {
            "environment": definition.environment if definition else "",
            "definition_snapshot_id": definition.snapshot_id if definition else None,
            "release_id": definition.release_id if definition else None,
            "definition_hash": definition.definition_hash if definition else "",
        },
    }
