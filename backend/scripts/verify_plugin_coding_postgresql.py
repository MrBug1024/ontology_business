"""Verify coding persistence/concurrency in a newly created disposable PG database.

The model stream and release acceptance are explicit synthetic fixtures. This
proves job/JSON/CAS behavior, not actual provider output or business acceptance.

Semantics under verification: one durable plugin project per (scenario, host)
owns the only mutable source tree; sessions are bounded conversations that never
imply versions; versions exist only as human-reviewed snapshots and can be
retired out of the active catalog.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def review_payload(value, version):
    from app.plugin_coding_schemas import PluginCodingExport
    return PluginCodingExport(expected_revision=value['revision'], files_hash=value['files_hash'],
                              format='plugin', plugin_version=version, confirmed_code_review=True)


def verify_session_continuity(session, release, request, first):
    """A second task continues the same codebase instead of reseeding a template."""
    from app.routers.plugin_coding import create
    from app.services import plugin_coding_workspace as coding
    with session() as db:
        current = coding.public_workspace(db, coding.owned_root(db, first['id']))
    with session() as db:
        second = create(request.model_copy(update={'request_id': uuid4().hex, 'instruction': 'Continue the evolved plugin'}), release_id=release, db=db)
    assert second['id'] == current['id'], 'a new task must reuse the durable plugin project'
    assert second['session_id'] and second['session_id'] != current['session_id'], 'each task is its own session'
    assert second['files_hash'] == current['files_hash'], 'the shared source tree must not be reset by a new session'
    assert 'plugin_version' not in second, 'the coding workspace carries no version state at all'
    assert [turn['instruction'] for turn in second['turns']] == ['Continue the evolved plugin']
    with session() as db:
        latest = coding.public_workspace(db, coding.owned_root(db, first['id']))
        assert [turn['instruction'] for turn in latest['turns']] == ['Continue the evolved plugin']
        older = coding.public_workspace(db, coding.owned_root(db, first['id']), session_id=first['session_id'])
        assert [turn['instruction'] for turn in older['turns']] == [turn['instruction'] for turn in first['turns']]
    return {'second_session_same_project': True, 'source_tree_not_reset': True,
            'per_session_turns': True}


def verify_concurrent_review(session, workspace_id):
    """Two concurrent reviews of the same tree yield exactly one snapshot."""
    from app.services import plugin_coding_workspace as coding, plugin_coding_export as export
    from fastapi import HTTPException
    with session() as db:
        value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
    barrier = threading.Barrier(2)

    def review():
        with session() as db:
            current = coding.public_workspace(db, coding.owned_root(db, workspace_id))
            barrier.wait(timeout=10)
            try:
                export.export_workspace(db, workspace_id, review_payload(current, '2.0.0'))
                return 200
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(list(pool.map(lambda _: review(), range(2))))
    assert results == [200, 200], 'same content under the same version reviews idempotently'
    return {'concurrent_review_idempotent_snapshot': True}


def verify_no_file_round(session, release, request, model_control, fresh_scenario):
    from app.routers.plugin_coding import create
    from app.services import plugin_coding_workspace as coding, plugin_coding_worker as worker
    from app.services import assistant_request_run_service as jobs
    model_control['files'] = False
    model_control['fresh_scenario'] = fresh_scenario
    try:
        with session() as db:
            value = create(request.model_copy(update={'request_id': uuid4().hex}), release_id=release, db=db)
        assert jobs.process_request(value['active_run_id'], worker.execute) is False
        with session() as db:
            failed = coding.public_workspace(db, coding.owned_root(db, value['id']))
            assert failed['phase'] == 'validation_failed' and failed['run_status'] == 'failed'
            assert failed['validation'], 'a plan without authored files cannot pass validation'
            assert 'plugin_version' not in failed, 'a coding project never carries version state'
    finally:
        model_control['files'] = True
        model_control.pop('fresh_scenario', None)


def verify_version_history(session, workspace_id, tenant, manifest, files):
    from app.models import AssistantMessage
    from app.plugin_coding_schemas import PluginCodingUpdate
    from app.services import plugin_coding_workspace as coding, plugin_coding_export as export
    from app.services.plugin_coding_identity import snapshot_id
    from fastapi import HTTPException
    with session() as db:
        snapshot_key = snapshot_id(tenant, manifest['package_name'], '1.0.0')
        snapshot_before = dict(db.get(AssistantMessage, snapshot_key).proposal)
        # A plain save ends the session left generating by the continuity check.
        value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
        value = coding.update_workspace(db, workspace_id, PluginCodingUpdate(
            expected_revision=value['revision'], session_id=value['session_id'], request_id=uuid4().hex,
            action='save', base_files_hash=value['files_hash'], files=[]))
        before = dict(coding.owned_root(db, workspace_id).proposal)
        # Reviewing identical content under the same version is idempotent and
        # leaves the project document identical: publication never touches it.
        export.export_workspace(db, workspace_id, review_payload(
            coding.public_workspace(db, coding.owned_root(db, workspace_id)), '1.0.0'))
        assert dict(coding.owned_root(db, workspace_id).proposal) == before, \
            'publication must never touch the plugin project'
        assert db.get(AssistantMessage, snapshot_key).proposal == snapshot_before
        value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
        value = coding.update_workspace(db, workspace_id, PluginCodingUpdate(
            expected_revision=value['revision'], session_id=value['session_id'], request_id=uuid4().hex,
            action='save', base_files_hash=value['files_hash'],
            files=[{'path': 'README.md', 'content': files['README.md'] + 'Next version source.\n'}]))
        try:
            export.export_workspace(db, workspace_id, review_payload(value, '1.0.0'))
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
        else:
            raise AssertionError('a delivered version was overwritten with different source')
        assert db.get(AssistantMessage, snapshot_key).proposal == snapshot_before
        exported = export.export_workspace(db, workspace_id, review_payload(value, '1.0.1'))
        assert exported[0].endswith('1.0.1')
        latest = dict(coding.owned_root(db, workspace_id).proposal)
        assert latest['revision'] == value['revision'] and latest['files'] != before['files'], \
            'only the explicit edit changed the project, never the publications'


def verify_retirement(session, workspace_id, tenant, manifest):
    from sqlalchemy import select
    from app.services import plugin_coding_workspace as coding
    from app.models import AssistantMessage
    from app.services import plugin_artifact_catalog as catalog
    from app.services.plugin_coding_identity import snapshot_id
    from app.plugin_coding_schemas import PluginArtifactDelete, PluginArtifactDownload, PluginArtifactRetire
    from fastapi import HTTPException
    artifact_id = snapshot_id(tenant, manifest['package_name'], '2.0.0')
    with session() as db:
        active = catalog.list_artifacts(db, scenario_id=None, offset=0, limit=50)
        assert any(item.id == artifact_id for item in active.items), 'reviewed snapshot is listed while active'
        row, _, _ = catalog.get_artifact(db, artifact_id)
        value = catalog.retire_artifact(db, artifact_id, PluginArtifactRetire(artifact_hash=row.proposal['artifact_hash']))
        db.commit()
        assert value.retired is True and value.available is False
        assert '已下线' in value.unavailable_reason
        audit = db.execute(select(AssistantMessage).where(
            AssistantMessage.proposal['kind'].as_string() == 'scenario-plugin-artifact-audit.v1')).scalars().all()
        assert any(item.proposal.get('artifact_id') == artifact_id and item.proposal.get('action') == 'retire' for item in audit)
        active = catalog.list_artifacts(db, scenario_id=None, offset=0, limit=50)
        assert all(item.id != artifact_id for item in active.items), 'retired snapshots leave the active list'
        audited = catalog.list_artifacts(db, scenario_id=None, offset=0, limit=50, include_retired=True)
        assert any(item.id == artifact_id and item.retired for item in audited.items), 'audit view keeps the snapshot'
        repeated = catalog.retire_artifact(db, artifact_id, PluginArtifactRetire(artifact_hash=row.proposal['artifact_hash']))
        db.commit()
        assert repeated.retired is True, 'retirement is idempotent'
        try:
            catalog.download_artifact(db, artifact_id, PluginArtifactDownload(artifact_hash=row.proposal['artifact_hash']))
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
        else:
            raise AssertionError('a retired snapshot was delivered')
        try:
            catalog.retire_artifact(db, artifact_id, PluginArtifactRetire(artifact_hash='0' * 64))
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
        else:
            raise AssertionError('retirement without the immutable hash binding was accepted')
        # Only an already-retired snapshot can be deleted; the audit row survives.
        try:
            catalog.delete_artifact(db, artifact_id, PluginArtifactDelete(artifact_hash='0' * 64))
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
        else:
            raise AssertionError('deletion without the immutable hash binding was accepted')
        project_before = dict(coding.owned_root(db, workspace_id).proposal)
        catalog.delete_artifact(db, artifact_id, PluginArtifactDelete(artifact_hash=row.proposal['artifact_hash']))
        db.commit()
        assert dict(coding.owned_root(db, workspace_id).proposal) == project_before, \
            'publication lifecycle must never touch the plugin project'
        try:
            catalog.get_artifact(db, artifact_id)
        except HTTPException as exc:
            assert exc.status_code == 404
            db.rollback()
        else:
            raise AssertionError('a deleted snapshot stayed readable')
        audited = catalog.list_artifacts(db, scenario_id=None, offset=0, limit=50, include_retired=True)
        assert all(item.id != artifact_id for item in audited.items), 'deleted snapshots leave every list'
        audit = db.execute(select(AssistantMessage).where(
            AssistantMessage.proposal['kind'].as_string() == 'scenario-plugin-artifact-audit.v1')).scalars().all()
        assert any(item.proposal.get('artifact_id') == artifact_id and item.proposal.get('action') == 'delete' for item in audit)
    return {'retired_leaves_active_list': True, 'retired_keeps_audit': True,
            'retired_delivery_rejected': True, 'retire_idempotent': True,
            'delete_requires_retired_hash': True, 'deleted_leaves_all_lists': True,
            'deleted_keeps_audit': True}


def verify_conversation_projection(db, session_id, instruction):
    from sqlalchemy import event
    from time import perf_counter
    from app.services.plugin_coding_conversation import coding_turns
    queries = []
    def count_query(*args):
        queries.append(1)
    engine = db.get_bind()
    event.listen(engine, 'before_cursor_execute', count_query)
    started = perf_counter()
    try:
        turns = coding_turns(db, session_id)
    finally:
        event.remove(engine, 'before_cursor_execute', count_query)
    assert len(queries) == 1 and 0 < len(turns) <= 20
    assert turns[0]['instruction'] == instruction
    assert set(turns[0]) == {'id', 'instruction', 'status', 'created_at', 'mode'}
    return {'query_count': len(queries), 'returned_turns': len(turns),
            'elapsed_ms': round((perf_counter() - started) * 1000, 3)}


def verify_explorer_projection(db, workspace_id, workspace):
    from app.plugin_coding_schemas import CodingProjectOut
    from app.routers.plugin_coding import project_files
    from fastapi import HTTPException
    projection = project_files(workspace_id=workspace_id, db=db)
    modelled = CodingProjectOut.model_validate(projection)
    assert (modelled.id, modelled.release_id, modelled.phase) == (
        workspace['id'], workspace['release_id'], workspace['phase'])
    assert 'plugin_version' not in modelled.model_dump(), 'the explorer projection carries no version label'
    assert {item.path: item.content for item in modelled.files} == {item['path']: item['content'] for item in workspace['files']}
    assert any(not item.editable for item in modelled.files), 'protected adapter must stay read-only'
    original_tenant = db.info['tenant_id']
    db.info['tenant_id'] = uuid4().hex
    try:
        project_files(workspace_id=workspace_id, db=db)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError('foreign tenant read the explorer projection')
    finally:
        db.info['tenant_id'] = original_tenant
        db.rollback()
    return {'files': len(modelled.files), 'foreign_tenant_rejected': True}


def verify_coding_settings(session, workspace_id, cfg_id, skill_id):
    from app.plugin_coding_schemas import PluginCodingSettings
    from app.services import plugin_coding_resources as resources, plugin_coding_workspace as coding
    from fastapi import HTTPException

    with session() as db:
        before = coding.public_workspace(db, coding.owned_root(db, workspace_id))
    barrier = threading.Barrier(2)

    def configure(install):
        payload = PluginCodingSettings(expected_revision=before['revision'], request_id=uuid4().hex,
            llm_config_id=cfg_id, skill_ids=[skill_id] if install else [], mcp_ids=[])
        with session() as db:
            barrier.wait(timeout=10)
            try:
                value = resources.update_settings(db, workspace_id, payload)
                return 200, payload, value
            except HTTPException as exc:
                db.rollback()
                return exc.status_code, payload, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(configure, (True, False)))
    assert sorted(item[0] for item in outcomes) == [200, 409]
    _, accepted, saved = next(item for item in outcomes if item[0] == 200)
    with session() as db:
        refreshed = coding.public_workspace(db, coding.owned_root(db, workspace_id))
        assert refreshed['resource_selection'] == saved['resource_selection']
        assert refreshed['files_hash'] == before['files_hash'] and refreshed['phase'] == before['phase']
        repeated = resources.update_settings(db, workspace_id, accepted)
        assert repeated['revision'] == saved['revision']
        try:
            resources.update_settings(db, workspace_id, accepted.model_copy(update={'skill_ids': [uuid4().hex]}))
        except HTTPException as exc:
            assert exc.status_code == 409
            db.rollback()
        else:
            raise AssertionError('settings request identity accepted different resources')
        installed = resources.update_settings(db, workspace_id, PluginCodingSettings(
            expected_revision=saved['revision'], request_id=uuid4().hex, llm_config_id=cfg_id,
            skill_ids=[skill_id], mcp_ids=[]))
        row = coding.owned_root(db, workspace_id)
        method = resources.prepare_context(db, row.proposal)['authoring_skills'][0]
        assert method['id'] == skill_id and method['instructions'] and method['content_sha256']
        for field in ('skill_ids', 'mcp_ids'):
            try:
                resources.update_settings(db, workspace_id, PluginCodingSettings(
                    expected_revision=installed['revision'], request_id=uuid4().hex, llm_config_id=cfg_id,
                    **{field: [uuid4().hex]}))
            except HTTPException as exc:
                assert exc.status_code == 409
                db.rollback()
            else:
                raise AssertionError('unavailable extension accepted')
        resources.update_settings(db, workspace_id, PluginCodingSettings(expected_revision=installed['revision'],
            request_id=uuid4().hex, llm_config_id=cfg_id, skill_ids=[], mcp_ids=[]))
    return {'settings_persistent': True, 'concurrent_settings_cas': True, 'settings_retry_idempotent': True,
            'settings_reused_identity_rejected': True, 'installed_skill_in_runtime_context': True,
            'unavailable_extension_rejected': True, 'settings_preserve_source_and_phase': True}


def verify_discussion_round(session, workspace_id, model_control):
    from app.plugin_coding_schemas import PluginCodingUpdate
    from app.services import plugin_coding_workspace as coding, plugin_coding_worker as worker
    from app.services import assistant_request_run_service as jobs

    with session() as db:
        before = coding.public_workspace(db, coding.owned_root(db, workspace_id))
        started = coding.update_workspace(db, workspace_id, PluginCodingUpdate(
            expected_revision=before['revision'], session_id=before['session_id'], base_files_hash=before['files_hash'],
            request_id=uuid4().hex, action='discuss', instruction='Explain the saved synthetic project'))
    model_control['discussion'] = True
    try:
        assert jobs.process_request(started['active_run_id'], worker.execute) is True
    finally:
        model_control['discussion'] = False
    with session() as db:
        answered = coding.public_workspace(db, coding.owned_root(db, workspace_id))
        assert answered['files_hash'] == before['files_hash'] and answered['phase'] == before['phase']
        assert answered['validation'] == before['validation'] and answered['run_status'] == 'succeeded'
        assert answered['turns'][-1]['mode'] == 'discuss'
        started = coding.update_workspace(db, workspace_id, PluginCodingUpdate(
            expected_revision=answered['revision'], session_id=answered['session_id'], base_files_hash=answered['files_hash'],
            request_id=uuid4().hex, action='discuss', instruction='A second synthetic question'))
        stopped = coding.update_workspace(db, workspace_id, PluginCodingUpdate(
            expected_revision=started['revision'], session_id=started['session_id'], base_files_hash=started['files_hash'],
            request_id=uuid4().hex, action='stop'))
        assert stopped['files_hash'] == before['files_hash'] and stopped['phase'] == before['phase']
        assert stopped['turns'][-1]['status'] == 'cancelled'
    assert jobs.process_request(started['active_run_id'], worker.execute) is False
    return {'discussion_preserves_source_and_phase': True, 'discussion_answer_persistent': True,
            'explicit_stop_preserves_source_and_phase': True, 'stopped_discussion_not_replayed': True}


def verify(factory):
    from app.models import Tenant, User, BusinessScenario, LLMConfig, OntologyRelease, OntologySnapshot, Skill
    from app.services import plugin_coding_workspace as coding, plugin_coding_worker as worker
    from app.services import plugin_coding_export as export, assistant_request_run_service as jobs
    from app.services import assistant_request_worker_service as dispatch
    from app.routers.plugin_coding import create
    from app.plugin_coding_schemas import PluginCodingCreate, PluginCodingSettings, PluginCodingUpdate
    from fastapi import HTTPException

    tenant, user, scenario, fresh_scenario, cfg_id, release, fresh_release = [uuid4().hex for _ in range(7)]
    skill_id = uuid4().hex
    with factory() as db:
        db.add(Tenant(id=tenant, name='Isolated coding fixture'))
        db.flush()
        db.add(User(id=user, tenant_id=tenant, email=f'{user}@example.invalid', password_hash='non-login-fixture', status='active'))
        db.add(BusinessScenario(id=scenario, tenant_id=tenant, name='Synthetic coding scenario', namespace='fixture'))
        db.add(BusinessScenario(id=fresh_scenario, tenant_id=tenant, name='Synthetic fresh scenario', namespace='fixture2'))
        db.add(LLMConfig(id=cfg_id, tenant_id=tenant, name='Synthetic stream model', model='fixture', enabled=True, capabilities=['chat']))
        from app.config import SKILLS_DIR
        db.add(Skill(id=skill_id, name='plugin-authoring', path=str(SKILLS_DIR / 'plugin-authoring'),
                     source='builtin', enabled=True, is_public=True, meta={'version': '1.0.0'}))
        db.flush()
        snapshot = OntologySnapshot(id=uuid4().hex, tenant_id=tenant, scenario_id=scenario, content={}, content_hash='a' * 64)
        fresh_snapshot = OntologySnapshot(id=uuid4().hex, tenant_id=tenant, scenario_id=fresh_scenario, content={}, content_hash='b' * 64)
        db.add_all([snapshot, fresh_snapshot])
        db.flush()
        db.add(OntologyRelease(id=release, tenant_id=tenant, scenario_id=scenario, snapshot_id=snapshot.id, name='Synthetic release', enabled=True))
        db.add(OntologyRelease(id=fresh_release, tenant_id=tenant, scenario_id=fresh_scenario, snapshot_id=fresh_snapshot.id, name='Fresh release', enabled=True))
        db.commit()
    manifest = {'package_name': 'scenario-synthetic', 'host': 'claude_code', 'scenario': {'id': scenario, 'name': 'Synthetic'},
        'deployment': {'release_id': release, 'snapshot_id': snapshot.id, 'definition_hash': 'a' * 64},
        'capabilities': [{'kind': 'function', 'key': 'check', 'name': 'Synthetic check', 'ready': True}]}
    fresh_manifest = {**manifest, 'scenario': {'id': fresh_scenario, 'name': 'Fresh'},
                      'deployment': {'release_id': fresh_release, 'snapshot_id': fresh_snapshot.id, 'definition_hash': 'b' * 64}}
    from app.services.plugin_delivery_profile import delivery_profile
    from app.services.scenario_capability_blueprint import blueprint
    function = SimpleNamespace(id='check', name='Synthetic check', description='Synthetic check',
                               runtime_kind='threshold', runtime_config={'threshold': 1})
    definition = SimpleNamespace(source='release', release_id=release, snapshot_id=snapshot.id, definition_hash='a' * 64,
                                 entities={}, relations={}, functions={'check': function}, actions={}, rules={},
                                 events={}, workflows={}, mappings={})
    scenario_blueprint = blueprint(definition, scenario={'id': scenario, 'name': 'Synthetic', 'description': 'Synthetic coding scenario'},
                                   deployment=manifest['deployment'],
                                   available=[{'kind': 'function', 'key': 'check', 'readiness': {'ready': True}}],
                                   selected=[{'kind': 'function', 'key': 'check'}],
                                   readable_property_keys=set(), invocation_authorized=set())
    full_contract = {**manifest, 'capabilities': [{**manifest['capabilities'][0], 'input_schema': {
        'type': 'object', 'properties': {'amount': {'type': 'integer'}}, 'required': ['amount'], 'additionalProperties': False}}],
        'delivery_profile': delivery_profile(), 'scenario_blueprint': scenario_blueprint}
    files = {'skills/run-scenario/SKILL.md': '---\nname: run-scenario\ndescription: Invoke the synthetic reviewed capability\n---\nUse invoke_scenario_capability and get_scenario_receipt.\n',
        'README.md': 'Configure SCENARIO_API_KEY externally. Run python -m examples.invoke.\n',
        'examples/invoke.py': 'import asyncio\nfrom server import invoke_scenario_capability\n\nasync def main():\n    result = await invoke_scenario_capability("function", "check", {"amount": 1})\n    print(result)\n\nif __name__ == "__main__":\n    asyncio.run(main())\n'}
    calls = []
    model_control = {'files': True}

    def model(*args, **kwargs):
        calls.append('provider')
        supplied = json.loads(args[1][-1]['content'])
        assert supplied['contract']['capabilities'][0]['input_schema']['required'] == ['amount']
        if model_control.get('discussion'):
            assert '讨论模式' in args[1][0]['content']
            yield {'type': 'token', 'content': '{"kind":"summary","message":"Synthetic explanation without source changes"}\n'}
            return
        yield {'type': 'token', 'content': json.dumps({'kind': 'plan', 'message': 'Synthetic public plan'}) + '\n'}
        if not model_control['files']:
            return
        for path, content in files.items():
            yield {'type': 'token', 'content': json.dumps({'kind': 'file', 'path': path, 'content': content}) + '\n'}
        yield {'type': 'token', 'content': '{"kind":"summary","message":"Synthetic generation complete"}\n'}

    def session():
        db = factory()
        db.info.update(tenant_id=tenant, user_id=user)
        return db

    with ExitStack() as stack:
        for target, value in [
            ('app.services.permission_service.require_principal', lambda db: SimpleNamespace(tenant_id=db.info['tenant_id'], user_id=db.info['user_id'])),
            ('app.services.permission_service.require_tenant_permission', lambda *a, **k: None),
            ('app.services.permission_service.require_scenario_permission', lambda *a, **k: None),
            ('app.services.permission_service.check_scenario', lambda db, scenario, verb: SimpleNamespace(allowed=True)),
            ('app.services.tenant_service.require_scenario', lambda db, key, **k: db.get(BusinessScenario, key)),
            ('app.services.release_service._scenario_for_read', lambda db, key: (db.get(BusinessScenario, key), None)),
            ('app.services.release_service._scenario_for_manage', lambda db, key: (db.get(BusinessScenario, key), None)),
            ('app.services.plugin_coding_workspace.prepare_package', lambda *a: ('scenario-synthetic', manifest)),
            ('app.services.plugin_coding_workspace.authoring_contract', lambda *a: full_contract),
            ('app.services.plugin_authoring_context.release_context', lambda db, release_id, *a: (None, fresh_manifest if model_control.get('fresh_scenario') else manifest)),
            ('app.services.plugin_coding_export.prepare_package', lambda *a: ('scenario-synthetic', dict(manifest))),
            ('app.services.plugin_coding_export.check_release_state', lambda *a: None),
            ('app.services.plugin_coding_worker.SessionLocal', session),
            ('app.services.assistant_request_run_service.SessionLocal', session),
            ('app.services.llm_service.chat_stream', model),
        ]:
            stack.enter_context(patch(target, value))
        request = PluginCodingCreate(expected_revision=1, llm_config_id=cfg_id, instruction='Write the synthetic plugin',
            request_id=uuid4().hex, capabilities=[{'kind': 'function', 'key': 'check'}],
            acceptance_cases=[{'kind': 'function', 'key': 'check', 'role': role, 'invocation_id': uuid4().hex}
                              for role in ('success', 'boundary', 'failure')], confirmed_business_acceptance=True)
        from app.routers.scenario_releases import get_release
        with session() as db:
            assert get_release(release, db).id == release
            db.info['tenant_id'] = uuid4().hex
            try:
                get_release(release, db)
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError('foreign tenant release was readable')
        barrier = threading.Barrier(2)

        def concurrent_create():
            with session() as db:
                barrier.wait(timeout=10)
                return create(request, release_id=release, db=db)
        with ThreadPoolExecutor(max_workers=2) as pool:
            created = list(pool.map(lambda _: concurrent_create(), range(2)))
        assert created[0]['id'] == created[1]['id'] and created[0]['session_id'] == created[1]['session_id']
        workspace_id, session_id, run_id = created[0]['id'], created[0]['session_id'], created[0]['active_run_id']
        with session() as db:
            from app.services.plugin_coding_resources import update_settings
            try:
                update_settings(db, workspace_id, PluginCodingSettings(
                    expected_revision=created[0]['revision'], request_id=uuid4().hex, llm_config_id=cfg_id))
            except HTTPException as exc:
                assert exc.status_code == 409
                db.rollback()
            else:
                raise AssertionError('running configuration change accepted')
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: jobs.process_request(run_id, worker.execute), range(2)))
        assert outcomes.count(True) == 1 and len(calls) == 1
        settings_result = verify_coding_settings(session, workspace_id, cfg_id, skill_id)
        with session() as db:
            value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
            assert value['phase'] == 'ready_for_review' and value['run_status'] == 'succeeded'
            assert not value['validation']
            assert value['session_id'] == session_id
            assert value['turns'][0]['instruction'] == request.instruction
            assert value['turns'][0]['status'] == 'succeeded'
            assert any(event.get('run_id') == run_id and event['kind'] == 'file' for event in value['events'])
            assert set(value['turns'][0]) == {'id', 'instruction', 'status', 'created_at', 'mode'}
            conversation_read = verify_conversation_projection(db, session_id, request.instruction)
            explorer_read = verify_explorer_projection(db, workspace_id, value)
            old_revision = value['revision']
            value = coding.update_workspace(db, workspace_id, PluginCodingUpdate(expected_revision=old_revision,
                session_id=session_id, request_id=uuid4().hex, action='save', base_files_hash=value['files_hash'],
                files=[{'path': 'README.md', 'content': files['README.md'] + 'Human correction retained.\n'}]))
            try:
                coding.update_workspace(db, workspace_id, PluginCodingUpdate(expected_revision=old_revision,
                    session_id=session_id, request_id=uuid4().hex, action='save', base_files_hash=value['files_hash']))
            except HTTPException as exc:
                assert exc.status_code == 409
                db.rollback()
            else:
                raise AssertionError('stale edit was accepted')
            value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
            value = coding.update_workspace(db, workspace_id, PluginCodingUpdate(expected_revision=value['revision'],
                session_id=session_id, request_id=uuid4().hex, action='generate', base_files_hash=value['files_hash'], instruction='Improve example'))
            active = value['active_run_id']
        with session() as db:
            lease = dispatch._claim(db, active, lease_seconds=120, upload_state_reader=jobs._upload_state)
            assert lease is not None
        with session() as db:
            value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
            value = coding.update_workspace(db, workspace_id, PluginCodingUpdate(expected_revision=value['revision'],
                session_id=session_id, request_id=uuid4().hex, action='save', base_files_hash=value['files_hash'], files=[]))
            try:
                jobs.assert_execution_lease(db, active, lease_token=lease.token, lease_generation=lease.generation)
            except jobs.AssistantRequestConflict:
                db.rollback()
            else:
                raise AssertionError('cancelled coding lease still authorized')
            value = coding.public_workspace(db, coding.owned_root(db, workspace_id))
            assert [turn['instruction'] for turn in value['turns']] == [request.instruction, 'Improve example']
            assert value['turns'][-1]['status'] == 'cancelled'
            name, artifact = export.export_workspace(db, workspace_id, review_payload(value, '1.0.0'))
            assert artifact.startswith(b'PK') and name.endswith('1.0.0')
            continuity = verify_session_continuity(session, release, request, value)
            verify_version_history(session, workspace_id, tenant, manifest, files)
        with session() as db:
            db.info['user_id'] = uuid4().hex
            from app.services.plugin_coding_conversation import coding_turns
            assert coding_turns(db, session_id) == []
            try:
                coding.owned_root(db, workspace_id)
            except HTTPException as exc:
                assert exc.status_code == 404
            else:
                raise AssertionError('foreign workspace was readable')
            try:
                coding.update_workspace(db, workspace_id, PluginCodingUpdate(expected_revision=1,
                    session_id=session_id, request_id=uuid4().hex, action='save', base_files_hash='0' * 64))
            except HTTPException as exc:
                assert exc.status_code == 404
                db.rollback()
            else:
                raise AssertionError('foreign session revision was accepted')
        concurrency_result = verify_concurrent_review(session, workspace_id)
        retirement_result = verify_retirement(session, workspace_id, tenant, manifest)
        verify_no_file_round(session, fresh_release, request, model_control, fresh_scenario)
        discussion_result = verify_discussion_round(session, workspace_id, model_control)
    return {'concurrent_create': True, 'single_worker_generation': True, 'durable_files': True,
            'stale_edit_rejected': True, 'running_lease_revoked': True, 'private_export_reviewed': True,
            'foreign_owner_rejected': True, 'delivered_version_immutable': True, 'explicit_new_version': True,
            'review_never_touches_project': True, **continuity, **concurrency_result,
            **retirement_result,
            'full_schema_in_model_prompt': True, 'plan_without_files_rejected': True,
            'durable_public_conversation': True, 'public_event_round_identity': True,
            'conversation_foreign_owner_rejected': True,
            'release_context_foreign_tenant_rejected': True,
            'conversation_read': conversation_read,
            'explorer_read': explorer_read,
            'running_settings_rejected': True, **settings_result, **discussion_result,
            'model_stream': 'synthetic', 'business_acceptance': 'synthetic'}


def main():
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from app.config import get_settings
    from scripts.verify_alembic_roundtrip import _database_url
    settings = get_settings()
    name = f'ontology_plugin_verify_{uuid4().hex[:12]}'
    assert re.fullmatch(r'ontology_plugin_verify_[0-9a-f]{12}', name)
    control = create_engine(_database_url(settings, 'postgres'), isolation_level='AUTOCOMMIT')
    target = create_engine(_database_url(settings, name))
    keys = ('ALEMBIC_DATABASE_URL', 'ALEMBIC_ROLE', 'ALEMBIC_USE_ADMIN')
    previous = {key: os.environ.get(key) for key in keys}
    created = False
    try:
        with control.connect() as db:
            assert db.execute(text('SELECT 1 FROM pg_database WHERE datname=:name'), {'name': name}).scalar() is None
            db.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
        os.environ['ALEMBIC_DATABASE_URL'] = _database_url(settings, name).render_as_string(hide_password=False)
        os.environ.pop('ALEMBIC_ROLE', None)
        os.environ.pop('ALEMBIC_USE_ADMIN', None)
        config = Config(str(BACKEND / 'alembic.ini'))
        head = ScriptDirectory.from_config(config).get_current_head()
        assert head
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            command.upgrade(config, head)
        result = verify(sessionmaker(bind=target, expire_on_commit=False, autoflush=False))
        print(json.dumps({'passed': True, 'alembic_head': head, **result}, indent=2))
    finally:
        target.dispose()
        if created:
            with control.connect() as db:
                db.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name AND pid<>pg_backend_pid()'), {'name': name})
                db.exec_driver_sql(f'DROP DATABASE "{name}"')
        control.dispose()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Database/provider errors must never print connection strings or data.
        print(f'Plugin coding verification failed: {type(exc).__name__}', file=sys.stderr)
        raise SystemExit(1) from None
