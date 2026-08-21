"""P2 可移植本体资源包 API。

资源包的实际应用必须进入发布提案/评审链路；本路由只提供已脱敏的导出、格式校验
和零副作用的目标导入预检。这样上传不可信 JSON 不会直接修改任何业务本体或外部
连接配置。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..schemas import PackageImportProposalIn, PackageImportProposalOut
from ..services import connector_service, package_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/packages", tags=["packages"])


def _owned_scenario(db: Session, scenario_id: str, *, verb: str):
    """Return an owned scenario after an ACL check.

    Package metadata includes unmerged architecture and dependency requirements, so
    public cross-tenant scenarios deliberately do not expose it even though their
    ordinary ontology graph may be readable.
    """
    scenario = tenant_service.require_scenario(db, scenario_id, writable=verb == "write")
    if scenario.tenant_id != tenant_service.current_tenant_id(db):
        raise HTTPException(status_code=403, detail="公共业务场景不提供资源包访问")
    permission_service.require_scenario_permission(db, scenario, verb)
    return scenario


def _package_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept the documented ``{package: ...}`` envelope and reject loose input."""
    package = payload.get("package")
    if not isinstance(package, dict):
        raise HTTPException(status_code=422, detail="请求体必须包含 package JSON 对象")
    return package


def _environment_from_payload(payload: dict[str, Any]) -> str:
    try:
        return connector_service.normalize_environment(str(payload.get("environment") or "dev"))
    except connector_service.ConnectorBindingError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _import_error(exc: Exception) -> None:
    if isinstance(exc, package_service.PackageImportConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, package_service.PackageImportError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/scenarios/{scenario_id}/export")
def export_scenario_package(scenario_id: str, db: Session = Depends(get_tenant_db)) -> dict[str, Any]:
    """Export a deterministic, credential-free definition package.

    This endpoint does not download raw database configuration, runtime objects,
    execution history, credentials or source identifiers.
    """
    scenario = _owned_scenario(db, scenario_id, verb="read")
    return package_service.export_scenario_package(db, scenario)


@router.post("/validate")
def validate_resource_package(payload: dict[str, Any], db: Session = Depends(get_tenant_db)) -> dict[str, Any]:
    """Validate an uploaded package before a user chooses a target scenario."""
    permission_service.require_principal(db)
    return package_service.validate_package(_package_from_payload(payload))


@router.post("/scenarios/{scenario_id}/import-preview")
def preview_package_import(
    scenario_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_tenant_db),
) -> dict[str, Any]:
    """Build a safe, read-only diff and external-binding checklist for an import.

    The result is explicitly a proposal preview (`mutates_target=false`), not an
    apply endpoint.  A future apply must create a governed release proposal.
    """
    scenario = _owned_scenario(db, scenario_id, verb="write")
    try:
        return package_service.plan_package_import(
            db,
            scenario,
            _package_from_payload(payload),
            environment=_environment_from_payload(payload),
        )
    except (package_service.PackageImportError, package_service.PackageImportConflictError) as exc:
        _import_error(exc)


@router.post(
    "/scenarios/{scenario_id}/import-proposal",
    response_model=PackageImportProposalOut,
)
def create_package_import_proposal(
    scenario_id: str,
    payload: PackageImportProposalIn,
    db: Session = Depends(get_tenant_db),
) -> PackageImportProposalOut:
    """Compile an applicable package into a submitted/draft governance proposal.

    This endpoint deliberately has no apply semantics.  It repeats validation and
    target binding checks at the write boundary, then hands the generated full
    snapshot to the regular independent-review and explicit-merge workflow.
    """
    scenario = _owned_scenario(db, scenario_id, verb="write")
    try:
        proposal, fingerprint, summary = package_service.create_governed_import_proposal(
            db,
            scenario,
            branch_id=payload.branch_id,
            package=payload.package,
            environment=payload.environment,
            title=payload.title,
            description=payload.description,
            submit=payload.submit,
        )
        return PackageImportProposalOut(
            id=proposal.id,
            branch_id=proposal.branch_id,
            base_snapshot_id=proposal.base_snapshot_id,
            proposed_snapshot_id=proposal.proposed_snapshot_id,
            status=proposal.status,
            environment=payload.environment,
            package_fingerprint=fingerprint,
            summary=summary,
        )
    except (package_service.PackageImportError, package_service.PackageImportConflictError) as exc:
        _import_error(exc)
