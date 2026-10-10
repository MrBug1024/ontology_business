"""Scenario-scoped plugin coding, revision, and private artifact downloads."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError
from ..plugin_coding_schemas import (
    CodingWorkspaceOut, PluginCodingCreate, PluginCodingExport, PluginCodingUpdate,
    PluginCodingReview, PluginArtifactDownload, PluginArtifactOut, PluginArtifactPage,
    PluginArtifactRetire, PluginArtifactDelete, PluginCodingDraftCreate, CodingContextOut, CodingSessionOut,
    CodingProjectOut, CodingProjectSummaryOut, CodingResourceCatalog, PluginCodingSettings,
)
from ..services.auth_service import get_tenant_db
from ..services import plugin_coding_workspace as coding
from ..services.plugin_coding_export import export_workspace, review_workspace
from ..services import plugin_artifact_catalog as catalog
from ..services import plugin_authoring_context as authoring
from ..services import plugin_coding_resources as resources
from ..services.assistant_request_run_service import AssistantRequestError
from ..services.scenario_package_acceptance import PackageValidationError

router = APIRouter(tags=['plugin-coding'])


@router.get('/plugin-coding/resources', response_model=CodingResourceCatalog)
def resource_catalog(scenario_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        return resources.resource_catalog(db, scenario_id)
    except ValueError as exc:
        raise failure(exc) from None


@router.post('/plugin-workspaces/{workspace_id}/settings', response_model=CodingWorkspaceOut)
def settings(payload: PluginCodingSettings, workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        return resources.update_settings(db, workspace_id, payload)
    except ValueError as exc:
        raise failure(exc) from None


@router.get('/plugin-projects', response_model=list[CodingProjectSummaryOut])
def projects(scenario_id: str = Query(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return authoring.list_projects(db, scenario_id)


@router.get('/plugin-workspaces', response_model=list[CodingSessionOut])
def sessions(scenario_id: str = Query(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return authoring.list_sessions(db, scenario_id)


@router.get('/plugin-workspaces/{workspace_id}/files', response_model=CodingProjectOut)
def project_files(workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return authoring.project_summary(db, workspace_id)


@router.get('/scenario-releases/{release_id}/plugin-context', response_model=CodingContextOut)
def context(release_id: str = Path(pattern=r'^[a-f0-9]{32}$'),
            target: Literal['claude_code', 'codex'] = Query(default='claude_code'), db=Depends(get_tenant_db)):
    try:
        return authoring.discover_context(db, release_id, target)
    except ValueError as exc:
        raise failure(exc) from None


@router.post('/scenario-releases/{release_id}/plugin-drafts', response_model=CodingWorkspaceOut)
def draft(payload: PluginCodingDraftCreate, release_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        try:
            return coding.create_workspace(db, release_id, payload, draft=True)
        except IntegrityError:
            db.rollback()
            return coding.create_workspace(db, release_id, payload, draft=True)
    except (PackageValidationError, AssistantRequestError, ValueError) as exc:
        raise failure(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '编码任务并发登记冲突，请重试') from None


def failure(exc):
    if isinstance(exc, AssistantRequestError):
        return HTTPException(exc.status_code, {'code': exc.code, 'message': exc.message})
    return HTTPException(409, {'code': 'plugin_coding_conflict', 'message': str(exc)})


@router.post('/scenario-releases/{release_id}/plugin-workspaces', response_model=CodingWorkspaceOut)
def create(payload: PluginCodingCreate, release_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        try:
            return coding.create_workspace(db, release_id, payload)
        except IntegrityError:
            db.rollback()
            return coding.create_workspace(db, release_id, payload)
    except (PackageValidationError, AssistantRequestError, ValueError) as exc:
        raise failure(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '编码会话并发登记冲突，请重试') from None


@router.get('/plugin-workspaces/{workspace_id}', response_model=CodingWorkspaceOut)
def get(workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'),
        session_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return coding.public_workspace(db, coding.owned_root(db, workspace_id), session_id=session_id)


@router.post('/plugin-workspaces/{workspace_id}/revisions', response_model=CodingWorkspaceOut)
def revise(payload: PluginCodingUpdate, workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        return coding.update_workspace(db, workspace_id, payload)
    except (AssistantRequestError, ValueError) as exc:
        raise failure(exc) from None


@router.post('/plugin-workspaces/{workspace_id}/review', response_model=PluginArtifactOut)
def review(payload: PluginCodingReview, workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        identity = review_workspace(db, workspace_id, payload)
        return catalog.artifact_out(*catalog.get_artifact(db, identity))
    except (PackageValidationError, ValueError) as exc:
        raise failure(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该版本正在并发定版，请刷新；内容不同时必须设置新版本') from None


@router.get('/plugin-artifacts', response_model=PluginArtifactPage)
def versions(scenario_id: str | None = Query(default=None, pattern=r'^[a-f0-9]{32}$'),
             include_retired: bool = Query(default=False),
             offset: int = Query(default=0, ge=0, le=100000), limit: int = Query(default=50, ge=1, le=100),
             db=Depends(get_tenant_db)):
    return catalog.list_artifacts(db, scenario_id=scenario_id, offset=offset, limit=limit,
                                  include_retired=include_retired)


@router.post('/plugin-artifacts/{artifact_id}/retire', response_model=PluginArtifactOut)
def retire(payload: PluginArtifactRetire, artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        value = catalog.retire_artifact(db, artifact_id, payload)
        db.commit()
        return value
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '插件版本下线并发冲突，请刷新后重试') from None


@router.post('/plugin-artifacts/{artifact_id}/delete', response_model=None)
def remove(payload: PluginArtifactDelete, artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        catalog.delete_artifact(db, artifact_id, payload)
        db.commit()
        return None
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '插件版本删除并发冲突，请刷新后重试') from None


@router.post('/plugin-artifacts/{artifact_id}/download')
def deliver(payload: PluginArtifactDownload, artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    name, data = catalog.download_artifact(db, artifact_id, payload)
    return zip_response(name, data)


@router.get('/plugin-artifacts/{artifact_id}', response_model=PluginArtifactOut)
def version(artifact_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    return catalog.artifact_out(*catalog.get_artifact(db, artifact_id))


def zip_response(name: str, data: bytes) -> Response:
    return Response(data, media_type='application/zip', headers={
        'Content-Disposition': f'attachment; filename="{name}.zip"', 'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    })


@router.post('/plugin-workspaces/{workspace_id}/artifact')
def artifact(payload: PluginCodingExport, workspace_id: str = Path(pattern=r'^[a-f0-9]{32}$'), db=Depends(get_tenant_db)):
    try:
        name, data = export_workspace(db, workspace_id, payload)
    except (PackageValidationError, ValueError) as exc:
        raise failure(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该版本正在并发交付，请刷新；内容不同时必须设置新版本') from None
    return zip_response(name, data)
