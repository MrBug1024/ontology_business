"""Refresh execution state and add a backward-compatible plain-text delivery view.

The structured output remains the business result. Consumers may ignore the
delivery view and present that output using their own Agent and channel tools.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BusinessScenario, WorkflowApprovalRequest, WorkflowRun
from . import permission_service, runtime_definition_service, workflow_approval_policy
from .capability_contracts import Actor, Receipt, canonical_hash
from .capability_invoker import _sanitize_output
from .channel_text import plain_text
from .workflow_execution_history import stored_execution


class CapabilityDeliveryError(ValueError):
    pass


def reply_code(kind: str, resource_id: str) -> str:
    prefix = "C" if kind == "confirmation" else "A"
    digest = hashlib.sha256(f"channel-interaction/v1:{kind}:{resource_id}".encode()).hexdigest()[:10]
    return f"{prefix}-{digest}"


def can_refresh_workflow_history(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    # Only immutable identity is replayed; every mutable value is replaced by the
    # freshly authorized receipt before history reaches the model.
    capability = current.get("capability")
    return bool(isinstance(capability, Mapping) and capability.get("kind") == "workflow"
                and all(previous.get(key) == current.get(key) for key in (
                    "invocation_id", "capability", "definition_hash",
                    "deployment_fingerprint", "data_context_fingerprint",
                )))


def approval_interaction(approval: WorkflowApprovalRequest, config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    code = reply_code("approval", approval.id)
    audience = workflow_approval_policy.audience(config or {})
    return {
        "kind": "workflow_approval", "id": approval.id, "code": code,
        "revision": getattr(approval, "revision", 1),
        "status": approval.status,
        "title": approval.node_name or "业务审批",
        "text": plain_text(approval.instructions or "请核对本次处理结果"),
        "reply_texts": [f"同意 {code}", f"驳回 {code}"] if approval.status == "pending" else [],
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "recipient_user_ids": audience.approver_user_ids,
        "recipient_roles": audience.approver_roles,
        "requires_evidence": audience.requires_evidence,
    }


def _result_text(output: Any) -> str:
    if isinstance(output, str):
        return plain_text(output)
    if not isinstance(output, Mapping):
        return ""
    steps = output.get("steps")
    if isinstance(steps, (list, tuple)):
        end_results = [step.get("result", {}).get("summary") for step in steps
                       if isinstance(step, Mapping) and step.get("type") == "end"
                       and isinstance(step.get("result"), Mapping)]
        if any(isinstance(value, str) and value.strip() for value in end_results):
            return plain_text("\n".join(value for value in end_results if isinstance(value, str)))
        return "\n".join(_result_text(step.get("result")) for step in steps
                         if isinstance(step, Mapping) and step.get("type") not in {"start", "approval"}).strip()
    for key in ("summary", "text", "content", "result"):
        if isinstance(output.get(key), str) and output[key].strip():
            return plain_text(output[key])
    return "\n".join(f"{key}: {value}" for key, value in list(output.items())[:30]
                     if isinstance(value, (str, int, float, bool))
                     and key not in {"workflow_run_id", "execution_key", "task_url", "status", "created"})


def _artifacts(value: Any) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}

    def visit(item: Any, depth: int) -> None:
        if depth > 10 or len(found) >= 30:
            return
        if isinstance(item, Mapping):
            if isinstance(item.get("steps"), (list, tuple)):
                for step in item["steps"][:500]:
                    if isinstance(step, Mapping) and step.get("type") == "action" and step.get("status") in {"success", "succeeded"}:
                        visit(step.get("result"), depth + 1)
                return
            artifact = item.get("artifact")
            if isinstance(artifact, Mapping) and isinstance(artifact.get("id"), str):
                found[artifact["id"]] = {"id": artifact["id"], "filename": str(artifact.get("filename") or "附件")[:500]}
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item[:500]:
                visit(child, depth + 1)

    visit(value, 0)
    return list(found.values())


def project(db: Session, actor: Actor, receipt: Receipt) -> Receipt:
    output = dict(receipt.output) if isinstance(receipt.output, Mapping) else receipt.output
    status = receipt.status
    interactions: list[dict[str, Any]] = []
    run_id = output.get("workflow_run_id") if isinstance(output, Mapping) else None
    if receipt.capability.kind == "workflow" and isinstance(run_id, str):
        run = db.scalar(select(WorkflowRun).join(BusinessScenario).where(
            WorkflowRun.id == run_id, BusinessScenario.tenant_id == actor.tenant_id,
            WorkflowRun.workflow_id == receipt.capability.resource_id,
            WorkflowRun.definition_hash == receipt.definition_hash,
        ))
        if run is None:
            raise CapabilityDeliveryError("执行结果不存在或无权访问")
        execution_key = output.get("execution_key")
        frozen = stored_execution(db, receipt.invocation_id, execution_key) if execution_key != run.execution_key else None
        if (execution_key != run.execution_key and frozen is None) or (frozen is None and run.created_by_user_id != actor.user_id):
            raise CapabilityDeliveryError("执行结果不存在或无权访问")
        try:
            definition = runtime_definition_service.resolve_for_run(db, run)
            workflow = runtime_definition_service.resolve_resource(definition, "workflow", run.workflow_id)
        except runtime_definition_service.RuntimeDefinitionError:
            raise CapabilityDeliveryError("执行结果不存在或无权访问") from None
        if not permission_service.check_workflow(db, workflow, "read").allowed:
            raise CapabilityDeliveryError("执行结果不存在或无权访问")
        run_status = frozen["status"] if frozen else run.status
        run_result = frozen["result"] if frozen else run.result
        run_error = frozen["error"] if frozen else run.error
        status = {"queued": "running", "retry_waiting": "running"}.get(run_status, run_status)
        output = {**output, "status": run_status, "result": _sanitize_output(run_result or {})}
        approvals = db.scalars(select(WorkflowApprovalRequest).where(
            WorkflowApprovalRequest.workflow_run_id == run.id,
            WorkflowApprovalRequest.status == "pending",
        ).order_by(WorkflowApprovalRequest.requested_at).limit(20)).all() if frozen is None and status == "awaiting_approval" else []
        interactions = [approval_interaction(item, workflow_approval_policy.node_config(workflow, item.node_id)) for item in approvals]
        receipt = replace(receipt, error_code="workflow_execution_failed" if run_error else None,
                          error_message=run_error or "")
    if status == "awaiting_confirmation":
        try:
            expires_at = datetime.fromisoformat(str(receipt.confirmation.get("expires_at", "")))
            if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
                status = "timed_out"
        except ValueError:
            status = "timed_out"
    if status == "awaiting_confirmation":
        code = reply_code("confirmation", receipt.invocation_id)
        interactions = [{
            "kind": "capability_confirmation", "id": receipt.invocation_id, "code": code,
            "revision": 1, "title": "执行确认", "text": _result_text(output),
            "reply_texts": [f"确认 {code}"],
            "expires_at": receipt.confirmation.get("expires_at"),
        }]
    state_text = {
        "pending": "请求已接收", "running": "正在处理", "awaiting_confirmation": "预演完成，等待发起人确认",
        "awaiting_approval": "等待业务审批", "succeeded": "处理完成", "failed": "处理失败",
        "cancelled": "已取消", "rejected": "已驳回", "timed_out": "处理超时",
        "indeterminate": "执行结果尚未核实，请勿重复提交",
    }.get(status, "执行状态待核实")
    result_text = _result_text(output.get("result", output) if isinstance(output, Mapping) else output)
    paragraphs = [state_text]
    if result_text and status != "awaiting_confirmation":
        paragraphs.append(result_text)
    if receipt.error_message:
        paragraphs.append(plain_text(receipt.error_message))
    for item in interactions:
        paragraphs.append(f"{item['title']}\n{item['text']}\n回复：" + " 或 ".join(item["reply_texts"]))
        if item.get("requires_evidence"):
            paragraphs.append("同意时请附上佐证文件")
    delivery = {
        "contract": "channel-delivery/v1", "format": "text/plain",
        "text": "\n\n".join(paragraphs), "interactions": interactions,
        "attachments": _artifacts(output),
    }
    delivery["revision"] = canonical_hash(delivery, domain="channel-delivery-v1")
    return replace(receipt, status=status, output=output, delivery=delivery)
