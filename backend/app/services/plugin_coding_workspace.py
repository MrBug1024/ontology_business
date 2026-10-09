"""Persistent authoring sessions reuse the fenced Assistant job infrastructure."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import zipfile
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import select

from ..models import AssistantMessage, AssistantRequestRun, AssistantThread, LLMConfig
from . import assistant_request_run_service as jobs, llm_service, permission_service, release_service, tenant_service
from .capability_contracts import canonical_hash
from .plugin_coding_validation import EDITABLE_PATHS, files_hash, safe_text, validate_files
from .plugin_source_policy import editable_path
from .plugin_project_validation import required_paths, validate_project
from .plugin_coding_identity import adapter_hash, assert_adapter_identity
from .plugin_coding_contract import authoring_contract
from .plugin_coding_conversation import coding_turns
from .plugin_coding_source import project_files
from .plugin_coding_project_state import project_state
from . import plugin_coding_resources as resources
from .plugin_host_artifact import build_artifact
from .scenario_package_service import prepare_package

WORKSPACE_KIND = 'scenario-plugin-workspace.v1'
JOB_KIND = 'scenario-plugin-coding.v1'


def root_id(workspace_id: str) -> str:
    return hashlib.sha256(f'plugin-coding-root/v1\0{workspace_id}'.encode()).hexdigest()[:32]


def owned_root(db, workspace_id: str, *, lock: bool = False):
    principal = permission_service.require_principal(db)
    statement = select(AssistantMessage).join(AssistantThread).where(
        AssistantMessage.id == root_id(workspace_id), AssistantThread.id == workspace_id,
        AssistantThread.tenant_id == principal.tenant_id,
        AssistantThread.created_by_user_id == principal.user_id,
    ).execution_options(populate_existing=True)
    row = db.scalar(statement.with_for_update(of=AssistantMessage) if lock else statement)
    if row is None or (row.proposal or {}).get('kind') != WORKSPACE_KIND:
        raise HTTPException(404, '编码工作台不可用')
    release_service._scenario_for_read(db, row.proposal['manifest']['scenario']['id'])
    return row


def append_event(document: dict, kind: str, message: str, path: str = '') -> None:
    sequence = int(document.get('event_sequence', 0)) + 1
    document['event_sequence'] = sequence
    document['events'] = [*document.get('events', []),
        {'sequence': sequence, 'kind': kind, 'message': message[:1000], 'path': path,
         'run_id': document.get('active_run_id')}][-50:]


def public_workspace(db, row) -> dict:
    document = row.proposal
    assert_adapter_identity(document)
    files = project_files(document)
    run = db.get(AssistantRequestRun, document.get('active_run_id')) if document.get('active_run_id') else None
    return {'id': row.thread_id, 'release_id': document['release_id'], 'revision': document['revision'],
        'host': document.get('manifest', {}).get('host', 'claude_code'),
        'phase': document['phase'], 'source_phase': project_state(document)['source_phase'], 'plugin_version': document['plugin_version'],
        'files_hash': files_hash(document['files']), 'files': files, 'events': document['events'],
        'validation': document['validation'], 'active_run_id': document.get('active_run_id'),
        'run_status': run.status if run else None, 'exported_count': int(document.get('exported_count', 0)),
        'turns': coding_turns(db, row.thread_id), 'capabilities': document['coding_contract']['capabilities'],
        'business_acceptance_required': not bool(document.get('acceptance_request', {}).get('acceptance_cases')),
        'resource_selection': resources.selection(document), 'resource_receipts': document.get('resource_receipts', []),
        'scenario_blueprint': document['coding_contract'].get('scenario_blueprint'),
        'delivery_profile': document['coding_contract'].get('delivery_profile')}


def enqueue_round(db, row, *, instruction: str, request_id: str, mode: str = 'generate') -> None:
    document = deepcopy(row.proposal)
    principal = permission_service.require_principal(db)
    safe_text(instruction)
    key = f'plugin:{request_id}'
    fingerprint = canonical_hash({'workspace': row.thread_id, 'revision': document['revision'],
        'instruction': instruction, 'files': document['files'], 'mode': mode,
        'resources': resources.selection(document), 'resource_snapshot': document.get('resource_snapshot')},
        domain='plugin-coding-request-v1')
    ids = {kind: jobs.request_message_id(kind, tenant_id=principal.tenant_id,
        user_id=principal.user_id, request_id=key) for kind in ('run', 'user', 'assistant')}
    document.update(round_base_phase=document['phase'], round_mode=mode, phase='generating', round_summary='', round_failed=False,
                    coding_failure_diagnostic=None,
                    active_run_id=ids['run'], revision=document['revision'] + 1, coding_resource_keys={})
    append_event(document, 'queued', '讨论问题已保存，等待后台分析' if mode == 'discuss' else '编码需求已保存，等待后台模型生成')
    row.proposal = document
    context = {'assistant_request_run_id': ids['run'], 'assistant_request_status': 'waiting_upload'}
    db.add_all([AssistantMessage(id=ids['user'], thread_id=row.thread_id, role='user', content=instruction),
        AssistantMessage(id=ids['assistant'], thread_id=row.thread_id, role='assistant',
                         content='插件编码任务已接收。', context=context)])
    jobs.enqueue_request(db, run_id=ids['run'], request_id=key, request_fingerprint=fingerprint,
        thread_id=row.thread_id, user_message_id=ids['user'], assistant_message_id=ids['assistant'],
        payload_document={'kind': JOB_KIND, 'workspace_id': row.thread_id, 'instruction': instruction,
                          'request_id': key, 'mode': mode}, upload_run_ids=[])


def cancel_coding_round(db, workspace_id: str, run_id: str) -> None:
    principal = permission_service.require_principal(db)
    run = db.scalar(select(AssistantRequestRun).where(
        AssistantRequestRun.id == run_id, AssistantRequestRun.tenant_id == principal.tenant_id,
        AssistantRequestRun.requested_by_user_id == principal.user_id,
        AssistantRequestRun.thread_id == workspace_id,
    ).with_for_update().execution_options(populate_existing=True))
    if run is None or (run.payload_document or {}).get('kind') != JOB_KIND:
        raise HTTPException(409, '编码轮次不可用')
    if run.status not in {'queued', 'waiting_upload', 'running'}:
        return
    # This fenced job only authors candidate text. Unlike arbitrary Assistant
    # executions, it can safely revoke a running lease without side effects.
    run.status = 'cancelled'
    run.revision += 1
    run.lease_token = ''
    run.lease_expires_at = None
    run.finished_at = datetime.now(timezone.utc)
    message = db.get(AssistantMessage, run.assistant_message_id)
    if message:
        message.content = '旧编码轮次已停止，迟到文件将被拒绝。'
        message.context = {**dict(message.context or {}), 'assistant_request_status': 'cancelled'}
    db.commit()


def create_workspace(db, release_id: str, request, *, draft: bool = False) -> dict:
    safe_text(request.instruction)
    principal = permission_service.require_principal(db)
    identity = canonical_hash({'tenant': principal.tenant_id, 'user': principal.user_id,
        'request': request.request_id}, domain='plugin-coding-workspace-id-v1')[:32]
    creation_hash = canonical_hash({'release_id': release_id, 'request': request.model_dump(mode='json')}, domain='plugin-coding-create-v1')
    existing = db.get(AssistantMessage, root_id(identity))
    if existing:
        row = owned_root(db, identity)
        if row.proposal['creation_hash'] != creation_hash:
            raise HTTPException(409, '本次构建请求身份已用于不同要求')
        return public_workspace(db, row)
    if draft:
        from .plugin_authoring_context import prepare_authoring
        _, manifest = prepare_authoring(db, release_id, request)
    else:
        _, manifest = prepare_package(db, release_id, request)
    contract = authoring_contract(db, manifest)
    # Only newly created workspaces adopt a delivery profile. Existing manifests
    # retain their byte identity when discussion refreshes semantic context.
    manifest.update({key: deepcopy(contract[key]) for key in ('scenario', 'delivery_profile', 'scenario_blueprint')})
    selected, resource_snapshot = resources.freeze_selection(db, {name: getattr(request, name)
        for name in ('llm_config_id', 'skill_ids', 'mcp_ids')})
    with zipfile.ZipFile(io.BytesIO(build_artifact(manifest))) as archive:
        name = manifest['package_name']
        files = {path: archive.read(f'{name}/{path}').decode() for path in EDITABLE_PATHS if path != 'examples/invoke.py'}
    cap = manifest['capabilities'][0]
    files['examples/invoke.py'] = ('import asyncio\nimport json\nfrom server import invoke_scenario_capability\n\n'
        f'async def main():\n    result = await invoke_scenario_capability({cap["kind"]!r}, {cap["key"]!r}, {{}})\n'
        '    print(json.dumps(result, ensure_ascii=False))\n\nif __name__ == "__main__":\n    asyncio.run(main())\n')
    db.add(AssistantThread(id=identity, tenant_id=principal.tenant_id, created_by_user_id=principal.user_id,
        scenario_id=manifest['scenario']['id'], scope_key=f'plugin-coding:{release_id}', title=request.instruction[:80]))
    db.flush()
    document = {'kind': WORKSPACE_KIND, 'creation_hash': creation_hash, 'release_id': release_id,
        'manifest': manifest, 'coding_contract': contract, 'task_goal': request.instruction,
        'required_paths': required_paths(request.instruction), 'authored_paths': [],
        'acceptance_request': {key: value for key, value in request.model_dump(mode='json').items()
             if key in {'expected_revision', 'target', 'capabilities', 'acceptance_cases', 'confirmed_business_acceptance'}},
        'llm_config_id': selected['llm_config_id'], 'resource_selection': selected, 'resource_snapshot': resource_snapshot,
        'plugin_version': request.plugin_version, 'adapter_hash': adapter_hash(manifest['delivery_profile']), 'revision': 1, 'phase': 'draft',
        'files': files, 'previous': {}, 'events': [], 'validation': [], 'active_run_id': None,
        'exported_count': 0}
    append_event(document, 'created', '已读取此场景版本的完整能力契约；初始文件尚未由 AI 定制')
    row = AssistantMessage(id=root_id(identity), thread_id=identity, role='system',
        content='场景插件编码工作台', proposal=document)
    db.add(row)
    db.flush()
    enqueue_round(db, row, instruction=request.instruction, request_id=request.request_id)
    return public_workspace(db, row)


def update_workspace(db, workspace_id: str, request) -> dict:
    safe_text(request.instruction)
    row = owned_root(db, workspace_id)
    assert_adapter_identity(row.proposal)
    release_service._scenario_for_manage(db, row.proposal['manifest']['scenario']['id'])
    request_hash = canonical_hash(request.model_dump(mode='json'), domain='plugin-coding-update-v1')
    applied = row.proposal.get('applied_requests', {})
    if request.request_id in applied:
        if applied[request.request_id] != request_hash:
            raise HTTPException(409, '修订请求身份已用于不同文件或意见')
        return public_workspace(db, row)
    if row.proposal['revision'] != request.expected_revision or files_hash(row.proposal['files']) != request.base_files_hash:
        raise HTTPException(409, '文件已变化，草稿已保留；请刷新并核对差异')
    active = row.proposal.get('active_run_id')
    if active:
        run = db.get(AssistantRequestRun, active)
        if run and run.status in {'queued', 'waiting_upload', 'running'}:
            cancel_coding_round(db, workspace_id, active)
    row = owned_root(db, workspace_id, lock=True)
    document = deepcopy(row.proposal)
    if document['revision'] != request.expected_revision or files_hash(document['files']) != request.base_files_hash:
        raise HTTPException(409, '文件已变化，草稿已保留；请刷新并核对差异')
    for item in request.files:
        safe_text(item.content)
        document['previous'][item.path] = document['files'].get(item.path, '')
        document['files'][item.path] = item.content
        document['authored_paths'] = sorted(set(document.get('authored_paths', [])) | {item.path})
    document.update(revision=document['revision'] + 1, active_run_id=None)
    if request.action in {'stop', 'discuss'} and document['phase'] == 'generating':
        document['phase'] = document.get('round_base_phase', 'draft')
    elif request.action not in {'discuss', 'stop'}:
        document['phase'] = 'draft'
    if request.plugin_version is not None:
        document['plugin_version'] = request.plugin_version
    document['applied_requests'] = dict(list({**document.get('applied_requests', {}), request.request_id: request_hash}.items())[-50:])
    if request.action not in {'discuss', 'stop'}:
        document['validation'] = validate_project(document)
    if request.action == 'generate':
        document['task_goal'] = document.get('task_goal') or request.instruction
        document['required_paths'] = sorted(set(document.get('required_paths', [])) | set(required_paths(request.instruction)))
    if request.action in {'generate', 'discuss'}:
        document['coding_contract'] = authoring_contract(db, document['manifest'])
    append_event(document, 'human_edit' if request.action == 'save' else 'cancelled' if request.action == 'stop' else 'feedback',
                 '当前文件已保存；旧轮次输出不能再覆盖文件' if request.action == 'save' else
                 '本轮已停止，候选源码与原校验状态已保留' if request.action == 'stop' else
                 '讨论问题已接收，保留当前源码与校验状态' if request.action == 'discuss' else '修正意见已接收，基于当前文件开始新轮次')
    row.proposal = document
    if request.action in {'generate', 'discuss'}:
        resources.prepare_context(db, document)
        enqueue_round(db, row, instruction=request.instruction, request_id=request.request_id,
                      mode='discuss' if request.action == 'discuss' else 'generate')
    else:
        db.commit()
    return public_workspace(db, row)
