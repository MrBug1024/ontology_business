"""Discover pinned scenario contracts without claiming business acceptance."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from ..models import AssistantMessage, AssistantRequestRun, AssistantThread, OntologyRelease
from . import capability_application_service, permission_service, release_service
from .plugin_coding_contract import authoring_contract
from .plugin_coding_source import project_files
from .plugin_coding_workspace import (
    SESSION_KIND, WORKSPACE_KIND, find_project_thread, owned_root, root_id,
    LEGACY_SCOPE_PREFIX, PROJECT_SCOPE_PREFIX,
)
from .plugin_host_profile import package_name, require_host


def release_context(db, release_id: str, expected_revision: int | None = None) -> tuple[object, dict]:
    principal = permission_service.require_principal(db)
    release = db.scalar(select(OntologyRelease).where(
        OntologyRelease.id == release_id, OntologyRelease.tenant_id == principal.tenant_id,
        OntologyRelease.deleted_at.is_(None)))
    if release is None:
        raise HTTPException(404, '能力版本不可用')
    scenario, _ = release_service._scenario_for_manage(db, release.scenario_id)
    if expected_revision is not None and release.revision != expected_revision:
        raise HTTPException(409, '能力版本状态已变化，请重新读取场景能力')
    if release.status != 'released' or not release.enabled:
        raise HTTPException(409, '此能力版本未启用；请在场景能力页面选择并启用明确版本')
    try:
        deployment, _ = capability_application_service.resolve_deployment(db, scenario, release_id=release.id)
        available = capability_application_service.list_capabilities(db, scenario, release_id=release.id)
    except capability_application_service.CapabilityApplicationError:
        raise HTTPException(409, '场景能力契约不可用，请在验证中心检查此能力版本') from None
    manifest = {
        'package_version': 'scenario-plugin.v1', 'adapter_version': '1.0.0',
        'package_name': f'scenario-{release.id}', 'host': 'claude_code',
        'scenario': {'id': scenario.id, 'name': scenario.name},
        'deployment': {'definition_source': 'release', 'release_id': release.id,
                       'snapshot_id': deployment.snapshot_id, 'definition_hash': deployment.definition_hash},
        'capabilities': [{'kind': item['kind'], 'key': item['key'], 'name': item['name']}
                         for item in available if item['kind'] in {'function', 'action', 'rule', 'workflow'}],
        'acceptance': {'kind': 'pending_human_business_acceptance', 'cases': []},
        'runtime': {'execution': 'platform', 'protocol': 'mcp', 'credentials': 'external_environment'},
    }
    if not manifest['capabilities']:
        raise HTTPException(409, '此场景版本没有可封装的函数、操作、规则或工作流')
    return release, manifest


def discover_context(db, release_id: str, target: str = 'claude_code') -> dict:
    _, manifest = release_context(db, release_id)
    manifest['host'] = require_host(target)
    manifest['package_name'] = package_name(release_id, target)
    contract = authoring_contract(db, manifest, blueprint_selection=[])
    return {key: contract[key] for key in ('scenario', 'deployment', 'capabilities', 'scenario_blueprint', 'delivery_profile')}


def prepare_authoring(db, release_id: str, request) -> tuple[str, dict]:
    _, manifest = release_context(db, release_id, request.expected_revision)
    selected = {(item.kind, item.key) for item in request.capabilities}
    indexed = {(item['kind'], item['key']): item for item in manifest['capabilities']}
    if not selected.issubset(indexed):
        raise HTTPException(409, '所选能力不属于此场景版本')
    manifest['capabilities'] = [indexed[(item.kind, item.key)] for item in request.capabilities]
    manifest['host'] = request.target
    manifest['package_name'] = package_name(release_id, request.target)
    return manifest['package_name'], manifest


def _scenario_projects(db, scenario_id: str) -> list[tuple[AssistantThread, AssistantMessage]]:
    """Resolve the durable project for each host plus its workspace root."""
    principal = permission_service.require_principal(db)
    resolved: dict[str, tuple[AssistantThread, AssistantMessage]] = {}
    rows = db.execute(select(AssistantThread, AssistantMessage).join(AssistantMessage).where(
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        AssistantThread.scenario_id == scenario_id,
        AssistantMessage.proposal['kind'].as_string() == WORKSPACE_KIND,
        AssistantThread.scope_key.like(f'{PROJECT_SCOPE_PREFIX}%'),
    ).order_by(AssistantThread.created_at.desc(), AssistantThread.id.desc())).all()
    for thread, row in rows:
        host = (row.proposal or {}).get('manifest', {}).get('host', 'claude_code')
        resolved.setdefault(host, (thread, row))
    for host in ('claude_code', 'codex'):
        if host in resolved:
            continue
        adopted = find_project_thread(db, scenario_id, host)
        if adopted is not None:
            root = db.get(AssistantMessage, root_id(adopted.id))
            if root is not None:
                resolved[host] = (adopted, root)
    return [resolved[host] for host in sorted(resolved)]


def _latest_snapshot_version(db, project_id: str) -> str:
    """Most recent non-deleted reviewed snapshot version; '' before first review."""
    from .plugin_artifact_catalog import ARTIFACT_KIND
    row = db.scalar(select(AssistantMessage).where(
        AssistantMessage.thread_id == project_id,
        AssistantMessage.proposal['kind'].as_string() == ARTIFACT_KIND,
        AssistantMessage.proposal['deleted_at'].as_string().is_(None),
    ).order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc()).limit(1))
    return (row.proposal or {}).get('plugin_version') or '' if row is not None else ''


def list_projects(db, scenario_id: str) -> list[dict]:
    release_service._scenario_for_read(db, scenario_id)
    projects = []
    for thread, row in _scenario_projects(db, scenario_id):
        document = row.proposal
        projects.append({'id': thread.id, 'scenario_id': scenario_id, 'release_id': document['release_id'],
            'host': document.get('manifest', {}).get('host', 'claude_code'), 'phase': document['phase'],
            'plugin_version': _latest_snapshot_version(db, thread.id),
            'capabilities': [{'kind': item['kind'], 'key': item['key'], 'name': item['name']}
                             for item in document['manifest'].get('capabilities', [])],
            'created_at': thread.created_at})
    return projects


def list_sessions(db, scenario_id: str) -> list[dict]:
    principal = permission_service.require_principal(db)
    release_service._scenario_for_read(db, scenario_id)
    statement = select(AssistantThread, AssistantMessage).join(AssistantMessage).where(
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        AssistantThread.scenario_id == scenario_id,
        AssistantMessage.proposal['kind'].as_string() == SESSION_KIND,
    ).order_by(AssistantThread.created_at.desc(), AssistantThread.id.desc()).limit(20)
    sessions = [_session_out(db, thread, marker.proposal['project_id'])
                for thread, marker in db.execute(statement).all()]
    sessions.extend(_legacy_frozen_sessions(db, scenario_id))
    sessions.sort(key=lambda item: item['created_at'], reverse=True)
    return sessions[:20]


def _session_out(db, thread: AssistantThread, project_id: str) -> dict:
    root = db.get(AssistantMessage, root_id(project_id))
    document = (root.proposal if root is not None else {}) or {}
    active_run = db.get(AssistantRequestRun, document.get('active_run_id')) if document.get('active_run_id') else None
    return {'id': thread.id, 'project_id': project_id, 'release_id': document.get('release_id', ''),
            'scenario_id': thread.scenario_id, 'title': thread.title,
            'host': document.get('manifest', {}).get('host', 'claude_code'),
            'phase': document.get('phase', 'draft'),
            'active': active_run is not None and active_run.thread_id == thread.id
                      and active_run.status in {'queued', 'waiting_upload', 'running'},
            'frozen': False, 'created_at': thread.created_at}


def _legacy_frozen_sessions(db, scenario_id: str) -> list[dict]:
    principal = permission_service.require_principal(db)
    rows = db.execute(select(AssistantThread, AssistantMessage).join(AssistantMessage).where(
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        AssistantThread.scenario_id == scenario_id,
        AssistantMessage.proposal['kind'].as_string() == WORKSPACE_KIND,
        AssistantThread.scope_key.like(f'{LEGACY_SCOPE_PREFIX}%'),
    ).order_by(AssistantThread.created_at.desc(), AssistantThread.id.desc())).all()
    frozen = []
    for thread, row in rows:
        host = (row.proposal or {}).get('manifest', {}).get('host', 'claude_code')
        current = find_project_thread(db, scenario_id, host)
        if current is not None and current.id == thread.id:
            continue
        document = row.proposal
        frozen.append({'id': thread.id, 'project_id': current.id if current is not None else thread.id,
            'release_id': document.get('release_id', ''), 'scenario_id': scenario_id, 'title': thread.title,
            'host': host, 'phase': document.get('phase', 'draft'), 'active': False, 'frozen': True,
            'created_at': thread.created_at})
    return frozen


def project_summary(db, workspace_id: str) -> dict:
    row = owned_root(db, workspace_id)
    document = row.proposal
    return {'id': row.thread_id, 'release_id': document['release_id'], 'revision': document['revision'],
            'phase': document['phase'], 'files': project_files(document)}
