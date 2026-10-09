"""Discover pinned scenario contracts without claiming business acceptance."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from ..models import AssistantMessage, AssistantThread, OntologyRelease
from . import capability_application_service, permission_service, release_service
from .plugin_coding_contract import authoring_contract
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


def list_tasks(db, scenario_id: str | None = None) -> list[dict]:
    principal = permission_service.require_principal(db)
    if scenario_id:
        release_service._scenario_for_read(db, scenario_id)
    statement = select(AssistantThread, AssistantMessage).join(AssistantMessage).where(
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
        AssistantMessage.proposal['kind'].as_string() == 'scenario-plugin-workspace.v1',
    ).order_by(AssistantThread.created_at.desc()).limit(20)
    if scenario_id:
        statement = statement.where(AssistantThread.scenario_id == scenario_id)
    tasks = []
    for thread, row in db.execute(statement).all():
        try:
            release_service._scenario_for_read(db, thread.scenario_id)
        except HTTPException as exc:
            if exc.status_code in {403, 404}:
                continue
            raise
        doc = row.proposal
        tasks.append({'id': thread.id, 'release_id': doc['release_id'], 'scenario_id': thread.scenario_id,
                      'host': doc['manifest'].get('host', 'claude_code'),
                      'title': thread.title, 'plugin_version': doc['plugin_version'],
                      'phase': doc['phase'], 'created_at': thread.created_at})
    return tasks
