"""Human-reviewed immutable code snapshots and private distribution artifacts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from fastapi import HTTPException
from sqlalchemy import select
from ..models import AssistantMessage, AssistantThread
from ..scenario_package_schemas import ScenarioPackageBuild
from .plugin_coding_validation import files_hash, validate_files
from .plugin_project_validation import validate_project
from .plugin_coding_identity import assert_adapter_identity, assert_same_version, snapshot_id
from .plugin_coding_workspace import append_event, owned_root, public_workspace
from .plugin_host_artifact import build_artifact
from .scenario_package_service import check_release_state, prepare_package
from . import permission_service


def list_workspaces(db, release_id: str) -> list[dict]:
    principal = permission_service.require_principal(db)
    threads = db.scalars(select(AssistantThread).where(
        AssistantThread.tenant_id == principal.tenant_id, AssistantThread.created_by_user_id == principal.user_id,
        AssistantThread.scope_key == f'plugin-coding:{release_id}',
    ).order_by(AssistantThread.created_at.desc()).limit(5)).all()
    return [public_workspace(db, owned_root(db, thread.id)) for thread in threads]


def review_workspace(db, workspace_id: str, request) -> str:
    row = owned_root(db, workspace_id, lock=True)
    document = deepcopy(row.proposal)
    assert_adapter_identity(document)
    if document['revision'] != request.expected_revision or files_hash(document['files']) != request.files_hash:
        raise HTTPException(409, '代码已变化，请重新审阅当前文件后构建')
    if document['phase'] == 'generating':
        raise HTTPException(409, '请等待编码完成或保存人工修订后再交付')
    if document['phase'] == 'validation_failed':
        raise HTTPException(409, '本轮编码未完成，请先修复并重新校验后定版')
    issues = validate_project(document)
    if issues:
        raise HTTPException(409, '；'.join(issues))
    supplied = getattr(request, 'acceptance', None)
    if supplied is not None:
        original = supplied
    elif not document.get('acceptance_request', {}).get('acceptance_cases'):
        raise HTTPException(409, '代码已保留；定版前请补齐成功、边界和失败案例的业务验收')
    else:
        original = ScenarioPackageBuild.model_validate(document['acceptance_request'])
    selected = {(item['kind'], item['key']) for item in document['manifest']['capabilities']}
    if {(item.kind, item.key) for item in original.capabilities} != selected or original.target != document['manifest']['host']:
        raise HTTPException(409, '验收必须覆盖此插件的全部能力，不能更换能力范围或宿主')
    name, manifest = prepare_package(db, document['release_id'], original)
    if manifest['deployment']['definition_hash'] != document['manifest']['deployment']['definition_hash']:
        raise HTTPException(409, '发布内容身份不匹配，不能交付')
    manifest['coding'] = {'source_hash': request.files_hash, 'plugin_version': document['plugin_version'],
                          'review': 'human_reviewed_candidate_files'}
    if document['coding_contract'].get('client_contract'):
        manifest['client_contract'] = document['coding_contract']['client_contract']
    if document['manifest'].get('delivery_profile'):
        for field in ('scenario_blueprint', 'delivery_profile'):
            manifest[field] = deepcopy(document['manifest'][field])
        manifest['scenario'] = deepcopy(manifest['scenario_blueprint']['scenario'])
    artifact = build_artifact(manifest, files=document['files'], plugin_version=document['plugin_version'])
    artifact_hash = hashlib.sha256(artifact).hexdigest()
    check_release_state(db, document['release_id'], original.expected_revision)
    principal = permission_service.require_principal(db)
    snapshot = {'kind': 'scenario-plugin-artifact.v1', 'manifest': manifest, 'files': deepcopy(document['files']),
        'plugin_version': document['plugin_version'], 'source_hash': request.files_hash,
        'reviewer_id': principal.user_id, 'artifact_hash': artifact_hash, 'adapter_hash': document['adapter_hash']}
    identity = snapshot_id(principal.tenant_id, name, document['plugin_version'])
    existing = db.get(AssistantMessage, identity)
    assert_same_version(existing, snapshot)
    if existing is None:
        db.add(AssistantMessage(id=identity, thread_id=workspace_id, role='system',
                               content='人工审阅的插件工件快照', proposal=snapshot))
    document.update(phase='released', revision=document['revision'] + 1,
                    exported_count=int(document.get('exported_count', 0)))
    document['acceptance_request'] = original.model_dump(mode='json')
    append_event(document, 'review', '人工审阅已完成，固定插件版本；可到发布中心选择此版本')
    row.proposal = document
    db.commit()
    return identity


def export_workspace(db, workspace_id: str, request) -> tuple[str, bytes]:
    # Keep the previous endpoint as a thin compatibility adapter. Both paths
    # reserve the same reviewed version; delivery always reads its snapshot.
    from .plugin_artifact_catalog import download_artifact
    from ..plugin_coding_schemas import PluginArtifactDownload
    identity = review_workspace(db, workspace_id, request)
    snapshot = db.get(AssistantMessage, identity)
    result = download_artifact(db, identity, PluginArtifactDownload(
        artifact_hash=snapshot.proposal['artifact_hash'], format=request.format))
    row = owned_root(db, workspace_id, lock=True)
    document = deepcopy(row.proposal)
    document['exported_count'] = int(document.get('exported_count', 0)) + 1
    row.proposal = document
    db.commit()
    return result
