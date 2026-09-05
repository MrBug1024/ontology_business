"""Compatibility facade and control plane for durable Agent turns."""
from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import (
    Agent,
    AgentTurnEvent,
    AgentTurnRun,
    BusinessScenario,
    Conversation,
    Message,
)
from ..schemas import ChatRequest
from . import (
    agent_readiness_service,
    agent_turn_input_service,
    agent_turn_payload_service,
    agent_turn_worker_service,
    permission_service,
)


AgentTurnError = agent_turn_input_service.AgentTurnError
AgentTurnConflict = agent_turn_input_service.AgentTurnConflict
TurnLease = agent_turn_worker_service.TurnLease
TurnExecutor = agent_turn_worker_service.TurnExecutor

TURN_LEASE_SECONDS = agent_turn_worker_service.TURN_LEASE_SECONDS
TURN_RECHECK_SECONDS = agent_turn_worker_service.TURN_RECHECK_SECONDS
TERMINAL_STATUSES = agent_turn_worker_service.TERMINAL_STATUSES
RUNNABLE_STATUSES = agent_turn_worker_service.RUNNABLE_STATUSES
_CANCEL_RECONCILIATION_STATUSES = (
    agent_turn_worker_service.CANCEL_RECONCILIATION_STATUSES
)
_STATUS_LABELS = agent_turn_worker_service.STATUS_LABELS

_now = agent_turn_input_service._now
_as_utc = agent_turn_input_service._as_utc
_canonical_bytes = agent_turn_input_service._canonical_bytes
_fingerprint = agent_turn_input_service._fingerprint
_payload_context = agent_turn_input_service._payload_context
_open_authenticated_request = agent_turn_input_service._open_authenticated_request
_require_agent = agent_turn_input_service._require_agent
_validate_attachment_scope = agent_turn_input_service._validate_attachment_scope
_resolve_upload_attachments = agent_turn_input_service._resolve_upload_attachments
_table_asset_version_ids = agent_turn_input_service._table_asset_version_ids
_prepared_payload = agent_turn_input_service._prepared_payload

_append_event = agent_turn_worker_service._append_event
_transition = agent_turn_worker_service._transition
_finish_expired_cancellation = agent_turn_worker_service._finish_expired_cancellation
_lease_matches = agent_turn_worker_service._lease_matches
_bounded_result = agent_turn_worker_service._bounded_result
_release_lease = agent_turn_worker_service._release_lease
_known_error = agent_turn_worker_service._known_error
claim_turn = agent_turn_worker_service.claim_turn
transition_claimed_turn = agent_turn_worker_service.transition_claimed_turn
finalize_turn = agent_turn_worker_service.finalize_turn


def require_legacy_chat_readiness(
    db: Session,
    agent_id: str,
    *,
    conversation_id: str | None,
    environment: str,
) -> None:
    """Preserve the old chat endpoint's fast control-plane rejection contract.

    The compatibility SSE route keeps creator scope and model/readiness errors
    that old clients depend on. Input-specific definition resolution, data
    preparation, and all LLM/tool work belong exclusively to the Turn worker.
    """

    principal = permission_service.require_principal(db)
    agent = _require_agent(db, agent_id)
    if conversation_id:
        _owned_conversation(
            db,
            conversation_id,
            agent=agent,
            user_id=principal.user_id,
        )
    readiness = agent_readiness_service.compute_agent_readiness(
        db,
        agent,
        environment=environment,
    )
    validation_missing = [
        str(item.get("label") or "")
        for item in readiness["validation"]["missing"]
        if str(item.get("label") or "")
    ]
    if validation_missing:
        raise AgentTurnError(
            "agent_not_ready",
            "Agent 尚未就绪，请先完成：" + "、".join(validation_missing),
        )


def _lock_active_scenario(db: Session, agent: Agent) -> None:
    if not agent.scenario_id:
        return
    scenario = db.scalar(
        select(BusinessScenario)
        .where(
            BusinessScenario.id == agent.scenario_id,
            BusinessScenario.tenant_id == agent.tenant_id,
        )
        .with_for_update()
    )
    if scenario is None or scenario.status == "retired":
        raise AgentTurnError("scenario_unavailable", "Agent 所属业务场景已不可用")


def _owned_conversation(
    db: Session,
    conversation_id: str,
    *,
    agent: Agent,
    user_id: str,
) -> Conversation:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.agent_id == agent.id,
            Conversation.created_by_user_id == user_id,
        )
    )
    if conversation is None:
        raise AgentTurnError(
            "conversation_unavailable",
            "对话不存在或不属于当前用户",
            status_code=404,
        )
    return conversation


def _public_run(run: AgentTurnRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "agent_id": run.agent_id,
        "conversation_id": run.conversation_id,
        "user_message_id": run.user_message_id,
        "assistant_message_id": run.assistant_message_id,
        "parent_run_id": run.parent_run_id,
        "status": run.status,
        "revision": run.revision,
        "environment": run.environment,
        "definition_hash": run.definition_hash,
        "deployment_fingerprint": run.deployment_fingerprint,
        "data_context_fingerprint": run.data_context_fingerprint,
        "result": dict(run.result_document or {}),
        "error": (
            {"code": run.error_code, "message": run.error_message}
            if run.error_code
            else None
        ),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def enqueue_turn(
    db: Session,
    agent_id: str,
    payload: ChatRequest,
    *,
    parent_run_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    principal = permission_service.require_principal(db)
    agent = _require_agent(db, agent_id)
    idempotency_key = str(payload.idempotency_key or "").strip()
    if not idempotency_key:
        raise AgentTurnError(
            "idempotency_key_required",
            "异步 Agent Turn 必须提供幂等键",
            status_code=422,
        )
    fingerprint = _fingerprint(
        payload,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        agent_id=agent.id,
        parent_run_id=parent_run_id,
    )
    existing = db.scalar(
        select(AgentTurnRun).where(
            AgentTurnRun.tenant_id == principal.tenant_id,
            AgentTurnRun.requested_by_user_id == principal.user_id,
            AgentTurnRun.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise AgentTurnConflict("同一幂等键不能提交不同的 Agent Turn 请求")
        return _public_run(existing)

    _validate_attachment_scope(db, payload, user_id=principal.user_id)
    _lock_active_scenario(db, agent)
    conversation = (
        _owned_conversation(
            db,
            payload.conversation_id,
            agent=agent,
            user_id=principal.user_id,
        )
        if payload.conversation_id
        else Conversation(
            agent_id=agent.id,
            created_by_user_id=principal.user_id,
            title=payload.message[:50] or "新对话",
        )
    )
    if not payload.conversation_id:
        db.add(conversation)
        db.flush()
    user_message = Message(
        conversation_id=conversation.id,
        role="user",
        content=payload.message,
        input_snapshot={"status": "accepted"},
        evidence_refs=[],
    )
    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="需求已接收，正在准备输入资料。",
        stream_finalized=False,
        input_snapshot={"status": "accepted"},
        evidence_refs=[],
    )
    db.add_all([user_message, assistant_message])
    db.flush()
    run = AgentTurnRun(
        id=uuid.uuid4().hex,
        tenant_id=principal.tenant_id,
        requested_by_user_id=principal.user_id,
        agent_id=agent.id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        parent_run_id=parent_run_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        request_payload={},
        request_summary={},
        request_digest="0" * 64,
        environment=payload.environment,
        status="accepted",
        revision=1,
        available_at=_now(),
        created_at=_now(),
        updated_at=_now(),
    )
    sealed = agent_turn_payload_service.seal_payload(
        payload.model_dump(mode="json", exclude={"message"}, exclude_none=True),
        context=_payload_context(run),
    )
    run.request_payload = sealed.envelope
    run.request_summary = sealed.summary
    run.request_digest = sealed.digest
    db.add(run)
    # AgentTurnEvent has no ORM relationship to communicate insert ordering;
    # materialize its parent first while keeping both rows in one transaction.
    db.flush()
    _append_event(
        db,
        run,
        "accepted",
        {"status": "accepted", "label": _STATUS_LABELS["accepted"]},
    )
    try:
        db.flush()
        if commit:
            db.commit()
    except IntegrityError as exc:
        db.rollback()
        replay = db.scalar(
            select(AgentTurnRun).where(
                AgentTurnRun.tenant_id == principal.tenant_id,
                AgentTurnRun.requested_by_user_id == principal.user_id,
                AgentTurnRun.idempotency_key == idempotency_key,
            )
        )
        if replay is None or replay.request_fingerprint != fingerprint:
            raise AgentTurnConflict("Agent Turn 并发提交发生冲突") from exc
        return _public_run(replay)
    if commit:
        db.refresh(run)
    return _public_run(run)


def _owned_run(db: Session, run_id: str, *, writable: bool = False) -> AgentTurnRun:
    principal = permission_service.require_principal(db)
    run = db.scalar(
        select(AgentTurnRun).where(
            AgentTurnRun.id == run_id,
            AgentTurnRun.tenant_id == principal.tenant_id,
            AgentTurnRun.requested_by_user_id == principal.user_id,
        )
    )
    if run is None:
        raise AgentTurnError("agent_turn_unavailable", "Agent Turn 不存在", status_code=404)
    if run.agent_id:
        _require_agent(db, run.agent_id, active_runtime=writable)
    return run


def get_turn(db: Session, run_id: str) -> dict[str, Any]:
    return _public_run(_owned_run(db, run_id))


def list_turns(
    db: Session,
    agent_id: str,
    *,
    conversation_id: str | None = None,
    active_only: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    principal = permission_service.require_principal(db)
    agent = _require_agent(db, agent_id, active_runtime=False)
    filters = [
        AgentTurnRun.tenant_id == principal.tenant_id,
        AgentTurnRun.requested_by_user_id == principal.user_id,
        AgentTurnRun.agent_id == agent.id,
    ]
    if conversation_id:
        _owned_conversation(
            db,
            conversation_id,
            agent=agent,
            user_id=principal.user_id,
        )
        filters.append(AgentTurnRun.conversation_id == conversation_id)
    if active_only:
        filters.append(AgentTurnRun.status.not_in(TERMINAL_STATUSES))
    runs = db.scalars(
        select(AgentTurnRun)
        .where(*filters)
        .order_by(AgentTurnRun.created_at.desc(), AgentTurnRun.id.desc())
        .limit(max(1, min(limit, 100)))
    )
    return [_public_run(item) for item in runs]


def list_turn_events(
    db: Session,
    run_id: str,
    *,
    after_revision: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]]:
    run = _owned_run(db, run_id)
    events = db.scalars(
        select(AgentTurnEvent)
        .where(
            AgentTurnEvent.run_id == run.id,
            AgentTurnEvent.tenant_id == run.tenant_id,
            AgentTurnEvent.revision > max(0, after_revision),
        )
        .order_by(AgentTurnEvent.revision)
        .limit(max(1, min(limit, 200)))
    )
    return [
        {
            "revision": item.revision,
            "type": item.event_type,
            "data": dict(item.data or {}),
            "created_at": item.created_at,
        }
        for item in events
    ]


def cancel_turn(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
) -> dict[str, Any]:
    run = _owned_run(db, run_id, writable=True)
    db.refresh(run, with_for_update=True)
    if run.revision != expected_revision:
        raise AgentTurnConflict("Agent Turn 状态已变化，请刷新后重试")
    if run.status in TERMINAL_STATUSES:
        raise AgentTurnConflict("Agent Turn 已结束，不能取消")
    if run.status == "cancel_requested":
        # Cancellation is idempotent. Do not recompute a worker's persisted
        # reconciliation decision from this intermediate status.
        return _public_run(run)
    now = _now()
    cancellation_requires_reconciliation = run.status in _CANCEL_RECONCILIATION_STATUSES
    run.cancel_requested_at = now
    lease_expiry = _as_utc(run.lease_expires_at)
    if not run.lease_token or lease_expiry is None or lease_expiry <= now:
        run.lease_token = ""
        run.lease_expires_at = None
        run.finished_at = now
        if cancellation_requires_reconciliation:
            run.error_code = "agent_turn_cancellation_indeterminate"
            run.error_message = "取消时外部执行可能已经开始，请先核对执行记录"
            _transition(
                db,
                run,
                "indeterminate",
                data={"error": {"code": run.error_code, "message": run.error_message}},
                as_of=now,
            )
        else:
            _transition(db, run, "cancelled", as_of=now)
        assistant = db.get(Message, run.assistant_message_id) if run.assistant_message_id else None
        if assistant is not None:
            assistant.content = (
                "取消时外部执行可能已经开始，请先核对执行记录。"
                if cancellation_requires_reconciliation
                else "该任务已取消。"
            )
            assistant.stream_finalized = True
    else:
        run.result_document = {
            **dict(run.result_document or {}),
            "cancellation_requires_reconciliation": cancellation_requires_reconciliation,
        }
        _transition(
            db,
            run,
            "cancel_requested",
            data={"requires_reconciliation": cancellation_requires_reconciliation},
            as_of=now,
        )
    db.commit()
    db.refresh(run)
    return _public_run(run)


def retry_turn(
    db: Session,
    run_id: str,
    *,
    expected_revision: int,
    idempotency_key: str,
) -> dict[str, Any]:
    run = _owned_run(db, run_id, writable=True)
    db.refresh(run, with_for_update=True)
    if run.revision != expected_revision:
        raise AgentTurnConflict("Agent Turn 状态已变化，请刷新后重试")
    if run.status not in {"failed", "cancelled", "indeterminate"}:
        raise AgentTurnConflict("只有失败、取消或待核对的 Agent Turn 可以重试")
    existing_child = db.scalar(
        select(AgentTurnRun).where(
            AgentTurnRun.tenant_id == run.tenant_id,
            AgentTurnRun.parent_run_id == run.id,
        )
    )
    if existing_child is not None:
        if existing_child.idempotency_key == idempotency_key:
            return _public_run(existing_child)
        raise AgentTurnConflict("该 Agent Turn 已创建重试任务，不能重复执行")
    if not run.agent_id or not run.user_message_id:
        raise AgentTurnConflict("原 Agent Turn 的执行上下文已失效")
    user_message = db.get(Message, run.user_message_id)
    if user_message is None:
        raise AgentTurnConflict("原 Agent Turn 的消息已失效")
    original_payload = _open_authenticated_request(run, user_message.content)
    payload = original_payload.model_copy(
        update={
            "conversation_id": run.conversation_id,
            "idempotency_key": idempotency_key,
        }
    )
    retried = enqueue_turn(
        db,
        run.agent_id,
        payload,
        parent_run_id=run.id,
        commit=False,
    )
    child = db.get(AgentTurnRun, retried["id"])
    if child is None:
        raise AgentTurnConflict("重试 Agent Turn 未能持久化")
    if child.parent_run_id != run.id:
        raise AgentTurnConflict("重试幂等键已用于其他执行链路")
    db.commit()
    db.refresh(child)
    return _public_run(child)


def _renew_turn_lease(run_id: str, lease: TurnLease) -> bool:
    return agent_turn_worker_service._renew_turn_lease(
        run_id,
        lease,
        session_factory=SessionLocal,
    )


def _turn_heartbeat(run_id: str, lease: TurnLease):
    return agent_turn_worker_service._turn_heartbeat(
        run_id,
        lease,
        session_factory=SessionLocal,
    )


def _process_claimed_turn(
    run_id: str,
    lease: TurnLease,
    executor: TurnExecutor,
) -> bool:
    return agent_turn_worker_service._process_claimed_turn(
        run_id,
        lease,
        executor,
        session_factory=SessionLocal,
        finalize_fn=finalize_turn,
    )


def process_turn(run_id: str, executor: TurnExecutor) -> bool:
    return agent_turn_worker_service.process_turn(
        run_id,
        executor,
        session_factory=SessionLocal,
        claim_fn=claim_turn,
        process_claimed_fn=_process_claimed_turn,
        finalize_fn=finalize_turn,
    )


def process_next_turn(executor: TurnExecutor) -> bool:
    return agent_turn_worker_service.process_next_turn(
        executor,
        session_factory=SessionLocal,
        process_fn=process_turn,
    )


__all__ = [
    "AgentTurnConflict",
    "AgentTurnError",
    "TERMINAL_STATUSES",
    "TurnLease",
    "cancel_turn",
    "claim_turn",
    "enqueue_turn",
    "finalize_turn",
    "get_turn",
    "list_turns",
    "list_turn_events",
    "process_next_turn",
    "process_turn",
    "require_legacy_chat_readiness",
    "retry_turn",
    "transition_claimed_turn",
]
