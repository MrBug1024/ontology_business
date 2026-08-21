"""P2 governed industry Starter Kit catalog.

Repository-owned Starter Kits are not an upload shortcut: the server loads a
fixed, verified package and sends it through the exact same preview → proposal
→ independent review → merge path as a user-supplied portable package.  No
endpoint here applies a Kit directly to a scenario.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..schemas import (
    StarterKitImportProposalIn,
    StarterKitImportProposalOut,
    StarterKitOut,
)
from ..services import (
    connector_service,
    package_service,
    permission_service,
    starter_kit_service,
    tenant_service,
)
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/starter-kits", tags=["starter-kits"])


def _kit_out(kit) -> StarterKitOut:
    return StarterKitOut(
        id=kit.id,
        name=kit.name,
        industry=kit.industry,
        version=kit.version,
        description=kit.description,
        fingerprint=kit.fingerprint,
        resource_counts=dict(kit.resource_counts),
    )


def _owned_scenario(db: Session, scenario_id: str, *, verb: str):
    """Starter-kit architecture is visible only inside the owned tenant."""
    scenario = tenant_service.require_scenario(db, scenario_id, writable=verb == "write")
    if scenario.tenant_id != tenant_service.current_tenant_id(db):
        raise HTTPException(status_code=403, detail="公共业务场景不提供 Starter Kit 导入")
    permission_service.require_scenario_permission(db, scenario, verb)
    return scenario


def _environment(value: str) -> str:
    try:
        return connector_service.normalize_environment(value)
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _kit_error(exc: Exception) -> None:
    if isinstance(exc, starter_kit_service.StarterKitNotFoundError):
        raise HTTPException(status_code=404, detail="未找到指定的 Starter Kit") from exc
    if isinstance(exc, starter_kit_service.StarterKitError):
        # Do not expose an altered static package or any part of a rejected
        # payload.  An operator can repair the repository asset and retry.
        raise HTTPException(status_code=503, detail="Starter Kit 校验未通过，暂不可用") from exc
    raise exc


def _import_error(exc: Exception) -> None:
    if isinstance(exc, package_service.PackageImportConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, package_service.PackageImportError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("", response_model=list[StarterKitOut])
def list_starter_kits(db: Session = Depends(get_tenant_db)) -> list[StarterKitOut]:
    """List verified catalog metadata; package content stays server-owned."""
    permission_service.require_principal(db)
    try:
        return [_kit_out(kit) for kit in starter_kit_service.list_starter_kits()]
    except starter_kit_service.StarterKitError as exc:
        _kit_error(exc)


@router.get("/{starter_kit_id}", response_model=StarterKitOut)
def get_starter_kit(starter_kit_id: str, db: Session = Depends(get_tenant_db)) -> StarterKitOut:
    permission_service.require_principal(db)
    try:
        return _kit_out(starter_kit_service.get_starter_kit(starter_kit_id))
    except starter_kit_service.StarterKitError as exc:
        _kit_error(exc)


@router.post("/{starter_kit_id}/scenarios/{scenario_id}/import-preview")
def preview_starter_kit_import(
    starter_kit_id: str,
    scenario_id: str,
    payload: dict | None = None,
    db: Session = Depends(get_tenant_db),
) -> dict:
    """Show a zero-side-effect diff for one immutable catalog package."""
    scenario = _owned_scenario(db, scenario_id, verb="write")
    raw_environment = str((payload or {}).get("environment") or "dev")
    try:
        kit = starter_kit_service.load_starter_kit_artifact(starter_kit_id)
        plan = package_service.plan_package_import(
            db,
            scenario,
            kit.package,
            environment=_environment(raw_environment),
        )
        plan["starter_kit"] = _kit_out(kit).model_dump()
        return plan
    except starter_kit_service.StarterKitError as exc:
        _kit_error(exc)
    except (package_service.PackageImportError, package_service.PackageImportConflictError) as exc:
        _import_error(exc)


@router.post(
    "/{starter_kit_id}/scenarios/{scenario_id}/import-proposal",
    response_model=StarterKitImportProposalOut,
)
def create_starter_kit_import_proposal(
    starter_kit_id: str,
    scenario_id: str,
    payload: StarterKitImportProposalIn,
    db: Session = Depends(get_tenant_db),
) -> StarterKitImportProposalOut:
    """Create only a governed proposal from a verified static Starter Kit."""
    scenario = _owned_scenario(db, scenario_id, verb="write")
    try:
        kit = starter_kit_service.load_starter_kit_artifact(starter_kit_id)
        if payload.expected_fingerprint != kit.fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Starter Kit 内容已更新，请重新预检并核对资源包指纹后再创建提案",
            )
        provenance = (
            f"Starter Kit 导入审计：{kit.id}@{kit.version}；"
            f"包指纹 {kit.fingerprint}。"
        )
        proposal, fingerprint, summary = package_service.create_governed_import_proposal(
            db,
            scenario,
            branch_id=payload.branch_id,
            package=kit.package,
            environment=payload.environment,
            title=payload.title,
            description=f"{payload.description.strip()}\n\n{provenance}".strip(),
            submit=payload.submit,
        )
        return StarterKitImportProposalOut(
            id=proposal.id,
            branch_id=proposal.branch_id,
            base_snapshot_id=proposal.base_snapshot_id,
            proposed_snapshot_id=proposal.proposed_snapshot_id,
            status=proposal.status,
            environment=payload.environment,
            package_fingerprint=fingerprint,
            summary=summary,
            starter_kit=_kit_out(kit),
        )
    except starter_kit_service.StarterKitError as exc:
        _kit_error(exc)
    except (package_service.PackageImportError, package_service.PackageImportConflictError) as exc:
        _import_error(exc)
