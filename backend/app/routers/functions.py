"""Browser compatibility endpoints for governed function capabilities.

Execution is owned exclusively by the protocol-neutral capability application
service.  This router keeps the historical URL and response envelope for the
current browser client; it does not maintain a second execution kernel.
"""
from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CapabilityInvocation, FunctionDefinition, FunctionRun
from ..schemas import FunctionRunIn, FunctionRunOut
from ..services import (
    capability_application_service,
    permission_service,
    tenant_service,
)
from ..services.capability_contracts import (
    Actor,
    CapabilityContractError,
    CapabilityRef,
    Request,
)
from ..services.capability_invoker import CapabilityInvocationError
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/functions",
    tags=["function-runtime"],
    dependencies=[Depends(get_current_user)],
)

_UNPROCESSABLE_INVOCATION_CODES = {
    "input_schema_invalid",
    "invalid_confirmation",
    "invalid_invocation_mode",
    "invalid_override_shape",
    "runtime_input_port_not_found",
    "runtime_input_override_forbidden",
    "unsupported_managed_reference",
}


def _function(db: Session, function_id: str, *, write: bool = False) -> FunctionDefinition:
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(status_code=404, detail="函数定义不存在")
    try:
        scenario = tenant_service.require_scenario(
            db,
            function.scenario_id,
            writable=write,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(status_code=404, detail="函数定义不存在") from None
        raise
    permission_service.require_scenario_permission(db, scenario, "write" if write else "read")
    return function


def _actor(db: Session) -> Actor:
    principal = permission_service.require_principal(db)
    return Actor(
        actor_type="user",
        principal_id=principal.user_id,
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        roles=principal.role_keys,
        scopes=("capability:read", "capability:invoke"),
    )


def _invocation_out(
    invocation: CapabilityInvocation,
    *,
    current_inputs: Mapping[str, object] | None = None,
) -> FunctionRunOut:
    request_document = (
        invocation.request_document
        if isinstance(invocation.request_document, dict)
        else {}
    )
    result_document = (
        invocation.result_document
        if isinstance(invocation.result_document, dict)
        else {}
    )
    structured_inputs = request_document.get("structured_inputs")
    if not isinstance(structured_inputs, Mapping):
        structured_inputs = {}
    output = result_document.get("output", {})
    return FunctionRunOut(
        id=invocation.id,
        tenant_id=invocation.tenant_id,
        scenario_id=invocation.scenario_id,
        function_id=invocation.capability_key,
        run_type="function",
        status=invocation.status,
        # The durable audit stores only a hash and a bounded shape outline.
        # Echo current values only to the same request for wire compatibility.
        input_payload=(
            dict(current_inputs)
            if current_inputs is not None
            else dict(structured_inputs)
        ),
        output_payload=dict(output) if isinstance(output, Mapping) else {"value": output},
        error=invocation.error_message or "",
        started_at=invocation.started_at,
        completed_at=invocation.completed_at,
        created_by_user_id=invocation.requested_by_user_id,
        created_at=invocation.created_at,
    )


@router.post("/{function_id}/run", response_model=FunctionRunOut, status_code=201)
def run_function(
    function_id: str,
    payload: FunctionRunIn,
    db: Session = Depends(get_db),
) -> FunctionRunOut:
    live_function = _function(db, function_id, write=True)
    scenario = tenant_service.require_scenario(
        db, live_function.scenario_id, writable=True
    )
    try:
        receipt = capability_application_service.invoke(
            db,
            scenario,
            _actor(db),
            Request(
                capability=CapabilityRef(kind="function", resource_id=function_id),
                inputs=payload.params,
                mode="execute",
                idempotency_key=payload.idempotency_key,
                correlation_id=f"browser:{uuid4().hex}",
            ),
            environment=payload.environment,
            invocation_source="internal",
        )
        db.commit()
    except capability_application_service.CapabilityApplicationError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.as_dict()) from exc
    except CapabilityInvocationError as exc:
        db.rollback()
        status_code = 422 if exc.code in _UNPROCESSABLE_INVOCATION_CODES else 409
        raise HTTPException(status_code=status_code, detail=exc.as_dict()) from exc
    except CapabilityContractError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_capability_request", "message": str(exc)},
        ) from exc
    invocation = db.get(CapabilityInvocation, receipt.invocation_id)
    if invocation is None:  # The invoker must persist every returned receipt.
        raise HTTPException(500, "函数调用回执不可用")
    return _invocation_out(invocation, current_inputs=payload.params)


@router.get("/{function_id}/runs", response_model=list[FunctionRunOut])
def list_function_runs(
    function_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[FunctionRunOut]:
    function = _function(db, function_id)
    tenant_id = tenant_service.current_tenant_id(db)
    capability_runs = list(
        db.execute(
            select(CapabilityInvocation)
            .where(
                CapabilityInvocation.tenant_id == tenant_id,
                CapabilityInvocation.scenario_id == function.scenario_id,
                CapabilityInvocation.capability_kind == "function",
                CapabilityInvocation.capability_key == function.id,
            )
            .order_by(
                CapabilityInvocation.created_at.desc(),
                CapabilityInvocation.id.desc(),
            )
            .limit(limit)
        ).scalars().all()
    )
    historical_runs = list(
        db.execute(
            select(FunctionRun)
            .where(
                FunctionRun.tenant_id == tenant_id,
                FunctionRun.scenario_id == function.scenario_id,
                FunctionRun.function_id == function.id,
                FunctionRun.run_type == "function",
            )
            .order_by(FunctionRun.created_at.desc(), FunctionRun.id.desc())
            .limit(limit)
        ).scalars().all()
    )
    combined: list[FunctionRunOut] = [
        *(_invocation_out(item) for item in capability_runs),
        *(FunctionRunOut.model_validate(item) for item in historical_runs),
    ]
    combined.sort(key=lambda item: (item.created_at, item.id), reverse=True)
    return combined[:limit]
