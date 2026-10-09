"""Select and deliver reviewed versions independently of mutable workspaces."""
from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from ..models import AssistantMessage, AssistantThread, BusinessScenario, OntologyRelease
from ..plugin_coding_schemas import PluginArtifactDownload, PluginArtifactOut, PluginArtifactPage
from . import permission_service, release_service
from .plugin_host_distribution import marketplace_artifact
from .plugin_coding_identity import assert_adapter_identity
from .plugin_host_artifact import build_artifact
from .plugin_host_profile import HOST_LABELS

ARTIFACT_KIND = 'scenario-plugin-artifact.v1'


def catalog_query(db: Session) -> Select[tuple[AssistantMessage, OntologyRelease, BusinessScenario]]:
    principal = permission_service.require_principal(db)
    permission_service.require_tenant_permission(db, 'read')
    return select(AssistantMessage, OntologyRelease, BusinessScenario).join(
        AssistantThread, AssistantThread.id == AssistantMessage.thread_id,
    ).join(OntologyRelease, OntologyRelease.id ==
           AssistantMessage.proposal['manifest']['deployment']['release_id'].as_string()).join(
        BusinessScenario, BusinessScenario.id == OntologyRelease.scenario_id,
    ).where(
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        OntologyRelease.tenant_id == principal.tenant_id,
        BusinessScenario.tenant_id == principal.tenant_id,
        AssistantMessage.proposal['kind'].as_string() == ARTIFACT_KIND,
    )


def artifact_out(row: AssistantMessage, release: OntologyRelease, scenario: BusinessScenario) -> PluginArtifactOut:
    document = row.proposal
    reason = ''
    if release.deleted_at is not None or release.status != 'released' or scenario.status == 'retired':
        reason = '绑定的能力版本已退役，需开发新插件版本'
    elif not release.enabled:
        reason = '绑定的能力版本已停用，请先在插件开发中启用'
    if not reason:
        try:
            assert_adapter_identity(document)
        except HTTPException:
            reason = '受信适配器已更新，需重新开发并审阅新版本'
    return PluginArtifactOut(
        id=row.id, workspace_id=row.thread_id, scenario_id=scenario.id, scenario_name=scenario.name,
        release_id=release.id, release_name=release.name, package_name=document['manifest']['package_name'],
        host=document['manifest'].get('host', 'claude_code'),
        host_label=HOST_LABELS[document['manifest'].get('host', 'claude_code')],
        plugin_version=document['plugin_version'], artifact_hash=document['artifact_hash'],
        created_at=row.created_at, available=not reason, unavailable_reason=reason,
    )


def list_artifacts(db: Session, *, scenario_id: str | None, offset: int, limit: int) -> PluginArtifactPage:
    statement = catalog_query(db)
    if scenario_id:
        release_service._scenario_for_read(db, scenario_id)
        statement = statement.where(BusinessScenario.id == scenario_id)
    rows = db.execute(statement.order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
                      .offset(offset).limit(limit + 1)).all()
    return PluginArtifactPage(items=[artifact_out(row, release, scenario)
        for row, release, scenario in rows[:limit]
        if permission_service.check_scenario(db, scenario, 'read').allowed],
        offset=offset, limit=limit, has_more=len(rows) > limit)


def get_artifact(db: Session, artifact_id: str) -> tuple[AssistantMessage, OntologyRelease, BusinessScenario]:
    result = db.execute(catalog_query(db).where(AssistantMessage.id == artifact_id)).first()
    if result is None:
        raise HTTPException(404, '插件版本不可用')
    release_service._scenario_for_read(db, result[2].id)
    return result[0], result[1], result[2]


def download_artifact(db: Session, artifact_id: str, request: PluginArtifactDownload) -> tuple[str, bytes]:
    row, release, scenario = get_artifact(db, artifact_id)
    value = artifact_out(row, release, scenario)
    if not value.available:
        raise HTTPException(409, value.unavailable_reason)
    document = row.proposal
    if request.artifact_hash != document['artifact_hash']:
        raise HTTPException(409, '所选插件版本身份不匹配，请重新选择')
    artifact = restore_artifact(document)
    # Re-read mutable deployment state after packaging; a disabled release may
    # never be presented as available because a page retained an old selection.
    db.refresh(release)
    db.refresh(scenario)
    release_service._scenario_for_read(db, scenario.id)
    current = artifact_out(row, release, scenario)
    if not current.available:
        raise HTTPException(409, current.unavailable_reason)
    if request.format == 'marketplace':
        artifact = marketplace_artifact(artifact, value.package_name, value.plugin_version, host=value.host)
    suffix = '-marketplace' if request.format == 'marketplace' else ''
    return f'{value.package_name}-{value.plugin_version}{suffix}', artifact


def restore_artifact(document: dict) -> bytes:
    """Restore immutable bytes only; each caller supplies its own authorization."""
    assert_adapter_identity(document)
    try:
        artifact = build_artifact(document['manifest'], files=document['files'], plugin_version=document['plugin_version'])
    except ValueError:
        raise HTTPException(409, '原工件未通过当前结构校验，请开发新版本；已定版内容仍保留') from None
    if hashlib.sha256(artifact).hexdigest() != document['artifact_hash']:
        raise HTTPException(409, '原工件无法准确恢复，请开发新版本；不会替换已定版内容')
    return artifact
