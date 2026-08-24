"""Execution endpoints for governed ``FunctionDefinition`` runtimes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import FunctionDefinition, FunctionRun
from ..schemas import FunctionRunIn, FunctionRunOut
from ..services import (
    capability_readiness_service,
    function_runtime_service,
    permission_service,
    runtime_connector_service,
    runtime_definition_service,
    tenant_service,
)
from ..services.auth_service import get_current_user


router = APIRouter(
    prefix="/functions",
    tags=["function-runtime"],
    dependencies=[Depends(get_current_user)],
)

def _function(db: Session, function_id: str, *, write: bool = False) -> FunctionDefinition:
    function = db.get(FunctionDefinition, function_id)
    if not function:
        raise HTTPException(status_code=404, detail="函数定义不存在")
    scenario = tenant_service.require_scenario(db, function.scenario_id, writable=write)
    permission_service.require_scenario_permission(db, scenario, "write" if write else "read")
    return function


def _user_id(db: Session) -> str | None:
    value = db.info.get("user_id")
    return str(value) if value else None


@router.post("/{function_id}/run", response_model=FunctionRunOut, status_code=201)
def run_function(
    function_id: str,
    payload: FunctionRunIn,
    db: Session = Depends(get_db),
) -> FunctionRun:
    live_function = _function(db, function_id, write=True)
    scenario = tenant_service.require_scenario(
        db, live_function.scenario_id, writable=True
    )
    try:
        definition = runtime_definition_service.resolve_active(
            db,
            scenario,
            environment=runtime_connector_service.runtime_environment(),
        )
        function = runtime_definition_service.resolve_resource(
            definition, "function", function_id
        )
        capability_readiness_service.require_executable(
            "function", function, definition=definition, db=db
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(409, f"函数当前不可执行: {exc}") from exc
    run = function_runtime_service.create_function_run(
        db,
        function,
        payload.params,
        tenant_id=tenant_service.current_tenant_id(db),
        scenario_id=function.scenario_id,
        user_id=_user_id(db),
        idempotency_key=payload.idempotency_key,
        definition_hash=definition.definition_hash,
    )
    db.commit()
    db.refresh(run)
    return run


@router.get("/{function_id}/runs", response_model=list[FunctionRunOut])
def list_function_runs(
    function_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[FunctionRun]:
    function = _function(db, function_id)
    return list(
        db.execute(
            select(FunctionRun)
            .where(FunctionRun.function_id == function.id)
            .order_by(FunctionRun.created_at.desc())
            .limit(limit)
        ).scalars().all()
    )
