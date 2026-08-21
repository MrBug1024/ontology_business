"""Standard connector catalog and scenario/environment binding API.

Physical connector configuration remains on the existing data-source, MCP and
LLM endpoints.  This router exposes only a normalized, credential-free catalog
and the governed links that make portable package references usable per
environment.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..models import OntologySnapshot
from ..schemas import (
    ConnectorBindingIn,
    ConnectorBindingOut,
    ConnectorCatalogOut,
    ConnectorReadinessOut,
    Msg,
)
from ..services import connector_service, permission_service, runtime_connector_service, tenant_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/connectors", tags=["connectors"])


def _scenario(db: Session, scenario_id: str, *, write: bool = False):
    scenario = tenant_service.require_scenario(db, scenario_id, writable=write)
    if scenario.tenant_id != tenant_service.current_tenant_id(db):
        raise HTTPException(status_code=403, detail="只能管理当前租户的场景连接器")
    permission_service.require_scenario_permission(db, scenario, "write" if write else "read")
    if write:
        permission_service.require_tenant_permission(db, "manage")
    return scenario


def _connector_error(exc: Exception) -> None:
    if isinstance(exc, connector_service.ConnectorBindingConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, connector_service.ConnectorBindingError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=list[ConnectorCatalogOut])
def list_connectors(
    scenario_id: str = Query(min_length=1, max_length=32),
    db: Session = Depends(get_tenant_db),
) -> list[dict]:
    """List legal same-tenant connector targets for one scenario."""
    return connector_service.list_catalog(db, _scenario(db, scenario_id))


@router.get("/runtime-environment")
def get_runtime_environment(db: Session = Depends(get_tenant_db)) -> dict:
    """Expose the deployment environment as read-only UI context."""
    # Resolving the tenant-backed session also keeps this operational detail
    # authenticated; it is never a client-controlled execution parameter.
    del db
    return {"environment": runtime_connector_service.runtime_environment()}


@router.get("/scenarios/{scenario_id}/bindings", response_model=list[ConnectorBindingOut])
def list_connector_bindings(
    scenario_id: str,
    environment: str | None = Query(default=None, max_length=20),
    db: Session = Depends(get_tenant_db),
) -> list[dict]:
    try:
        return connector_service.list_bindings(
            db, _scenario(db, scenario_id), environment=environment
        )
    except connector_service.ConnectorBindingError as exc:
        _connector_error(exc)


@router.put("/scenarios/{scenario_id}/bindings", response_model=ConnectorBindingOut)
def upsert_connector_binding(
    scenario_id: str,
    payload: ConnectorBindingIn,
    db: Session = Depends(get_tenant_db),
) -> dict:
    """Bind a reusable physical target and optionally perform an explicit check."""
    scenario = _scenario(db, scenario_id, write=True)
    principal = permission_service.require_principal(db)
    try:
        binding = connector_service.upsert_binding(
            db,
            scenario,
            environment=payload.environment,
            binding_key_value=payload.binding_key,
            kind=payload.kind,
            connector_id=payload.connector_id,
            reference_label=payload.reference_label,
            check=payload.check,
            created_by_user_id=principal.user_id,
        )
        db.commit()
        db.refresh(binding)
        return connector_service.binding_summary(db, binding, scenario)
    except connector_service.ConnectorBindingError as exc:
        db.rollback()
        _connector_error(exc)


@router.post("/scenarios/{scenario_id}/bindings/{binding_id}/check", response_model=ConnectorBindingOut)
def check_connector_binding(
    scenario_id: str,
    binding_id: str,
    db: Session = Depends(get_tenant_db),
) -> dict:
    scenario = _scenario(db, scenario_id, write=True)
    try:
        binding = connector_service.get_binding(db, binding_id, scenario)
        connector_service.check_binding(db, binding, scenario)
        db.commit()
        db.refresh(binding)
        return connector_service.binding_summary(db, binding, scenario)
    except connector_service.ConnectorBindingError as exc:
        db.rollback()
        _connector_error(exc)


@router.delete("/scenarios/{scenario_id}/bindings/{binding_id}", response_model=Msg)
def delete_connector_binding(
    scenario_id: str,
    binding_id: str,
    db: Session = Depends(get_tenant_db),
) -> Msg:
    scenario = _scenario(db, scenario_id, write=True)
    try:
        binding = connector_service.get_binding(db, binding_id, scenario)
        db.delete(binding)
        db.commit()
        return Msg(message="已删除环境绑定；后续发布会重新检查连接器依赖")
    except connector_service.ConnectorBindingError as exc:
        db.rollback()
        _connector_error(exc)


@router.get("/scenarios/{scenario_id}/readiness", response_model=ConnectorReadinessOut)
def connector_readiness(
    scenario_id: str,
    snapshot_id: str = Query(min_length=1, max_length=32),
    environment: str = Query(default="dev", max_length=20),
    db: Session = Depends(get_tenant_db),
) -> dict:
    """Read the server-side release gate without triggering connector I/O."""
    scenario = _scenario(db, scenario_id)
    snapshot = db.get(OntologySnapshot, snapshot_id)
    if not snapshot or snapshot.scenario_id != scenario.id or snapshot.tenant_id != scenario.tenant_id:
        raise HTTPException(status_code=404, detail="本体快照不存在")
    try:
        return connector_service.readiness(
            db, scenario, snapshot.content or {}, environment=environment
        )
    except connector_service.ConnectorBindingError as exc:
        _connector_error(exc)
