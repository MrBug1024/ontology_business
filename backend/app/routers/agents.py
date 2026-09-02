"""Agent 路由：CRUD + 对话（SSE 流式）。"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
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
    ConnectorBinding,
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
    AgentRuntimeConnectionIn,
    AgentRuntimeConnectionOut,
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
    agent_migration_service,
    agent_readiness_service,
    agent_runtime_adapter,
    connector_service,
    datasource_service,
    llm_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)
from ..services.capability_contracts import DataBindingOverride
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/agents", tags=["agents"])

_HISTORIC_MODEL_REPLAY_PLACEHOLDER = (
    "此前回答保留在会话记录中，但其数据快照未参与本轮推理。"
)

_RUNTIME_CONNECTION_SECRET_KEYS = {
    "password", "api_key", "token", "secret", "access_token"
}


def _public_runtime_connection_config(config: dict[str, Any]) -> dict[str, Any]:
    safe = dict(config or {})
    for key in _RUNTIME_CONNECTION_SECRET_KEYS:
        if key in safe:
            safe[key] = ""
    return safe


def _merge_runtime_connection_config(
    previous: dict[str, Any],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(submitted or {})
    for key in _RUNTIME_CONNECTION_SECRET_KEYS:
        if not merged.get(key) and key in previous:
            merged[key] = previous[key]
    return merged


def _runtime_connection_out(source: DataSource) -> AgentRuntimeConnectionOut:
    return AgentRuntimeConnectionOut(
        id=source.id,
        name=source.name,
        type="postgres",
        config=_public_runtime_connection_config(source.config or {}),
        status=source.status,
        last_error=source.last_error,
    )


def _agent_runtime_sources(db: Session, agent: Agent) -> list[DataSource]:
    return list(
        db.scalars(
            select(DataSource)
            .where(
                DataSource.tenant_id == tenant_service.current_tenant_id(db),
                DataSource.resource_scope == "agent_runtime",
                DataSource.owner_agent_id == agent.id,
            )
            .order_by(DataSource.created_at, DataSource.id)
        ).all()
    )


def _sync_runtime_connections(
    db: Session,
    agent: Agent,
    submitted: list[AgentRuntimeConnectionIn],
) -> None:
    if submitted and not agent.scenario_id:
        raise HTTPException(400, "配置业务数据库前必须先选择业务场景")
    existing = {item.id: item for item in _agent_runtime_sources(db, agent)}
    submitted_ids = [item.id for item in submitted if item.id]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise HTTPException(400, "同一个业务数据库不能重复配置")
    unknown = sorted(set(submitted_ids) - set(existing))
    if unknown:
        raise HTTPException(400, "业务数据库配置不存在或不属于当前 Agent")

    normalized_configs: list[dict[str, Any]] = []
    for item in submitted:
        source = existing.get(item.id or "")
        submitted_config = (
            dict(item.config or {})
            if source is None
            else _merge_runtime_connection_config(
                source.config or {},
                item.config or {},
            )
        )
        try:
            normalized_configs.append(
                datasource_service.normalize_postgres_config(submitted_config)
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    keep: list[DataSource] = []
    principal = permission_service.require_principal(db)
    scenario = (
        tenant_service.require_scenario(db, agent.scenario_id, writable=True)
        if agent.scenario_id
        else None
    )
    for item, normalized_config in zip(submitted, normalized_configs, strict=True):
        source = existing.get(item.id or "")
        if source is None:
            source = DataSource(
                tenant_id=tenant_service.current_tenant_id(db),
                scenario_id=agent.scenario_id,
                resource_scope="agent_runtime",
                owner_agent_id=agent.id,
                name=item.name,
                type=item.type,
                config=normalized_config,
                status="unknown",
                last_error="",
            )
            db.add(source)
            db.flush()
        else:
            source.name = item.name
            source.type = item.type
            source.scenario_id = agent.scenario_id
            source.config = normalized_config
            source.status = "unknown"
            source.last_error = ""
            datasource_service.invalidate_engine(source)
            connector_service.invalidate_connector_bindings(db, "data_source", source.id)
        keep.append(source)
        if scenario is not None:
            binding = connector_service.upsert_binding(
                db,
                scenario,
                environment="dev",
                binding_key_value=f"agent:{agent.id}:database:{source.id}",
                kind="data_source",
                connector_id=source.id,
                reference_label=f"{agent.name} · {source.name}",
                check=True,
                created_by_user_id=principal.user_id,
            )
            if binding.health_status == "healthy":
                source.status = "ok"
                source.last_error = ""
            else:
                source.status = "error"
                source.last_error = binding.health_message or "数据库连接检查未通过"

    keep_ids = {item.id for item in keep}
    for source_id, source in existing.items():
        if source_id in keep_ids:
            continue
        bindings = list(
            db.scalars(
                select(ConnectorBinding).where(
                    ConnectorBinding.tenant_id == source.tenant_id,
                    ConnectorBinding.connector_kind == "data_source",
                    ConnectorBinding.connector_id == source.id,
                )
            ).all()
        )
        for binding in bindings:
            db.delete(binding)
        datasource_service.invalidate_engine(source)
        db.delete(source)
    agent.runtime_data_source_ids = [item.id for item in keep]


def _stream_error_content(content: str, error: object) -> str:
    """Match the browser's durable rendering for a failed SSE turn."""
    separator = "\n\n" if content else ""
    return f"{content}{separator}[错误] {error}"


def _current_user_id(db: Session) -> str:
    return permission_service.require_principal(db).user_id


def _require_agent_access(
    db: Session,
    agent: Agent,
    *,
    writable: bool = False,
    active_runtime: bool = False,
) -> Agent:
    """Agents inherit the ACL of their bound scenario.

    Agent rows are tenant-scoped, so tenant ownership alone is insufficient:
    a same-tenant user explicitly denied a scenario must not use its Agent as a
    side door to the scenario context, data sources or historic conversations.
    """
    if agent.scenario_id:
        scenario = tenant_service.require_scenario(db, agent.scenario_id, writable=writable)
        if active_runtime and scenario.status == "retired":
            raise HTTPException(409, "业务场景已退役，不能创建新的 Agent 对话或调用")
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


def _agent(
    db: Session,
    agent_id: str,
    *,
    writable: bool = False,
    active_runtime: bool = False,
) -> Agent:
    agent = tenant_service.require_owned(db, Agent, agent_id, "Agent 不存在")
    return _require_agent_access(
        db,
        agent,
        writable=writable,
        active_runtime=active_runtime,
    )


def _lock_active_agent_scenario(
    db: Session,
    *,
    scenario_id: str | None,
    tenant_id: str,
) -> None:
    """Serialize durable conversation writes with scenario retirement."""
    if not scenario_id:
        return
    scenario = db.scalar(
        select(BusinessScenario)
        .where(BusinessScenario.id == scenario_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if scenario is None or (
        scenario.tenant_id != tenant_id and not scenario.is_public
    ):
        raise HTTPException(409, "Agent 所属业务场景已不可用")
    if scenario.status == "retired":
        raise HTTPException(409, "业务场景已退役，不能写入新的 Agent 对话消息")


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
    mode = str(getattr(agent, "runtime_binding_mode", "legacy") or "legacy")
    if mode in agent_readiness_service.CAPABILITY_MODES:
        readiness = agent_readiness_service.compute_agent_readiness(db, agent)
        return [
            str(item.get("label") or "")
            for item in readiness["validation"]["missing"]
            if str(item.get("label") or "")
        ]
    return agent_readiness_service.legacy_chat_missing(
        db, agent, runtime_context=runtime_context
    )


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
    if not source_id or source_id not in set(agent.runtime_data_source_ids or []):
        return False
    source = tenant_service.get_visible(db, DataSource, source_id)
    if (
        not source
        or source.type != "file_bucket"
        or source.resource_scope != "agent_runtime"
        or source.owner_agent_id != agent.id
        or not _can_access_agent_data_source(db, agent, source)
    ):
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
    *,
    turn_input: agent_runtime_adapter.AgentTurnInput | None = None,
    environment: str = "dev",
) -> Any | None:
    """Build the exact current runtime/ACL view used for historic replay."""
    try:
        return agent_runtime_adapter.build_runtime_context(
            db,
            agent,
            llm or LLMConfig(name="历史权限校验"),
            turn_input=turn_input,
            environment=environment,
        )
    except Exception:  # noqa: BLE001 - missing release/binding must fail closed.
        return None


def _binding_override(item: Any) -> DataBindingOverride:
    candidates = (
        ("dataset_version", getattr(item, "dataset_version_id", None)),
        ("dataset_head", getattr(item, "dataset_head_id", None)),
        ("asset_version", getattr(item, "asset_version_id", None)),
        ("artifact", getattr(item, "artifact_id", None)),
    )
    for binding_kind, reference_id in candidates:
        if reference_id is not None:
            return DataBindingOverride(
                port_key=item.port_key,
                binding_kind=binding_kind,
                reference_id=reference_id,
                signature=item.expected_signature,
            )
    return DataBindingOverride(
        port_key=item.port_key,
        binding_kind="connector_binding",
        binding_key=item.binding_key,
        signature=item.expected_signature,
    )


def _agent_turn_input(payload: ChatRequest) -> agent_runtime_adapter.AgentTurnInput:
    target = payload.capability
    return agent_runtime_adapter.AgentTurnInput(
        structured_inputs=payload.inputs,
        binding_overrides=tuple(_binding_override(item) for item in payload.managed_inputs),
        target_kind=target.kind if target is not None else None,
        target_key=target.key if target is not None else None,
        idempotency_key=payload.idempotency_key,
        attachments=tuple(
            agent_runtime_adapter.AgentAttachmentInput(
                asset_version_id=item.asset_version_id,
                dataset_version_id=item.dataset_version_id,
                filename=item.filename,
                expected_signature=item.expected_signature,
            )
            for item in payload.attachments
        ),
    )


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
    runtime_sources = (
        getattr(authorization_context, "data_sources", None)
        or getattr(authorization_context, "runtime_connections", None)
        or ()
    )
    runtime_source_ids = {str(source.id) for source in runtime_sources}
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
    readiness = agent_readiness_service.compute_agent_readiness(db, a)
    runtime_connections = [
        _runtime_connection_out(source)
        for source in _agent_runtime_sources(db, a)
    ]
    return AgentOut(
        id=a.id,
        name=a.name,
        description=a.description,
        scenario_id=a.scenario_id,
        llm_config_id=a.llm_config_id,
        system_prompt=a.system_prompt,
        # Kept in the response schema for old SDKs only. Modeling resources can
        # no longer be attached to or exposed through an Agent.
        data_source_ids=[],
        temperature=a.temperature,
        max_tokens=a.max_tokens,
        created_at=a.created_at,
        updated_at=a.updated_at,
        scenario_name=scenario.name if scenario else "",
        llm_name=llm.name if llm else "",
        data_source_names=[],
        runtime_connections=runtime_connections,
        capability_scope=capability_scope,
        capability_scope_legacy=a.capability_scope is None,
        capability_summary=agent_capability_service.capability_summary(
            db,
            capability_scope,
            definition=definition,
            definition_error=definition_error,
        ),
        runtime_binding_mode=a.runtime_binding_mode or "legacy",
        readiness=readiness,
        definition_valid=bool(readiness["definition_valid"]),
        validation_ready=bool(readiness["validation_ready"]),
        release_ready=bool(readiness["release_ready"]),
        runtime_ready=bool(readiness["runtime_ready"]),
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

    # ``data_source_ids`` is a wire-compatibility field only. Accepting old
    # clients is harmless because create/update always discard it below.
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


@router.get("/{agent_id}/runtime-capabilities")
def get_agent_runtime_capabilities(
    agent_id: str,
    db: Session = Depends(get_tenant_db),
) -> list[dict[str, Any]]:
    """Return logical capability contracts without physical binding details."""

    agent = _agent(db, agent_id)
    try:
        runtime = agent_runtime_adapter.CapabilityAgentRuntime(
            db,
            agent,
            LLMConfig(name="能力契约发现"),
            environment=runtime_connector_service.runtime_environment(),
        )
    except agent_runtime_adapter.AgentRuntimeAdapterError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return runtime.public_catalog()


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
    if payload.runtime_binding_mode not in {None, "capability_only"}:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "historical_runtime_disabled",
                "message": (
                    "Historical Agent runtime modes are disabled; new Agents "
                    "must use capability_only"
                ),
            },
        )
    capability_scope = agent_capability_service.normalize_scope(
        (
            payload.capability_scope
            if payload.capability_scope is not None
            else agent_capability_service.explicit_empty_scope()
        ),
        legacy_default=False,
        allow_all=True,
    )
    capability_scope = _validate_bindings(
        payload, db, capability_scope=capability_scope
    )
    runtime_mode = "capability_only"
    values = payload.model_dump(
        exclude={"capability_scope", "runtime_binding_mode", "runtime_connections"}
    )
    values["data_source_ids"] = []
    a = Agent(
        tenant_id=tenant_service.current_tenant_id(db),
        **values,
        capability_scope=capability_scope,
        runtime_binding_mode=runtime_mode,
    )
    try:
        db.add(a)
        db.flush()
        _sync_runtime_connections(db, a, payload.runtime_connections)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(a)
    return _out(a, db)


@router.get("/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id)
    return _out(a, db)


@router.put("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, payload: AgentIn, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id, writable=True)
    try:
        agent_migration_service.assert_direct_mode_update_allowed(
            a,
            payload.runtime_binding_mode,
        )
    except agent_migration_service.AgentMigrationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    scenario_changed = payload.scenario_id != a.scenario_id
    if payload.capability_scope is not None:
        capability_scope = agent_capability_service.normalize_scope(
            payload.capability_scope,
            legacy_default=False,
            allow_all=True,
        )
        stored_scope: dict[str, dict[str, Any]] | None = capability_scope
    elif scenario_changed:
        # A scenario change is a new authorization boundary. Never carry or
        # dynamically grant capabilities from either scenario.
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
    values = payload.model_dump(
        exclude={"capability_scope", "runtime_binding_mode", "runtime_connections"}
    )
    values["data_source_ids"] = []
    for k, v in values.items():
        setattr(a, k, v)
    a.capability_scope = stored_scope
    try:
        db.flush()
        _sync_runtime_connections(db, a, payload.runtime_connections)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(a)
    return _out(a, db)


@router.delete("/{agent_id}", response_model=Msg)
def delete_agent(agent_id: str, db: Session = Depends(get_tenant_db)):
    a = _agent(db, agent_id, writable=True)
    _sync_runtime_connections(db, a, [])
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
    a = _agent(db, agent_id, active_runtime=True)
    _lock_active_agent_scenario(
        db,
        scenario_id=a.scenario_id,
        tenant_id=a.tenant_id,
    )
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
    agent = _agent(db, agent_id, active_runtime=True)
    _lock_active_agent_scenario(
        db,
        scenario_id=agent.scenario_id,
        tenant_id=agent.tenant_id,
    )
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
    a = _agent(db, agent_id, active_runtime=True)
    turn_input = _agent_turn_input(payload)
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
    history_context = _authorization_context(
        db,
        a,
        turn_input=turn_input,
        environment=payload.environment,
    )
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
        _lock_active_agent_scenario(
            db,
            scenario_id=a.scenario_id,
            tenant_id=a.tenant_id,
        )
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
        input_snapshot=agent_runtime_adapter.input_snapshot(history_context),
        evidence_refs=agent_runtime_adapter.evidence_snapshot(history_context),
    )
    db.add(current_user_message)
    _lock_active_agent_scenario(
        db,
        scenario_id=a.scenario_id,
        tenant_id=a.tenant_id,
    )
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
    stream_scenario_id = str(a.scenario_id or "") or None
    stream_agent_tenant_id = str(a.tenant_id)
    # Action tools may commit their dry-run audit row before the streaming turn
    # finishes.  Persist its parent answer first so PostgreSQL FK checks and
    # lineage never depend on a not-yet-created message id.
    db.add(
        Message(
            id=trace_context["assistant_message_id"],
            conversation_id=conv_id,
            role="assistant",
            content="正在准备受控工具调用。",
            stream_finalized=False,
            input_snapshot=agent_runtime_adapter.input_snapshot(history_context),
            evidence_refs=agent_runtime_adapter.evidence_snapshot(history_context),
        )
    )
    _lock_active_agent_scenario(
        db,
        scenario_id=a.scenario_id,
        tenant_id=a.tenant_id,
    )
    db.commit()

    def persist_answer(
        content: str,
        tool_calls: list[dict[str, Any]],
        tool_results: list[dict[str, Any]],
        citations: list[dict[str, Any]],
        input_snapshot: dict[str, Any],
        evidence_refs: list[dict[str, Any]],
        *,
        finalized: bool = False,
    ) -> None:
        save_db = SessionLocal()
        try:
            _lock_active_agent_scenario(
                save_db,
                scenario_id=stream_scenario_id,
                tenant_id=stream_agent_tenant_id,
            )
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
            message.input_snapshot = input_snapshot
            message.evidence_refs = evidence_refs
            user_message = save_db.get(Message, current_user_message_id)
            if (
                user_message is not None
                and user_message.conversation_id == conv_id
                and user_message.role == "user"
            ):
                user_message.input_snapshot = input_snapshot
                user_message.evidence_refs = evidence_refs
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
        stream_context: Any | None = None
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
            stream_agent = _agent(stream_db, agent_id, active_runtime=True)
            stream_context = _authorization_context(
                stream_db,
                stream_agent,
                turn_input=turn_input,
            )
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
            runtime_snapshot = agent_runtime_adapter.input_snapshot(stream_context)
            runtime_decision = runtime_snapshot.get("runtime")
            if isinstance(runtime_decision, dict) and runtime_decision:
                yield f"data: {json.dumps({'type': 'runtime_decision', 'data': runtime_decision}, ensure_ascii=False)}\n\n"
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
                elif etype == "evidence_refs":
                    # Capability evidence is read from the runtime context
                    # below; the event simply makes the transport observable.
                    pass
                if etype in {"tool_result", "citations", "evidence_refs"}:
                    # A tool result can contain a durable Action dry-run id.  Save
                    # it into the already-existing answer before the SSE event is
                    # visible so early client cancellation cannot break lineage.
                    stream_db.commit()
                    persist_answer(
                        assistant_content or "已完成受控工具预演，正在整理最终说明。",
                        tool_calls_log,
                        tool_results_log,
                        citations_log,
                        agent_runtime_adapter.input_snapshot(stream_context),
                        agent_runtime_adapter.evidence_snapshot(stream_context),
                    )
                yield f"data: {json.dumps({'type': etype, 'data': ev['data']}, ensure_ascii=False)}\n\n"
            # Complete the pre-persisted answer using an independent session;
            # the request-scoped session may be closed while SSE is streaming.
            persist_answer(
                assistant_content,
                tool_calls_log,
                tool_results_log,
                citations_log,
                agent_runtime_adapter.input_snapshot(stream_context),
                agent_runtime_adapter.evidence_snapshot(stream_context),
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
                    (
                        agent_runtime_adapter.input_snapshot(stream_context)
                        if stream_context is not None
                        else {}
                    ),
                    (
                        agent_runtime_adapter.evidence_snapshot(stream_context)
                        if stream_context is not None
                        else []
                    ),
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
                    (
                        agent_runtime_adapter.input_snapshot(stream_context)
                        if stream_context is not None
                        else {}
                    ),
                    (
                        agent_runtime_adapter.evidence_snapshot(stream_context)
                        if stream_context is not None
                        else []
                    ),
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
    inputs: dict[str, Any] | None = None,
    managed_inputs: list[Any] | None = None,
    capability: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    environment: str = "dev",
) -> dict[str, Any]:
    """Run one durable Agent turn for a non-browser transport.

    The MCP adapter deliberately enters the same authorization context, model
    routing, history replay and ``agent_engine.run_agent`` loop as browser chat.
    Only the transport envelope differs: this function returns one structured
    result instead of yielding SSE frames.
    """
    payload = ChatRequest.model_validate(
        {
            "message": message,
            "conversation_id": conversation_id,
            "inputs": inputs or {},
            "managed_inputs": managed_inputs or [],
            "capability": capability,
            "idempotency_key": idempotency_key,
            "environment": environment,
        }
    )
    turn_input = _agent_turn_input(payload)
    a = _agent(db, agent_id, active_runtime=True)
    conv = None
    if conversation_id:
        conv = _conversation(db, conversation_id)
        if conv.agent_id != agent_id:
            raise HTTPException(400, "对话不属于当前 Agent")

    runtime_context = _authorization_context(
        db,
        a,
        turn_input=turn_input,
        environment=payload.environment,
    )
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
        _lock_active_agent_scenario(
            db,
            scenario_id=a.scenario_id,
            tenant_id=a.tenant_id,
        )
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
    user_message = Message(
        conversation_id=conv.id,
        role="user",
        content=message,
        input_snapshot=agent_runtime_adapter.input_snapshot(runtime_context),
        evidence_refs=agent_runtime_adapter.evidence_snapshot(runtime_context),
    )
    db.add(user_message)
    db.flush()
    assistant_message_id = uuid.uuid4().hex
    assistant_message = Message(
        id=assistant_message_id,
        conversation_id=conv.id,
        role="assistant",
        content="正在准备受控工具调用。",
        stream_finalized=False,
        created_at=user_message.created_at + timedelta(microseconds=1),
        input_snapshot=agent_runtime_adapter.input_snapshot(runtime_context),
        evidence_refs=agent_runtime_adapter.evidence_snapshot(runtime_context),
    )
    db.add(assistant_message)
    _lock_active_agent_scenario(
        db,
        scenario_id=a.scenario_id,
        tenant_id=a.tenant_id,
    )
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
    evidence_refs: list[dict[str, Any]] = []
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
            elif event_type == "evidence_refs" and isinstance(data, list):
                evidence_refs = data
        assistant_message.content = content
        assistant_message.tool_calls = tool_calls
        assistant_message.tool_results = tool_results
        assistant_message.citations = citations
        assistant_message.input_snapshot = agent_runtime_adapter.input_snapshot(
            runtime_context
        )
        evidence_refs = agent_runtime_adapter.evidence_snapshot(runtime_context)
        assistant_message.evidence_refs = evidence_refs
        user_message.input_snapshot = assistant_message.input_snapshot
        user_message.evidence_refs = evidence_refs
        assistant_message.stream_finalized = True
        _lock_active_agent_scenario(
            db,
            scenario_id=a.scenario_id,
            tenant_id=a.tenant_id,
        )
        db.commit()
    except Exception as exc:
        assistant_message.content = _stream_error_content(content, str(exc))
        assistant_message.tool_calls = tool_calls
        assistant_message.tool_results = tool_results
        assistant_message.citations = citations
        assistant_message.input_snapshot = agent_runtime_adapter.input_snapshot(
            runtime_context
        )
        evidence_refs = agent_runtime_adapter.evidence_snapshot(runtime_context)
        assistant_message.evidence_refs = evidence_refs
        user_message.input_snapshot = assistant_message.input_snapshot
        user_message.evidence_refs = evidence_refs
        assistant_message.stream_finalized = True
        _lock_active_agent_scenario(
            db,
            scenario_id=a.scenario_id,
            tenant_id=a.tenant_id,
        )
        db.commit()
        raise

    definition = runtime_context.runtime_definition
    return {
        "answer": content,
        "conversation_id": conv.id,
        "trace_id": trace_id,
        "assistant_message_id": assistant_message_id,
        "citations": citations,
        "input_snapshot": agent_runtime_adapter.input_snapshot(runtime_context),
        "evidence_refs": evidence_refs,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "runtime": {
            "environment": definition.environment if definition else "",
            "definition_snapshot_id": definition.snapshot_id if definition else None,
            "release_id": definition.release_id if definition else None,
            "definition_hash": definition.definition_hash if definition else "",
        },
    }
