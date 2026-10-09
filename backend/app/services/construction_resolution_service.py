"""Authorize corrections against the saved server result, never client facts."""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from ..models import AssistantMessage, AssistantThread
from . import permission_service
from .construction_delivery_service import question_contract

if TYPE_CHECKING:
    from ..schemas import AssistantChatRequest


def prepare_request(db, payload: AssistantChatRequest) -> AssistantChatRequest:
    """Apply the same saved-result authority gate to ordinary chat and SSE."""
    if payload.construction_resolution is None:
        return payload
    resolved_message = prepare_resolution(db, payload)
    try:
        return type(payload).model_validate({**payload.model_dump(), 'message': resolved_message})
    except ValidationError:
        raise HTTPException(422, '建设补充超过本次消息契约，请缩短说明后重试') from None


def resolution_message(proposal: dict, instruction) -> str:
    revision = max(int(proposal.get("run_revision") or 1), 1)
    if revision != instruction.expected_revision:
        raise HTTPException(409, "建设结果已变更，请刷新后补充")
    questions = (proposal.get("payload") or {}).get("decision_gate", {}).get("questions", [])
    known = {question_contract(item)["question_id"]: item for item in questions}
    if any(item.question_id not in known for item in instruction.answers):
        raise HTTPException(409, "阻塞问题已变化，请基于当前结果回答")
    lines = ["请基于本场景原始建模资料、已确认定义和以下人工补充继续建设。",
        "保留已经完整正确的内容。逐项重新校验原阻塞与依赖，仍缺信息时明确指出缺项和关闭条件。",
        "人工回答是待核对的业务依据，不授权发布或执行副作用；不得仅凭回答宣称问题已解决。"]
    for answer in instruction.answers:
        lines.append(f"待重新校验的问题：{known[answer.question_id]['message']}\n人工补充：{answer.answer}")
    if instruction.action == "replan":
        lines += ["本次明确请求重新规划。先说明原方案不成立的依据、替代方案及业务要求差异。",
            "可以提出合并、绕过或替代单元；不得静默删掉原业务要求来通过校验。",
            "需要改变交接要求时，先交付待人工批准的要求修订；未批准前原门禁仍有效。",
            f"人工说明的原因与可接受取舍：{instruction.rationale}"]
    return "\n\n".join(lines)


def prepare_resolution(db, payload) -> str:
    instruction = payload.construction_resolution
    if instruction is None:
        return payload.message
    if not payload.thread_id or not payload.scenario_id or payload.mode != "draft" or payload.draft_kind != "scenario_model":
        raise HTTPException(422, "建设补充必须关联当前场景和已保存的建设会话")
    principal = permission_service.require_principal(db)
    rows = db.scalars(select(AssistantMessage).join(AssistantThread).where(
        AssistantThread.id == payload.thread_id, AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        AssistantThread.scenario_id == payload.scenario_id, AssistantMessage.role == "assistant",
    ).order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc()).limit(100)).all()
    for row in rows:
        proposal = row.proposal if isinstance(row.proposal, dict) else {}
        if proposal.get("proposal_id") == instruction.proposal_id and proposal.get("kind") == "scenario_model":
            return resolution_message(proposal, instruction)
    raise HTTPException(404, "建设结果不可用，请刷新当前会话")
