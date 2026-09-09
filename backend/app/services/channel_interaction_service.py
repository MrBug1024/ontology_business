"""Authenticated text replies over capability confirmation and workflow approval."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..channel_interaction_schemas import ChannelReplyIn
from ..models import BusinessScenario, CapabilityInvocation, WorkflowApprovalRequest, WorkflowRun
from . import (
    capability_application_service, capability_delivery_service, channel_confirmation_input,
    operations_service, permission_service, runtime_definition_service, workflow_approval_policy,
)
from .capability_contracts import Actor, canonical_hash
from .channel_reply_parser import ParsedReply, parse_reply
from .policies import PolicyViolation


class ChannelInteractionError(ValueError):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def _principal(db: Session, actor: Actor) -> None:
    principal = permission_service.require_principal(db)
    if principal.tenant_id != actor.tenant_id or principal.user_id != actor.user_id:
        raise ChannelInteractionError("回复身份不可用", 403)


def _parsed(kind: str, resource_id: str, payload: ChannelReplyIn) -> ParsedReply:
    parsed = parse_reply(payload.text)
    if parsed is None or (parsed.code and parsed.code != capability_delivery_service.reply_code(kind, resource_id)):
        raise ChannelInteractionError("回复未匹配到该待办，请核对待办编号", 422)
    return parsed


def decision_message_key(actor: Actor, message_id: str) -> str:
    return hashlib.sha256(f"approval-message/v1:{actor.tenant_id}:{actor.principal_id}:{message_id}".encode()).hexdigest()


def approval_context(db: Session, actor: Actor, approval_id: str, *, lock: bool = False):
    _principal(db, actor)
    statement = select(WorkflowApprovalRequest).join(WorkflowRun).join(BusinessScenario).where(
        WorkflowApprovalRequest.id == approval_id, BusinessScenario.tenant_id == actor.tenant_id,
        WorkflowApprovalRequest.scenario_id == BusinessScenario.id,
    )
    if lock:
        statement = statement.with_for_update(of=WorkflowApprovalRequest)
    approval = db.scalar(statement.execution_options(populate_existing=True))
    if approval is None:
        raise ChannelInteractionError("审批待办不存在或无权访问", 404)
    run = approval.workflow_run
    try:
        definition = runtime_definition_service.resolve_for_run(db, run)
        workflow = runtime_definition_service.resolve_resource(definition, "workflow", run.workflow_id)
    except runtime_definition_service.RuntimeDefinitionError:
        raise ChannelInteractionError("审批待办不存在或无权访问", 404) from None
    if not permission_service.check_workflow(db, workflow, "approve").allowed:
        raise ChannelInteractionError("审批待办不存在或无权访问", 404)
    try:
        config = workflow_approval_policy.node_config(workflow, approval.node_id)
        workflow_approval_policy.require_audience(db, config)
    except PolicyViolation:
        raise ChannelInteractionError("审批待办不存在或无权访问", 404) from None
    return approval, run, workflow, config


def read_approval(db: Session, actor: Actor, approval_id: str) -> dict[str, Any]:
    approval, _, _, config = approval_context(db, actor, approval_id)
    policy = workflow_approval_policy.audience(config)
    return {**capability_delivery_service.approval_interaction(approval),
            "recipient_user_ids": policy.approver_user_ids, "recipient_roles": policy.approver_roles,
            "requires_evidence": policy.requires_evidence}


def reply_approval(db: Session, actor: Actor, approval_id: str, payload: ChannelReplyIn) -> dict[str, Any]:
    parsed = _parsed("approval", approval_id, payload)
    if parsed.action not in {"approve", "reject"}:
        raise ChannelInteractionError("该待办需要回复同意或驳回", 422)
    approval, run, _, _ = approval_context(db, actor, approval_id, lock=True)
    message_key = decision_message_key(actor, payload.message_id)
    evidence = [item.model_dump(exclude_none=True) for item in payload.evidence]
    reply_hash = canonical_hash({"approved": parsed.action == "approve", "comment": parsed.comment,
                                 "evidence": evidence}, domain="approval-message-content-v1")
    if approval.decision_message_id == message_key and approval.resolved_by_user_id == actor.user_id:
        if approval.decision_digest != reply_hash or approval.revision != payload.expected_revision + 1:
            raise ChannelInteractionError("同一消息对应的审批内容发生变化")
    else:
        if approval.status != "pending" or approval.revision != payload.expected_revision:
            raise ChannelInteractionError("该审批已处理或版本已变化，请读取当前待办")
        try:
            run = operations_service.decide_approval(
                db, run, approved=parsed.action == "approve", comment=parsed.comment,
                user_id=actor.user_id, approval_id=approval.id, expected_revision=payload.expected_revision,
                decision_message_id=message_key, evidence_refs=evidence, reply_hash=reply_hash,
            )
        except PolicyViolation as exc:
            raise ChannelInteractionError(str(exc)) from None
    return {"interaction_id": approval.id, "status": approval.status,
            "text": "审批已通过，继续处理" if approval.status == "approved" else "审批已驳回",
            "workflow_run_id": run.id}


def reply_confirmation(db: Session, actor: Actor, invocation_id: str, payload: ChannelReplyIn) -> dict[str, Any]:
    _principal(db, actor)
    parsed = _parsed("confirmation", invocation_id, payload)
    if parsed.action not in {"confirm", "cancel"} or payload.expected_revision != 1:
        raise ChannelInteractionError("该待办需要回复确认或取消，并匹配当前版本", 422)
    if payload.evidence:
        raise ChannelInteractionError("执行确认使用预演时的固定输入；新增文件后需重新预演", 422)
    capability_application_service.get_receipt(db, actor, invocation_id)
    invocation = db.get(CapabilityInvocation, invocation_id)
    if parsed.action == "cancel":
        if invocation.status not in {"awaiting_confirmation", "cancelled"}:
            raise ChannelInteractionError("执行已经开始或结束，不能取消该预演")
        claimed = db.execute(update(CapabilityInvocation).where(
            CapabilityInvocation.id == invocation.id, CapabilityInvocation.status == "awaiting_confirmation",
        ).values(status="cancelled", completed_at=datetime.now(timezone.utc))).rowcount
        if claimed != 1 and invocation.status != "cancelled":
            raise ChannelInteractionError("执行状态已变化，请读取当前处理结果")
        db.flush()
        db.refresh(invocation)
    else:
        request = channel_confirmation_input.restore(invocation, actor)
        scenario = db.get(BusinessScenario, invocation.scenario_id)
        capability_application_service.invoke(db, scenario, actor, request, release_id=invocation.release_id,
                                               invocation_source="rest")
    document = capability_application_service.get_receipt(db, actor, invocation.id)
    return {"interaction_id": invocation.id, "invocation_id": invocation.id,
            "status": document["status"], "text": document["delivery"]["text"],
            "workflow_run_id": document["output"].get("workflow_run_id") if isinstance(document["output"], dict) else None}
