"""Durable, server-authoritative confirmations for Agent event/workflow tools.

The model may only create a dry-run preview.  A later authenticated HTTP request
confirms that exact preview after its parent SSE message is final.  Parameters
are read from the preview log instead of being accepted from the browser.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    ActionExecutionLog,
    Agent,
    BusinessScenario,
    Conversation,
    Message,
)
from . import (
    agent_capability_service,
    capability_readiness_service,
    operations_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
)
from .policies import PolicyViolation, validate_action_params


class AgentConfirmationError(ValueError):
    """A preview cannot be safely confirmed in its current state."""


_TOOL_BY_TARGET = {
    "event": "prepare_event_publish",
    "workflow": "execute_workflow",
}


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

    existing = _existing_execution(db, preview)
    if existing:
        if not _message_contains_log(message, tool_name, existing.id):
            raise AgentConfirmationError("已执行结果与对话记录不一致，已阻止重复提交")
        return _response_from_execution(existing, replay=True)
    if not _message_contains_log(message, tool_name, preview.id):
        raise AgentConfirmationError("对话中不存在该服务端预演结果，已阻止执行")

    scenario = db.get(BusinessScenario, preview.scenario_id)
    if not scenario:
        raise AgentConfirmationError("预演所属业务场景已不存在")
    try:
        definition = runtime_definition_service.resolve_active(
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
    ):
        raise AgentConfirmationError("定义在预演后已变化，请重新预演")

    category = "events" if preview.target_type == "event" else "workflows"
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
    if (
        selected["mode"] != "all"
        and preview.target_id not in set(selected["selected_ids"])
    ):
        raise AgentConfirmationError(
            f"该{'事件' if preview.target_type == 'event' else '工作流'}已不在当前 Agent 的授权范围，请重新预演"
        )

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
