"""Browser-authenticated business discovery and modeling-material handoff."""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from ..distillation_models import DistillationProject, DistillationPublication
from ..distillation_resource_schemas import InvestigationResourceCatalogOut
from ..models import AuthorizationGrant
from ..distillation_schemas import (
    AnalysisOut, AnalyzeRequest, ArtifactOut, DistillationDocument, ProjectCreate,
    ProjectOut, ProjectUpdate, PublicationOut, RevisionRequest, ScenarioStateOut,
)
from ..services import distillation_analysis_service, distillation_service, permission_service
from ..services import distillation_resource_service
from ..services.auth_service import get_tenant_db


router = APIRouter(prefix="/business-distillation", tags=["business-distillation"])


@router.get("/resources", response_model=InvestigationResourceCatalogOut)
def list_investigation_resources(
    scenario_id: str | None = Query(None, max_length=32),
    db: Session = Depends(get_tenant_db),
):
    try:
        return distillation_resource_service.resource_catalog(db, scenario_id)
    except PermissionError as exc:
        raise HTTPException(403, "没有该业务场景的调查资源权限") from exc


def _project_out(db: Session, row: DistillationProject) -> ProjectOut:
    return ProjectOut(id=row.id, name=row.name, scenario_id=row.scenario_id, revision=row.revision,
        document=DistillationDocument.model_validate(row.document), created_at=row.created_at,
        updated_at=row.updated_at, can_write=distillation_service.can_write(db, row))


def _publication_out(row: DistillationPublication) -> PublicationOut:
    return PublicationOut(id=row.id, project_id=row.project_id, scenario_id=row.scenario_id,
        project_revision=row.project_revision, data_source_id=row.data_source_id, created_at=row.created_at,
        artifacts=[ArtifactOut(**{key: item[key] for key in ("key", "filename", "mime", "sha256")}) for item in row.artifacts])


@router.get("/scenario/{scenario_id}/state", response_model=ScenarioStateOut)
def get_scenario_state(scenario_id: str, db: Session = Depends(get_tenant_db)):
    row = distillation_service.scenario_state(db, scenario_id, write=False, create=False)
    if row is None:
        return ScenarioStateOut(scenario_id=scenario_id, revision=1,
            document=DistillationDocument(), updated_at=datetime.now(timezone.utc))
    return ScenarioStateOut(scenario_id=row.scenario_id, revision=row.revision,
        document=DistillationDocument.model_validate(row.document), updated_at=row.updated_at)


@router.get("/scenario/{scenario_id}/publications", response_model=list[PublicationOut])
def list_scenario_publications(scenario_id: str, limit: int = Query(50, ge=1, le=100),
                               offset: int = Query(0, ge=0, le=100_000), db: Session = Depends(get_tenant_db)):
    return [_publication_out(row) for row in distillation_service.list_scenario_publications(
        db, scenario_id, limit=limit, offset=offset,
    )]


@router.get("/scenario/{scenario_id}/publications/{publication_id}", response_model=PublicationOut)
def get_scenario_publication(scenario_id: str, publication_id: str, db: Session = Depends(get_tenant_db)):
    return _publication_out(distillation_service.scenario_publication(db, scenario_id, publication_id))


@router.get("/scenario/{scenario_id}/publications/{publication_id}/artifacts/{artifact_key}")
def download_scenario_artifact(scenario_id: str, publication_id: str, artifact_key: str,
                               db: Session = Depends(get_tenant_db)):
    publication = distillation_service.scenario_publication(db, scenario_id, publication_id)
    artifact = distillation_service.artifact_content(publication, artifact_key)
    return Response(content=artifact["content"], media_type=artifact["mime"], headers={
        "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
    })


@router.get("", response_model=list[ProjectOut])
def list_projects(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000),
                  scenario_id: str | None = Query(None, max_length=32), shared_only: bool = False,
                  db: Session = Depends(get_tenant_db)):
    principal = permission_service.require_principal(db)
    # A scenario-scoped view is governed by that scenario's ACL.  Do not
    # require workspace-level read here: a collaborator may have an explicit
    # scenario read grant while the workspace's shared catalog remains out of
    # scope.  The tenant gate is retained for global/shared history below.
    query = select(DistillationProject).where(DistillationProject.tenant_id == principal.tenant_id)
    denied = exists(select(AuthorizationGrant.id).where(
        AuthorizationGrant.organization_id == principal.organization_id,
        AuthorizationGrant.resource_type == "scenario",
        or_(AuthorizationGrant.resource_id == DistillationProject.scenario_id, AuthorizationGrant.resource_id == "*"),
        AuthorizationGrant.verb.in_(("read", "*")), AuthorizationGrant.effect == "deny",
        or_(AuthorizationGrant.role_id == principal.role_id, AuthorizationGrant.user_id == principal.user_id),
    ))
    query = query.where(or_(DistillationProject.scenario_id.is_(None), ~denied))
    if shared_only and scenario_id:
        raise HTTPException(422, "场景筛选和共享历史筛选不能同时使用")
    if shared_only:
        permission_service.require_tenant_permission(db, "read")
        query = query.where(DistillationProject.scenario_id.is_(None))
    if scenario_id:
        distillation_service.authorize_scope(db, scenario_id)
        query = query.where(DistillationProject.scenario_id == scenario_id)
    elif not shared_only:
        permission_service.require_tenant_permission(db, "read")
    rows = db.scalars(query.order_by(DistillationProject.updated_at.desc(), DistillationProject.id).offset(offset).limit(limit)).all()
    result = []
    for row in rows:
        try:
            distillation_service.authorize_scope(db, row.scenario_id)
        except HTTPException:
            continue
        result.append(_project_out(db, row))
    return result


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_tenant_db)):
    row = distillation_service.create_project(db, payload)
    db.commit()
    return _project_out(db, row)


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_tenant_db)):
    return _project_out(db, distillation_service.project(db, project_id))


@router.put("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_tenant_db)):
    row = distillation_service.update_project(db, project_id, payload)
    db.commit()
    return _project_out(db, row)


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, db: Session = Depends(get_tenant_db)):
    distillation_service.delete_project(db, project_id)
    db.commit()
    return Response(status_code=204)


@router.post("/{project_id}/analyze", response_model=AnalysisOut)
def analyze_project(project_id: str, payload: AnalyzeRequest, db: Session = Depends(get_tenant_db)):
    return distillation_analysis_service.analyze(db, project_id, payload)


@router.post("/{project_id}/publish", response_model=PublicationOut)
def publish_project(project_id: str, payload: RevisionRequest, db: Session = Depends(get_tenant_db)):
    row = distillation_service.publish(db, project_id, payload.expected_revision)
    db.commit()
    return _publication_out(row)


@router.get("/{project_id}/publications", response_model=list[PublicationOut])
def list_publications(project_id: str, limit: int = Query(50, ge=1, le=100),
                      offset: int = Query(0, ge=0, le=100_000), db: Session = Depends(get_tenant_db)):
    project = distillation_service.project(db, project_id)
    rows = db.scalars(select(DistillationPublication).where(DistillationPublication.project_id == project.id,
        DistillationPublication.tenant_id == project.tenant_id).order_by(
        DistillationPublication.project_revision.desc()).offset(offset).limit(limit)).all()
    return [_publication_out(row) for row in rows]


@router.get("/{project_id}/publications/{publication_id}/artifacts/{artifact_key}")
def download_artifact(project_id: str, publication_id: str, artifact_key: str, db: Session = Depends(get_tenant_db)):
    project = distillation_service.project(db, project_id)
    publication = db.scalar(select(DistillationPublication).where(DistillationPublication.id == publication_id,
        DistillationPublication.project_id == project.id, DistillationPublication.tenant_id == project.tenant_id))
    if publication is None:
        raise HTTPException(404, "交接文件不存在")
    artifact = distillation_service.artifact_content(publication, artifact_key)
    return Response(content=artifact["content"], media_type=artifact["mime"], headers={
        "Content-Disposition": f'attachment; filename="{artifact["filename"]}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
    })


@router.get("/{project_id}/publications/{publication_id}", response_model=PublicationOut)
def get_publication(project_id: str, publication_id: str, db: Session = Depends(get_tenant_db)):
    project = distillation_service.project(db, project_id)
    publication = db.scalar(select(DistillationPublication).where(DistillationPublication.id == publication_id,
        DistillationPublication.project_id == project.id, DistillationPublication.tenant_id == project.tenant_id))
    if publication is None:
        raise HTTPException(404, "交接资料不存在")
    return _publication_out(publication)
