"""Actual expert statements are sources, not independently corroborated facts."""
from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import select

from ..distillation_conversation_models import DistillationConversationTurn
from ..distillation_models import DistillationProject
from ..distillation_schemas import Evidence, InterviewReference
from . import distillation_service, permission_service


def evidence(turn: DistillationConversationTurn) -> Evidence:
    return Evidence(key="interview_" + turn.id[:20], title=f"专家陈述 · 第 {turn.turn_number} 轮",
        kind="observation", role="reference", summary=turn.message[:2000],
        coverage="当前对话的专家描述或访谈转述，原文保留在对话记录中。",
        limitations="专家陈述尚未独立核实；不能代替系统操作记录或证明结果已经执行。",
        interview=InterviewReference(turn_id=turn.id, message_sha256=hashlib.sha256(turn.message.encode()).hexdigest()))


def resolve(db, source: Evidence, scenario_id: str | None) -> dict:
    principal = permission_service.require_principal(db)
    reference = source.interview
    if reference is None:
        raise HTTPException(422, "缺少访谈来源")
    row = db.scalar(select(DistillationConversationTurn).where(DistillationConversationTurn.id == reference.turn_id,
        DistillationConversationTurn.tenant_id == principal.tenant_id))
    project = db.get(DistillationProject, row.project_id) if row else None
    if project is None or (project.scenario_id is not None and project.scenario_id != scenario_id):
        raise HTTPException(404, "访谈来源不存在")
    distillation_service.authorize_scope(db, project.scenario_id)
    if hashlib.sha256(row.message.encode()).hexdigest() != reference.message_sha256:
        raise HTTPException(409, "访谈原文与引用不一致")
    return {"turn_id": row.id, "message": row.message, "created_at": row.created_at.isoformat(),
        "basis": "expert_statement_not_independently_verified"}
