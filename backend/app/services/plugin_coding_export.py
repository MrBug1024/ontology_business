"""Human-reviewed immutable code snapshots and private distribution artifacts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from ..models import AssistantMessage
from ..scenario_package_schemas import ScenarioPackageBuild
from .plugin_coding_validation import files_hash
from .plugin_project_validation import validate_project
from .plugin_coding_identity import assert_adapter_identity, assert_same_version, snapshot_id
from .plugin_coding_workspace import owned_root
from .plugin_host_artifact import build_artifact
from .scenario_package_service import check_release_state, prepare_package
from . import permission_service


def review_workspace(db, workspace_id: str, request) -> str:
    """Freeze a read-only snapshot of the current source under a review version.

    Publication never touches the project: no revision bump, no phase change,
    no acceptance write-back. The tree keeps evolving exactly as it was, and the
    reviewed bytes live only in the immutable snapshot handed to the catalog.
    """
    row = owned_root(db, workspace_id)
    document = row.proposal
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
    version = request.plugin_version
    manifest['coding'] = {'source_hash': request.files_hash, 'plugin_version': version,
                          'review': 'human_reviewed_candidate_files'}
    if document['coding_contract'].get('client_contract'):
        manifest['client_contract'] = document['coding_contract']['client_contract']
    if document['manifest'].get('delivery_profile'):
        for field in ('scenario_blueprint', 'delivery_profile'):
            manifest[field] = deepcopy(document['manifest'][field])
        manifest['scenario'] = deepcopy(manifest['scenario_blueprint']['scenario'])
    artifact = build_artifact(manifest, files=document['files'], plugin_version=version)
    artifact_hash = hashlib.sha256(artifact).hexdigest()
    check_release_state(db, document['release_id'], original.expected_revision)
    principal = permission_service.require_principal(db)
    snapshot = {'kind': 'scenario-plugin-artifact.v1', 'manifest': manifest, 'files': deepcopy(document['files']),
        'plugin_version': version, 'source_hash': request.files_hash,
        'reviewer_id': principal.user_id, 'artifact_hash': artifact_hash, 'adapter_hash': document['adapter_hash']}
    identity = snapshot_id(principal.tenant_id, name, version)
    existing = db.get(AssistantMessage, identity)
    assert_same_version(existing, snapshot)
    if existing is None:
        db.add(AssistantMessage(id=identity, thread_id=workspace_id, role='system',
                               content='人工审阅的插件工件快照', proposal=snapshot))
        try:
            db.commit()
        except IntegrityError:
            # A concurrent review of identical content won the insert; verify the
            # winner reserves exactly these bytes and report it idempotently.
            db.rollback()
            winner = db.get(AssistantMessage, identity)
            if winner is None:
                raise
            assert_same_version(winner, snapshot)
    return identity


def export_workspace(db, workspace_id: str, request) -> tuple[str, bytes]:
    # Keep the previous endpoint as a thin compatibility adapter. Both paths
    # reserve the same reviewed version; delivery always reads its snapshot.
    from .plugin_artifact_catalog import download_artifact
    from ..plugin_coding_schemas import PluginArtifactDownload
    identity = review_workspace(db, workspace_id, request)
    snapshot = db.get(AssistantMessage, identity)
    return download_artifact(db, identity, PluginArtifactDownload(
        artifact_hash=snapshot.proposal['artifact_hash'], format=request.format))
