"""Durable, server-authoritative confirmations for Agent event/workflow tools.

The model may only create a dry-run preview.  A later authenticated HTTP request
confirms that exact preview after its parent SSE message is final.  Parameters
are read from the preview log instead of being accepted from the browser.
"""
from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    BusinessScenario,
    Conversation,
    Message,
    WorkflowApprovalRequest,
    WorkflowRun,
)
from . import (
    agent_capability_service,
    capability_readiness_service,
    operations_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    workflow_service,
)
from .policies import PolicyViolation, validate_action_params


class AgentConfirmationError(ValueError):
    """A preview cannot be safely confirmed in its current state."""


_TOOL_BY_TARGET = {
    "action": "execute_action",
    "event": "prepare_event_publish",
    "workflow": "execute_workflow",
}

_SIDE_EFFECT_TARGETS = frozenset(_TOOL_BY_TARGET)
_TEXT_CONFIRMATION_EXACT = frozenset(
    {
        "confirm",
        "confirmed",
        "yes, confirm",
        "确认",
        "确认执行",
        "确认提交",
        "确认发布",
        "同意执行",
        "同意提交",
        "同意发布",
        "批准执行",
        "批准提交",
        "批准发布",
    }
)
_TEXT_CONFIRMATION_NEGATIONS = (
    "取消",
    "拒绝",
    "不要",
    "不确认",
    "不执行",
    "不提交",
    "不发布",
    "暂不",
)
_TEXT_CONFIRMATION_CONTINUATION_NAME = "__agent_text_confirmation_continuation__"
_TEXT_CONFIRMATION_CONTINUATION_VERSION = 1
_NEGATIVE_CONFIRMATION_TARGET_PREFIXES = (
    "不",
    "别",
    "勿",
    "no ",
    "not ",
    "don't ",
    "do not ",
)
_WORKFLOW_APPROVAL_APPROVE_EXACT = frozenset(
    {
        "批准",
        "确认批准",
        "确认通过",
        "确认审批",
        "同意审批",
    }
)
_WORKFLOW_APPROVAL_REJECT_EXACT = frozenset(
    {
        "驳回",
        "拒绝",
        "确认驳回",
        "确认拒绝",
    }
)
_WORKFLOW_APPROVAL_NEGATION_TERMS = (
    "不批准",
    "不通过",
    "不驳回",
    "不拒绝",
    "不要",
    "暂不",
    "取消审批",
    "不处理审批",
    "别批准",
    "勿批准",
)
_WORKFLOW_APPROVAL_APPROVE_MARKERS = ("批准", "通过", "同意")
_WORKFLOW_APPROVAL_REJECT_MARKERS = ("驳回", "拒绝")


def _runtime_provenance(
    definition: runtime_definition_service.RuntimeDefinition,
) -> dict[str, Any]:
    environment = runtime_connector_service.runtime_environment()
    if definition.environment != environment:
        raise AgentConfirmationError("运行定义环境与当前部署环境不一致，已阻止预演")
    return {
        "environment": definition.environment,
        "definition_snapshot_id": definition.snapshot_id,
        "release_id": definition.release_id,
        "definition_hash": definition.definition_hash,
        "definition_source": definition.source,
    }


def _decision_context(db: Session, permission: dict[str, Any]) -> dict[str, Any]:
    audit = db.info.get("action_audit_context")
    audit = audit if isinstance(audit, dict) else {}
    trace = db.info.get("llm_trace_context")
    trace = trace if isinstance(trace, dict) else {}
    agent_id = str(audit.get("agent_id") or "").strip() or None
    actor_user_id = str(db.info.get("user_id") or "").strip() or None
    return {
        "actor_type": "agent" if agent_id else "user" if actor_user_id else "unknown",
        "actor_user_id": actor_user_id,
        "agent_id": agent_id,
        "llm_config_id": str(audit.get("llm_config_id") or "").strip() or None,
        "model_name": str(audit.get("model_name") or "")[:240],
        "permission_decision": permission,
        "data_context": {},
        "correlation_id": str(trace.get("correlation_id") or uuid.uuid4().hex)[:64],
        "agent_message_id": (
            str(trace.get("assistant_message_id") or "").strip() or None
            if agent_id else None
        ),
        "assistant_message_id": None,
    }


def _preview_response(log: ActionExecutionLog) -> dict[str, Any]:
    result = log.result or {}
    plan = result.get("plan") if isinstance(result, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    response = {
        "log_id": log.id,
        "status": "confirmation_required",
        "confirmation_type": log.target_type,
        "requires_confirmation": True,
        "result": result,
        "environment": log.environment,
        "definition_snapshot_id": log.definition_snapshot_id,
        "release_id": log.release_id,
        "definition_hash": log.definition_hash,
        "definition_source": log.definition_source,
        "correlation_id": log.correlation_id,
        "message": "预演已固定定义和参数；只有当前用户在对话完成后确认，才会产生副作用。",
    }
    if log.target_type == "event":
        response.update({
            "event_id": plan.get("event_id") or log.target_id,
            "event_name": plan.get("event_name") or log.target_name,
            "payload": plan.get("payload") or {},
        })
    elif log.target_type == "workflow":
        response.update({
            "workflow_id": plan.get("workflow_id") or log.target_id,
            "workflow_name": plan.get("workflow_name") or log.target_name,
            "params": plan.get("params") or {},
        })
    return response


def preview_event_publish(
    db: Session,
    event: Any,
    payload: dict[str, Any] | None,
    *,
    runtime_definition: runtime_definition_service.RuntimeDefinition,
) -> dict[str, Any]:
    """Persist a no-side-effect event publication preview."""
    try:
        event = runtime_definition_service.resolve_resource(
            runtime_definition, "event", event.id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation("事件不存在于当前运行定义") from exc
    capability_readiness_service.require_executable(
        "event", event, definition=runtime_definition, db=db
    )
    normalized = validate_action_params(event.payload_schema or {}, payload or {})
    scenario = db.get(BusinessScenario, event.scenario_id)
    if not scenario:
        raise PolicyViolation("事件所属业务场景不存在")
    decision = permission_service.check_scenario(db, scenario, "read")
    if not decision.allowed:
        raise PermissionError("没有预演该事件的权限")
    permission = {
        "allowed": True,
        "scope": "event",
        "verb": "read",
        "reason": decision.reason,
        "role": decision.role_key,
        "confirmed": False,
    }
    plan = {
        "confirmation_type": "event",
        "event_id": event.id,
        "event_name": event.name,
        "payload": normalized,
        "side_effects": ["发布事件", "可能触发订阅工作流"],
        "side_effects_skipped": True,
    }
    log = ActionExecutionLog(
        scenario_id=event.scenario_id,
        target_type="event",
        target_id=event.id,
        target_name=event.name,
        input_params=normalized,
        status="dry_run",
        mode="dry_run",
        result={"plan": plan, "permission": permission},
        **_decision_context(db, permission),
        **_runtime_provenance(runtime_definition),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _preview_response(log)


def preview_workflow_run(
    db: Session,
    workflow: Any,
    params: dict[str, Any] | None,
    *,
    runtime_definition: runtime_definition_service.RuntimeDefinition,
) -> dict[str, Any]:
    """Persist a no-side-effect WorkflowRun preview."""
    try:
        workflow = runtime_definition_service.resolve_resource(
            runtime_definition, "workflow", workflow.id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise PolicyViolation("工作流不存在于当前运行定义") from exc
    capability_readiness_service.require_executable(
        "workflow", workflow, definition=runtime_definition, db=db
    )
    normalized = dict(params or {})
    decision = permission_service.check_workflow(db, workflow, "read")
    if not decision.allowed:
        raise PermissionError("没有预演该工作流的权限")
    permission = {
        "allowed": True,
        "scope": "workflow",
        "verb": "read",
        "reason": decision.reason,
        "role": decision.role_key,
        "confirmed": False,
    }
    plan = {
        "confirmation_type": "workflow",
        "workflow_id": workflow.id,
        "workflow_name": workflow.name,
        "params": normalized,
        "side_effects": ["提交工作流任务", "工作流节点可能执行已配置操作"],
        "side_effects_skipped": True,
    }
    log = ActionExecutionLog(
        scenario_id=workflow.scenario_id,
        target_type="workflow",
        target_id=workflow.id,
        target_name=workflow.name,
        input_params=normalized,
        status="dry_run",
        mode="dry_run",
        result={"plan": plan, "permission": permission},
        **_decision_context(db, permission),
        **_runtime_provenance(runtime_definition),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return _preview_response(log)


def _parsed_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _tool_call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or function.get("name") or "")


def _message_contains_log(message: Message, tool_name: str, log_id: str) -> bool:
    call_ids = {
        str(call.get("id") or "")
        for call in (message.tool_calls or [])
        if isinstance(call, dict) and _tool_call_name(call) == tool_name
    }
    for entry in message.tool_results or []:
        if not isinstance(entry, dict):
            continue
        if call_ids and str(entry.get("id") or "") not in call_ids:
            continue
        if str(entry.get("name") or tool_name) != tool_name:
            continue
        parsed = _parsed_result(entry.get("result"))
        if parsed and str(parsed.get("log_id") or "") == log_id:
            return True
    return False


def _replace_message_result(
    message: Message,
    *,
    tool_name: str,
    preview_log_id: str,
    response: dict[str, Any],
) -> bool:
    updated: list[dict[str, Any]] = []
    changed = False
    for entry in message.tool_results or []:
        item = dict(entry) if isinstance(entry, dict) else {"result": entry}
        parsed = _parsed_result(item.get("result"))
        if (
            str(item.get("name") or tool_name) == tool_name
            and parsed
            and str(parsed.get("log_id") or "") == preview_log_id
        ):
            item["result"] = json.dumps(response, ensure_ascii=False, default=str)
            changed = True
        updated.append(item)
    if changed:
        message.tool_results = updated
    return changed


def _normalize_confirmation_text(value: object) -> str:
    """Normalize user-entered confirmation references without fuzzy matching."""
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()
    )


def _normalize_confirmation_reference(value: object) -> str:
    return _normalize_confirmation_text(value).casefold()


def _is_negative_confirmation_target(target: str) -> bool:
    normalized = _normalize_confirmation_reference(target)
    return bool(normalized) and normalized.startswith(
        _NEGATIVE_CONFIRMATION_TARGET_PREFIXES
    )


def _text_confirmation_target(text: object) -> str | None:
    """Return an explicitly confirmed target reference, or ``None``.

    A pending Agent action can have external side effects.  Treat only a small
    set of unambiguous confirmation phrases as an approval; conversational
    phrases such as "continue" or a question containing "confirm" must still
    go through the normal Agent turn.
    """
    normalized = _normalize_confirmation_text(text).strip("。！？!?，,；;：:")
    folded = normalized.casefold()
    if not folded or any(term in normalized for term in _TEXT_CONFIRMATION_NEGATIONS):
        return None
    if folded in _TEXT_CONFIRMATION_EXACT:
        return ""
    match = re.fullmatch(
        r"(?:请)?(?:确认|同意|批准)\s*(?:执行|提交|发布)?(?:[：:\s]+)(.+)",
        normalized,
    )
    if not match:
        return None
    target = match.group(1).strip("。！？!?，,；;：: ")
    if not target or _is_negative_confirmation_target(target):
        return None
    return target


def _workflow_approval_text_intent(
    text: object,
) -> tuple[str, bool | None, str] | None:
    """Parse an explicit workflow-node approval decision without fuzzy intent.

    The model must never infer a pending workflow decision from conversational
    language.  ``decision`` is the only executable outcome; negative and
    mixed-direction approval language are consumed as safe no-ops so they do
    not fall through into another Agent tool turn.
    """
    normalized = _normalize_confirmation_text(text).strip("。！？!?，,；;：:")
    if not normalized:
        return None
    has_approve = any(marker in normalized for marker in _WORKFLOW_APPROVAL_APPROVE_MARKERS)
    has_reject = any(marker in normalized for marker in _WORKFLOW_APPROVAL_REJECT_MARKERS)
    if any(term in normalized for term in _WORKFLOW_APPROVAL_NEGATION_TERMS):
        if has_approve or has_reject or "审批" in normalized:
            return "negative", None, ""
        return None
    if has_approve and has_reject:
        return "ambiguous", None, ""

    folded = normalized.casefold()
    if folded in _WORKFLOW_APPROVAL_APPROVE_EXACT:
        return "decision", True, ""
    if folded in _WORKFLOW_APPROVAL_REJECT_EXACT:
        return "decision", False, ""

    patterns = (
        (True, r"(?:请)?(?:确认)?(?:批准|通过)(?:审批)?"),
        (True, r"(?:请)?同意审批"),
        (False, r"(?:请)?(?:确认)?(?:驳回|拒绝)(?:审批)?"),
    )
    for approved, prefix in patterns:
        match = re.fullmatch(rf"{prefix}(?:[：:\s]+(.+))?", normalized)
        if not match:
            continue
        target = str(match.group(1) or "").strip("。！？!?，,；;：: ")
        if not target:
            return "decision", approved, ""
        target_has_approve = any(
            marker in target for marker in _WORKFLOW_APPROVAL_APPROVE_MARKERS
        )
        target_has_reject = any(
            marker in target for marker in _WORKFLOW_APPROVAL_REJECT_MARKERS
        )
        if (
            _is_negative_confirmation_target(target)
            or any(term in target for term in _WORKFLOW_APPROVAL_NEGATION_TERMS)
            or (target_has_approve and target_has_reject)
            or (approved and target_has_reject)
            or (not approved and target_has_approve)
        ):
            return "ambiguous", None, ""
        return "decision", approved, target
    return None


def _pending_previews_for_message(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
    message_id: str,
    preview_ids: tuple[str, ...] | None = None,
) -> list[ActionExecutionLog]:
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    statement = select(ActionExecutionLog).where(
        ActionExecutionLog.agent_message_id == message_id,
        ActionExecutionLog.agent_id == agent.id,
        ActionExecutionLog.actor_user_id == current_user_id,
        ActionExecutionLog.scenario_id == agent.scenario_id,
        ActionExecutionLog.target_type.in_(tuple(sorted(_SIDE_EFFECT_TARGETS))),
        ActionExecutionLog.mode == "dry_run",
        ActionExecutionLog.status == "dry_run",
    )
    if preview_ids is not None:
        if not preview_ids:
            return []
        statement = statement.where(ActionExecutionLog.id.in_(preview_ids))
    return list(
        db.execute(statement.order_by(ActionExecutionLog.created_at.desc())).scalars().all()
    )


def _continuation_payload(message: Message) -> dict[str, Any] | None:
    for entry in reversed(message.tool_results or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "") != _TEXT_CONFIRMATION_CONTINUATION_NAME:
            continue
        payload = _parsed_result(entry.get("result"))
        if payload and payload.get("version") == _TEXT_CONFIRMATION_CONTINUATION_VERSION:
            return payload
    return None


def _continuation_preview_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    raw_ids = payload.get("preview_ids")
    if not isinstance(raw_ids, list):
        return ()
    ids = tuple(dict.fromkeys(str(item).strip() for item in raw_ids if str(item).strip()))
    return ids if len(ids) <= 20 else ()


def _record_ambiguous_continuation(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
    candidates: list[ActionExecutionLog],
    text: object,
) -> None:
    """Bind one ambiguous reply to its immediate preview turn.

    This lives in the server-created user message metadata rather than message
    content, so a later model response cannot forge a continuation to an old
    preview. The router commits this metadata with its ambiguity response.
    """
    source_ids = {str(preview.agent_message_id or "") for preview in candidates}
    source_ids.discard("")
    if len(source_ids) != 1:
        return
    latest_user_message = db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.role == "user",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    ).scalars().first()
    if latest_user_message is None or latest_user_message.content != str(text or ""):
        return
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    payload = {
        "version": _TEXT_CONFIRMATION_CONTINUATION_VERSION,
        "conversation_id": conversation.id,
        "agent_id": agent.id,
        "actor_user_id": current_user_id,
        "source_message_id": source_ids.pop(),
        "preview_ids": [str(preview.id) for preview in candidates],
    }
    results = [
        dict(entry) if isinstance(entry, dict) else {"result": entry}
        for entry in (latest_user_message.tool_results or [])
        if not (
            isinstance(entry, dict)
            and str(entry.get("name") or "") == _TEXT_CONFIRMATION_CONTINUATION_NAME
        )
    ]
    results.append(
        {
            "id": f"agent-confirmation-continuation:{uuid.uuid4().hex}",
            "name": _TEXT_CONFIRMATION_CONTINUATION_NAME,
            "result": payload,
        }
    )
    latest_user_message.tool_results = results


def _pending_previews_from_continuation(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
    messages: list[Message],
    latest_answer_index: int,
) -> list[ActionExecutionLog]:
    """Resolve only a server-recorded ambiguity continuation, never old history."""
    if latest_answer_index < 2:
        return []
    continuation_message = messages[latest_answer_index - 1]
    if continuation_message.role != "user":
        return []
    payload = _continuation_payload(continuation_message)
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    if not payload or (
        str(payload.get("conversation_id") or "") != str(conversation.id)
        or str(payload.get("agent_id") or "") != str(agent.id)
        or payload.get("actor_user_id") != current_user_id
    ):
        return []
    preview_ids = _continuation_preview_ids(payload)
    source_message_id = str(payload.get("source_message_id") or "").strip()
    if not preview_ids or not source_message_id:
        return []
    source_message = messages[latest_answer_index - 2]
    if source_message.role != "assistant" or source_message.id != source_message_id:
        return []
    return _pending_previews_for_message(
        db,
        agent=agent,
        conversation=conversation,
        message_id=source_message_id,
        preview_ids=preview_ids,
    )


def _latest_pending_previews(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
) -> list[ActionExecutionLog]:
    """Return this turn's previews or its one server-recorded retry continuation."""
    messages = list(
        db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at, Message.id)
        ).scalars().all()
    )
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "assistant":
            continue
        previews = _pending_previews_for_message(
            db,
            agent=agent,
            conversation=conversation,
            message_id=message.id,
        )
        if previews:
            return previews
        return _pending_previews_from_continuation(
            db,
            agent=agent,
            conversation=conversation,
            messages=messages,
            latest_answer_index=index,
        )
    return []


def _matches_confirmation_target(preview: ActionExecutionLog, target: str) -> bool:
    requested = _normalize_confirmation_reference(target)
    candidates = (
        _normalize_confirmation_reference(preview.target_name),
        _normalize_confirmation_reference(preview.target_id),
    )
    return bool(requested) and any(candidate == requested for candidate in candidates if candidate)


def _pending_workflow_approvals_for_conversation(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
) -> list[tuple[WorkflowApprovalRequest, WorkflowRun]]:
    """Load only approval nodes durably bound to this private Agent chat."""
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    if (
        not current_user_id
        or conversation.agent_id != agent.id
        or conversation.created_by_user_id != current_user_id
        or not agent.scenario_id
    ):
        return []
    rows = db.execute(
        select(WorkflowApprovalRequest, WorkflowRun)
        .join(WorkflowRun, WorkflowRun.id == WorkflowApprovalRequest.workflow_run_id)
        .where(
            WorkflowApprovalRequest.status == "pending",
            WorkflowRun.status == "awaiting_approval",
            WorkflowRun.agent_conversation_id == conversation.id,
            WorkflowRun.created_by_user_id == current_user_id,
            WorkflowRun.scenario_id == agent.scenario_id,
        )
        .order_by(
            WorkflowApprovalRequest.requested_at.asc(),
            WorkflowApprovalRequest.id.asc(),
        )
    ).all()
    return [(approval, run) for approval, run in rows]


def _workflow_approval_label(
    approval: WorkflowApprovalRequest,
    run: WorkflowRun,
) -> tuple[str, str]:
    workflow_name = str(getattr(getattr(run, "workflow", None), "name", "") or run.workflow_id)
    node_name = str(approval.node_name or approval.node_id)
    return workflow_name, node_name


def _matches_workflow_approval_target(
    approval: WorkflowApprovalRequest,
    run: WorkflowRun,
    target: str,
) -> bool:
    requested = _normalize_confirmation_reference(target)
    if not requested:
        return False
    workflow_name, node_name = _workflow_approval_label(approval, run)
    candidates = (
        workflow_name,
        run.workflow_id,
        node_name,
        approval.node_id,
        f"{workflow_name} {node_name}",
        f"{workflow_name}/{node_name}",
        f"{run.workflow_id}/{approval.node_id}",
    )
    return any(
        _normalize_confirmation_reference(candidate) == requested
        for candidate in candidates
        if candidate
    )


def _workflow_approval_selection_message(
    candidates: list[tuple[WorkflowApprovalRequest, WorkflowRun]],
) -> str:
    labels = [
        f"{_workflow_approval_label(approval, run)[0]} / "
        f"{_workflow_approval_label(approval, run)[1]}"
        for approval, run in candidates[:5]
    ]
    suffix = f"待审批：{'、'.join(labels)}" if labels else ""
    return (
        "当前回复无法唯一定位待审批节点。请回复“确认批准 <工作流或节点名称>”"
        "或“确认驳回 <工作流或节点名称>”。"
        + suffix
    )


def _confirm_workflow_approval_text_reply(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
    text: object,
) -> dict[str, Any] | None:
    """Apply one explicit approval-node decision through the existing service.

    This is intentionally separate from Agent dry-run confirmation.  An
    explicit workflow ``approval`` node is a durable pause even when preceding
    Actions did not require individual confirmation.
    """
    intent = _workflow_approval_text_intent(text)
    if intent is None:
        return None
    kind, approved, target = intent
    if kind == "negative":
        return {
            "status": "approval_not_decided",
            "message": "未批准或驳回工作流审批；任务仍保持等待审批状态。",
        }
    if kind == "ambiguous":
        return {
            "status": "ambiguous_approval",
            "message": "审批回复同时包含冲突或否定含义，未改变任务状态。",
        }

    candidates = _pending_workflow_approvals_for_conversation(
        db,
        agent=agent,
        conversation=conversation,
    )
    if not candidates:
        return {
            "status": "no_pending_approval",
            "message": "当前对话没有待处理的工作流审批；不会改变任务状态。",
        }
    selected = candidates
    if target:
        selected = [
            item
            for item in candidates
            if _matches_workflow_approval_target(item[0], item[1], target)
        ]
    if len(selected) != 1:
        return {
            "status": "ambiguous_approval",
            "message": _workflow_approval_selection_message(candidates),
        }

    approval, run = selected[0]
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    workflow_name, node_name = _workflow_approval_label(approval, run)
    try:
        updated_run = operations_service.decide_approval(
            db,
            run,
            approved=bool(approved),
            comment=("Agent 对话文本批准" if approved else "Agent 对话文本驳回"),
            user_id=current_user_id,
        )
    except (HTTPException, PermissionError, PolicyViolation, ValueError) as exc:
        db.rollback()
        return {
            "status": "approval_failed",
            "workflow_run_id": run.id,
            "approval_id": approval.id,
            "message": f"无法处理工作流“{workflow_name}”的审批节点“{node_name}”：{exc}",
        }

    decision = "approved" if approved else "rejected"
    message = (
        f"已批准工作流“{workflow_name}”的审批节点“{node_name}”，任务已恢复排队。"
        if approved
        else f"已驳回工作流“{workflow_name}”的审批节点“{node_name}”，任务已结束。"
    )
    return {
        "status": decision,
        "decision": decision,
        "approval_id": approval.id,
        "workflow_run_id": updated_run.id,
        "workflow_id": updated_run.workflow_id,
        "workflow_name": workflow_name,
        "node_id": approval.node_id,
        "node_name": node_name,
        "run_status": updated_run.status,
        "message": message,
    }


def _pinned_preview_resource(
    db: Session,
    preview: ActionExecutionLog,
) -> tuple[BusinessScenario, runtime_definition_service.RuntimeDefinition, Any]:
    scenario = db.get(BusinessScenario, preview.scenario_id)
    if not scenario:
        raise AgentConfirmationError("预演所属业务场景已不存在")
    try:
        is_legacy_live_preview = (
            preview.definition_source == "live"
            and preview.definition_snapshot_id is None
            and preview.release_id is None
        )
        if is_legacy_live_preview:
            definition = runtime_definition_service.resolve_authoring(
                db,
                scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
        else:
            definition = runtime_definition_service.resolve_execution(
                db,
                scenario,
                environment=runtime_connector_service.runtime_environment(),
            )
        resource = runtime_definition_service.resolve_resource(
            definition, preview.target_type, preview.target_id
        )
    except runtime_definition_service.RuntimeDefinitionError as exc:
        raise AgentConfirmationError(f"当前运行定义不可执行该预演：{exc}") from exc
    if (
        definition.environment != preview.environment
        or definition.snapshot_id != preview.definition_snapshot_id
        or definition.release_id != preview.release_id
        or definition.definition_hash != preview.definition_hash
        or definition.source != preview.definition_source
    ):
        raise AgentConfirmationError("定义在预演后已变化，请重新预演")
    return scenario, definition, resource


def _require_agent_capability(
    agent: Agent,
    preview: ActionExecutionLog,
) -> None:
    category_by_target = {
        "action": "actions",
        "event": "events",
        "workflow": "workflows",
    }
    category = category_by_target.get(preview.target_type)
    if category is None:
        raise AgentConfirmationError("该预演目标不支持确认")
    raw_scope = (
        agent_capability_service.legacy_all_scope()
        if agent.capability_scope is None
        else agent.capability_scope
    )
    scope = agent_capability_service.normalize_scope(
        raw_scope,
        legacy_default=False,
        allow_all=True,
    )
    selected = scope[category]
    if selected["mode"] != "all" and preview.target_id not in set(selected["selected_ids"]):
        labels = {"action": "操作", "event": "事件", "workflow": "工作流"}
        raise AgentConfirmationError(
            f"该{labels[preview.target_type]}已不在当前 Agent 的授权范围，请重新预演"
        )


def _event_dict(envelope: Any, queued_runs: list[Any]) -> dict[str, Any]:
    return {
        "id": envelope.id,
        "scenario_id": envelope.scenario_id,
        "event_id": envelope.event_id,
        "name": envelope.name,
        "payload": envelope.payload or {},
        "source": envelope.source,
        "source_run_id": envelope.source_run_id,
        "environment": envelope.environment,
        "definition_snapshot_id": envelope.definition_snapshot_id,
        "release_id": envelope.release_id,
        "definition_hash": envelope.definition_hash,
        "definition_source": envelope.definition_source,
        "created_at": envelope.created_at.isoformat() if envelope.created_at else None,
        "queued_workflow_run_ids": [run.id for run in queued_runs],
    }


def _workflow_run_dict(run: Any, workflow_name: str) -> dict[str, Any]:
    return {
        "id": run.id,
        "scenario_id": run.scenario_id,
        "workflow_id": run.workflow_id,
        "workflow_name": workflow_name,
        "trigger_source": run.trigger_source,
        "status": run.status,
        "input_params": run.input_params or {},
        "environment": run.environment,
        "definition_snapshot_id": run.definition_snapshot_id,
        "release_id": run.release_id,
        "definition_hash": run.definition_hash,
        "definition_source": run.definition_source,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _response_from_execution(
    execution: ActionExecutionLog,
    *,
    replay: bool = False,
) -> dict[str, Any]:
    return {
        "log_id": execution.id,
        "status": "idempotent_replay" if replay else execution.status,
        "original_status": execution.status if replay else None,
        "confirmation_type": execution.target_type,
        "result": execution.result or {},
        "error": execution.error or "",
        "environment": execution.environment,
        "definition_snapshot_id": execution.definition_snapshot_id,
        "release_id": execution.release_id,
        "definition_hash": execution.definition_hash,
        "definition_source": execution.definition_source,
        "correlation_id": execution.correlation_id,
        "parent_preview_log_id": execution.parent_action_log_id,
    }


def _existing_execution(db: Session, preview: ActionExecutionLog) -> ActionExecutionLog | None:
    return db.execute(
        select(ActionExecutionLog).where(
            ActionExecutionLog.parent_action_log_id == preview.id,
            ActionExecutionLog.target_type == preview.target_type,
            ActionExecutionLog.target_id == preview.target_id,
            ActionExecutionLog.mode == "execute",
        )
    ).scalars().first()


def _validate_preview_context(
    db: Session,
    preview: ActionExecutionLog | None,
    *,
    agent: Agent,
    conversation: Conversation,
    correlation_id: str,
    expected_environment: str,
    expected_definition_snapshot_id: str | None,
    expected_release_id: str | None,
    expected_definition_hash: str,
) -> tuple[Message, str]:
    current_user_id = str(db.info.get("user_id") or "").strip() or None
    if (
        preview is None
        or preview.target_type not in _TOOL_BY_TARGET
        or preview.mode != "dry_run"
        or preview.status != "dry_run"
        or preview.actor_user_id != current_user_id
        or preview.agent_id != agent.id
        or preview.scenario_id != agent.scenario_id
        or conversation.agent_id != agent.id
        or conversation.created_by_user_id != current_user_id
        or not preview.agent_message_id
    ):
        raise AgentConfirmationError("预演与当前用户、Agent、对话或目标不一致，请重新预演")
    if (
        correlation_id != preview.correlation_id
        or expected_environment != preview.environment
        or expected_definition_snapshot_id != preview.definition_snapshot_id
        or expected_release_id != preview.release_id
        or expected_definition_hash != preview.definition_hash
    ):
        raise AgentConfirmationError("确认信息与服务端预演版本不一致，请重新预演")
    message = db.get(Message, preview.agent_message_id)
    if (
        not message
        or message.role != "assistant"
        or message.conversation_id != conversation.id
    ):
        raise AgentConfirmationError("预演所属的 Agent 消息或对话已不可用，请重新预演")
    if not message.stream_finalized:
        raise AgentConfirmationError("Agent 回答仍在生成，请等待对话完成后再确认")
    return message, _TOOL_BY_TARGET[preview.target_type]


def confirm_preview(
    db: Session,
    preview_log_id: str,
    *,
    agent: Agent,
    conversation: Conversation,
    correlation_id: str,
    expected_environment: str,
    expected_definition_snapshot_id: str | None,
    expected_release_id: str | None,
    expected_definition_hash: str,
) -> dict[str, Any]:
    """Confirm exactly one server-issued event/workflow preview, at most once."""
    preview = db.get(ActionExecutionLog, preview_log_id)
    message, tool_name = _validate_preview_context(
        db,
        preview,
        agent=agent,
        conversation=conversation,
        correlation_id=correlation_id,
        expected_environment=expected_environment,
        expected_definition_snapshot_id=expected_definition_snapshot_id,
        expected_release_id=expected_release_id,
        expected_definition_hash=expected_definition_hash,
    )
    assert preview is not None  # narrowed by _validate_preview_context
    if preview.target_type not in {"event", "workflow"}:
        raise AgentConfirmationError("该预演必须通过操作确认流程执行")

    existing = _existing_execution(db, preview)
    if existing:
        if not _message_contains_log(message, tool_name, existing.id):
            raise AgentConfirmationError("已执行结果与对话记录不一致，已阻止重复提交")
        return _response_from_execution(existing, replay=True)
    if not _message_contains_log(message, tool_name, preview.id):
        raise AgentConfirmationError("对话中不存在该服务端预演结果，已阻止执行")

    scenario, definition, resource = _pinned_preview_resource(db, preview)
    _require_agent_capability(agent, preview)

    current_user_id = str(db.info.get("user_id") or "").strip() or None
    if preview.target_type == "event":
        decision = permission_service.check_scenario(db, scenario, "write")
        if not decision.allowed:
            raise PermissionError("没有发布该事件的权限")
        normalized = validate_action_params(
            resource.payload_schema or {}, dict(preview.input_params or {})
        )
        if normalized != (preview.input_params or {}):
            raise AgentConfirmationError("事件载荷校验结果已变化，请重新预演")
    else:
        decision = permission_service.check_workflow(db, resource, "execute")
        if not decision.allowed:
            raise PermissionError("没有提交该工作流的权限")

    permission = {
        "allowed": True,
        "scope": preview.target_type,
        "verb": "execute",
        "reason": decision.reason,
        "role": decision.role_key,
        "confirmed": True,
    }
    execution = ActionExecutionLog(
        scenario_id=preview.scenario_id,
        target_type=preview.target_type,
        target_id=preview.target_id,
        target_name=preview.target_name,
        input_params=dict(preview.input_params or {}),
        status="running",
        mode="execute",
        idempotency_key=f"{preview.environment}:agent-confirm:{preview.id}",
        environment=preview.environment,
        definition_snapshot_id=preview.definition_snapshot_id,
        release_id=preview.release_id,
        definition_hash=preview.definition_hash,
        definition_source=preview.definition_source,
        actor_type="user",
        actor_user_id=current_user_id,
        agent_id=preview.agent_id,
        llm_config_id=preview.llm_config_id,
        model_name=preview.model_name,
        permission_decision=permission,
        data_context=preview.data_context or {},
        correlation_id=preview.correlation_id,
        parent_action_log_id=preview.id,
        agent_message_id=preview.agent_message_id,
        assistant_message_id=preview.assistant_message_id,
    )
    db.add(execution)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = _existing_execution(db, preview)
        if replay and _message_contains_log(message, tool_name, replay.id):
            return _response_from_execution(replay, replay=True)
        raise AgentConfirmationError("该预演已被确认或正在处理，请刷新对话结果")

    try:
        dedupe_key = f"agent-confirm:{preview.id}"
        if preview.target_type == "event":
            envelope, queued_runs = operations_service.publish_event(
                db,
                resource,
                dict(preview.input_params or {}),
                source="agent",
                dedupe_key=dedupe_key,
                created_by_user_id=current_user_id,
                runtime_definition=definition,
            )
            execution.result = {
                "event_envelope": _event_dict(envelope, queued_runs),
                "queued_workflow_run_ids": [run.id for run in queued_runs],
            }
        else:
            run, _created = operations_service.enqueue_workflow_run(
                db,
                resource,
                dict(preview.input_params or {}),
                trigger_source="manual",
                dedupe_key=dedupe_key,
                created_by_user_id=current_user_id,
                runtime_definition=definition,
                agent_conversation_id=conversation.id,
            )
            execution.result = {
                "workflow_run": _workflow_run_dict(run, resource.name),
                "task_url": f"/tasks?task={run.id}",
            }
        execution.status = "success"
        response = _response_from_execution(execution)
        if not _replace_message_result(
            message,
            tool_name=tool_name,
            preview_log_id=preview.id,
            response=response,
        ):
            raise AgentConfirmationError("预演结果已从对话中变化，已阻止执行")
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def confirm_action_preview(
    db: Session,
    preview_log_id: str,
    *,
    agent: Agent,
    conversation: Conversation,
    correlation_id: str,
    expected_environment: str,
    expected_definition_snapshot_id: str | None,
    expected_release_id: str | None,
    expected_definition_hash: str,
) -> dict[str, Any]:
    """Confirm one Agent Action preview without accepting browser parameters."""
    preview = db.get(ActionExecutionLog, preview_log_id)
    message, tool_name = _validate_preview_context(
        db,
        preview,
        agent=agent,
        conversation=conversation,
        correlation_id=correlation_id,
        expected_environment=expected_environment,
        expected_definition_snapshot_id=expected_definition_snapshot_id,
        expected_release_id=expected_release_id,
        expected_definition_hash=expected_definition_hash,
    )
    assert preview is not None  # narrowed by _validate_preview_context
    if preview.target_type != "action":
        raise AgentConfirmationError("该预演不是操作确认")

    existing = _existing_execution(db, preview)
    if existing:
        if not _message_contains_log(message, tool_name, existing.id):
            raise AgentConfirmationError("已执行结果与对话记录不一致，已阻止重复提交")
    elif not _message_contains_log(message, tool_name, preview.id):
        raise AgentConfirmationError("对话中不存在该服务端预演结果，已阻止执行")

    _scenario, definition, action = _pinned_preview_resource(db, preview)
    _require_agent_capability(agent, preview)
    if not bool(getattr(action, "requires_confirmation", False)):
        raise AgentConfirmationError("该操作已不再要求人工确认，请重新发起业务请求")
    normalized = workflow_service.validate_action_params(
        action.input_schema or {}, dict(preview.input_params or {})
    )
    if normalized != (preview.input_params or {}):
        raise AgentConfirmationError("操作参数校验结果已变化，请重新预演")

    previous_lineage = db.info.get("action_lineage_context")
    db.info["action_lineage_context"] = {
        "correlation_id": preview.correlation_id,
        "parent_action_log_id": preview.id,
        "agent_message_id": preview.agent_message_id,
        "assistant_message_id": preview.assistant_message_id,
    }
    try:
        response = workflow_service.execute_action(
            db,
            action,
            normalized,
            confirm=True,
            dry_run=False,
            idempotency_key=f"agent-confirm:{preview.id}",
            runtime_environment=definition.environment,
            runtime_definition=definition,
        )
        if not _replace_message_result(
            message,
            tool_name=tool_name,
            preview_log_id=preview.id,
            response=response,
        ):
            raise AgentConfirmationError("预演结果已从对话中变化，已阻止执行")
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise
    finally:
        if previous_lineage is None:
            db.info.pop("action_lineage_context", None)
        else:
            db.info["action_lineage_context"] = previous_lineage


def confirm_text_reply(
    db: Session,
    *,
    agent: Agent,
    conversation: Conversation,
    text: object,
) -> dict[str, Any] | None:
    """Handle a user text approval for exactly one current Agent preview.

    The browser and MCP transports both call this before routing another model
    turn.  All effect-bearing values remain in the durable preview log; text
    only selects a current, already-rendered preview by its display name.
    """
    workflow_approval = _confirm_workflow_approval_text_reply(
        db,
        agent=agent,
        conversation=conversation,
        text=text,
    )
    if workflow_approval is not None:
        return workflow_approval
    target = _text_confirmation_target(text)
    if target is None:
        return None
    candidates = _latest_pending_previews(
        db,
        agent=agent,
        conversation=conversation,
    )
    if not candidates:
        # A transport can retry after the original confirmation already
        # committed but before its response reached the caller.  Do not send a
        # second explicit approval back through the model loop: it could be
        # interpreted as a fresh request and create another effect.  Returning
        # a durable no-op keeps browser and MCP retries safe in the same
        # conversation.
        return {
            "status": "no_pending",
            "message": "当前对话没有待确认项；不会执行新的操作。",
        }
    selected = candidates
    if target:
        selected = [
            preview for preview in candidates
            if _matches_confirmation_target(preview, target)
        ]
    if len(selected) != 1:
        _record_ambiguous_continuation(
            db,
            agent=agent,
            conversation=conversation,
            candidates=candidates,
            text=text,
        )
        names = "、".join(
            f"{preview.target_name or preview.target_id}（{preview.target_type}）"
            for preview in candidates[:5]
        )
        return {
            "status": "ambiguous",
            "message": (
                "当前回复无法唯一定位待确认项。请回复“确认执行 <操作名称>”、"
                "“确认发布 <事件名称>”或“确认提交 <工作流名称>”。"
                + (f"待确认：{names}" if names else "")
            ),
        }

    preview = selected[0]
    try:
        if preview.target_type == "action":
            response = confirm_action_preview(
                db,
                preview.id,
                agent=agent,
                conversation=conversation,
                correlation_id=preview.correlation_id,
                expected_environment=preview.environment,
                expected_definition_snapshot_id=preview.definition_snapshot_id,
                expected_release_id=preview.release_id,
                expected_definition_hash=preview.definition_hash,
            )
        else:
            response = confirm_preview(
                db,
                preview.id,
                agent=agent,
                conversation=conversation,
                correlation_id=preview.correlation_id,
                expected_environment=preview.environment,
                expected_definition_snapshot_id=preview.definition_snapshot_id,
                expected_release_id=preview.release_id,
                expected_definition_hash=preview.definition_hash,
            )
    except (AgentConfirmationError, PermissionError, PolicyViolation, ValueError) as exc:
        db.rollback()
        return {
            "status": "failed",
            "preview_log_id": preview.id,
            "message": f"无法确认“{preview.target_name or preview.target_id}”：{exc}",
        }

    succeeded = response.get("status") == "success" or (
        response.get("status") == "idempotent_replay"
        and response.get("original_status") == "success"
    )
    label = {"action": "操作", "event": "事件", "workflow": "工作流"}[preview.target_type]
    return {
        "status": "confirmed" if succeeded else "failed",
        "preview_log_id": preview.id,
        "response": response,
        "message": (
            f"已根据你的文本确认完成{label}“{preview.target_name or preview.target_id}”。"
            if succeeded
            else f"已根据你的文本确认执行{label}“{preview.target_name or preview.target_id}”，但未成功完成。"
        ),
    }
