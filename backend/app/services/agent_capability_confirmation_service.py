"""Human confirmation adapter over the same CapabilityInvoker used by Agent tools."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, CapabilityInvocation, Conversation, LLMConfig, Message
from . import (
    agent_capability_confirmation_payload as payload_store,
    agent_runtime_adapter,
    agent_turn_input_service,
    capability_application_service,
    permission_service,
    agent_capability_service,
    runtime_definition_service,
)
from .capability_contracts import Actor


class AgentCapabilityConfirmationError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _linked(message: Message, invocation_id: str) -> bool:
    for item in message.tool_results or []:
        if not isinstance(item, dict) or item.get("name") != "invoke_capability":
            continue
        value = item.get("result")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict) and value.get("invocation_id") == invocation_id:
            return True
    return False


def _records(db: Session, agent_id: str, invocation_id: str, message_id: str):
    principal = permission_service.require_principal(db)
    agent = agent_turn_input_service._require_agent(db, agent_id)
    message = db.get(Message, message_id)
    conversation = db.get(Conversation, message.conversation_id) if message else None
    invocation = db.scalar(select(CapabilityInvocation).where(
        CapabilityInvocation.id == invocation_id,
        CapabilityInvocation.tenant_id == principal.tenant_id,
        CapabilityInvocation.principal_type == "agent",
        CapabilityInvocation.principal_id == agent.id,
        CapabilityInvocation.scenario_id == agent.scenario_id,
    ))
    if (
        not message or message.role != "assistant" or not conversation
        or conversation.created_by_user_id != principal.user_id
        or conversation.agent_id != agent.id or not invocation
        or not _linked(message, invocation_id)
    ):
        raise AgentCapabilityConfirmationError("能力回执不存在或无权访问", 404)
    return principal, message, invocation, agent


def _load(db: Session, agent_id: str, invocation_id: str, message_id: str):
    principal, message, invocation, agent = _records(db, agent_id, invocation_id, message_id)
    runtime = agent_runtime_adapter.build_runtime_context(
        db, agent, LLMConfig(name="能力确认权限校验"), release_id=invocation.release_id,
    )
    capability = next((item for item in runtime.public_catalog() if
        item["kind"] == invocation.capability_kind and item["key"] == invocation.capability_key), None)
    if capability is None:
        raise AgentCapabilityConfirmationError("当前 Agent 已无此能力的授权", 403)
    return principal, message, invocation, runtime, capability


def _view(invocation: CapabilityInvocation, message: Message, capability: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    output = (invocation.result_document or {}).get("output") or {}
    output = output if isinstance(output, dict) else {}
    stored = (invocation.request_document or {}).get(payload_store.STORAGE_KEY) or {}
    current = (
        capability.get("definition_hash") == invocation.definition_hash
        and capability.get("deployment_fingerprint") == invocation.deployment_fingerprint
    )
    can_confirm = bool(
        document["status"] == "awaiting_confirmation" and message.stream_finalized
        and stored.get("message_id") == message.id and current
    )
    label = {
        "running": "正在处理",
        "awaiting_approval": "等待业务审批",
        "awaiting_confirmation": "预演已完成，等待确认",
        "succeeded": "能力调用已完成",
        "failed": "能力执行失败",
        "indeterminate": "执行结果待核对，请勿重复提交",
    }.get(document["status"], "能力状态待核对")
    delivery = dict(document.get("delivery") or {})
    if document["status"] == "awaiting_confirmation" and not can_confirm:
        label = "预演上下文已变化或不完整，请重新预演" if message.stream_finalized else "请等待回答完成"
        delivery.update(text=label, interactions=[])
    nested = output.get("result")
    nested = nested if isinstance(nested, dict) else {}
    artifact = output.get("artifact") or nested.get("artifact") or {}
    artifact = artifact if isinstance(artifact, dict) else {}
    return {
        "invocation_id": invocation.id,
        "status": document["status"],
        "name": str(capability.get("name") or "业务能力"),
        "can_confirm": can_confirm,
        "message": label,
        "workflow_run_id": output.get("workflow_run_id"),
        "artifact_file_id": artifact.get("id"),
        "artifact_filename": artifact.get("filename"),
        "delivery": delivery,
    }


def get_receipt(db: Session, agent_id: str, invocation_id: str, message_id: str) -> dict[str, Any]:
    principal, message, invocation, agent = _records(db, agent_id, invocation_id, message_id)
    scope = agent_capability_service.normalize_scope(
        agent_capability_service.legacy_all_scope() if agent.capability_scope is None else agent.capability_scope,
        allow_all=True,
    )
    category = next((key for key, kind in agent_capability_service.KIND_BY_CATEGORY.items() if kind == invocation.capability_kind), None)
    if category is None or (scope[category]["mode"] != "all" and invocation.capability_key not in scope[category]["selected_ids"]):
        raise AgentCapabilityConfirmationError("当前 Agent 已无此能力的授权", 403)
    scenario = db.get(BusinessScenario, invocation.scenario_id)
    try:
        definition = runtime_definition_service.resolve_pinned(
            db, scenario, snapshot_id=invocation.definition_snapshot_id, release_id=invocation.release_id,
            definition_hash=invocation.definition_hash,
        ) if invocation.release_id else (runtime_definition_service.resolve_retired_history(db, scenario) if scenario.status == "retired" else runtime_definition_service.resolve_authoring(db, scenario))
        resource = runtime_definition_service.resolve_resource(definition, invocation.capability_kind, invocation.capability_key)
        capability_application_service._require_permission(db, definition, invocation.capability_kind, resource, "read")
    except (runtime_definition_service.RuntimeDefinitionError, capability_application_service.CapabilityApplicationError):
        raise AgentCapabilityConfirmationError("能力回执不存在或无权访问", 404) from None
    capability = {"name": resource.name, "definition_hash": definition.definition_hash, "deployment_fingerprint": ""}
    if invocation.status == "awaiting_confirmation":
        try:
            _, _, _, _, capability = _load(db, agent_id, invocation_id, message_id)
        except agent_runtime_adapter.AgentRuntimeAdapterError:
            pass
    actor = Actor(actor_type="agent", principal_id=agent.id, tenant_id=principal.tenant_id, user_id=principal.user_id)
    document = capability_application_service.get_receipt(db, actor, invocation.id)
    return _view(invocation, message, capability, document)


def confirm(db: Session, agent_id: str, invocation_id: str, message_id: str) -> dict[str, Any]:
    principal, message, invocation, runtime, capability = _load(db, agent_id, invocation_id, message_id)
    stored = (invocation.request_document or {}).get(payload_store.STORAGE_KEY) or {}
    if not message.stream_finalized or stored.get("message_id") != message.id or stored.get("user_id") != principal.user_id:
        raise AgentCapabilityConfirmationError("预演尚未完成或确认上下文不可用，请重新预演")
    request = payload_store.restore_request(invocation, principal.user_id, message.id)
    db.info["action_audit_context"] = {"agent_id": agent_id}
    receipt = capability_application_service.invoke(
        db, runtime.scenario, runtime._actor(), request,
        invocation_source="agent", definition=runtime.runtime_definition,
    )
    current_message = db.scalar(select(Message).where(Message.id == message.id).with_for_update().execution_options(populate_existing=True))
    if current_message is not None:
        document = runtime._model_receipt(capability_application_service.receipt_document(receipt))
        updated = []
        for item in current_message.tool_results or []:
            entry = dict(item)
            value = entry.get("result")
            try:
                parsed = json.loads(value) if isinstance(value, str) else value
            except (TypeError, ValueError):
                parsed = None
            if entry.get("name") == "invoke_capability" and isinstance(parsed, dict) and parsed.get("invocation_id") == invocation_id:
                entry["result"] = json.dumps(document, ensure_ascii=False)
            updated.append(entry)
        current_message.tool_results = updated
    # The canonical receipt stays authoritative across reloads and repeated confirmations.
    db.flush()
    return get_receipt(db, agent_id, invocation_id, message_id)
