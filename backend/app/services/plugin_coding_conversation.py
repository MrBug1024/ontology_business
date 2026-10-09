"""Bounded public turns from the existing, principal-owned coding jobs."""
from __future__ import annotations

from sqlalchemy import select

from ..models import AssistantMessage, AssistantRequestRun
from . import permission_service


def coding_turns(db, workspace_id: str) -> list[dict]:
    principal = permission_service.require_principal(db)
    rows = db.execute(select(AssistantRequestRun.id, AssistantRequestRun.status,
        AssistantRequestRun.created_at, AssistantMessage.content,
        AssistantRequestRun.payload_document['mode'].as_string().label('mode')).join(
        AssistantMessage,
        (AssistantMessage.id == AssistantRequestRun.user_message_id)
        & (AssistantMessage.thread_id == AssistantRequestRun.thread_id),
    ).where(
        AssistantRequestRun.thread_id == workspace_id,
        AssistantRequestRun.tenant_id == principal.tenant_id,
        AssistantRequestRun.requested_by_user_id == principal.user_id,
        AssistantRequestRun.payload_document['kind'].as_string() == 'scenario-plugin-coding.v1',
    ).order_by(AssistantRequestRun.created_at.desc(), AssistantRequestRun.id.desc()).limit(20)).all()
    return [{'id': row.id, 'instruction': row.content[:4000], 'status': row.status,
             'created_at': row.created_at.isoformat(), 'mode': row.mode or 'generate'} for row in reversed(rows)]
