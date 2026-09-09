"""Authenticated browser entry point for unified Agent capability receipts."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..agent_capability_confirmation_schemas import AgentCapabilityConfirmationIn, AgentCapabilityReceiptOut
from ..services import agent_capability_confirmation_service as service
from ..services import agent_turn_payload_service, capability_application_service, agent_runtime_adapter, agent_turn_input_service, workflow_payload_service
from ..services.auth_service import get_tenant_db
from ..services.capability_invoker import CapabilityInvocationError


router = APIRouter(prefix="/agents", tags=["agent-capability-confirmations"])


@router.get("/{agent_id}/capability-invocations/{invocation_id}", response_model=AgentCapabilityReceiptOut)
def get_receipt(agent_id: str, invocation_id: str, message_id: str = Query(min_length=1, max_length=64), db: Session = Depends(get_tenant_db)):
    try:
        return service.get_receipt(db, agent_id, invocation_id, message_id)
    except service.AgentCapabilityConfirmationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except (agent_runtime_adapter.AgentRuntimeAdapterError, agent_turn_input_service.AgentTurnError) as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": "当前能力上下文不可用，请重新核对 Agent 配置。"}) from None


@router.post("/{agent_id}/capability-invocations/{invocation_id}/confirm", response_model=AgentCapabilityReceiptOut)
def confirm(agent_id: str, invocation_id: str, payload: AgentCapabilityConfirmationIn, db: Session = Depends(get_tenant_db)):
    try:
        result = service.confirm(db, agent_id, invocation_id, payload.message_id)
        db.commit()
        return result
    except service.AgentCapabilityConfirmationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    except (CapabilityInvocationError, capability_application_service.CapabilityApplicationError, agent_turn_payload_service.AgentTurnPayloadError, agent_runtime_adapter.AgentRuntimeAdapterError, agent_turn_input_service.AgentTurnError, workflow_payload_service.WorkflowPayloadError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": "确认未完成，预演可能已过期、定义已变化或输入不可用，请核对后重新预演。"}) from None
