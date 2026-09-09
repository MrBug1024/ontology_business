"""Message-only reference client for the shared interaction application services."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channel_interaction_schemas import ChannelReplyIn, EvidenceReference
from ..models import Agent, CapabilityInvocation, Message, WorkflowApprovalRequest, WorkflowRun
from . import agent_capability_confirmation_service, capability_application_service, capability_delivery_service, channel_interaction_service
from . import agent_capability_confirmation_payload, agent_turn_payload_service, agent_capability_service
from .capability_invoker import CapabilityInvocationError
from .channel_reply_parser import ParsedReply, parse_reply


def _linked_previews(db: Session, agent: Agent, conversation_id: str) -> list[tuple[Message, CapabilityInvocation]]:
    messages = db.scalars(select(Message).where(
        Message.conversation_id == conversation_id, Message.role == "assistant", Message.stream_finalized.is_(True),
    ).order_by(Message.created_at.desc()).limit(100)).all()
    linked: dict[str, Message] = {}
    for message in messages:
        for tool in message.tool_results or []:
            if not isinstance(tool, dict) or tool.get("name") != "invoke_capability":
                continue
            value = tool.get("result")
            try:
                value = json.loads(value) if isinstance(value, str) else value
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and isinstance(value.get("invocation_id"), str):
                linked.setdefault(value["invocation_id"], message)
    if not linked:
        return []
    invocations = db.scalars(select(CapabilityInvocation).where(
        CapabilityInvocation.id.in_(list(linked)[:200]), CapabilityInvocation.tenant_id == agent.tenant_id,
        CapabilityInvocation.principal_type == "agent", CapabilityInvocation.principal_id == agent.id,
    )).all()
    results = []
    for invocation in invocations:
        original_id = ((invocation.request_document or {}).get(agent_capability_confirmation_payload.STORAGE_KEY) or {}).get("message_id")
        message = db.get(Message, original_id) if original_id else linked[invocation.id]
        if message is not None and message.conversation_id == conversation_id:
            results.append((message, invocation))
    return results


def _confirmation(db: Session, agent: Agent, conversation_id: str, parsed: ParsedReply, payload: ChannelReplyIn, runtime: Any) -> dict[str, Any]:
    candidates = []
    for message, invocation in _linked_previews(db, agent, conversation_id):
        stored = (invocation.request_document or {}).get(agent_capability_confirmation_payload.STORAGE_KEY) or {}
        if parsed.code:
            selected = parsed.code == capability_delivery_service.reply_code("confirmation", invocation.id)
        else:
            selected = invocation.status == "awaiting_confirmation" or stored.get("reply_message_id") == payload.message_id
        if selected:
            candidates.append((message, invocation))
    if len(candidates) != 1:
        raise channel_interaction_service.ChannelInteractionError(
            "当前没有匹配的执行确认待办" if not candidates else "有多个执行确认待办，请带上待办消息中的确认编号",
        )
    message, invocation = candidates[0]
    if payload.evidence:
        raise channel_interaction_service.ChannelInteractionError("新增文件会改变输入，请重新预演后再确认")
    if parsed.action == "cancel":
        result = channel_interaction_service.reply_confirmation(db, runtime._actor(), invocation.id, payload)
    else:
        request_document = dict(invocation.request_document or {})
        stored = dict(request_document.get(agent_capability_confirmation_payload.STORAGE_KEY) or {})
        stored["reply_message_id"] = payload.message_id
        request_document[agent_capability_confirmation_payload.STORAGE_KEY] = stored
        invocation.request_document = request_document
        db.flush()
        view = agent_capability_confirmation_service.confirm(db, agent.id, invocation.id, message.id)
        result = {"text": view["delivery"]["text"]}
    document = capability_application_service.get_receipt(db, runtime._actor(), invocation.id)
    return {"answer": "已收到取消请求。" if parsed.action == "cancel" else "已收到确认，本次执行进展如下。", "receipt": runtime._model_receipt(document)}


def _approval(db: Session, agent: Agent, conversation_id: str, parsed: ParsedReply, payload: ChannelReplyIn, runtime: Any) -> dict[str, Any]:
    statement = select(WorkflowApprovalRequest).join(WorkflowRun).where(WorkflowRun.scenario_id == agent.scenario_id)
    scope = agent_capability_service.normalize_scope(agent_capability_service.legacy_all_scope() if agent.capability_scope is None else agent.capability_scope, allow_all=True)["workflows"]
    if scope["mode"] != "all":
        statement = statement.where(WorkflowRun.workflow_id.in_(scope["selected_ids"]))
    if not parsed.code:
        linked_runs = [(invocation.result_document or {}).get("output", {}).get("workflow_run_id")
                       for _, invocation in _linked_previews(db, agent, conversation_id)]
        statement = statement.where(WorkflowRun.id.in_([run_id for run_id in linked_runs if isinstance(run_id, str)]))
        replay = db.scalar(statement.where(WorkflowApprovalRequest.decision_message_id == channel_interaction_service.decision_message_key(runtime._actor(), payload.message_id)))
        statement = statement.where(WorkflowApprovalRequest.id == replay.id) if replay else statement.where(WorkflowApprovalRequest.status == "pending")
    else:
        from sqlalchemy import or_
        statement = statement.where(or_(WorkflowApprovalRequest.status == "pending",
            WorkflowApprovalRequest.decision_message_id == channel_interaction_service.decision_message_key(runtime._actor(), payload.message_id)))
    approvals = db.scalars(statement.order_by(WorkflowApprovalRequest.requested_at.desc()).limit(100)).all()
    eligible = []
    for approval in approvals:
        if parsed.code and parsed.code != capability_delivery_service.reply_code("approval", approval.id):
            continue
        try:
            channel_interaction_service.approval_context(db, runtime._actor(), approval.id)
        except channel_interaction_service.ChannelInteractionError:
            continue
        eligible.append(approval)
    if len(eligible) != 1:
        raise channel_interaction_service.ChannelInteractionError(
            "当前没有匹配且有权处理的审批待办" if not eligible else "有多个审批待办，请带上待办消息中的审批编号",
        )
    approval = eligible[0]
    reply = payload.model_copy(update={"expected_revision": 1})
    result = channel_interaction_service.reply_approval(db, runtime._actor(), approval.id, reply)
    response = {"answer": result["text"]}
    for _, invocation in _linked_previews(db, agent, conversation_id):
        output = (invocation.result_document or {}).get("output") or {}
        if output.get("workflow_run_id") == approval.workflow_run_id and output.get("execution_key") == approval.execution_key:
            document = capability_application_service.get_receipt(db, runtime._actor(), invocation.id)
            response["receipt"] = runtime._model_receipt(document)
            break
    return response


def handle_message(
    db: Session, agent: Agent, conversation_id: str, text: str, message_id: str,
    attachments: list[Any], runtime: Any,
    *, before_apply: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    parsed = parse_reply(text)
    if parsed is None:
        return None
    if before_apply is not None:
        before_apply()
    evidence = [EvidenceReference(**{
        key: getattr(item, key) for key in ("asset_version_id", "dataset_version_id", "expected_signature") if getattr(item, key, None) is not None
    }) for item in attachments]
    payload = ChannelReplyIn(text=text, message_id=message_id, expected_revision=1, evidence=evidence)
    try:
        if parsed.action in {"confirm", "cancel"}:
            return _confirmation(db, agent, conversation_id, parsed, payload, runtime)
        return _approval(db, agent, conversation_id, parsed, payload, runtime)
    except (channel_interaction_service.ChannelInteractionError, agent_capability_confirmation_service.AgentCapabilityConfirmationError,
            CapabilityInvocationError, capability_application_service.CapabilityApplicationError, agent_turn_payload_service.AgentTurnPayloadError) as exc:
        db.rollback()
        return {"answer": str(exc)}


def reply_events(reply: dict[str, Any], message_id: str) -> list[dict[str, Any]]:
    events = [{"type": "response_start"}, {"type": "token", "data": reply["answer"]}]
    if isinstance(reply.get("receipt"), dict):
        tool_id = f"reply-{message_id}"
        events.extend([
            {"type": "tool_call", "data": {"id": tool_id, "name": "invoke_capability", "args": {}}},
            {"type": "tool_result", "data": {"id": tool_id, "name": "invoke_capability", "result": json.dumps(reply["receipt"], ensure_ascii=False)}},
        ])
    return events
