"""HTTP adapter for durable asynchronous Agent conversation turns."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import AuthSession, Message
from ..schemas import (
    AgentTurnCancelIn,
    AgentTurnEventOut,
    AgentTurnRunOut,
    AgentTurnRetryIn,
    ChatRequest,
)
from ..services import agent_turn_payload_service, agent_turn_service, workflow_payload_service
from ..services.auth_service import get_tenant_db


router = APIRouter(tags=["agent-turns"])


_TURN_REQUEST_ERRORS = (
    agent_turn_service.AgentTurnError,
    agent_turn_payload_service.AgentTurnPayloadError,
    workflow_payload_service.WorkflowPayloadError,
)


def _http_error(
    exc: agent_turn_service.AgentTurnError
    | agent_turn_payload_service.AgentTurnPayloadError
    | workflow_payload_service.WorkflowPayloadError,
) -> HTTPException:
    if isinstance(exc, workflow_payload_service.WorkflowPayloadError):
        return HTTPException(
            status_code=503,
            detail={
                "code": "agent_turn_encryption_unavailable",
                "message": "当前服务的输入加密配置不可用，请联系管理员；需求尚未提交，草稿已保留。",
            },
        )
    return HTTPException(
        status_code=exc.status_code if isinstance(exc, agent_turn_service.AgentTurnError) else 409,
        detail={"code": exc.code, "message": exc.message},
    )


def _legacy_http_error(
    exc: agent_turn_service.AgentTurnError
    | agent_turn_payload_service.AgentTurnPayloadError
    | workflow_payload_service.WorkflowPayloadError,
) -> HTTPException:
    # The deprecated endpoint historically returned a string ``detail``.  Keep
    # that envelope stable while the versioned Turn API uses code/message DTOs.
    mapped = _http_error(exc)
    return HTTPException(status_code=mapped.status_code, detail=mapped.detail["message"])


def _browser_session_invalid(
    db: Session,
    *,
    auth_session_id: str,
    user_id: str,
) -> bool:
    if not auth_session_id:
        return False
    auth_session = db.get(AuthSession, auth_session_id)
    expiry = auth_session.expires_at if auth_session is not None else None
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return bool(
        auth_session is None
        or auth_session.user_id != user_id
        or expiry is None
        or expiry <= datetime.now(timezone.utc)
    )


def _legacy_frame(event_type: str, data: Any) -> str:
    payload = json.dumps(
        jsonable_encoder({"type": event_type, "data": data}),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"data: {payload}\n\n"


def _legacy_terminal_frames(
    run: Mapping[str, Any],
    assistant: Mapping[str, Any] | None,
) -> Iterator[str]:
    """Translate one durable terminal snapshot to the retired SSE envelope."""

    result = run.get("result") if isinstance(run.get("result"), Mapping) else {}
    assistant = assistant or {}
    if run.get("status") == "succeeded":
        input_snapshot = (
            result.get("input_snapshot")
            if isinstance(result.get("input_snapshot"), Mapping)
            else {}
        )
        runtime_decision = input_snapshot.get("runtime")
        if isinstance(runtime_decision, Mapping) and runtime_decision:
            yield _legacy_frame("runtime_decision", runtime_decision)
        for tool_call in assistant.get("tool_calls") or []:
            if isinstance(tool_call, Mapping):
                yield _legacy_frame("tool_call", tool_call)
        for tool_result in assistant.get("tool_results") or []:
            if isinstance(tool_result, Mapping):
                yield _legacy_frame("tool_result", tool_result)
        citations = assistant.get("citations") or result.get("citations")
        if isinstance(citations, list) and citations:
            yield _legacy_frame("citations", citations)
        evidence_refs = assistant.get("evidence_refs") or result.get("evidence_refs")
        if isinstance(evidence_refs, list) and evidence_refs:
            yield _legacy_frame("evidence_refs", evidence_refs)
        answer = str(result.get("answer") or assistant.get("content") or "")
        if answer:
            # Durable execution intentionally does not persist every model token.
            # A single final token frame preserves the old consumer envelope
            # without making correctness depend on in-process streaming state.
            yield _legacy_frame("token", answer)
        yield _legacy_frame("done", answer)
        return

    error = run.get("error") if isinstance(run.get("error"), Mapping) else {}
    message = str(
        error.get("message")
        or assistant.get("content")
        or "Agent Turn 未能完成"
    )
    yield _legacy_frame("error", message)


def _legacy_chat_events(
    run_id: str,
    *,
    tenant_id: str,
    user_id: str,
    auth_session_id: str,
) -> Iterator[str]:
    """Observe durable state only; disconnecting this iterator never cancels it."""

    revision = 0
    keepalive_at = time.monotonic()
    # Force response headers onto the wire before the background worker starts.
    yield ": durable-agent-turn-accepted\n\n"
    while True:
        event_db = SessionLocal()
        authorization_error = False
        current: Mapping[str, Any] = {}
        assistant: dict[str, Any] | None = None
        try:
            event_db.info["tenant_id"] = tenant_id
            event_db.info["user_id"] = user_id
            authorization_error = _browser_session_invalid(
                event_db,
                auth_session_id=auth_session_id,
                user_id=user_id,
            )
            if not authorization_error:
                events = agent_turn_service.list_turn_events(
                    event_db,
                    run_id,
                    after_revision=revision,
                )
                if events:
                    revision = max(int(item["revision"]) for item in events)
                current = agent_turn_service.get_turn(event_db, run_id)
                if current["status"] in agent_turn_service.TERMINAL_STATUSES:
                    message_id = str(current.get("assistant_message_id") or "")
                    message = event_db.get(Message, message_id) if message_id else None
                    if (
                        message is not None
                        and message.conversation_id == current.get("conversation_id")
                        and message.role == "assistant"
                    ):
                        assistant = {
                            "content": message.content,
                            "tool_calls": list(message.tool_calls or []),
                            "tool_results": list(message.tool_results or []),
                            "citations": list(message.citations or []),
                            "evidence_refs": list(message.evidence_refs or []),
                        }
        except (agent_turn_service.AgentTurnError, HTTPException):
            authorization_error = True
        finally:
            event_db.close()
        if authorization_error:
            yield _legacy_frame("error", "Agent Turn 已不可用或访问权限已变化")
            yield "data: [DONE]\n\n"
            return
        if current.get("status") in agent_turn_service.TERMINAL_STATUSES:
            yield from _legacy_terminal_frames(current, assistant)
            yield "data: [DONE]\n\n"
            return
        if time.monotonic() - keepalive_at >= 15:
            keepalive_at = time.monotonic()
            yield ": keepalive\n\n"
        time.sleep(0.5)


@router.post(
    "/agents/{agent_id}/chat",
    deprecated=True,
    response_class=StreamingResponse,
)
def legacy_chat(
    agent_id: str,
    payload: ChatRequest,
    request: Request,
    db: Session = Depends(get_tenant_db),
) -> StreamingResponse:
    """Backward-compatible observer over the durable asynchronous Turn API."""

    try:
        agent_turn_service.require_legacy_chat_readiness(
            db,
            agent_id,
            conversation_id=payload.conversation_id,
        )
        accepted_payload = payload
        if not payload.idempotency_key:
            accepted_payload = payload.model_copy(
                update={"idempotency_key": f"legacy-chat:{uuid.uuid4().hex}"}
            )
        run = agent_turn_service.enqueue_turn(db, agent_id, accepted_payload)
    except _TURN_REQUEST_ERRORS as exc:
        db.rollback()
        raise _legacy_http_error(exc) from exc

    tenant_id = str(db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    auth_session_id = str(getattr(request.state, "auth_session_id", "") or "")
    # enqueue_turn committed the conversation, messages, run, and accepted event.
    # Release the request connection before opening the potentially long stream.
    db.close()
    response = StreamingResponse(
        _legacy_chat_events(
            str(run["id"]),
            tenant_id=tenant_id,
            user_id=user_id,
            auth_session_id=auth_session_id,
        ),
        media_type="text/event-stream",
        status_code=status.HTTP_200_OK,
    )
    location = request.url_for("get_agent_turn", run_id=str(run["id"])).path
    successor = request.url_for("create_agent_turn", agent_id=agent_id).path
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Agent-Turn-Id"] = str(run["id"])
    response.headers["Location"] = location
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'
    return response


@router.post(
    "/agents/{agent_id}/turns",
    response_model=AgentTurnRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_agent_turn(
    agent_id: str,
    payload: ChatRequest,
    db: Session = Depends(get_tenant_db),
) -> AgentTurnRunOut:
    try:
        return AgentTurnRunOut.model_validate(
            agent_turn_service.enqueue_turn(db, agent_id, payload)
        )
    except _TURN_REQUEST_ERRORS as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.get("/agent-turns/{run_id}", response_model=AgentTurnRunOut)
def get_agent_turn(
    run_id: str,
    db: Session = Depends(get_tenant_db),
) -> AgentTurnRunOut:
    try:
        return AgentTurnRunOut.model_validate(agent_turn_service.get_turn(db, run_id))
    except agent_turn_service.AgentTurnError as exc:
        raise _http_error(exc) from exc


@router.get("/agents/{agent_id}/turns", response_model=list[AgentTurnRunOut])
def list_agent_turns(
    agent_id: str,
    conversation_id: str | None = None,
    active_only: bool = False,
    limit: int = 50,
    db: Session = Depends(get_tenant_db),
) -> list[AgentTurnRunOut]:
    try:
        return [
            AgentTurnRunOut.model_validate(item)
            for item in agent_turn_service.list_turns(
                db,
                agent_id,
                conversation_id=conversation_id,
                active_only=active_only,
                limit=limit,
            )
        ]
    except agent_turn_service.AgentTurnError as exc:
        raise _http_error(exc) from exc


@router.get(
    "/agent-turns/{run_id}/event-log",
    response_model=list[AgentTurnEventOut],
)
def get_agent_turn_events(
    run_id: str,
    after_revision: int = 0,
    db: Session = Depends(get_tenant_db),
) -> list[AgentTurnEventOut]:
    try:
        return [
            AgentTurnEventOut.model_validate(item)
            for item in agent_turn_service.list_turn_events(
                db,
                run_id,
                after_revision=max(0, after_revision),
            )
        ]
    except agent_turn_service.AgentTurnError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/agent-turns/{run_id}/cancel",
    response_model=AgentTurnRunOut,
)
def cancel_agent_turn(
    run_id: str,
    payload: AgentTurnCancelIn,
    db: Session = Depends(get_tenant_db),
) -> AgentTurnRunOut:
    try:
        return AgentTurnRunOut.model_validate(
            agent_turn_service.cancel_turn(
                db,
                run_id,
                expected_revision=payload.expected_revision,
            )
        )
    except agent_turn_service.AgentTurnError as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.post(
    "/agent-turns/{run_id}/retry",
    response_model=AgentTurnRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_agent_turn(
    run_id: str,
    payload: AgentTurnRetryIn,
    db: Session = Depends(get_tenant_db),
) -> AgentTurnRunOut:
    try:
        return AgentTurnRunOut.model_validate(
            agent_turn_service.retry_turn(
                db,
                run_id,
                expected_revision=payload.expected_revision,
                idempotency_key=payload.idempotency_key,
            )
        )
    except _TURN_REQUEST_ERRORS as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.get("/agent-turns/{run_id}/events")
def stream_agent_turn_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    db: Session = Depends(get_tenant_db),
) -> StreamingResponse:
    try:
        initial = agent_turn_service.get_turn(db, run_id)
    except agent_turn_service.AgentTurnError as exc:
        raise _http_error(exc) from exc
    tenant_id = str(db.info.get("tenant_id") or "")
    user_id = str(db.info.get("user_id") or "")
    auth_session_id = str(getattr(request.state, "auth_session_id", "") or "")
    # The stream re-authorizes through short-lived sessions; do not pin the
    # request dependency's database connection for the lifetime of the SSE.
    db.close()
    try:
        initial_revision = max(0, int(last_event_id or 0))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID 必须是整数") from exc

    def generate() -> Iterator[str]:
        revision = initial_revision
        keepalive_at = time.monotonic()
        while True:
            event_db = SessionLocal()
            authorization_error = False
            try:
                event_db.info["tenant_id"] = tenant_id
                event_db.info["user_id"] = user_id
                if auth_session_id:
                    auth_session = event_db.get(AuthSession, auth_session_id)
                    expiry = (
                        auth_session.expires_at
                        if auth_session is not None
                        else None
                    )
                    if expiry is not None and expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    authorization_error = bool(
                        auth_session is None
                        or auth_session.user_id != user_id
                        or expiry is None
                        or expiry <= datetime.now(timezone.utc)
                    )
                if not authorization_error:
                    events = agent_turn_service.list_turn_events(
                        event_db,
                        run_id,
                        after_revision=revision,
                    )
                    current = agent_turn_service.get_turn(event_db, run_id)
            except (agent_turn_service.AgentTurnError, HTTPException):
                authorization_error = True
            finally:
                event_db.close()
            if authorization_error:
                yield "event: error\ndata: {\"code\":\"agent_turn_unavailable\"}\n\n"
                return
            for event in events:
                revision = int(event["revision"])
                payload = json.dumps(
                    jsonable_encoder(event),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {revision}\ndata: {payload}\n\n"
            if (current["status"] in agent_turn_service.TERMINAL_STATUSES
                and revision >= int(current["revision"])):
                yield "data: [DONE]\n\n"
                return
            if events and revision < int(current["revision"]):
                continue
            if time.monotonic() - keepalive_at >= 15:
                keepalive_at = time.monotonic()
                yield ": keepalive\n\n"
            time.sleep(0.5)

    response = StreamingResponse(generate(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Agent-Turn-Id"] = str(initial["id"])
    return response
