"""External message replies use the same execution and approval services."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..channel_interaction_schemas import ChannelInteractionOut, ChannelReplyIn, ChannelReplyOut
from ..services import agent_turn_payload_service, auth_service, capability_application_service, channel_interaction_service, external_api_service, permission_service
from ..services.capability_contracts import Actor
from ..services.capability_invoker import CapabilityInvocationError
from .external_capabilities import _actor, _application_error, _external_context, _invocation_error


router = APIRouter(prefix="/external/v2/interactions", tags=["channel-interactions"])
browser_router = APIRouter(prefix="/tasks/approvals", tags=["channel-interactions"])


@router.get("/approval/{interaction_id}", response_model=ChannelInteractionOut)
def read_approval(interaction_id: str, context: external_api_service.ExternalApiContext = Depends(_external_context)):
    external_api_service.require_scope(context, "capabilities:read")
    try:
        return channel_interaction_service.read_approval(context.db, _actor(context), interaction_id)
    except channel_interaction_service.ChannelInteractionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.post("/{kind}/{interaction_id}/reply", response_model=ChannelReplyOut)
def reply(
    kind: Literal["confirmation", "approval"], interaction_id: str, payload: ChannelReplyIn,
    context: external_api_service.ExternalApiContext = Depends(_external_context),
):
    external_api_service.require_scope(context, "capabilities:invoke")
    return _apply_reply(context.db, _actor(context), kind, interaction_id, payload)


def _apply_reply(db: Session, actor: Actor, kind: str, interaction_id: str, payload: ChannelReplyIn):
    try:
        handler = channel_interaction_service.reply_confirmation if kind == "confirmation" else channel_interaction_service.reply_approval
        result = handler(db, actor, interaction_id, payload)
        db.commit()
        return result
    except channel_interaction_service.ChannelInteractionError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from None
    except capability_application_service.CapabilityApplicationError as exc:
        db.rollback()
        _application_error(exc)
    except CapabilityInvocationError as exc:
        db.rollback()
        _invocation_error(exc)
    except (IntegrityError, agent_turn_payload_service.AgentTurnPayloadError):
        db.rollback()
        raise HTTPException(409, "回复未完成，消息可能已处理或预演输入已不可用，请刷新当前待办") from None


@browser_router.post("/{interaction_id}/reply", response_model=ChannelReplyOut)
def browser_reply(interaction_id: str, payload: ChannelReplyIn, db: Session = Depends(auth_service.get_tenant_db)):
    principal = permission_service.require_principal(db)
    actor = Actor(actor_type="user", principal_id=principal.user_id, tenant_id=principal.tenant_id, user_id=principal.user_id)
    return _apply_reply(db, actor, "approval", interaction_id, payload)
